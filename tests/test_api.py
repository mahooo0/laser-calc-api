"""Integration tests for the laser_calc Flask app."""

from __future__ import annotations

import csv
import io
import json
import math
from pathlib import Path

import pytest

from laser_calc_api.app import create_app
from laser_calc_api.services import DEFAULT_PRICE_CATALOG

CATALOG_FIXTURE: dict[str, dict[str, dict[str, object]]] = {
    "Ст3": {
        # price_meter tiers: [>=100, 50-100, 10-50, <10] м/п
        "1.0": {
            "price_meter": [10.0, 12.0, 16.0, 24.0],
            "feed_mm_min": 30000,
            "pierce_time_s": 1.0,
            "material_m2": 0,
        },
        "2.0": {
            "price_meter": [12.0, 14.0, 18.0, 26.0],
            "feed_mm_min": 12000,
            "pierce_time_s": 1.4,
            "material_m2": 0,
        },
    },
    "Алюминий AMg": {
        "3.0": {
            "price_meter": [40.0, 50.0, 65.0, 90.0],
            "feed_mm_min": 17000,
            "pierce_time_s": 1.3,
            "material_m2": 0,
        },
    },
}


@pytest.fixture
def app(tmp_path: Path):
    prices_path = tmp_path / "prices_catalog.json"
    orders_path = tmp_path / "orders.csv"
    web_dir = tmp_path / "web"
    web_dir.mkdir()
    (web_dir / "index.html").write_text("<html><body>stub</body></html>", encoding="utf-8")

    prices_path.write_text(json.dumps({"grades": CATALOG_FIXTURE}), encoding="utf-8")

    flask_app = create_app(
        prices_path=prices_path,
        orders_path=orders_path,
        web_dir=web_dir,
        configure_logging_on_init=False,
    )
    flask_app.config["TESTING"] = True
    flask_app.config["_PRICES_PATH"] = prices_path
    flask_app.config["_ORDERS_PATH"] = orders_path
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


def _multipart(
    sample_dxf: Path,
    *,
    metal_grade: str = "Ст3",
    metal_thickness: str = "1.0",
    quantity: str = "1",
    client_name: str = "Тест Клиент",
    company_name: str = "",
    phone: str = "+380501234567",
    email: str = "test@example.com",
) -> dict[str, object]:
    return {
        "file": (io.BytesIO(sample_dxf.read_bytes()), sample_dxf.name),
        "metal_grade": metal_grade,
        "metal_thickness": metal_thickness,
        "quantity": quantity,
        "client_name": client_name,
        "company_name": company_name,
        "phone": phone,
        "email": email,
    }


def test_health(client) -> None:
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True}


def test_catalog_returns_grades(client) -> None:
    resp = client.get("/api/catalog")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    grades = {g["metal_grade"]: g["thicknesses"] for g in body["grades"]}
    assert grades == {
        "Ст3": ["1.0", "2.0"],
        "Алюминий AMg": ["3.0"],
    }


def test_catalog_falls_back_to_default_when_file_missing(tmp_path: Path) -> None:
    web_dir = tmp_path / "web"
    web_dir.mkdir()
    (web_dir / "index.html").write_text("<html></html>", encoding="utf-8")
    flask_app = create_app(
        prices_path=tmp_path / "missing.json",
        orders_path=tmp_path / "orders.csv",
        web_dir=web_dir,
        configure_logging_on_init=False,
    )

    resp = flask_app.test_client().get("/api/catalog")
    assert resp.status_code == 200
    grades = {g["metal_grade"] for g in resp.get_json()["grades"]}
    assert grades == set(DEFAULT_PRICE_CATALOG.keys())


def test_calculate_requires_file(client) -> None:
    resp = client.post("/api/calculate", data={"client_name": "x"})
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "DXF file is required"


def test_calculate_requires_grade_and_thickness(client, sample_dxf: Path) -> None:
    payload = _multipart(sample_dxf, metal_grade="", metal_thickness="")
    resp = client.post("/api/calculate", data=payload, content_type="multipart/form-data")
    assert resp.status_code == 400
    assert "Metal grade and thickness" in resp.get_json()["error"]


