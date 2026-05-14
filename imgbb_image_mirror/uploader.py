import mimetypes
import os
import re
import logging
import time
from threading import Lock

import httpx

logger = logging.getLogger(__name__)


class ImgbbUploader:
    """imgbb 网页端 API 客户端，支持创建相册和上传图片到指定相册。"""

    def __init__(self, cookie: str, auth_token: str = "", delay: float = 1.0):
        self._cookie = cookie
        self._auth_token = auth_token
        self._delay = delay
        self._last_request = 0.0
        self._lock = Lock()
        self._client = httpx.Client(
            headers={
                "Cookie": cookie,
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/131.0.0.0 Safari/537.36",
            },
            timeout=60,
            follow_redirects=True,
        )
        if not self._auth_token:
            self._refresh_auth_token()

    def _throttle(self):
        with self._lock:
            elapsed = time.monotonic() - self._last_request
            if elapsed < self._delay:
                time.sleep(self._delay - elapsed)
            self._last_request = time.monotonic()

    def _refresh_auth_token(self):
        """从 imgbb 页面获取 auth_token"""
        self._throttle()
        resp = self._client.get("https://imgbb.com/")
        match = re.search(r'auth_token\s*=\s*"([a-f0-9]+)"', resp.text)
        if not match:
            match = re.search(r'name="auth_token"[^>]*value="([a-f0-9]+)"', resp.text)
        if match:
            self._auth_token = match.group(1)
            logger.debug(f"auth_token 已刷新: {self._auth_token[:8]}...")
        else:
            raise RuntimeError("无法获取 auth_token，请检查 cookie 是否有效")

    def _post_json(self, data: dict, files: dict | None = None) -> dict:
        data["auth_token"] = self._auth_token
        self._throttle()
        resp = self._client.post("https://imgbb.com/json", data=data, files=files)
        resp.raise_for_status()
        result = resp.json()
        if result.get("status_code") == 400:
            error = result.get("error", {}).get("message", "Unknown error")
            if "auth_token" in error.lower() or "denied" in error.lower():
                logger.info("auth_token 过期，刷新中...")
                self._refresh_auth_token()
                data["auth_token"] = self._auth_token
                self._throttle()
                resp = self._client.post("https://imgbb.com/json", data=data, files=files)
                resp.raise_for_status()
                result = resp.json()
            if result.get("status_code") != 200:
                raise RuntimeError(f"imgbb API 错误: {result}")
        return result

    def create_album(self, name: str, privacy: str = "public",
                     description: str = "") -> dict:
        result = self._post_json({
            "action": "create-album",
            "type": "images",
            "album[new]": "true",
            "album[name]": name,
            "album[privacy]": privacy,
            "album[description]": description,
        })
        album = result.get("album", {})
        album_id = album.get("id_encoded", "")
        album_url = album.get("url", "")
        logger.info(f"创建相册: {name} (id={album_id}, url={album_url})")
        return {"id": album_id, "url": album_url, "name": name}

    def upload_image(self, image_url: str, album_id: str,
                     name: str = "") -> dict | None:
        """通过 URL 上传图片到指定相册（imgbb 支持 URL 上传，无需下载到本地）"""
        try:
            data = {
                "action": "upload",
                "type": "url",
                "source": image_url,
                "album_id": album_id,
            }
            if name:
                data["title"] = name
            result = self._post_json(data)
            img = result.get("image", {})
            return {
                "url": img.get("url", ""),
                "thumb": img.get("thumb", {}).get("url", ""),
                "viewer": img.get("url_viewer", ""),
            }
        except Exception as e:
            logger.error(f"上传失败 ({name}): {e}")
            return None

    def upload_file(self, file_path: str, album_id: str,
                    name: str = "", upload_filename: str = "") -> dict | None:
        """上传本地文件到指定相册"""
        try:
            data = {
                "action": "upload",
                "type": "file",
                "album_id": album_id,
            }
            if name:
                data["title"] = name
            multipart_name = upload_filename or os.path.basename(file_path)
            mime_type = mimetypes.guess_type(multipart_name)[0] or "application/octet-stream"
            with open(file_path, "rb") as f:
                result = self._post_json(data, files={
                    "source": (multipart_name, f, mime_type),
                })
            img = result.get("image", {})
            return {
                "url": img.get("url", ""),
                "thumb": img.get("thumb", {}).get("url", ""),
                "viewer": img.get("url_viewer", ""),
            }
        except Exception as e:
            logger.error(f"上传失败 ({file_path}): {e}")
            return None

    def delete_album(self, album_id: str) -> bool:
        try:
            self._post_json({
                "action": "delete",
                "delete": "album",
                "deleting[id]": album_id,
                "single": "true",
            })
            return True
        except Exception:
            return False

    def close(self):
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
