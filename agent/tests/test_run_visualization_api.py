from __future__ import annotations

import json

from fastapi.testclient import TestClient

import api_server


def _client() -> TestClient:
    return TestClient(api_server.app, client=("127.0.0.1", 50000))


def test_get_run_visualization_returns_sanitized_payload(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(api_server, "RUNS_DIR", tmp_path)
    output = tmp_path / "run_chart" / "artifacts" / "visualizations"
    output.mkdir(parents=True)
    (output / "kline_demo.json").write_text(json.dumps({
        "schema_version": 1,
        "visualization_id": "kline_demo",
        "type": "candlestick_volume",
        "symbol": "AAPL.US",
        "source": "yahoo",
        "timeframe": "5m",
        "timezone": "UTC",
        "effective_fetch_start": "2026-07-15",
        "effective_fetch_end": "2026-07-21",
        "retention_policy": "latest_contiguous_up_to_5000_bars",
        "truncated": True,
        "dropped_bar_count": 3,
        "secret": "must-not-leak",
        "bars": [
            {"time": "2026-07-21T09:35:00", "open": 10, "high": 12, "low": 9, "close": 11, "volume": 100},
        ],
    }), encoding="utf-8")

    response = _client().get("/runs/run_chart/visualizations/kline_demo")
    assert response.status_code == 200
    assert response.json()["bars"][0]["close"] == 11
    assert response.json()["bars"][0]["time"] == "2026-07-21T09:35:00"
    assert response.json()["truncated"] is True
    assert response.json()["dropped_bar_count"] == 3
    assert response.json()["timezone"] == "UTC"
    assert response.json()["effective_fetch_start"] == "2026-07-15"
    assert response.json()["effective_fetch_end"] == "2026-07-21"
    assert response.json()["retention_policy"] == "latest_contiguous_up_to_5000_bars"
    assert "secret" not in response.json()


def test_get_run_visualization_rejects_html_shaped_timestamp(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(api_server, "RUNS_DIR", tmp_path)
    output = tmp_path / "run_chart" / "artifacts" / "visualizations"
    output.mkdir(parents=True)
    (output / "kline_bad_time.json").write_text(json.dumps({
        "schema_version": 1,
        "visualization_id": "kline_bad_time",
        "type": "candlestick_volume",
        "bars": [
            {
                "time": '<img src=x onerror="alert(1)">',
                "open": 10,
                "high": 12,
                "low": 9,
                "close": 11,
                "volume": 100,
            },
        ],
    }), encoding="utf-8")

    response = _client().get("/runs/run_chart/visualizations/kline_bad_time")
    assert response.status_code == 422


def test_get_run_visualization_rejects_path_shaped_id(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(api_server, "RUNS_DIR", tmp_path)
    response = _client().get("/runs/run_chart/visualizations/not%20safe")
    assert response.status_code == 400
