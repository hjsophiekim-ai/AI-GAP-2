"""SMART 사이징 — toxic 판정 + override 배수 (2026-09-22).

SMART = P2 슬롯 배분 + toxic confirmation 감액을 **하나의 정책**으로 합친 것.
toxic 이면 슬롯 배수를 **곱하지 않고 덮어쓴다**(0.0625 같은 이중감액 금지).
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from app.trading.macd2 import (
    config,
    position_sizing as PS,
    smart_sizing as SS,
    state_store,
    time_window_3slot as tw3,
)
from app.trading.macd2.models import Direction, RuntimeState

KST = tw3.KST if hasattr(tw3, "KST") else None
MORNING, AFTERNOON = tw3.SESSION_MORNING, tw3.SESSION_AFTERNOON
T0 = datetime.fromisoformat("2026-09-22T09:51:00+09:00")
ENTRY = datetime.fromisoformat("2026-09-22T09:57:00+09:00")


class _Knobs:
    """monkeypatch.undo() 를 쓰지 않기 위한 holder (tests/macd2 규약)."""

    def __init__(self, monkeypatch):
        self.mp = monkeypatch

    def env(self, value: str):
        self.mp.setattr(config, "MACD2_SIZING_MODE", value, raising=False)


def _state(*, n1=True, c1=True, smart=True) -> RuntimeState:
    s = state_store.default_state()
    s.auto_trade_on = True
    s.budget = 10_000_000.0
    for f in ("time_window_2_filter_enabled", "time_window_teg_filter_enabled",
              "time_window_3slot_filter_enabled", "time_window_twf_filter_enabled",
              "time_window_x2lite_filter_enabled", "time_window_h50_filter_enabled"):
        setattr(s, f, False)
    s.time_window_n1_filter_enabled = bool(n1)
    s.c1_peak_protection_enabled = bool(c1)
    s.smart_sizing_enabled = bool(smart)
    return s


def _bars(closes) -> pd.DataFrame:
    n = len(closes)
    start = datetime.fromisoformat("2026-09-22T09:00:00+09:00")
    return pd.DataFrame({
        "datetime": [start + timedelta(minutes=3 * i) for i in range(n)],
        "open": closes, "high": closes, "low": closes, "close": closes,
        "volume": [1000] * n,
    })


def _samples(prices, *, start=T0, step_sec=30):
    return [((start + timedelta(seconds=step_sec * i)).isoformat(), p)
            for i, p in enumerate(prices)]


# ════════════════════════════════════════════════════════════════════════
# 1. confirmation 구간 ETF 수익률
# ════════════════════════════════════════════════════════════════════════
def test_confirmation_return_uses_only_samples_before_entry():
    """진입 시각 **이후** 샘플은 절대 쓰지 않는다 (미래정보 차단)."""
    good = _samples([100.0, 101.0, 102.0])                       # 09:51:00~09:52:00
    future = [(ENTRY.isoformat(), 999.0),                        # 진입 시각 = 제외
              ((ENTRY + timedelta(minutes=1)).isoformat(), 9999.0)]
    ret, used = SS.confirmation_etf_return_pct(
        good + future, start_at=T0, end_before=ENTRY)
    assert used == 3
    assert ret == pytest.approx(2.0)                             # 100 -> 102


def test_confirmation_return_ignores_samples_before_flag_bar():
    early = [((T0 - timedelta(minutes=5)).isoformat(), 50.0)]
    ret, used = SS.confirmation_etf_return_pct(
        early + _samples([100.0, 99.0, 98.0]), start_at=T0, end_before=ENTRY)
    assert used == 3
    assert ret == pytest.approx(-2.0)


def test_confirmation_return_is_none_when_too_few_samples():
    ret, used = SS.confirmation_etf_return_pct(
        _samples([100.0, 101.0]), start_at=T0, end_before=ENTRY)
    assert ret is None and used == 2


# ════════════════════════════════════════════════════════════════════════
# 2. EMA20-50 방향정규화
# ════════════════════════════════════════════════════════════════════════
def test_ema_directional_sign_flips_with_direction():
    up_trend = _bars([100.0 + i for i in range(60)])              # EMA20 > EMA50
    red = SS.ema20_50_directional_pct(up_trend, Direction.UP_RED)
    blue = SS.ema20_50_directional_pct(up_trend, Direction.DOWN_BLUE)
    assert red is not None and red > 0
    assert blue == pytest.approx(-red)


def test_ema_needs_enough_bars():
    assert SS.ema20_50_directional_pct(_bars([100.0] * 10), Direction.UP_RED) is None
    assert SS.ema20_50_directional_pct(None, Direction.UP_RED) is None


def test_ema_uses_the_frame_as_given_without_trimming():
    """호출부가 넘긴 완성봉 프레임을 그대로 쓴다 — 마지막 봉을 잘라내지 않는다."""
    bars = _bars([100.0 + i for i in range(60)])
    full = SS.ema20_50_directional_pct(bars, Direction.UP_RED)
    trimmed = SS.ema20_50_directional_pct(bars.iloc[:-1], Direction.UP_RED)
    assert full != trimmed        # 마지막 봉이 실제로 반영된다


# ════════════════════════════════════════════════════════════════════════
# 3. toxic 판정
# ════════════════════════════════════════════════════════════════════════
def _down_trend_bars():
    """UP_RED 보유 기준 ema20_50 이 -0.20% 보다 낮아지는 하락 프레임."""
    return _bars([200.0 - i * 0.7 for i in range(80)])


def test_toxic_requires_both_conditions():
    bars = _down_trend_bars()
    ema = SS.ema20_50_directional_pct(bars, Direction.UP_RED)
    assert ema is not None and ema < config.TOXIC_EMA20_50_MAX_PCT

    weak = SS.assess(bars_3m=bars, direction=Direction.UP_RED,
                     samples=_samples([100.0, 99.5, 99.0]),
                     confirm_start_at=T0, entry_at=ENTRY)
    assert weak.confirmation_weak is True and weak.toxic is True

    strong = SS.assess(bars_3m=bars, direction=Direction.UP_RED,
                       samples=_samples([100.0, 100.5, 101.0]),
                       confirm_start_at=T0, entry_at=ENTRY)
    assert strong.confirmation_weak is False and strong.toxic is False
    assert strong.reason == "NORMAL"


def test_weak_alone_is_not_toxic():
    """확인구간이 약해도 역추세가 아니면 toxic 이 아니다."""
    up = _bars([100.0 + i for i in range(80)])
    a = SS.assess(bars_3m=up, direction=Direction.UP_RED,
                  samples=_samples([100.0, 99.0, 98.0]),
                  confirm_start_at=T0, entry_at=ENTRY)
    assert a.confirmation_weak is True and a.toxic is False
    assert a.reason == "WEAK_ONLY"


def test_zero_return_counts_as_weak():
    """임계는 <= 0% 다 — 정확히 0 도 weak."""
    bars = _down_trend_bars()
    a = SS.assess(bars_3m=bars, direction=Direction.UP_RED,
                  samples=_samples([100.0, 100.0, 100.0]),
                  confirm_start_at=T0, entry_at=ENTRY)
    assert a.confirmation_return_pct == pytest.approx(0.0)
    assert a.toxic is True


@pytest.mark.parametrize("samples,start,entry,reason", [
    ([], T0, ENTRY, "NO_CONFIRM_SAMPLES"),
    (_samples([100.0, 99.0, 98.0]), None, ENTRY, "NO_WINDOW"),
    (_samples([100.0, 99.0, 98.0]), T0, None, "NO_WINDOW"),
])
def test_missing_inputs_fail_open_to_not_toxic(samples, start, entry, reason):
    a = SS.assess(bars_3m=_down_trend_bars(), direction=Direction.UP_RED,
                  samples=samples, confirm_start_at=start, entry_at=entry)
    assert a.toxic is False and a.reason == reason


def test_no_ema_fails_open():
    a = SS.assess(bars_3m=_bars([100.0] * 5), direction=Direction.UP_RED,
                  samples=_samples([100.0, 99.0, 98.0]),
                  confirm_start_at=T0, entry_at=ENTRY)
    assert a.toxic is False and a.reason == "NO_EMA"


# ════════════════════════════════════════════════════════════════════════
# 4. SMART 배수 — override 이지 곱셈이 아니다
# ════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("slot,session,p2", [
    (1, MORNING, 1.05), (2, MORNING, 1.05),
    (3, MORNING, 0.25), (3, AFTERNOON, 1.00),
])
def test_toxic_overrides_every_slot_to_quarter(slot, session, p2):
    s = _state()
    assert PS.p2_multiplier(s, slot, session) == pytest.approx(p2)
    normal = PS.evaluate(s, entry_chop=False, slot_number=slot, session=session, toxic=False)
    toxic = PS.evaluate(s, entry_chop=False, slot_number=slot, session=session, toxic=True)
    assert normal.smart == pytest.approx(p2)
    assert toxic.smart == pytest.approx(config.SMART_TOXIC_MULT)
    assert toxic.toxic is True


def test_morning_slot3_toxic_is_not_double_reduced():
    """0.25 x 0.25 = 0.0625 가 절대 나오면 안 된다."""
    s = _state()
    d = PS.evaluate(s, entry_chop=False, slot_number=3, session=MORNING, toxic=True)
    assert d.smart == pytest.approx(0.25)
    assert d.smart != pytest.approx(0.0625)
    assert d.clipped == pytest.approx(0.25)          # clip 하한과 같아 유지


def test_smart_multiplier_helper_matches_policy():
    assert SS.smart_multiplier(1.05, toxic=False) == pytest.approx(1.05)
    assert SS.smart_multiplier(1.05, toxic=True) == pytest.approx(0.25)
    assert SS.smart_multiplier(0.25, toxic=True) == pytest.approx(0.25)


def test_toxic_is_ignored_in_base_mode():
    s = _state(smart=False)
    d = PS.evaluate(s, entry_chop=False, slot_number=1, session=MORNING, toxic=True)
    assert d.toxic is False
    assert d.smart == pytest.approx(1.0) and d.clipped == pytest.approx(1.0)


@pytest.mark.parametrize("n1,c1", [(True, False), (False, True), (False, False)])
def test_toxic_is_ignored_without_n1_and_c1(n1, c1):
    s = _state(n1=n1, c1=c1)
    d = PS.evaluate(s, entry_chop=False, slot_number=1, session=MORNING, toxic=True)
    assert d.toxic is False and d.smart == pytest.approx(1.0)


def test_chop_multiplier_still_applies_under_toxic():
    """toxic 은 **슬롯 배수**만 덮어쓴다 — W1a 규칙배수(CHOP)는 그대로다."""
    s = _state()
    d = PS.evaluate(s, entry_chop=True, slot_number=1, session=MORNING, toxic=True)
    # raw = CHOP(0.80) x toxic(0.25) = 0.20 -> MIN clip 0.25
    assert d.raw == pytest.approx(0.80 * 0.25)
    assert d.clipped == pytest.approx(config.X2LITE_SIZING_MIN_MULT)


# ════════════════════════════════════════════════════════════════════════
# 5. 호가 trail
# ════════════════════════════════════════════════════════════════════════
def test_trail_appends_and_caps_length():
    s = _state()
    limit = int(config.TOXIC_QUOTE_TRAIL_MAX)
    for i in range(limit + 25):
        SS.append_quote_sample(s, config.LONG_SYMBOL,
                               100.0 + i, T0 + timedelta(seconds=5 * i))
    rows = SS.trail_for(s, config.LONG_SYMBOL)
    assert len(rows) == limit
    assert rows[-1][1] == pytest.approx(100.0 + limit + 24)       # 최신이 살아남는다


def test_trail_rejects_bad_samples():
    s = _state()
    SS.append_quote_sample(s, config.LONG_SYMBOL, 0.0, T0)
    SS.append_quote_sample(s, config.LONG_SYMBOL, -5.0, T0)
    SS.append_quote_sample(s, config.LONG_SYMBOL, None, T0)
    SS.append_quote_sample(s, config.LONG_SYMBOL, 100.0, T0.replace(tzinfo=None))
    assert SS.trail_for(s, config.LONG_SYMBOL) == []


def test_trail_is_per_symbol():
    s = _state()
    SS.append_quote_sample(s, config.LONG_SYMBOL, 100.0, T0)
    SS.append_quote_sample(s, config.INVERSE_SYMBOL, 200.0, T0)
    assert SS.trail_for(s, config.LONG_SYMBOL)[0][1] == pytest.approx(100.0)
    assert SS.trail_for(s, config.INVERSE_SYMBOL)[0][1] == pytest.approx(200.0)


def test_trail_survives_state_roundtrip():
    s = _state()
    SS.append_quote_sample(s, config.LONG_SYMBOL, 100.0, T0)
    back = state_store.deserialize(state_store.serialize(s))
    assert SS.trail_for(back, config.LONG_SYMBOL)[0][1] == pytest.approx(100.0)


# ════════════════════════════════════════════════════════════════════════
# 6. 예산 계약 — 감액분 재배분 없음 / 일예산 준수
# ════════════════════════════════════════════════════════════════════════
def test_toxic_reduction_does_not_raise_later_exposure_room():
    """감액은 노출 누적만 낮춘다 — 뒤 거래의 **배수**를 키우지 않는다."""
    s = _state()
    d1 = PS.evaluate(s, entry_chop=False, slot_number=1, session=MORNING, toxic=True)
    PS.note_entry(s, d1)
    d2 = PS.evaluate(s, entry_chop=False, slot_number=2, session=MORNING, toxic=False)
    # 두 번째 거래 배수는 toxic 여부와 무관하게 P2 slot2 값 그대로다
    assert d2.smart == pytest.approx(config.P2_SIZING_SLOT12_MULT)
    assert d2.clipped == pytest.approx(config.P2_SIZING_SLOT12_MULT)


def test_daily_exposure_cap_still_binds_under_smart():
    s = _state()
    s.x2lite_exposure_used_today = float(config.X2LITE_SIZING_DAILY_EXPOSURE_CAP) - 0.10
    d = PS.evaluate(s, entry_chop=False, slot_number=1, session=MORNING, toxic=False)
    assert d.capped is True
    assert d.applied == pytest.approx(0.10)
    assert d.exposure_after <= float(config.X2LITE_SIZING_DAILY_EXPOSURE_CAP) + 1e-9


def test_daily_capital_is_unchanged_by_smart():
    s = _state()
    assert PS.daily_capital(s) == pytest.approx(
        10_000_000.0 * float(config.X2LITE_SIZING_DAILY_EXPOSURE_CAP))


# ════════════════════════════════════════════════════════════════════════
# 7. 모드 / legacy alias
# ════════════════════════════════════════════════════════════════════════
def test_default_state_is_base():
    assert RuntimeState().smart_sizing_enabled is False
    assert PS.sizing_mode(_state(smart=False)) == config.SIZING_MODE_BASE


def test_legacy_env_p2_maps_to_smart(monkeypatch):
    k = _Knobs(monkeypatch)
    k.env(config.SIZING_MODE_P2)
    assert PS.sizing_mode(_state(smart=False)) == config.SIZING_MODE_SMART
    assert PS.forced_by_env() is True


def test_env_smart_also_works(monkeypatch):
    k = _Knobs(monkeypatch)
    k.env(config.SIZING_MODE_SMART)
    assert PS.sizing_mode(_state(smart=False)) == config.SIZING_MODE_SMART


def test_legacy_state_key_is_inherited():
    """구버전 state 파일(smart_* 키 없음)은 p2_sizing_enabled 를 승계한다."""
    s = _state(smart=True)
    raw = state_store.serialize(s)
    for key in ("smart_sizing_enabled", "smart_sizing_enabled_at",
                "smart_sizing_enabled_by"):
        raw.pop(key, None)
    back = state_store.deserialize(raw)
    assert back.smart_sizing_enabled is True
