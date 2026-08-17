import json
import os
import tempfile
from contextlib import suppress
from datetime import UTC, datetime


def load_metadata_file(path: str) -> dict | None:
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        # 进程中途崩溃可能留下半截 JSON：不抛异常，交由调用方按"无缓存"重建
        return None


def save_metadata_file(path: str, metadata: dict):
    """原子写入：先写临时文件再 os.replace，避免中断留下半个 JSON。

    失败时保证不破坏原文件 —— 临时文件会保留为孤儿，由调用方清理。
    """
    target_dir = os.path.dirname(path)
    if target_dir:
        os.makedirs(target_dir, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=".tmp_", suffix=os.path.basename(path), dir=target_dir or None
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(metadata, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        # 清理临时文件，原文件保持不变
        with suppress(OSError):
            os.unlink(tmp_path)
        raise


def load_metadata(album_dir: str) -> dict | None:
    path = os.path.join(album_dir, "metadata.json")
    return load_metadata_file(path)


def save_metadata(album_dir: str, metadata: dict):
    path = os.path.join(album_dir, "metadata.json")
    save_metadata_file(path, metadata)


def create_metadata(album: dict, source_url: str = "") -> dict:
    return {
        "album_id": album["id"],
        "album_name": album["name"],
        "album_url": album["url"],
        "source_url": source_url,
        "scraped_at": datetime.now(UTC).isoformat(),
        "images": [],
    }


def add_image_entry(
    metadata: dict,
    index: int,
    original_url: str,
    thumb_url: str,
    page_url: str,
    filename: str,
    alt: str,
):
    metadata["images"].append(
        {
            "index": index,
            "original_url": original_url,
            "thumb_url": thumb_url,
            "page_url": page_url,
            "filename": filename,
            "alt": alt,
            "size": 0,
            "status": "pending",
        }
    )


def mark_downloaded(metadata: dict, index: int, size: int):
    for img in metadata["images"]:
        if img["index"] == index:
            img["status"] = "downloaded"
            img["size"] = size
            break


def mark_uploaded(metadata: dict, index: int, result: dict | None):
    for img in metadata["images"]:
        if img["index"] == index:
            img["status"] = "uploaded" if result else "failed"
            if result:
                img["uploaded_url"] = result.get("url", "")
                img["viewer_url"] = result.get("viewer", "")
                img["uploaded_thumb"] = result.get("thumb", "")
            break


def is_downloaded(metadata: dict, index: int, album_dir: str) -> bool:
    for img in metadata["images"]:
        if img["index"] == index and img["status"] == "downloaded":
            path = os.path.join(album_dir, img["filename"])
            if os.path.exists(path) and img["size"] > 0:
                return os.path.getsize(path) == img["size"]
    return False


def is_uploaded(metadata: dict, index: int) -> bool:
    for img in metadata.get("images", []):
        if img["index"] == index and img.get("status") == "uploaded":
            return True
    return False
