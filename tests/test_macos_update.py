"""macOS 自动更新测试。

背景：macOS 发布物改成 .pkg 后更新彻底失效——
  1. .pkg 是 xar 归档（魔数 xar!），旧的可执行文件魔数校验把它判成「非有效安装包」，
     下载 100% 失败；
  2. 即使下载成功，Darwin 分支也只是打开下载页让用户手动换 DMG/.app，没有安装动作；
  3. 更新清单只取第一个成功响应，加速节点/raw CDN 的旧缓存会让刚发布的版本
     被判成「已是最新版本」。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gui  # noqa: E402


def _write(path, head: bytes, size: int = 64):
    with open(path, "wb") as fh:
        fh.write(head)
        fh.write(b"\x00" * max(0, size - len(head)))
    return path


class LooksLikeInstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _path(self, name, head):
        return _write(os.path.join(self.tmp.name, name), head)

    def test_accepts_xar_pkg(self):
        pkg = self._path("Moisten-2.3.9-macOS.pkg", b"xar!")
        self.assertTrue(gui._looks_like_installer(pkg))

    def test_rejects_pkg_with_executable_magic(self):
        """Windows 的 PE 产物被改名成 .pkg 时要拦住。"""
        pkg = self._path("fake.pkg", b"MZ\x90\x00")
        self.assertFalse(gui._looks_like_installer(pkg))

    def test_rejects_proxy_error_page_named_pkg(self):
        pkg = self._path("truncated.pkg", b"<!DO")
        self.assertFalse(gui._looks_like_installer(pkg))

    def test_accepts_macho_on_macos(self):
        binary = self._path("Moisten", b"\xcf\xfa\xed\xfe")
        with patch("gui.sys.platform", "darwin"):
            self.assertTrue(gui._looks_like_installer(binary))

    def test_accepts_pe_on_windows(self):
        exe = self._path("Moisten-2.3.9-Windows.exe", b"MZ\x90\x00")
        with patch("gui.sys.platform", "win32"):
            self.assertTrue(gui._looks_like_installer(exe))

    def test_rejects_macho_on_windows(self):
        exe = self._path("Moisten.exe", b"\xcf\xfa\xed\xfe")
        with patch("gui.sys.platform", "win32"):
            self.assertFalse(gui._looks_like_installer(exe))

    def test_missing_file_is_rejected(self):
        self.assertFalse(gui._looks_like_installer(
            os.path.join(self.tmp.name, "nope.pkg")))

    def test_real_pkgbuild_artifact_has_xar_magic(self):
        """守住假设：真的 .pkg 头 4 字节就是 xar!（没有 pkgbuild 就跳过）。"""
        pkgbuild = shutil.which("pkgbuild")
        if sys.platform != "darwin" or not pkgbuild:
            self.skipTest("需要 macOS pkgbuild")
        root = os.path.join(self.tmp.name, "root", "Applications")
        os.makedirs(root)
        pkg = os.path.join(self.tmp.name, "real-macOS.pkg")
        proc = subprocess.run(
            [pkgbuild, "--root", os.path.join(self.tmp.name, "root"),
             "--identifier", "com.moisten.test", "--version", "1.0", pkg],
            capture_output=True, text=True)
        if proc.returncode != 0:  # pragma: no cover
            self.skipTest(f"pkgbuild 失败: {proc.stderr[:120]}")
        self.assertTrue(gui._looks_like_installer(pkg))


class FakeResponse:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _release(tag, file_name="Moisten-macOS.pkg", notes="notes"):
    return {"tag": tag, "notes": notes,
            "assets": [{"name": "macOS", "file": file_name, "size": 1}]}


class UpdateFeedTests(unittest.TestCase):
    def test_takes_highest_version_across_sources(self):
        """回归：第一个源是旧缓存时，不能就此判定「已是最新版本」。"""
        payloads = {
            "proxy-a": _release("v2.3.7"),
            "proxy-b": _release("v2.3.7"),
            "direct": _release("v2.3.8"),
        }

        def opener(url):
            for key, payload in payloads.items():
                if key in url:
                    return FakeResponse(payload)
            raise OSError("unreachable")

        best = gui.fetch_releases_json(
            candidates=["https://proxy-a/x", "https://proxy-b/x", "https://direct/x"],
            opener=opener)
        self.assertEqual(best["tag"], "v2.3.8")

    def test_returns_none_when_every_source_fails(self):
        def opener(url):
            raise OSError("down")

        self.assertIsNone(gui.fetch_releases_json(
            candidates=["https://a/x", "https://b/x"], opener=opener))

    def test_candidates_are_cache_busted_and_keep_priority_order(self):
        candidates = gui._releases_json_candidates()
        self.assertTrue(gui.RELEASES_JSON_URL in candidates[-1])
        self.assertEqual(len(candidates), 3)          # 两个加速节点 + 直连
        for url in candidates:
            self.assertIn("t=", url)                  # 带时间戳绕过缓存

    def test_check_for_update_reports_new_version_with_asset_url(self):
        with patch("gui.fetch_releases_json",
                   return_value=_release("v9.9.9", "Moisten-9.9.9-macOS.pkg")):
            latest, needs, notes, urls = gui.check_for_update()
        self.assertEqual(latest, "9.9.9")
        self.assertTrue(needs)
        self.assertEqual(notes, "notes")
        self.assertEqual(
            urls["macOS"],
            "https://github.com/signxer/silent-rain/releases/download/v9.9.9/"
            "Moisten-9.9.9-macOS.pkg")

    def test_check_for_update_silent_when_same_version(self):
        with patch("gui.fetch_releases_json",
                   return_value=_release(f"v{gui.CURRENT_VERSION}")):
            latest, needs, _notes, urls = gui.check_for_update()
        self.assertEqual(latest, gui.CURRENT_VERSION)
        self.assertFalse(needs)
        self.assertEqual(urls, {})

    def test_check_for_update_survives_empty_feed(self):
        with patch("gui.fetch_releases_json", return_value=None):
            latest, needs, _notes, urls = gui.check_for_update()
        self.assertEqual(latest, gui.CURRENT_VERSION)
        self.assertFalse(needs)


class MacOSUpdateHelperTests(unittest.TestCase):
    def test_applescript_string_escapes_quotes_and_backslashes(self):
        self.assertEqual(gui._applescript_string('a"b\\c'), '"a\\"b\\\\c"')

    def test_app_bundle_from_frozen_executable(self):
        with patch("gui.sys.frozen", True, create=True), \
             patch("gui.sys.executable",
                   "/Applications/Moisten.app/Contents/MacOS/Moisten"), \
             patch("gui.os.path.abspath", side_effect=lambda p: p):
            self.assertEqual(gui._macos_app_bundle(), "/Applications/Moisten.app")

    def test_app_bundle_is_none_for_source_run(self):
        with patch("gui.sys.frozen", False, create=True):
            self.assertIsNone(gui._macos_app_bundle())

    def test_install_pkg_uses_installer_with_admin_privileges(self):
        calls = []

        def runner(args, **kwargs):
            calls.append(args)
            return type("P", (), {"returncode": 0})()

        ok, reason = gui._install_macos_pkg("/tmp/Moisten 2.3.9-macOS.pkg", runner=runner)
        self.assertTrue(ok)
        self.assertEqual(reason, "")
        self.assertEqual(calls[0][0], gui.MACOS_OSASCRIPT)
        self.assertEqual(calls[0][1], "-e")
        script = calls[0][2]
        self.assertIn("with administrator privileges", script)
        self.assertIn("/usr/sbin/installer -pkg", script)
        self.assertIn("-target /", script)
        self.assertIn("'", script)          # 含空格的路径被 shell 引号保护

    def test_install_pkg_reports_failure(self):
        def runner(args, **kwargs):
            return type("P", (), {"returncode": 1})()

        ok, reason = gui._install_macos_pkg("/tmp/x.pkg", runner=runner)
        self.assertFalse(ok)
        self.assertIn("exit=1", reason)

    def test_install_pkg_handles_runner_exception(self):
        def runner(args, **kwargs):
            raise OSError("osascript missing")

        ok, reason = gui._install_macos_pkg("/tmp/x.pkg", runner=runner)
        self.assertFalse(ok)
        self.assertIn("OSError", reason)

    def test_relaunch_waits_for_pid_then_opens_app(self):
        calls = []

        def popen(args, **kwargs):
            calls.append((args, kwargs))
            return object()

        gui._schedule_macos_relaunch("/Applications/Moisten.app", pid=4242,
                                     popen=popen)
        args, kwargs = calls[0]
        self.assertEqual(args[0], "/bin/sh")
        self.assertIn("kill -0 4242", args[2])
        self.assertIn("/usr/bin/open /Applications/Moisten.app", args[2])
        self.assertTrue(kwargs.get("start_new_session"))


class _FakeUpdateWindow:
    """只带更新相关状态的替身；_launch_macos_update 用真实实现。"""

    _launch_macos_update = gui.MainWindow._launch_macos_update

    def __init__(self, download_path):
        self._update_download_path = download_path
        self._update_in_progress = True

    def _set_update_status(self, message):
        self.status = message


class MacOSUpdateLaunchTests(unittest.TestCase):
    def _window(self):
        return _FakeUpdateWindow("/tmp/Moisten-2.3.9-macOS.pkg")

    def _run(self, window, install_ok=True, frozen=True,
             installed_app_exists=True, executable=None):
        with patch("gui.platform.system", return_value="Darwin"), \
             patch("gui.sys.frozen", frozen, create=True), \
             patch("gui.sys.executable",
                   executable or "/Applications/Moisten.app/Contents/MacOS/Moisten"), \
             patch("gui.os.path.isfile", return_value=True), \
             patch("gui.os.path.isdir", return_value=installed_app_exists), \
             patch("gui.os.path.abspath", side_effect=lambda p: p), \
             patch("gui._install_macos_pkg",
                    return_value=(install_ok, "" if install_ok else "test-fail")) as install, \
             patch("gui._schedule_macos_relaunch") as relaunch, \
             patch("gui.subprocess.Popen") as popen, \
             patch("gui.InfoBar"), \
             patch("gui.QTimer.singleShot") as single_shot, \
             patch("gui.QApplication.instance",
                   return_value=type("A", (), {"quit": lambda self: None})()):
            gui.MainWindow._launch_update_process(window)
        return install, relaunch, popen, single_shot

    def test_pkg_is_installed_then_relaunched(self):
        window = self._window()
        install, relaunch, popen, single_shot = self._run(window, install_ok=True)
        install.assert_called_once_with("/tmp/Moisten-2.3.9-macOS.pkg")
        relaunch.assert_called_once()
        self.assertEqual(relaunch.call_args.args[0], "/Applications/Moisten.app")
        single_shot.assert_called_once()
        popen.assert_not_called()

    def test_relaunch_targets_installed_app_when_run_from_elsewhere(self):
        """从下载目录启动时，重启的必须是新装到 /Applications 的那份。"""
        window = self._window()
        _install, relaunch, _popen, _single = self._run(
            window, executable="/Users/me/Downloads/Moisten.app/Contents/MacOS/Moisten")
        self.assertEqual(relaunch.call_args.args[0], "/Applications/Moisten.app")

    def test_relaunch_falls_back_to_current_bundle(self):
        window = self._window()
        _install, relaunch, _popen, _single = self._run(
            window, installed_app_exists=False,
            executable="/Users/me/Downloads/Moisten.app/Contents/MacOS/Moisten")
        self.assertEqual(relaunch.call_args.args[0], "/Users/me/Downloads/Moisten.app")

    def test_failed_install_falls_back_to_system_installer(self):
        window = self._window()
        install, relaunch, popen, single_shot = self._run(window, install_ok=False)
        install.assert_called_once()
        relaunch.assert_not_called()
        single_shot.assert_not_called()
        self.assertEqual(popen.call_args.args[0],
                         ["/usr/bin/open", "/tmp/Moisten-2.3.9-macOS.pkg"])
        # 失败后可再次尝试更新，不能卡在「更新中」
        self.assertFalse(window._update_in_progress)

    def test_source_run_opens_pkg_without_installing(self):
        window = self._window()
        install, relaunch, popen, _single = self._run(window, frozen=False)
        install.assert_not_called()
        relaunch.assert_not_called()
        self.assertEqual(popen.call_args.args[0],
                         ["/usr/bin/open", "/tmp/Moisten-2.3.9-macOS.pkg"])
        self.assertFalse(window._update_in_progress)

    def test_non_pkg_artifact_is_not_installed(self):
        window = self._window()
        window._update_download_path = "/tmp/Moisten-2.3.9-Windows.exe"
        install, relaunch, _popen, _single = self._run(window)
        install.assert_not_called()
        relaunch.assert_not_called()
        self.assertFalse(window._update_in_progress)


class DownloadValidationWiringTests(unittest.TestCase):
    def test_download_path_uses_installer_aware_check(self):
        """下载校验必须走 _looks_like_installer，否则 .pkg 会被判为无效。"""
        with open(gui.__file__, encoding="utf-8") as fh:
            source = fh.read()
        self.assertNotIn("_looks_like_executable(", source)
        self.assertIn("_looks_like_installer(download_path)", source)


class UpdateQuitFlowTests(unittest.TestCase):
    """更新流程的自动退出不能弹"确认退出"，否则旧进程会卡住不退。"""

    class _Event:
        def __init__(self):
            self.accepted = False
            self.ignored = False

        def accept(self):
            self.accepted = True

        def ignore(self):
            self.ignored = True

    def _close(self, quitting, exec_result=True):
        win = SimpleNamespace(_quitting_for_update=quitting, screen_dashboard=None)
        event = self._Event()
        with patch("gui.Dialog") as dialog_cls, \
             patch("gui.style_moisten_dialog"):
            dialog_cls.return_value.exec.return_value = exec_result
            gui.MainWindow.closeEvent(win, event)
        return event, dialog_cls

    def test_update_quit_skips_confirmation_dialog(self):
        event, dialog_cls = self._close(quitting=True)
        dialog_cls.assert_not_called()
        self.assertTrue(event.accepted)
        self.assertFalse(event.ignored)

    def test_normal_quit_still_confirms(self):
        event, dialog_cls = self._close(quitting=False, exec_result=True)
        dialog_cls.assert_called_once()
        self.assertTrue(event.accepted)

    def test_cancelled_quit_keeps_window_open(self):
        event, dialog_cls = self._close(quitting=False, exec_result=False)
        dialog_cls.assert_called_once()
        self.assertTrue(event.ignored)
        self.assertFalse(event.accepted)

    def test_relaunch_escalates_and_does_not_force_a_second_instance(self):
        """等旧进程退出；等不到就 TERM/KILL；最后用 open（不带 -n）启动。"""
        calls = []

        def popen(args, **kwargs):
            calls.append((args, kwargs))
            return object()

        gui._schedule_macos_relaunch("/Applications/Moisten.app", pid=4242, popen=popen)
        script = calls[0][0][2]
        self.assertIn("kill -0 4242", script)
        self.assertIn("kill -TERM 4242", script)
        self.assertIn("kill -KILL 4242", script)
        self.assertIn("/usr/bin/open /Applications/Moisten.app", script)
        self.assertNotIn("open -n", script)          # -n 会在旧实例还活着时叠一个
        self.assertTrue(calls[0][1].get("start_new_session"))

    def test_install_failure_reason_is_logged(self):
        def runner(args, **kwargs):
            return SimpleNamespace(returncode=1, stdout="", stderr="installer: not permitted")

        logs = []
        with patch("gui.debug", side_effect=lambda m: logs.append(str(m))):
            ok, reason = gui._install_macos_pkg("/tmp/x.pkg", runner=runner)
        self.assertFalse(ok)
        self.assertIn("not permitted", reason)
        self.assertTrue(any("installer 退出码=1" in m for m in logs), logs)


if __name__ == "__main__":
    unittest.main()
