import asyncio
import os
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from main import (
    AutoLearner, OnlineCourseListUnavailable,
    _build_online_playlist_tasks, _defer_online_course, _online_course_target_url,
)
from ui_theme import LIGHT, RoundedGradientProgressBar, _stylesheet


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


class EmptyLocator:
    def filter(self, **kwargs):
        return self

    async def count(self):
        return 0


class RoutePage:
    def __init__(self, page_number=1, redirect_to=None):
        self.url = f"https://example.test/course/#/list/{page_number}"
        self.redirect_to = redirect_to

    def locator(self, selector):
        return EmptyLocator()

    async def goto(self, url, **kwargs):
        self.url = self.redirect_to or url

    async def wait_for_timeout(self, milliseconds):
        pass


class CourseEntryTests(unittest.TestCase):
    def setUp(self):
        self.learner = AutoLearner.__new__(AutoLearner)
        self.learner._stop_event = threading.Event()

    def test_direct_player_needs_no_detail_button(self):
        page = FakePage("https://example.test/course/#/play/123")
        result = asyncio.run(self.learner._enter_online_course_player(page, 0))
        self.assertIs(result, page)

    def test_pagination_falls_back_to_list_spa_route(self):
        page = RoutePage(page_number=1)
        moved = asyncio.run(self.learner._advance_online_course_page(
            page, "https://example.test/course/#/list/1", 1))
        self.assertTrue(moved)
        self.assertTrue(page.url.endswith("#/list/2"))

    def test_pagination_route_fallback_detects_last_page_redirect(self):
        page = RoutePage(page_number=3, redirect_to="https://example.test/course/#/list/3")
        moved = asyncio.run(self.learner._advance_online_course_page(
            page, "https://example.test/course/#/list/1", 3))
        self.assertFalse(moved)

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

    def test_list_timeout_is_classified_as_recoverable(self):
        class UnavailablePage:
            async def goto(self, *args, **kwargs):
                pass

            async def wait_for_selector(self, *args, **kwargs):
                raise TimeoutError("course cards did not load")

        task = {"page": 1, "title": "example", "href": ""}
        with self.assertRaises(OnlineCourseListUnavailable):
            asyncio.run(self.learner._open_online_course_from_list(
                UnavailablePage(), "https://example.test/course/#/list/1", task))

    def test_list_card_can_open_player_in_same_tab(self):
        async def run():
            worker = MagicMock()
            worker.url = "https://example.test/course/#/list/1"
            worker.goto = AsyncMock()
            worker.wait_for_selector = AsyncMock()
            worker.wait_for_timeout = AsyncMock()
            worker.is_closed.return_value = False

            async def wait_for_popup(*args, **kwargs):
                await asyncio.Future()

            worker.wait_for_event = wait_for_popup
            card = MagicMock()
            card.get_attribute = AsyncMock(side_effect=["Example", "javascript:void(0)"])

            async def click():
                worker.url = "https://example.test/course/#/play/123"

            card.click = click
            links = MagicMock()
            links.count = AsyncMock(return_value=1)
            links.nth.return_value = card
            worker.locator.return_value = links
            task = {"page": 1, "title": "Example", "href": ""}
            result = await self.learner._open_online_course_from_list(
                worker, "https://example.test/course/#/list/1", task)
            self.assertIs(result, worker)

        asyncio.run(run())

    def test_list_card_popup_is_tied_to_its_worker_page(self):
        async def run():
            worker = MagicMock()
            worker.url = "https://example.test/course/#/list/1"
            worker.goto = AsyncMock()
            worker.wait_for_selector = AsyncMock()

            async def tick(*args):
                await asyncio.sleep(0)

            worker.wait_for_timeout = tick
            worker.is_closed.return_value = False
            popup_ready = asyncio.Event()
            player = MagicMock()
            player.wait_for_load_state = AsyncMock()

            async def wait_for_popup(*args, **kwargs):
                await popup_ready.wait()
                return player

            worker.wait_for_event = wait_for_popup
            card = MagicMock()
            card.get_attribute = AsyncMock(side_effect=["Example", "javascript:void(0)"])
            card.click = AsyncMock(side_effect=popup_ready.set)
            links = MagicMock()
            links.count = AsyncMock(return_value=1)
            links.nth.return_value = card
            worker.locator.return_value = links
            task = {"page": 1, "title": "Example", "href": ""}
            result = await self.learner._open_online_course_from_list(
                worker, "https://example.test/course/#/list/1", task)
            self.assertIs(result, player)

        asyncio.run(run())

    def test_failed_popup_load_closes_partial_page(self):
        async def run():
            worker = MagicMock()
            worker.url = "https://example.test/course/#/list/1"
            worker.goto = AsyncMock()
            worker.wait_for_selector = AsyncMock()

            async def tick(*args):
                await asyncio.sleep(0)

            worker.wait_for_timeout = tick
            worker.is_closed.return_value = False
            popup_ready = asyncio.Event()
            player = MagicMock()
            player.wait_for_load_state = AsyncMock(side_effect=TimeoutError("load failed"))
            player.is_closed.return_value = False
            player.close = AsyncMock()

            async def wait_for_popup(*args, **kwargs):
                await popup_ready.wait()
                return player

            worker.wait_for_event = wait_for_popup
            card = MagicMock()
            card.get_attribute = AsyncMock(side_effect=["Example", "javascript:void(0)"])
            card.click = AsyncMock(side_effect=popup_ready.set)
            links = MagicMock()
            links.count = AsyncMock(return_value=1)
            links.nth.return_value = card
            worker.locator.return_value = links
            task = {"page": 1, "title": "Example", "href": ""}
            with self.assertRaises(TimeoutError):
                await self.learner._open_online_course_from_list(
                    worker, "https://example.test/course/#/list/1", task)
            player.close.assert_awaited_once()

        asyncio.run(run())