def test_calculate_requires_client_or_company(client, sample_dxf: Path) -> None:
    payload = _multipart(sample_dxf, client_name="", company_name="")
    resp = client.post("/api/calculate", data=payload, content_type="multipart/form-data")
    assert resp.status_code == 400
    assert "Client name or company name" in resp.get_json()["error"]


def test_calculate_requires_phone(client, sample_dxf: Path) -> None:
    payload = _multipart(sample_dxf, phone="")
    resp = client.post("/api/calculate", data=payload, content_type="multipart/form-data")
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "Phone is required"


def test_calculate_requires_email(client, sample_dxf: Path) -> None:
    payload = _multipart(sample_dxf, email="")
    resp = client.post("/api/calculate", data=payload, content_type="multipart/form-data")
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "Email is required"


def test_calculate_rejects_non_dxf_extension(client, sample_dxf: Path) -> None:
    payload = _multipart(sample_dxf)
    payload["file"] = (io.BytesIO(sample_dxf.read_bytes()), "evil.exe")
    resp = client.post("/api/calculate", data=payload, content_type="multipart/form-data")
    assert resp.status_code == 400
    assert "dxf" in resp.get_json()["error"].lower()


def test_calculate_rejects_invalid_email(client, sample_dxf: Path) -> None:
    payload = _multipart(sample_dxf, email="not-an-email")
    resp = client.post("/api/calculate", data=payload, content_type="multipart/form-data")
    assert resp.status_code == 400
    assert "Email format" in resp.get_json()["error"]


def test_calculate_rejects_overlong_phone(client, sample_dxf: Path) -> None:
    payload = _multipart(sample_dxf, phone="+" + "1" * 64)
    resp = client.post("/api/calculate", data=payload, content_type="multipart/form-data")
    assert resp.status_code == 400
    assert "Phone" in resp.get_json()["error"]


@pytest.mark.parametrize(
    "bad_phone",
    ["+380", "abc", "12345", "+38067111223", "+3806711122334", "+49 151 23456789"],
)
def test_calculate_rejects_non_ukrainian_phone(client, sample_dxf: Path, bad_phone: str) -> None:
    payload = _multipart(sample_dxf, phone=bad_phone)
    resp = client.post("/api/calculate", data=payload, content_type="multipart/form-data")
    assert resp.status_code == 400
    assert "Ukrainian" in resp.get_json()["error"]


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [
        ("+380671112233", "+380671112233"),
        ("380671112233", "+380671112233"),
        ("0671112233", "+380671112233"),
        ("+38 (067) 111-22-33", "+380671112233"),
        ("067 111 22 33", "+380671112233"),
    ],
)
def test_calculate_normalizes_ukrainian_phone(
    client, app, sample_dxf: Path, raw: str, canonical: str
) -> None:
    payload = _multipart(sample_dxf, phone=raw)
    resp = client.post("/api/calculate", data=payload, content_type="multipart/form-data")
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["client"]["phone"] == canonical
    # The canonical form is what gets persisted to orders.csv.
    orders_path = Path(app.config["ORDERS_PATH"])
    last_row = list(csv.DictReader(orders_path.open(encoding="utf-8-sig")))[-1]
    assert last_row["phone"] == canonical


def test_calculate_rejects_excessive_quantity(client, sample_dxf: Path) -> None:
    payload = _multipart(sample_dxf, quantity="999999999")
    resp = client.post("/api/calculate", data=payload, content_type="multipart/form-data")
    assert resp.status_code == 400
    assert "Quantity" in resp.get_json()["error"]


