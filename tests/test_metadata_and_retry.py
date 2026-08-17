import os
import queue
import tempfile
import threading
import unittest
from unittest import mock

from imgbb_image_mirror.metadata import (
    load_metadata_file,
    save_metadata_file,
)
from imgbb_image_mirror.orchestrator import upload_worker


class AtomicWriteTests(unittest.TestCase):
    def test_save_then_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "sub", "state.json")
            payload = {"album_id": "x", "images": [{"index": 0, "status": "uploaded"}]}
            save_metadata_file(path, payload)
            self.assertEqual(load_metadata_file(path), payload)

    def test_corrupted_file_returns_none(self):
        # 模拟进程中途崩溃留下的半截 JSON
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "state.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write('{"album_id": "x", "images": [')
            # 不抛异常，交由调用方按无缓存重建
            self.assertIsNone(load_metadata_file(path))

    def test_save_does_not_corrupt_existing_on_failure(self):
        # 已有合法文件时，写新内容失败应保留原文件
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "state.json")
            original = {"v": 1}
            save_metadata_file(path, original)

            # 让 json.dump 抛异常
            with mock.patch(
                "imgbb_image_mirror.metadata.json.dump", side_effect=RuntimeError("boom")
            ):
                with self.assertRaises(RuntimeError):
                    save_metadata_file(path, {"v": 2})

            # 原文件应仍然可读且内容不变
            self.assertEqual(load_metadata_file(path), original)

    def test_no_tmp_file_left_after_success(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "state.json")
            save_metadata_file(path, {"x": 1})
            leftovers = [n for n in os.listdir(d) if n.startswith(".tmp_")]
            self.assertEqual(leftovers, [])


class FakeUploader:
    """记录每次 upload_image 的调用次数与返回值序列。"""

    def __init__(self, results):
        self._results = list(results)
        self.calls = 0

    def upload_image(self, url, album_id, name=""):
        self.calls += 1
        if self.calls <= len(self._results):
            r = self._results[self.calls - 1]
            if isinstance(r, Exception):
                raise r
            return r
        return None


class UploadWorkerRetryTests(unittest.TestCase):
    def _task(self, idx=0):
        return {
            "type": "url",
            "url": "https://x/y.jpg",
            "album_id": "a",
            "index": idx,
            "title": "t",
            "upload_name": "y.jpg",
        }

    def _run_worker(self, uploader, task):
        """在后台线程跑 worker，主线程投递单个任务后取结果。"""
        q = queue.Queue()
        out = queue.Queue()
        with mock.patch("imgbb_image_mirror.orchestrator.time.sleep"):
            worker = threading.Thread(
                target=upload_worker,
                args=(uploader, "a", q, out),
                kwargs={"max_retries": 2},
                daemon=True,
            )
            worker.start()
            q.put(task)
            q.put(None)
            worker.join(timeout=5)
        self.assertFalse(worker.is_alive(), "worker 超时未退出")
        return out.get_nowait()

    def test_success_on_first_try(self):
        uploader = FakeUploader([{"url": "ok"}])
        item = self._run_worker(uploader, self._task())
        self.assertEqual(item["result"], {"url": "ok"})
        self.assertEqual(uploader.calls, 1)

    def test_retries_then_succeeds(self):
        # 前 2 次抛异常，第 3 次成功 —— max_retries=2 刚好覆盖
        uploader = FakeUploader(
            [
                RuntimeError("transient"),
                RuntimeError("transient"),
                {"url": "ok"},
            ]
        )
        item = self._run_worker(uploader, self._task())
        self.assertEqual(item["result"], {"url": "ok"})
        self.assertEqual(uploader.calls, 3)

    def test_exhausts_retries_returns_last_error(self):
        uploader = FakeUploader(
            [
                RuntimeError("boom"),
                RuntimeError("boom"),
                RuntimeError("boom"),
            ]
        )
        item = self._run_worker(uploader, self._task())
        self.assertIsNone(item["result"])
        self.assertIn("boom", item["error"])
        self.assertEqual(uploader.calls, 3)

    def test_empty_result_is_retried(self):
        # upload_image 返回 None 视为失败，应重试
        uploader = FakeUploader([None, None, {"url": "ok"}])
        item = self._run_worker(uploader, self._task())
        self.assertEqual(item["result"], {"url": "ok"})


if __name__ == "__main__":
    unittest.main()
