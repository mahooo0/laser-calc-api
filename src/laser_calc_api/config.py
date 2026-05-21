"""Runtime configuration for the laser_calc HTTP service.

All knobs live here so deployment never needs code changes — only env vars.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _parse_int(name: str, raw: str | None, default: int) -> int:
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as ex:
        raise ValueError(f"Env {name} must be an integer, got {raw!r}") from ex


def _parse_origins(raw: str | None) -> list[str]:
    if raw is None or raw.strip() == "":
        return ["*"]
    return [item.strip() for item in raw.split(",") if item.strip()]


def _parse_csv(raw: str | None) -> list[str]:
    if raw is None or raw.strip() == "":
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _parse_bool(name: str, raw: str | None, default: bool) -> bool:
    if raw is None or raw.strip() == "":
        return default
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ValueError(f"Env {name} must be a boolean, got {raw!r}")


@dataclass(frozen=True)
class Settings:
    host: str = "0.0.0.0"
    port: int = 8080
    allowed_origins: list[str] = field(default_factory=lambda: ["*"])
    max_upload_bytes: int = 10 * 1024 * 1024  # 10 MB
    prices_path: Path = field(default_factory=lambda: _project_root() / "prices_catalog.json")
    orders_path: Path = field(default_factory=lambda: _project_root() / "orders.csv")
    web_dir: Path = field(default_factory=lambda: _project_root() / "web")
    log_level: str = "INFO"
    telegram_bot_token: str = ""
    telegram_chat_ids: list[str] = field(default_factory=list)
    telegram_enabled: bool = True
    telegram_timeout_seconds: float = 5.0

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        env = env if env is not None else dict(os.environ)
        root = _project_root()
        return cls(
            host=env.get("HOST", "0.0.0.0"),
            port=_parse_int("PORT", env.get("PORT"), 8080),
            allowed_origins=_parse_origins(env.get("ALLOWED_ORIGINS")),
            max_upload_bytes=_parse_int(
                "MAX_UPLOAD_BYTES", env.get("MAX_UPLOAD_BYTES"), 10 * 1024 * 1024
            ),
            prices_path=Path(env.get("PRICES_CATALOG_PATH", str(root / "prices_catalog.json"))),
            orders_path=Path(env.get("ORDERS_CSV_PATH", str(root / "orders.csv"))),
            web_dir=Path(env.get("WEB_DIR", str(root / "web"))),
            log_level=env.get("LOG_LEVEL", "INFO").upper(),
            telegram_bot_token=env.get("TELEGRAM_BOT_TOKEN", "").strip(),
            telegram_chat_ids=_parse_csv(env.get("TELEGRAM_CHAT_IDS")),
            telegram_enabled=_parse_bool("TELEGRAM_ENABLED", env.get("TELEGRAM_ENABLED"), True),
            telegram_timeout_seconds=float(env.get("TELEGRAM_TIMEOUT_SECONDS", "5")),
        )

    def is_telegram_active(self) -> bool:
        """Auto-disabled when token or chat_ids are empty, even if enabled flag is true."""
        return (
            self.telegram_enabled and bool(self.telegram_bot_token) and bool(self.telegram_chat_ids)
        )