def test_calculate_rejects_oversized_upload(tmp_path: Path) -> None:
    from laser_calc_api.config import Settings

    web_dir = tmp_path / "web"
    web_dir.mkdir()
    (web_dir / "index.html").write_text("<html></html>", encoding="utf-8")
    settings = Settings(
        max_upload_bytes=1024,  # 1 KB
        prices_path=tmp_path / "missing.json",
        orders_path=tmp_path / "orders.csv",
        web_dir=web_dir,
    )
    flask_app = create_app(settings=settings, configure_logging_on_init=False)
    test_client = flask_app.test_client()

    # > 1 KB of bytes pretending to be a DXF
    big_payload = b"0" * 4096
    resp = test_client.post(
        "/api/calculate",
        data={
            "file": (io.BytesIO(big_payload), "big.dxf"),
            "metal_grade": "Ст3",
            "metal_thickness": "1.0",
            "client_name": "Test",
            "phone": "+380501234567",
            "email": "test@example.com",
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 413
    assert resp.get_json()["ok"] is False


def test_calculate_unknown_grade(client, sample_dxf: Path) -> None:
    payload = _multipart(sample_dxf, metal_grade="Unobtainium")
    resp = client.post("/api/calculate", data=payload, content_type="multipart/form-data")
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "Unknown metal grade"


def test_calculate_happy_path_pricing_snapshot(client, app, sample_dxf: Path) -> None:
    payload = _multipart(sample_dxf, metal_grade="Ст3", metal_thickness="1.0", quantity="2")
    resp = client.post("/api/calculate", data=payload, content_type="multipart/form-data")

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["order_saved"] is True

    cut_length_mm_expected = 40.0 + 2.0 * math.pi * 5.0 + 10.0
    area_mm2_expected = 100.0 + math.pi * 25.0
    cut_m_expected = cut_length_mm_expected / 1000.0
    area_m2_expected = area_mm2_expected / 1_000_000.0
    pierces_expected = 3
    # Batch total cut length (~0.16 m) lands in the smallest-volume tier (<10 м/п).
    price_meter = 24.0  # Ст3 1.0 tier[3]
    pierce_equiv_m = 1.0 * 30000 / 60 / 1000  # pierce_time_s * feed_mm_min -> 0.5 m
    price_pierce = pierce_equiv_m * price_meter  # 12.0 грн per pierce
    material_m2 = 0.0  # no metal cost in source sheet
    per_part_expected = (
        cut_m_expected * price_meter
        + pierces_expected * price_pierce
        + area_m2_expected * material_m2
    )
    batch_expected = per_part_expected * 2

    metrics = body["metrics"]
    assert metrics["cut_length_mm"] == pytest.approx(cut_length_mm_expected, rel=1e-6)
    assert metrics["area_mm2"] == pytest.approx(area_mm2_expected, rel=1e-6)
    assert metrics["pierces"] == pierces_expected
    assert metrics["price_total_per_part"] == pytest.approx(per_part_expected, rel=1e-6)
    assert metrics["price_total_batch"] == pytest.approx(batch_expected, rel=1e-6)

    tariff = body["material"]["tariff"]
    assert tariff["volume_tier"] == 3
    assert tariff["price_meter"] == pytest.approx(price_meter)
    assert tariff["price_pierce"] == pytest.approx(price_pierce)

    assert body["batch_metrics"]["pierces"] == pierces_expected * 2
    assert body["preview"]["shape_count"] >= 2

    orders_path: Path = app.config["_ORDERS_PATH"]
    assert orders_path.exists()
    with orders_path.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1
    row = rows[0]
    assert row["metal_grade"] == "Ст3"
    assert row["metal_thickness_mm"] == "1.0"
    assert row["quantity"] == "2"
    assert row["status"] == "new"
    assert row["pierces_per_part"] == str(pierces_expected)
    assert float(row["price_total_batch"]) == pytest.approx(round(batch_expected, 2), abs=0.01)


@pytest.fixture
def empty_dxf(tmp_path: Path) -> Path:
    """Valid DXF with no cuttable entities (empty modelspace)."""
    import ezdxf

    doc = ezdxf.new(setup=True)
    doc.units = 4  # MM
    doc.modelspace()  # no entities added
    path = tmp_path / "empty.dxf"
    doc.saveas(path)
    return path


def test_calculate_rejects_geometryless_dxf(client, app, empty_dxf: Path) -> None:
    payload = _multipart(empty_dxf)
    resp = client.post("/api/calculate", data=payload, content_type="multipart/form-data")
    assert resp.status_code == 400
    assert "geometry" in resp.get_json()["error"].lower()
    # No phantom 0-грн order should be persisted (nor pushed to Telegram).
    orders_path: Path = app.config["_ORDERS_PATH"]
    assert not orders_path.exists()


def test_calculate_applies_volume_tier_discount(client, app, sample_dxf: Path) -> None:
    # cut length per part ~0.0814 m; quantity 200 -> ~16.3 m total -> tier index 2 (10-50 м/п).
    payload = _multipart(sample_dxf, metal_grade="Ст3", metal_thickness="1.0", quantity="200")
    resp = client.post("/api/calculate", data=payload, content_type="multipart/form-data")

    assert resp.status_code == 200
    tariff = resp.get_json()["material"]["tariff"]
    assert tariff["volume_tier"] == 2
    assert tariff["price_meter"] == pytest.approx(
        16.0
    )  # Ст3 1.0 tier[2], cheaper than the <10 rate


def test_calculate_appends_subsequent_orders(client, app, sample_dxf: Path) -> None:
    payload = _multipart(sample_dxf)
    client.post("/api/calculate", data=payload, content_type="multipart/form-data")
    payload2 = _multipart(sample_dxf, client_name="Другой")
    client.post("/api/calculate", data=payload2, content_type="multipart/form-data")

    orders_path: Path = app.config["_ORDERS_PATH"]
    with orders_path.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 2
    assert rows[0]["client_name"] == "Тест Клиент"
    assert rows[1]["client_name"] == "Другой"


def test_index_route_serves_html(client) -> None:
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"<html>" in resp.data


def test_cors_allows_whitelisted_origin(tmp_path: Path) -> None:
    from laser_calc_api.config import Settings

    web_dir = tmp_path / "web"
    web_dir.mkdir()
    (web_dir / "index.html").write_text("<html></html>", encoding="utf-8")
    settings = Settings(
        allowed_origins=["https://example.com"],
        prices_path=tmp_path / "missing.json",
        orders_path=tmp_path / "orders.csv",
        web_dir=web_dir,
    )
    flask_app = create_app(settings=settings, configure_logging_on_init=False)
    test_client = flask_app.test_client()

    resp = test_client.get("/api/health", headers={"Origin": "https://example.com"})
    assert resp.status_code == 200
    assert resp.headers.get("Access-Control-Allow-Origin") == "https://example.com"


def test_cors_blocks_foreign_origin(tmp_path: Path) -> None:
    from laser_calc_api.config import Settings

    web_dir = tmp_path / "web"
    web_dir.mkdir()
    (web_dir / "index.html").write_text("<html></html>", encoding="utf-8")
    settings = Settings(
        allowed_origins=["https://example.com"],
        prices_path=tmp_path / "missing.json",
        orders_path=tmp_path / "orders.csv",
        web_dir=web_dir,
    )
    flask_app = create_app(settings=settings, configure_logging_on_init=False)
    test_client = flask_app.test_client()

    resp = test_client.get("/api/health", headers={"Origin": "https://evil.com"})
    assert resp.status_code == 200
    assert resp.headers.get("Access-Control-Allow-Origin") != "https://evil.com"


def test_cors_preflight_allows_post(tmp_path: Path) -> None:
    from laser_calc_api.config import Settings

    web_dir = tmp_path / "web"
    web_dir.mkdir()
    (web_dir / "index.html").write_text("<html></html>", encoding="utf-8")
    settings = Settings(
        allowed_origins=["https://example.com"],
        prices_path=tmp_path / "missing.json",
        orders_path=tmp_path / "orders.csv",
        web_dir=web_dir,
    )
    flask_app = create_app(settings=settings, configure_logging_on_init=False)
    test_client = flask_app.test_client()

    resp = test_client.options(
        "/api/calculate",
        headers={
            "Origin": "https://example.com",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Content-Type",
        },
    )
    assert resp.status_code in (200, 204)
    assert resp.headers.get("Access-Control-Allow-Origin") == "https://example.com"
    assert "POST" in resp.headers.get("Access-Control-Allow-Methods", "")
