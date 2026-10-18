from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")


def _integer(name: str, default: int, low: int, high: int) -> int:
    value = int(os.getenv(name, str(default)))
    if not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}")
    return value


@dataclass(frozen=True)
class Settings:
    feishu_app_id: str = os.getenv("FEISHU_APP_ID", "").strip()
    feishu_app_secret: str = field(default_factory=lambda: os.getenv("FEISHU_APP_SECRET", "").strip(), repr=False)
    allowed_open_id: str = os.getenv("ALLOWED_FEISHU_OPEN_ID", "").strip()
    daily_send_time: str = os.getenv("DAILY_SEND_TIME", "09:00").strip()
    timezone: str = os.getenv("TIMEZONE", "Asia/Shanghai").strip()
    daily_catchup_minutes: int = _integer("DAILY_CATCHUP_MINUTES", 30, 0, 180)
    ai_provider: str = os.getenv("AI_PROVIDER", "api").strip().lower()
    codex_model: str = os.getenv("CODEX_MODEL", "").strip()
    codex_timeout: int = _integer("CODEX_TIMEOUT_SECONDS", 180, 10, 900)
    api_base_url: str = os.getenv("NAN_BASE_URL", "https://meta-api.vip/v1").strip().rstrip("/")
    api_key: str = field(default_factory=lambda: os.getenv("NAN_API_KEY", "").strip(), repr=False)
    api_model: str = os.getenv("NAN_MODEL", "").strip()
    api_timeout: int = _integer("API_TIMEOUT_SECONDS", 60, 5, 180)
    api_daily_max_calls: int = _integer("API_DAILY_MAX_CALLS", 30, 1, 500)
    api_max_output_tokens: int = _integer("API_MAX_OUTPUT_TOKENS", 900, 64, 4096)
    article_max_chars: int = _integer("ARTICLE_MAX_CHARS", 18000, 1000, 60000)
    proxy_urls: str = os.getenv("PROXY_URLS", "").strip()
    rss_feeds: str = os.getenv("RSS_FEEDS", "").strip()
    data_dir: Path = Path(os.getenv("DATA_DIR", str(ROOT / "data")))

    def validate(self) -> list[str]:
        invalid = []
        if not self.feishu_app_id or not self.feishu_app_id.startswith("cli_"):
            invalid.append("FEISHU_APP_ID")
        if not self.feishu_app_secret:
            invalid.append("FEISHU_APP_SECRET")
        if not self.allowed_open_id or not re.fullmatch(r"ou_[A-Za-z0-9_-]+", self.allowed_open_id):
            invalid.append("ALLOWED_FEISHU_OPEN_ID (one open_id)")
        if self.ai_provider not in {"api", "codex"}:
            invalid.append("AI_PROVIDER (api/codex)")
        if self.ai_provider == "api" and (not self.api_key or not self.api_model):
            invalid.append("NAN_API_KEY/NAN_MODEL")
        parsed = urlsplit(self.api_base_url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.path.rstrip("/") != "/v1"):
            invalid.append("NAN_BASE_URL (HTTPS /v1 endpoint)")
        if not re.fullmatch(r"[0-2][0-9]:[0-5][0-9]", self.daily_send_time):
            invalid.append("DAILY_SEND_TIME")
        else:
            try:
                self.send_hour
            except ValueError:
                invalid.append("DAILY_SEND_TIME")
        try:
            ZoneInfo(self.timezone)
        except (ValueError, KeyError):
            invalid.append("TIMEZONE")
        return invalid

    @property
    def send_hour(self) -> int:
        hour, minute = (int(part) for part in self.daily_send_time.split(":"))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError("invalid time")
        return hour

    @property
    def send_minute(self) -> int:
        return int(self.daily_send_time.split(":")[1])


settings = Settings()
