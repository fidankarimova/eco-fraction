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
    assert len(reading["signature"]) == 128
    assert reading["is_trusted"] is True


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


# --- Stage 2: verification, tokenisation and impact over HTTP ---------------


def test_trust_report(client):
    body = client.get(f"/api/v1/assets/{ASSET_ID}/trust?window_hours=24").json()
    assert body["sample_count"] > 0
    assert body["trust_rate_percent"] == 100.0, "honest backfill must not be flagged"
    assert body["anchored_batch_count"] >= 1
    assert len(body["latest_batch"]["merkle_root"]) == 64


def test_backfill_is_anchored_at_startup(client):
    body = client.get(f"/api/v1/assets/{ASSET_ID}/batches").json()
    assert body["count"] >= 1
    assert body["batches"][0]["anchor_target"] == "local-anchor"


def test_verify_reading_returns_a_working_proof(client):
    reading = client.get(f"/api/v1/assets/{ASSET_ID}/readings?limit=1&order=asc").json()
    reading_id = reading["readings"][0]["id"]
    body = client.get(f"/api/v1/verify/reading/{reading_id}").json()
    assert body["proof_verified"] is True
    assert body["proof"]["proof_length"] > 0
    assert len(body["merkle_root"]) == 64


def test_verify_batch_recomputes_the_root(client):
    batches = client.get(f"/api/v1/assets/{ASSET_ID}/batches").json()["batches"]
    root = batches[0]["merkle_root"]
    body = client.get(f"/api/v1/verify/batch/{root}").json()
    assert body["root_matches"] is True
    assert body["revenue"] is not None
    assert body["impact"] is not None


def test_verify_unknown_root_is_404(client):
    assert client.get("/api/v1/verify/batch/" + "0" * 64).status_code == 404


def test_attack_catalogue(client):
    attacks = client.get("/api/v1/attacks").json()
    assert len(attacks) == 6
    assert {a["key"] for a in attacks} >= {"replay", "inflate_output", "phantom_night"}


def test_every_attack_is_caught_over_http(client):
    for attack in client.get("/api/v1/attacks").json():
        result = client.post(
            f"/api/v1/assets/{ASSET_ID}/attacks/{attack['key']}"
        ).json()
        assert result["detected"] is True, f"{attack['key']} was not detected"
        assert result["failed_checks"]


def test_unknown_attack_is_404(client):
    assert client.post(f"/api/v1/assets/{ASSET_ID}/attacks/nope").status_code == 404


def test_token_lifecycle_over_http(client):
    address = "0xTESTHOLDER0000000000000000000000000000001"
    ledger = client.get(f"/api/v1/assets/{ASSET_ID}/token").json()
    assert ledger["total_supply"] == 2500
    assert ledger["minimum_investment_usdc"] == 50.0

    holder = client.post(
        f"/api/v1/assets/{ASSET_ID}/token/purchase",
        json={"address": address, "token_count": 25},
    ).json()
    assert holder["token_balance"] == 25
    assert holder["investment_usdc"] == 1250.0

    client.post(f"/api/v1/assets/{ASSET_ID}/anchor")
    after = client.get(
        f"/api/v1/assets/{ASSET_ID}/token/holders/{address}"
    ).json()
    assert after["claimable_usdc"] >= 0


def test_purchase_beyond_supply_is_rejected(client):
    response = client.post(
        f"/api/v1/assets/{ASSET_ID}/token/purchase",
        json={"address": "0xWHALE", "token_count": 2500},
    )
    assert response.status_code in (409, 422)


def test_impact_reports_method_and_double_counting(client):
    body = client.get(f"/api/v1/assets/{ASSET_ID}/impact").json()
    assert "double_counting_note" in body
    assert body["total_verified_kwh"] > 0
    assert body["certificate_status"] == "unknown"
    assert all(r["is_claimable"] is False for r in body["records"])


def test_summary_exposes_trust_and_claimability(client):
    body = client.get(f"/api/v1/assets/{ASSET_ID}/summary").json()
    assert "latest_trust_score" in body
    assert body["co2_is_claimable"] is False
    assert "PLACEHOLDER" in body["emission_factor_source"]
