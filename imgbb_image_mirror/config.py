import os
from dataclasses import dataclass, field

try:
    import tomllib
except ImportError:
    import tomli as tomllib


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
    return cfg
