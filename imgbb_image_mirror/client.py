import os
import time
import logging
import threading

import httpx

logger = logging.getLogger(__name__)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


class ImgbbClient:
    def __init__(self, workers: int = 4, delay: float = 0.5, timeout: float = 30):
        self._delay = delay
        self._semaphore = threading.Semaphore(workers)
        self._lock = threading.Lock()
        self._last_request = 0.0
        self._client = httpx.Client(
            headers={"User-Agent": UA},
            timeout=httpx.Timeout(timeout, read=60),
            follow_redirects=True,
            limits=httpx.Limits(max_connections=workers * 2, max_keepalive_connections=workers),
        )

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
                    resp = self._client.get(url, headers=headers)
                    resp.raise_for_status()
                    return resp.text
                except (httpx.HTTPError, httpx.TimeoutException) as e:
                    logger.debug(f"fetch {url} attempt {attempt+1} failed: {e}")
                    if attempt == retries - 1:
                        raise
                    time.sleep(2 ** attempt)
        return ""

    def download(self, url: str, dest: str, retries: int = 3,
                 headers: dict | None = None) -> bool:
        with self._semaphore:
            self._throttle()
            for attempt in range(retries):
                try:
                    with self._client.stream("GET", url, headers=headers) as resp:
                        resp.raise_for_status()
                        with open(dest, "wb") as f:
                            for chunk in resp.iter_bytes(8192):
                                f.write(chunk)
                    return True
                except (httpx.HTTPError, httpx.TimeoutException, OSError) as e:
                    logger.debug(f"download {url} attempt {attempt+1} failed: {e}")
                    if attempt == retries - 1:
                        logger.warning(f"下载失败: {os.path.basename(dest)}: {e}")
                        return False
                    time.sleep(2 ** attempt)
        return False

    def close(self):
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
