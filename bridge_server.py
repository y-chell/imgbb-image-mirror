"""本地中转服务器：接收浏览器 POST 的图片数据，用 curl 转发上传到 imgbb。"""
import json
import base64
import subprocess
import tempfile
import os
from http.server import HTTPServer, BaseHTTPRequestHandler

IMGBB_COOKIE = os.environ.get("IMGBB_COOKIE", "")
IMGBB_TOKEN = os.environ.get("IMGBB_TOKEN", "")


def require_imgbb_session():
    if not IMGBB_COOKIE or not IMGBB_TOKEN:
        raise RuntimeError("请先设置 IMGBB_COOKIE 和 IMGBB_TOKEN 环境变量")


def curl_upload(file_path: str, album_id: str, name: str = "") -> dict:
    """用 curl 上传文件到 imgbb（绕过 Python OpenSSL 兼容性问题）"""
    require_imgbb_session()
    cmd = [
        "curl", "-s", "-X", "POST", "https://imgbb.com/json",
        "-H", f"Cookie: {IMGBB_COOKIE}",
        "-H", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "-F", "action=upload",
        "-F", "type=file",
        "-F", f"album_id={album_id}",
        "-F", f"auth_token={IMGBB_TOKEN}",
        "-F", f"source=@{file_path};filename={name or 'image.webp'}",
        "--max-time", "60",
    ]
    if name:
        cmd.extend(["-F", f"title={name}"])
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    if result.returncode != 0:
        return {"error": f"curl failed: {result.stderr}"}
    try:
        data = json.loads(result.stdout)
        if data.get("status_code") == 200:
            img = data.get("image", {})
            return {
                "url": img.get("url", ""),
                "thumb": img.get("thumb", {}).get("url", ""),
                "viewer": img.get("url_viewer", ""),
            }
        return {"error": f"imgbb: {data.get('error', {}).get('message', 'unknown')}"}
    except json.JSONDecodeError:
        return {"error": f"bad response: {result.stdout[:200]}"}


def curl_create_album(name: str) -> dict:
    """用 curl 创建 imgbb 相册"""
    require_imgbb_session()
    cmd = [
        "curl", "-s", "-X", "POST", "https://imgbb.com/json",
        "-H", f"Cookie: {IMGBB_COOKIE}",
        "-H", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "-F", "action=create-album",
        "-F", "type=images",
        "-F", "album[new]=true",
        "-F", f"album[name]={name}",
        "-F", "album[privacy]=public",
        "-F", f"auth_token={IMGBB_TOKEN}",
        "--max-time", "30",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
    if result.returncode != 0:
        return {"error": f"curl failed: {result.stderr}"}
    try:
        data = json.loads(result.stdout)
        album = data.get("album", {})
        return {"id": album.get("id_encoded", ""), "url": album.get("url", ""), "name": name}
    except json.JSONDecodeError:
        return {"error": f"bad response: {result.stdout[:200]}"}


class Handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(200)
        self._cors_headers()
        self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))

        action = body.get("action")
        try:
            if action == "create_album":
                result = curl_create_album(body["name"])
                self._respond(200, result)
            elif action == "upload":
                img_data = base64.b64decode(body["data"])
                tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".webp")
                tmp.write(img_data)
                tmp.close()
                try:
                    result = curl_upload(tmp.name, body["album_id"], name=body.get("name", ""))
                    self._respond(200, result)
                finally:
                    os.unlink(tmp.name)
            else:
                self._respond(400, {"error": f"unknown action: {action}"})
        except Exception as e:
            self._respond(500, {"error": str(e)})

    def _respond(self, code, data):
        self.send_response(code)
        self._cors_headers()
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def _cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def log_message(self, fmt, *args):
        print(f"[bridge] {args[0]}")


if __name__ == "__main__":
    server = HTTPServer(("127.0.0.1", 18888), Handler)
    print("Bridge server running on http://127.0.0.1:18888")
    print("Using curl for imgbb uploads (bypasses Python SSL issues)")
    print("Waiting for browser to send images...")
    server.serve_forever()
