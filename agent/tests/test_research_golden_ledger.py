"""QE1 tests for the hand-calculated, exact-fen golden account ledger."""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.research.golden_ledger import GoldenLedger, load_golden_ledger
from src.research.market_fixture import load_market_fixture

_ROOT = Path(__file__).parent / "fixtures" / "research"
_LEDGER = _ROOT / "qe1_golden_ledger_v1.json"
_MARKET = _ROOT / "qe1_market_v1.json"
_EXPECTED_LEDGER_SHA256 = "d9f2fd786aa138e2f7842eb99760c1eb2f2bf5524ccfdc8034e6267f2ee00d8d"


def test_qe1_golden_ledger_is_content_bound_to_the_market_snapshot() -> None:
    ledger = load_golden_ledger(_LEDGER)
    market = load_market_fixture(_MARKET)

    assert ledger.content_sha256 == _EXPECTED_LEDGER_SHA256
    assert ledger.market_snapshot_sha256 == market.snapshot_sha256
    assert ledger.model_validate_json(ledger.model_dump_json()) == ledger


def test_qe1_golden_ledger_freezes_t1_fees_actions_and_exact_ending_equity() -> None:
    ledger = load_golden_ledger(_LEDGER)
    steps = {step.event: step for step in ledger.steps}

    assert steps["rejected_t1_sell"].rejection_reason == "T1_LOCKED"
    assert steps["rejected_t1_sell"].cash_delta_fen == 0
    assert steps["share_split"].positions["000003.SZ"] == 2000
    assert steps["dividend_ex"].dividend_receivable_fen == 50000
    assert steps["dividend_pay"].dividend_receivable_fen == 0
    assert steps["sell"].fee_fen == 600 + 20 + 1000
    assert ledger.steps[-1].equity_fen == 10067251
    assert ledger.steps[-1].equity_fen - ledger.steps[0].equity_fen == 67251


def test_qe1_golden_ledger_tampering_fails_at_first_accounting_invariant() -> None:
    raw = json.loads(_LEDGER.read_text(encoding="utf-8"))
    raw["steps"][6]["cash_fen"] += 1

    with pytest.raises(ValidationError, match="cash invariant failed at sequence 6"):
        GoldenLedger.model_validate(raw)


def test_qe1_golden_ledger_reader_is_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    def deny_network(*_args, **_kwargs):
        raise AssertionError("golden ledger reader attempted network access")

    monkeypatch.setattr(socket, "socket", deny_network)
    assert load_golden_ledger(_LEDGER).ledger_id == "qe1-cn-account-v1"