class CourseQueueTests(unittest.TestCase):
    def test_card_href_resolves_only_navigable_same_site_urls(self):
        base = "https://u.ccb.com/course/#/list/1"
        self.assertEqual(_online_course_target_url(base, "#/detail/123"),
                         "https://u.ccb.com/course/#/detail/123")
        self.assertEqual(_online_course_target_url(base, "/course/#/play/123"),
                         "https://u.ccb.com/course/#/play/123")
        for href in ("", "#", "javascript:void(0)", "https://example.test/course/123"):
            self.assertEqual(_online_course_target_url(base, href), "")

    def test_unavailable_course_is_deferred_then_exhausted(self):
        async def run():
            queue = asyncio.Queue()
            task = {"title": "example"}
            for failure in (1, 2):
                self.assertTrue(_defer_online_course(queue, task))
                self.assertIs(queue.get_nowait(), task)
                self.assertEqual(task["list_failures"], failure)
            self.assertFalse(_defer_online_course(queue, task))
            self.assertTrue(queue.empty())

        asyncio.run(run())

    def test_playlist_videos_become_independent_queue_tasks(self):
        parent = {
            "page": 2,
            "title": "Parent course",
            "href": "https://u.ccb.com/course/#/play/course-id?pKnowledgeId=first&cid=course-id",
            "key": "course-key",
        }
        entries = [
            {"id": "video-1", "title": "第一节", "href": "https://u.ccb.com/course/#/play/course-id?pKnowledgeId=video-1&cid=course-id"},
            {"id": "video-2", "title": "第二节", "href": "https://u.ccb.com/course/#/play/course-id?pKnowledgeId=video-2&cid=course-id"},
            {"id": "video-2", "title": "第二节重复", "href": "https://u.ccb.com/course/#/play/course-id?pKnowledgeId=video-2&cid=course-id"},
            {"id": "foreign", "title": "外部链接", "href": "https://example.test/course/#/play/x?pKnowledgeId=foreign&cid=x"},
            {"id": "wrong", "title": "参数不匹配", "href": "https://u.ccb.com/course/#/play/x?pKnowledgeId=other&cid=x"},
        ]
        tasks = _build_online_playlist_tasks(parent, entries)
        self.assertEqual(len(tasks), 2)
        self.assertTrue(all(task["playlist_child"] for task in tasks))
        self.assertEqual([task["video_id"] for task in tasks], ["video-1", "video-2"])
        self.assertEqual(tasks[0]["key"], "course-key::video:video-1")
        self.assertIn("pKnowledgeId=video-2", tasks[1]["href"])

    def test_playlist_children_skip_individually_completed_videos(self):
        parent = {"title": "Parent", "key": "parent-key"}
        entries = [
            {"id": "done", "title": "Done video", "href": "https://u.ccb.com/course/#/play/x?pKnowledgeId=done&cid=x"},
            {"id": "todo", "title": "Todo video", "href": "https://u.ccb.com/course/#/play/x?pKnowledgeId=todo&cid=x"},
        ]
        tasks = _build_online_playlist_tasks(parent, entries, done_keys={"parent-key::video:done"})
        self.assertEqual([task["video_id"] for task in tasks], ["todo"])


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


class ProgressStyleTests(unittest.TestCase):
    def test_progress_chunk_keeps_pill_shape_at_tiny_values(self):
        stylesheet = _stylesheet(LIGHT)
        chunk_style = stylesheet.split("QProgressBar::chunk", 1)[1].split("}", 1)[0]
        self.assertIn("border-radius: 7px", chunk_style)
        self.assertIn("min-width: 14px", chunk_style)

    def test_custom_progress_paint_keeps_tiny_fill_wider_than_its_height(self):
        fill_width = RoundedGradientProgressBar.fill_width(108, 13, 3, 100)
        self.assertEqual(fill_width, 13)
        self.assertEqual(RoundedGradientProgressBar.fill_width(108, 13, 0, 100), 0)


if __name__ == "__main__":
    unittest.main()
