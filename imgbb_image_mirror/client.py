"""源站抓取客户端。

历史上用 ``httpx``，但图片站常有 Cloudflare/TLS 指纹拦截，导致 403
或握手失败。这里改用 ``curl_cffi`` 的 ``impersonate="chrome"``，让
Python 侧的 JA3/JA4 指纹与真实 Chrome 一致，从根上消除这类 403。

对外接口（fetch / download / close / 上下文管理）与旧 httpx 实现一致，
downloader.py 无需改动。
"""

import logging
import os
import threading
import time
from contextlib import suppress

from curl_cffi import requests as cffi_requests

logger = logging.getLogger(__name__)


class ImgbbClient:
    """源站抓取客户端：限流 + 重试，curl_cffi impersonate Chrome。

    UA 使用 impersonate 自带头，与 TLS 指纹严格配套；调用方传 headers 时
    只做增量补充（如 Referer），不要覆盖 UA。
    """

    def __init__(self, workers: int = 4, delay: float = 0.5, timeout: float = 30):
        self._delay = delay
        self._semaphore = threading.Semaphore(workers)
        self._lock = threading.Lock()
        self._last_request = 0.0
        self._timeout = timeout
        self._session: cffi_requests.Session = cffi_requests.Session(impersonate="chrome")

    def _throttle(self):
        with self._lock:
            now = time.monotonic()
            wait = self._delay - (now - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()

    def fetch(self, url: str, retries: int = 3, headers: dict | None = None) -> str:
        with self._semaphore:
            self._throttle()
            for attempt in range(retries):
                try:
                    resp = self._session.get(
                        url,
                        headers=headers,
                        timeout=self._timeout,
                        allow_redirects=True,
                    )
                    resp.raise_for_status()
                    return resp.text
                except Exception as e:
                    logger.debug(f"fetch {url} attempt {attempt + 1} failed: {e}")
                    if attempt == retries - 1:
                        raise
                    time.sleep(2**attempt)
        return ""

    def download(self, url: str, dest: str, retries: int = 3, headers: dict | None = None) -> bool:
        """下载到 dest。先写 dest.part，成功后原子替换，失败不留半截文件。"""
        with self._semaphore:
            self._throttle()
            tmp_path = f"{dest}.part"
            try:
                for attempt in range(retries):
                    try:
                        resp = self._session.get(
                            url,
                            headers=headers,
                            timeout=self._timeout,
                            allow_redirects=True,
                            stream=True,
                        )
                        resp.raise_for_status()
                        with open(tmp_path, "wb") as f:
                            for chunk in resp.iter_content(8192):
                                if chunk:
                                    f.write(chunk)
                        os.replace(tmp_path, dest)
                        return True
                    except Exception as e:
                        logger.debug(f"download {url} attempt {attempt + 1} failed: {e}")
                        if attempt == retries - 1:
                            logger.warning(f"下载失败: {os.path.basename(dest)}: {e}")
                            return False
                        time.sleep(2**attempt)
            finally:
                # os.replace 成功后 tmp 已不存在；任何失败路径都清掉半截文件
                with suppress(OSError):
                    os.unlink(tmp_path)
        return False

    def close(self):
        self._session.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
