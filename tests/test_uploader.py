"""uploader._post_json / upload_image 的错误语义测试（假 session，不访问网络）。

imgbb 把业务错误嵌在 HTTP 200 的 JSON 里（status_code 400/403/429 等），
这里的用例锁死"任何非 200 都抛错"的契约，防止"静默成功"回归。
"""

import unittest

from imgbb_image_mirror.uploader import ImgbbUploader


class FakeResponse:
    def __init__(self, payload=None, http_status=200, text=""):
        self._payload = payload
        self.status_code = http_status
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class FakeSession:
    """按预设顺序对 post/get 返回响应，记录 post 调用。"""

    def __init__(self, responses):
        self._responses = list(responses)
        self.post_calls = []

    def post(self, url, **kwargs):
        self.post_calls.append(kwargs)
        return self._responses.pop(0)

    def get(self, url, **kwargs):
        return self._responses.pop(0)

    def close(self):
        pass


def _make_uploader(responses, proxy: str = "") -> tuple[ImgbbUploader, FakeSession]:
    session = FakeSession(responses)
    uploader = ImgbbUploader(cookie="c=1", auth_token="tok", delay=0, proxy=proxy)
    uploader._session = session
    return uploader, session


class PostJsonErrorTests(unittest.TestCase):
    def test_embedded_403_raises(self):
        # 回归用例：限流/flood 等嵌入状态码曾被静默放行，导致空 URL 被标记成已上传
        uploader, _ = _make_uploader(
            [FakeResponse({"status_code": 403, "error": {"message": "Flood detected"}})]
        )
        with self.assertRaisesRegex(RuntimeError, "403"):
            uploader._post_json({"action": "upload"})

    def test_embedded_400_raises(self):
        uploader, _ = _make_uploader(
            [FakeResponse({"status_code": 400, "error": {"message": "_upload failed"}})]
        )
        with self.assertRaisesRegex(RuntimeError, "imgbb API 错误"):
            uploader._post_json({"action": "upload"})

    def test_http_error_raises(self):
        uploader, _ = _make_uploader([FakeResponse({}, http_status=500)])
        with self.assertRaises(RuntimeError):
            uploader._post_json({"action": "upload"})

    def test_success_returns_result(self):
        payload = {"status_code": 200, "image": {"url": "https://i.ibb.co/x.jpg"}}
        uploader, _ = _make_uploader([FakeResponse(payload)])
        self.assertEqual(uploader._post_json({"action": "upload"}), payload)

    def test_proxy_param_accepted(self):
        # proxy 参数用于上传走独立出口（本机网络被图床风控时绕开）
        uploader = ImgbbUploader(cookie="c=1", auth_token="tok", delay=0, proxy="http://127.0.0.1:20899")
        self.assertIsNotNone(uploader._session)
        uploader.close()

    def test_token_expired_refreshes_and_retries(self):
        first = FakeResponse(
            {"status_code": 400, "error": {"message": "auth_token is invalid"}}
        )
        page = FakeResponse(text='var auth_token = "deadbeef";')
        ok = FakeResponse({"status_code": 200, "image": {"url": "u"}})
        uploader, session = _make_uploader([first, page, ok])

        result = uploader._post_json({"action": "upload"})

        self.assertEqual(result["image"]["url"], "u")
        self.assertEqual(uploader._auth_token, "deadbeef")
        self.assertEqual(len(session.post_calls), 2)
        self.assertEqual(session.post_calls[1]["data"]["auth_token"], "deadbeef")


class UploadImageTests(unittest.TestCase):
    def test_raises_on_embedded_error(self):
        uploader, _ = _make_uploader(
            [FakeResponse({"status_code": 429, "error": {"message": "rate limited"}})]
        )
        with self.assertRaisesRegex(RuntimeError, "429"):
            uploader.upload_image("https://src/x.jpg", "album-1")

    def test_extracts_fields_on_success(self):
        payload = {
            "status_code": 200,
            "image": {
                "url": "https://i.ibb.co/x.jpg",
                "thumb": {"url": "t"},
                "url_viewer": "v",
            },
        }
        uploader, _ = _make_uploader([FakeResponse(payload)])
        result = uploader.upload_image("https://src/x.jpg", "album-1", name="pic")
        self.assertEqual(
            result, {"url": "https://i.ibb.co/x.jpg", "thumb": "t", "viewer": "v"}
        )


if __name__ == "__main__":
    unittest.main()
