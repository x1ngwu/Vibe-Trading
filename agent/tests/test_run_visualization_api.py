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


def test_get_similarity_visualization_returns_sanitized_payload(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(api_server, "RUNS_DIR", tmp_path)
    output = tmp_path / "run_similarity" / "artifacts" / "visualizations"
    output.mkdir(parents=True)
    digest = "a" * 64
    (output / "similarity_demo.json").write_text(json.dumps({
        "schema_version": 1,
        "visualization_id": "similarity_demo",
        "type": "similarity_ranking",
        "similarity_run_id": f"similarity_run:{digest}",
        "similarity_sha256": digest,
        "research_spec_id": "research_spec:" + "b" * 64,
        "target_symbols": ["600519.SH"],
        "as_of": "2026-07-25",
        "candidate_universe": "csi300@2026-07-25",
        "weights": {"business": 0.3, "factor": 0.4, "price_volume": 0.3},
        "candidates": [{
            "rank": 1,
            "symbol": "000858.SZ",
            "combined_score": 0.82,
            "coverage": 1.0,
            "business_score": 0.91,
            "factor_score": 0.82,
            "price_volume_score": 0.73,
            "rank_stability": 0.875,
            "evidence": ["same_industry:白酒"],
            "counterevidence": ["market_cap_gap:0.31"],
        }],
        "excluded_symbol_count": 1,
    }), encoding="utf-8")

    response = _client().get("/runs/run_similarity/visualizations/similarity_demo")
    assert response.status_code == 200
    assert response.json()["type"] == "similarity_ranking"
    assert response.json()["candidates"][0]["symbol"] == "000858.SZ"
    assert response.json()["similarity_sha256"] == digest


def test_get_similarity_visualization_rejects_extra_or_mismatched_identity(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(api_server, "RUNS_DIR", tmp_path)
    output = tmp_path / "run_similarity" / "artifacts" / "visualizations"
    output.mkdir(parents=True)
    (output / "similarity_bad.json").write_text(json.dumps({
        "schema_version": 1,
        "visualization_id": "similarity_bad",
        "type": "similarity_ranking",
        "similarity_run_id": "similarity_run:" + "a" * 64,
        "similarity_sha256": "b" * 64,
        "research_spec_id": "research_spec:" + "c" * 64,
        "target_symbols": ["600519.SH"],
        "as_of": "2026-07-25",
        "candidate_universe": "csi300@2026-07-25",
        "weights": {"business": 0.3, "factor": 0.4, "price_volume": 0.3},
        "candidates": [{
            "rank": 1,
            "symbol": "000858.SZ",
            "combined_score": 0.82,
            "coverage": 1.0,
            "evidence": ["support"],
            "counterevidence": ["risk"],
        }],
        "excluded_symbol_count": 0,
        "secret": "must-not-be-ignored",
    }), encoding="utf-8")

    response = _client().get("/runs/run_similarity/visualizations/similarity_bad")
    assert response.status_code == 422
