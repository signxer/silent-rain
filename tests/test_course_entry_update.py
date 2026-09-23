import asyncio
import os
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from main import AutoLearner


class FakeButton:
    def __init__(self, page, label, destination=None):
        self.page = page
        self.label = label
        self.destination = destination
        self.first = self
        self.clicked = False

    async def count(self):
        return 1

    async def inner_text(self):
        return self.label

    async def click(self):
        self.clicked = True
        if self.destination:
            self.destination()


class FakeProgress:
    def __init__(self, value):
        self.value = value
        self.first = self

    async def count(self):
        return int(self.value is not None)

    async def get_attribute(self, name):
        return str(self.value)


class FakePage:
    def __init__(self, url, progress=None, button=None, context=None):
        self.url = url
        self.progress = FakeProgress(progress)
        self.button = button
        self.context = context or type("Context", (), {"pages": []})()
        self.context.pages.append(self)

    def locator(self, selector):
        if selector == ".progress-contain [role='progressbar']":
            return self.progress
        return self.button

    def is_closed(self):
        return False

    async def query_selector(self, selector):
        return None

    async def wait_for_timeout(self, milliseconds):
        pass

    async def wait_for_load_state(self, state, timeout):
        pass


class CourseEntryTests(unittest.TestCase):
    def setUp(self):
        self.learner = AutoLearner.__new__(AutoLearner)
        self.learner._stop_event = threading.Event()

    def test_direct_player_needs_no_detail_button(self):
        page = FakePage("https://example.test/course/#/play/123")
        result = asyncio.run(self.learner._enter_online_course_player(page, 0))
        self.assertIs(result, page)

    def test_completed_detail_does_not_restart_course(self):
        page = FakePage("https://example.test/course/#/detail/123", progress=100)
        button = FakeButton(page, "重新学习")
        page.button = button
        result = asyncio.run(self.learner._enter_online_course_player(page, 0))
        self.assertIsNone(result)
        self.assertFalse(button.clicked)

    def test_unlearned_detail_opens_player_in_same_page(self):
        page = FakePage("https://example.test/course/#/detail/123", progress=0)
        button = FakeButton(page, "我要学习", lambda: setattr(page, "url", "https://example.test/course/#/play/123"))
        page.button = button
        result = asyncio.run(self.learner._enter_online_course_player(page, 0))
        self.assertIs(result, page)
        self.assertTrue(button.clicked)

    def test_unlearned_detail_opens_player_in_new_page(self):
        page = FakePage("https://example.test/course/#/detail/123", progress=40)
        player = []
        button = FakeButton(page, "继续学习", lambda: player.append(
            FakePage("https://example.test/course/#/play/123", context=page.context)))
        page.button = button
        result = asyncio.run(self.learner._enter_online_course_player(page, 0))
        self.assertIs(result, player[0])

    def test_relearn_button_without_progress_is_not_clicked(self):
        page = FakePage("https://example.test/course/#/detail/123")
        button = FakeButton(page, "重新学习")
        page.button = button
        with self.assertRaisesRegex(RuntimeError, "进度未加载"):
            asyncio.run(self.learner._enter_online_course_player(page, 0))
        self.assertFalse(button.clicked)

    def test_detail_page_is_not_refreshed_for_missing_video(self):
        page = FakePage("https://example.test/course/#/detail/123")
        result = asyncio.run(self.learner.find_and_play_video(page, 0))
        self.assertFalse(result)


class UpdateLaunchTests(unittest.TestCase):
    def test_windows_update_launch_uses_saved_download_path(self):
        import gui

        window = type("Window", (), {})()
        window._update_download_path = "C:/Moisten/Moisten.new.exe"
        window._update_in_progress = True
        with (patch("gui.platform.system", return_value="Windows"),
              patch("gui.sys.executable", "C:/Moisten/Moisten.exe"),
              patch("gui.os.path.isfile", return_value=True),
              patch("subprocess.Popen") as popen,
              patch("gui.InfoBar.success"),
              patch("gui.QTimer.singleShot"),
              patch("gui.QApplication.instance", return_value=SimpleNamespace(quit=lambda: None))):
            gui.MainWindow._launch_update_process(window)
        self.assertEqual(popen.call_args.args[0], [
            "C:/Moisten/Moisten.new.exe", "--post-update-old", "C:/Moisten/Moisten.exe"])

    def test_post_update_replaces_unversioned_old_exe(self):
        import gui

        old_path = os.path.join(tempfile.gettempdir(), "Moisten.exe")
        new_path = os.path.join(tempfile.gettempdir(), "Moisten.new.exe")
        with (patch("gui.sys.argv", [new_path, "--post-update-old", old_path]),
              patch("gui.sys.executable", new_path),
              patch("gui.os.path.exists", return_value=True),
              patch("gui.os.remove") as remove,
              patch("gui.os.replace") as replace):
            gui._handle_self_update()
        remove.assert_called_once_with(os.path.abspath(old_path))
        replace.assert_called_once_with(os.path.abspath(new_path), os.path.abspath(old_path))

    def test_post_update_renames_versioned_exe(self):
        import gui

        old_path = os.path.join(tempfile.gettempdir(), "Moisten-2.2.6-Windows.exe")
        new_path = os.path.join(tempfile.gettempdir(), "Moisten.new.exe")
        target = os.path.join(tempfile.gettempdir(), "Moisten-2.2.7-Windows.exe")
        with (patch("gui.CURRENT_VERSION", "2.2.7"),
              patch("gui.sys.argv", [new_path, "--post-update-old", old_path]),
              patch("gui.sys.executable", new_path),
              patch("gui.os.path.exists", return_value=True),
              patch("gui.os.remove") as remove,
              patch("gui.os.replace") as replace):
            gui._handle_self_update()
        remove.assert_called_once_with(os.path.abspath(old_path))
        replace.assert_called_once_with(os.path.abspath(new_path), os.path.abspath(target))


if __name__ == "__main__":
    unittest.main()
