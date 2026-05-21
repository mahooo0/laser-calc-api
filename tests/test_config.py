"""Unit tests for laser_calc_api.config.Settings."""

from __future__ import annotations

from pathlib import Path

import pytest

from laser_calc_api.config import Settings


def test_defaults_when_env_empty() -> None:
    settings = Settings.from_env({})
    assert settings.host == "0.0.0.0"
    assert settings.port == 8080
    assert settings.allowed_origins == ["*"]
    assert settings.max_upload_bytes == 10 * 1024 * 1024
    assert settings.log_level == "INFO"
    assert settings.prices_path.name == "prices_catalog.json"
    assert settings.orders_path.name == "orders.csv"


def test_overrides_from_env() -> None:
    env = {
        "HOST": "127.0.0.1",
        "PORT": "9090",
        "ALLOWED_ORIGINS": "https://a.example,https://b.example",
        "MAX_UPLOAD_BYTES": "5242880",
        "PRICES_CATALOG_PATH": "/data/prices.json",
        "ORDERS_CSV_PATH": "/data/orders.csv",
        "LOG_LEVEL": "debug",
    }
    settings = Settings.from_env(env)
    assert settings.host == "127.0.0.1"
    assert settings.port == 9090
    assert settings.allowed_origins == ["https://a.example", "https://b.example"]
    assert settings.max_upload_bytes == 5_242_880
    assert settings.prices_path == Path("/data/prices.json")
    assert settings.orders_path == Path("/data/orders.csv")
    assert settings.log_level == "DEBUG"


def test_origins_strip_whitespace_and_drop_empty() -> None:
    settings = Settings.from_env({"ALLOWED_ORIGINS": " https://a.example , , https://b.example "})
    assert settings.allowed_origins == ["https://a.example", "https://b.example"]


def test_origins_default_when_value_blank() -> None:
    settings = Settings.from_env({"ALLOWED_ORIGINS": "   "})
    assert settings.allowed_origins == ["*"]


def test_invalid_port_raises() -> None:
    with pytest.raises(ValueError, match="PORT"):
        Settings.from_env({"PORT": "not-a-number"})


def test_invalid_max_upload_raises() -> None:
    with pytest.raises(ValueError, match="MAX_UPLOAD_BYTES"):
        Settings.from_env({"MAX_UPLOAD_BYTES": "ten"})
