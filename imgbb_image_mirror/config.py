import os
from dataclasses import dataclass, field

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore[no-redef]

# keyring 是可选依赖，缺失时优雅降级
try:
    import keyring

    _HAS_KEYRING = True
except ImportError:
    _HAS_KEYRING = False

# 环境变量名 + keyring service 名
IMGBB_COOKIE_ENV = "IMGBB_COOKIE"
IMGBB_TOKEN_ENV = "IMGBB_TOKEN"
KEYRING_SERVICE = "imgbb-image-mirror"


@dataclass
class ImgbbConfig:
    enabled: bool = False
    cookie: str = ""
    auth_token: str = ""


@dataclass
class Config:
    output: str = "./downloads"
    workers: int = 4
    delay: float = 0.5
    max_pages: int = 100
    user_agent: str = ""
    imgbb: ImgbbConfig = field(default_factory=ImgbbConfig)


def _secret_from_env_or_keyring(env_name: str, keyring_key: str, fallback: str) -> str:
    """读取密钥：环境变量 > keyring > 传入的 fallback（通常是配置文件值）。

    三者都为空时返回空串。keyring 缺失或读取异常时降级到 fallback。
    """
    env_val = os.environ.get(env_name, "").strip()
    if env_val:
        return env_val
    if _HAS_KEYRING:
        try:
            kr_val = keyring.get_password(KEYRING_SERVICE, keyring_key)
            if kr_val:
                return kr_val
        except Exception:
            # keyring backend 异常（如无 DBus）时静默降级
            pass
    return fallback


def load_config(path: str | None = None) -> Config:
    cfg = Config()
    if path and os.path.exists(path):
        with open(path, "rb") as f:
            data = tomllib.load(f)
        scraper = data.get("scraper", {})
        for key in ("output", "workers", "delay", "max_pages", "user_agent"):
            if key in scraper:
                setattr(cfg, key, scraper[key])
        imgbb_data = data.get("imgbb", {})
        for key in ("enabled", "cookie", "auth_token"):
            if key in imgbb_data:
                setattr(cfg.imgbb, key, imgbb_data[key])

    # cookie/token 支持从环境变量或 keyring 读取（优先级高于配置文件）
    # 这样配置文件不必保留敏感信息，可直接提交
    cfg.imgbb.cookie = _secret_from_env_or_keyring(IMGBB_COOKIE_ENV, "cookie", cfg.imgbb.cookie)
    cfg.imgbb.auth_token = _secret_from_env_or_keyring(
        IMGBB_TOKEN_ENV, "auth_token", cfg.imgbb.auth_token
    )
    return cfg
