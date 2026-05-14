import json
import os
from datetime import datetime, timezone


def load_metadata_file(path: str) -> dict | None:
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_metadata_file(path: str, metadata: dict):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(metadata, f, ensure_ascii=False, indent=2)


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
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "images": [],
    }


def add_image_entry(metadata: dict, index: int, original_url: str,
                    thumb_url: str, page_url: str, filename: str, alt: str):
    metadata["images"].append({
        "index": index,
        "original_url": original_url,
        "thumb_url": thumb_url,
        "page_url": page_url,
        "filename": filename,
        "alt": alt,
        "size": 0,
        "status": "pending",
    })


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
