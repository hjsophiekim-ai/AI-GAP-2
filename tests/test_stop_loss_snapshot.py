"""
test_stop_loss_snapshot.py — 손절 계산의 단일 입력(position_snapshot) 검증.

entry_price는 KIS 평단을 최우선으로 쓰고, effective_sl_pct는 confirmed adaptive
regime 프로필 하나에서만 도출되며(과거 regime/이전 effective_sl_pct/UI 캐시값을
쓰지 않음), 실제 순손익률이 그 값 이하면 하드손절이 켜져야 한다.
"""

from __future__ import annotations

from datetime import datetime

from app.data_sources.hynix_inverse_collector import INVERSE_SYMBOL
from app.trading.adaptive_market_regime import (
    RANGE, VOLATILE_RANGE, STRONG_UP, STRONG_DOWN, DATA_INSUFFICIENT,
    effective_sl_pct_for_position,
)
from app.trading.hynix_symbols import LONG_SYMBOL
from app.trading.stop_loss_snapshot import build_stop_loss_snapshot

NOW = datetime(2026, 7, 20, 10, 30)


def test_entry_price_prefers_kis_avg_price_over_state_fallback():
    """구값 참조 문제 방지 — 브로커가 방금 확인해 준 실제 평단이 있으면, state에
    남아있던(추가매수 전 최초매수가일 수 있는) 캐시값보다 항상 우선한다."""
    snapshot = build_stop_loss_snapshot(
        symbol=LONG_SYMBOL, quantity=50, kis_entry_price=108_000.0,
        fallback_entry_price=100_000.0, current_price=104_000.0,
        confirmed_regime=RANGE, now=NOW,
    )
    assert snapshot["entry_price"] == 108_000.0
    assert snapshot["entry_price_source"] == "KIS_AVG_PRICE"


def test_entry_price_falls_back_to_state_when_kis_avg_price_missing():
    snapshot = build_stop_loss_snapshot(
        symbol=LONG_SYMBOL, quantity=50, kis_entry_price=None,
        fallback_entry_price=100_000.0, current_price=99_000.0,
        confirmed_regime=RANGE, now=NOW,
    )
    assert snapshot["entry_price"] == 100_000.0
    assert snapshot["entry_price_source"] == "STATE_FALLBACK"


def test_no_position_returns_none():
    assert build_stop_loss_snapshot(
        symbol=None, quantity=0, kis_entry_price=None, fallback_entry_price=None,
        current_price=100.0, confirmed_regime=RANGE, now=NOW,
    ) is None
    assert build_stop_loss_snapshot(
        symbol=LONG_SYMBOL, quantity=0, kis_entry_price=100_000.0, fallback_entry_price=None,
        current_price=100.0, confirmed_regime=RANGE, now=NOW,
    ) is None


def test_effective_sl_pct_matches_confirmed_regime_profile():
    assert effective_sl_pct_for_position(RANGE, LONG_SYMBOL) == -0.8
    assert effective_sl_pct_for_position(VOLATILE_RANGE, LONG_SYMBOL) == -0.6
    assert effective_sl_pct_for_position(STRONG_UP, LONG_SYMBOL) == -1.5
    assert effective_sl_pct_for_position(STRONG_DOWN, LONG_SYMBOL) == -1.5


def test_effective_sl_pct_flips_for_inverse_position():
    # 인버스 보유 중엔 하이닉스 STRONG_DOWN(=인버스에 유리)이 STRONG_UP 프로필로 뒤집힌다.
    assert effective_sl_pct_for_position(STRONG_DOWN, INVERSE_SYMBOL) == -1.5
    assert effective_sl_pct_for_position(STRONG_UP, INVERSE_SYMBOL) == -1.5


def test_unknown_regime_falls_back_to_data_insufficient_conservative_profile():
    assert effective_sl_pct_for_position(None, LONG_SYMBOL) == effective_sl_pct_for_position(DATA_INSUFFICIENT, LONG_SYMBOL)


def test_hard_stop_triggered_at_range_threshold():
    snapshot = build_stop_loss_snapshot(
        symbol=LONG_SYMBOL, quantity=10, kis_entry_price=100_000.0, fallback_entry_price=None,
        current_price=99_150.0,  # -0.85% > RANGE -0.8% 임계
        confirmed_regime=RANGE, now=NOW,
    )
    assert snapshot["hard_stop_triggered"] is True
    assert snapshot["effective_sl_pct"] == -0.8


def test_hard_stop_not_triggered_when_within_strong_trend_threshold():
    snapshot = build_stop_loss_snapshot(
        symbol=LONG_SYMBOL, quantity=10, kis_entry_price=100_000.0, fallback_entry_price=None,
        current_price=99_150.0,  # -0.85%, STRONG_UP 임계(-1.5%)보다는 안전권
        confirmed_regime=STRONG_UP, now=NOW,
    )
    assert snapshot["hard_stop_triggered"] is False


def test_three_percent_loss_triggers_hard_stop_in_every_regime():
    """요구사항 — 실제 손실 -3%이면 어떤 confirmed regime이든 반드시 하드손절."""
    entry = 100_000.0
    current = entry * 0.97  # -3.0%
    for regime in (RANGE, VOLATILE_RANGE, STRONG_UP, STRONG_DOWN, DATA_INSUFFICIENT):
        snapshot = build_stop_loss_snapshot(
            symbol=LONG_SYMBOL, quantity=10, kis_entry_price=entry, fallback_entry_price=None,
            current_price=current, confirmed_regime=regime, now=NOW,
        )
        assert snapshot["hard_stop_triggered"] is True, f"regime={regime} failed to hard-stop at -3%"


def test_snapshot_id_changes_with_time_and_is_deterministic_for_same_inputs():
    a = build_stop_loss_snapshot(
        symbol=LONG_SYMBOL, quantity=10, kis_entry_price=100_000.0, fallback_entry_price=None,
        current_price=99_000.0, confirmed_regime=RANGE, now=NOW,
    )
    b = build_stop_loss_snapshot(
        symbol=LONG_SYMBOL, quantity=10, kis_entry_price=100_000.0, fallback_entry_price=None,
        current_price=99_000.0, confirmed_regime=RANGE, now=NOW,
    )
    c = build_stop_loss_snapshot(
        symbol=LONG_SYMBOL, quantity=10, kis_entry_price=100_000.0, fallback_entry_price=None,
        current_price=99_000.0, confirmed_regime=RANGE, now=datetime(2026, 7, 20, 10, 31),
    )
    assert a["position_snapshot_id"] == b["position_snapshot_id"]
    assert a["position_snapshot_id"] != c["position_snapshot_id"]
