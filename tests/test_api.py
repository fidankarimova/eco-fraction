"""API contract tests."""

from __future__ import annotations

ASSET_ID = "solar-baku-01"


def test_health(client):
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["simulator_enabled"] is False
    assert body["reading_count"] > 0


def test_list_assets(client):
    response = client.get("/api/v1/assets")
    assert response.status_code == 200
    assets = response.json()
    assert len(assets) == 1
    assert assets[0]["id"] == ASSET_ID
    assert assets[0]["device_count"] == 1
    assert assets[0]["dc_capacity_kw"] == 12.5


def test_get_unknown_asset_returns_404(client):
    response = client.get("/api/v1/assets/does-not-exist")
    assert response.status_code == 404
    assert "does-not-exist" in response.json()["detail"]


def test_latest_reading_shape(client):
    response = client.get(f"/api/v1/assets/{ASSET_ID}/readings/latest")
    assert response.status_code == 200
    reading = response.json()
    for field in (
        "recorded_at",
        "ac_power_w",
        "poa_irradiance_w_m2",
        "module_temp_c",
        "energy_wh",
        "cumulative_energy_wh",
        "payload_hash",
    ):
        assert field in reading
    assert reading["ac_power_w"] >= 0
    assert len(reading["payload_hash"]) == 64


def test_readings_pagination_and_order(client):
    response = client.get(f"/api/v1/assets/{ASSET_ID}/readings?limit=5&order=desc")
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 5
    timestamps = [r["recorded_at"] for r in body["readings"]]
    assert timestamps == sorted(timestamps, reverse=True)

    ascending = client.get(
        f"/api/v1/assets/{ASSET_ID}/readings?limit=5&order=asc"
    ).json()["readings"]
    ascending_times = [r["recorded_at"] for r in ascending]
    assert ascending_times == sorted(ascending_times)


def test_readings_rejects_inverted_range(client):
    response = client.get(
        f"/api/v1/assets/{ASSET_ID}/readings"
        "?start=2026-08-10T10:00:00Z&end=2026-08-09T10:00:00Z"
    )
    assert response.status_code == 422


def test_cumulative_energy_is_non_decreasing(client):
    body = client.get(
        f"/api/v1/assets/{ASSET_ID}/readings?limit=200&order=asc"
    ).json()
    values = [r["cumulative_energy_wh"] for r in body["readings"]]
    assert values == sorted(values)


def test_series_buckets(client):
    response = client.get(
        f"/api/v1/assets/{ASSET_ID}/series?window_hours=24&bucket_minutes=60"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["bucket_minutes"] == 60
    assert body["point_count"] > 0
    assert body["total_energy_kwh"] >= 0
    point = body["points"][0]
    assert point["peak_power_w"] >= point["average_power_w"]
    assert point["sample_count"] >= 1


def test_series_respects_max_points(client):
    """A silly-fine bucket must be widened rather than returning 100k points."""
    body = client.get(
        f"/api/v1/assets/{ASSET_ID}/series?window_hours=24&bucket_minutes=0.05"
    ).json()
    assert body["point_count"] <= 1000
    assert body["bucket_minutes"] > 0.05


def test_summary(client):
    response = client.get(f"/api/v1/assets/{ASSET_ID}/summary")
    assert response.status_code == 200
    body = response.json()
    assert body["asset_id"] == ASSET_ID
    assert body["status"] in {"generating", "standby", "night", "offline", "no_data"}
    assert body["reading_count"] > 0
    assert body["energy_total_kwh"] > 0
    assert body["current_power_w"] >= 0
    assert body["co2_avoided_total_kg"] >= 0


def test_openapi_document_available(client):
    response = client.get("/api/openapi.json")
    assert response.status_code == 200
    assert "/api/v1/assets/{asset_id}/summary" in response.json()["paths"]


def test_timestamps_are_timezone_aware(client):
    """Naive timestamps would be parsed as browser-local time by the dashboard."""
    reading = client.get(f"/api/v1/assets/{ASSET_ID}/readings/latest").json()
    assert reading["recorded_at"].endswith("Z") or "+" in reading["recorded_at"]

    summary = client.get(f"/api/v1/assets/{ASSET_ID}/summary").json()
    assert summary["last_reading_at"].endswith("Z") or "+" in summary["last_reading_at"]

    series = client.get(f"/api/v1/assets/{ASSET_ID}/series?window_hours=6").json()
    first = series["points"][0]["bucket_start"]
    assert first.endswith("Z") or "+" in first
