"""docs/MACD2_LOGIC.md <-> app/trading/macd2/config.py consistency check.

If the strategy-fixed values in config.py change (SIGNAL_RULE bumped,
STRATEGY_VERSION bumped, EMA/risk parameters changed, ...) without updating
the doc to match, this test fails — the doc is required to literally
mention the current values so it can never silently drift from the code
(docs 2026-07-27 §7 정합화 요건).
"""
from __future__ import annotations

from pathlib import Path

from app.trading.macd2 import config

_DOC_PATH = Path(__file__).resolve().parents[2] / "docs" / "MACD2_LOGIC.md"


def _doc_text() -> str:
    assert _DOC_PATH.exists(), f"docs/MACD2_LOGIC.md missing at {_DOC_PATH}"
    return _DOC_PATH.read_text(encoding="utf-8")


def test_doc_mentions_current_signal_rule_and_strategy_version():
    text = _doc_text()
    assert config.SIGNAL_RULE in text, (
        f"config.SIGNAL_RULE={config.SIGNAL_RULE!r} not found in docs/MACD2_LOGIC.md — "
        "update the doc whenever this strategy-fixed value changes"
    )
    assert config.STRATEGY_VERSION in text, (
        f"config.STRATEGY_VERSION={config.STRATEGY_VERSION!r} not found in docs/MACD2_LOGIC.md"
    )
    # CONFIRMED_SIGNAL_RULE is now an alias of SIGNAL_RULE (2026-07-27
    # KIS-parity fix) — this test would also catch it being re-split apart
    # again without a doc update.
    assert config.CONFIRMED_SIGNAL_RULE == config.SIGNAL_RULE


def test_doc_mentions_ema_parameters():
    text = _doc_text()
    assert f"EMA {config.EMA_FAST}" in text or str(config.EMA_FAST) in text
    assert str(config.EMA_SLOW) in text
    assert str(config.EMA_SIGNAL) in text


def test_doc_mentions_risk_exit_parameters():
    text = _doc_text()
    assert str(config.STOP_LOSS_NET_PCT) in text
    assert str(config.PROFIT_LOCK_ACTIVATE_NET_PCT) in text
    assert str(config.PROFIT_LOCK_GIVEBACK_PP) in text


def test_doc_mentions_order_fill_and_history_freshness_constants():
    text = _doc_text()
    assert str(int(config.ORDER_FILL_POLL_MAX_SEC)) in text
    assert "당일 1분봉" in text
    assert "MALFORMED_SCHEMA" in text


def test_doc_no_longer_claims_forming_bar_has_order_authority():
    """The pre-2026-07-27 doc said the forming/provisional bar's onset
    dispatches orders immediately — that claim must be gone now that order
    authority moved to the confirmed completed-bar crossover."""
    text = _doc_text()
    assert "진행봉 Primary" not in text
    assert "_PROVISIONAL" not in text or "원장에 기록되지 않는다" in text
