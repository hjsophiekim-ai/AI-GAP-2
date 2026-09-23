"""X1 CONTEXT 전용 테스트.

원칙
----
* X1 OFF 일 때 기존 경로가 **전혀 호출되지 않는다**(OFF parity).
* **hard reject 는 어떤 경우에도 되살아나지 않는다.**
* 모든 판정 함수는 ``now`` 이후 봉을 섞어 넣어도 결과가 같아야 한다(future leak 0).
* 데이터 부족은 언제나 fail-open (기존 N1+C1 동작 유지).

주의: 이 디렉토리에서는 ``monkeypatch.undo()`` 를 쓰지 않는다(conftest 격리가
같이 풀린다). 값 교체는 ``monkeypatch.setattr`` 만 쓴다.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

import pandas as pd
import pytest

from app.trading.macd2 import config, x1_context as X, x1_shadow as S
from app.trading.macd2.models import Direction

KST = "Asia/Seoul"


# ── helpers ────────────────────────────────────────────────────────────────
def bars3m(start: str, closes, *, highs=None, lows=None, volume=1000):
    """3분봉 프레임. start 는 첫 봉의 **시작** 시각."""
    t0 = pd.Timestamp(start, tz=KST)
    idx = [t0 + pd.Timedelta(minutes=3 * i) for i in range(len(closes))]
    highs = highs if highs is not None else [c + 50 for c in closes]
    lows = lows if lows is not None else [c - 50 for c in closes]
    return pd.DataFrame({
        "datetime": idx,
        "open": list(closes),
        "high": list(highs),
        "low": list(lows),
        "close": list(closes),
        "volume": [volume] * len(closes),
    })


def bars1m(start: str, closes, volume=100):
    t0 = pd.Timestamp(start, tz=KST)
    idx = [t0 + pd.Timedelta(minutes=i) for i in range(len(closes))]
    return pd.DataFrame({
        "datetime": idx,
        "open": list(closes), "high": [c + 20 for c in closes],
        "low": [c - 20 for c in closes], "close": list(closes),
        "volume": [volume] * len(closes),
    })


def flags(*pairs, source=X.LIVE_CONFIRMED):
    """(("HH:MM", "R"|"B"), ...) -> LIVE 원장 이벤트 리스트 (2026-09-22 기준)."""
    out = []
    for hhmm, side in pairs:
        out.append({
            "at": pd.Timestamp("2026-09-22 " + hhmm, tz=KST),
            "direction": Direction.UP_RED.value if side == "R" else Direction.DOWN_BLUE.value,
            "source": source,
        })
    return out


def end_of(frame):
    """프레임 마지막 3분봉이 막 완성된 시각."""
    return (frame["datetime"].iloc[-1] + pd.Timedelta(minutes=3)).to_pydatetime()


@dataclass
class FakeTeg:
    approved: bool
    conditions: dict
    metrics: dict = None

    def __post_init__(self):
        if self.metrics is None:
            self.metrics = {}


def teg_conditions(**overrides):
    from app.trading.macd2 import teg_gate as T
    base = {c: True for c in T.ALL_CONDITIONS}
    base.update(overrides)
    return base


# ── 0. OFF parity / 활성 조건 ──────────────────────────────────────────────
def test_x1_defaults_off():
    assert config.X1_ENABLED is False
    assert config.X1_SHADOW_MODE is False
    assert X.x1_active(n1_enabled=True, c1_enabled=True) is False


@pytest.mark.parametrize("n1,c1,expected", [
    (True, True, True), (True, False, False), (False, True, False), (False, False, False),
])
def test_x1_requires_n1_and_c1(n1, c1, expected):
    assert X.x1_active(n1_enabled=n1, c1_enabled=c1, x1_enabled=True) is expected


def test_shadow_writes_nothing_when_off(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "SHADOW_LEDGER_PATH", tmp_path / "x1_shadow_ledger.csv")
    monkeypatch.setattr(config, "X1_SHADOW_MODE", False)
    ctx = X.build_context(now=pd.Timestamp("2026-09-22 12:15", tz=KST))
    assert S.record(ctx, now=pd.Timestamp("2026-09-22 12:15", tz=KST).to_pydatetime()) is False
    assert not (tmp_path / "x1_shadow_ledger.csv").exists()


def test_shadow_writes_row_when_on(tmp_path, monkeypatch):
    path = tmp_path / "x1_shadow_ledger.csv"
    monkeypatch.setattr(S, "SHADOW_LEDGER_PATH", path)
    monkeypatch.setattr(config, "X1_SHADOW_MODE", True)
    now = pd.Timestamp("2026-09-22 12:15", tz=KST).to_pydatetime()
    ctx = X.build_context(now=now)
    assert S.record(ctx, now=now, direction="DOWN_BLUE") is True
    rows = list(pd.read_csv(path).to_dict("records"))
    assert len(rows) == 1
    # 사후 채움 컬럼은 기록 시점에 반드시 비어 있다(미래정보 금지).
    assert pd.isna(rows[0]["future_mfe_pct"]) or rows[0]["future_mfe_pct"] == ""
    assert pd.isna(rows[0]["backfilled_at"]) or rows[0]["backfilled_at"] == ""


def test_shadow_record_never_raises(monkeypatch):
    monkeypatch.setattr(config, "X1_SHADOW_MODE", True)
    monkeypatch.setattr(S, "SHADOW_LEDGER_PATH", None)  # 고의로 깨뜨린다
    assert S.record(object(), now=pd.Timestamp("2026-09-22 12:15", tz=KST).to_pydatetime()) is False


# ── 1. hard reject 는 절대 되살리지 않는다 ─────────────────────────────────
@pytest.mark.parametrize("reason", sorted(X.HARD_REJECT_REASONS))
def test_hard_reject_never_resurrected(reason):
    assert X.is_soft_reject(reason) is False
    fr = bars3m("2026-09-22 11:00", [1910000 + 500 * i for i in range(30)])
    watch = X.X1FlipState(flip_count=5, flag_count=6, watching=True,
                          last_direction=Direction.UP_RED.value,
                          box_high=1915000, box_low=1905000,
                          last_flag_at=pd.Timestamp("2026-09-22 11:30", tz=KST))
    late = X.evaluate_late_entry(fr, watch, now=end_of(fr), base_reject_reason=reason)
    assert late.candidate is False
    assert late.reason == "X1_LATE_HARD_REJECT_NOT_RESURRECTED"
    ctx = X.build_context(now=end_of(fr), direction=Direction.UP_RED, bars_3m=fr,
                          base_reject_reason=reason)
    assert ctx.final_action == X.ACTION_BLOCK


def test_unknown_reject_reason_is_not_resurrected():
    assert X.is_soft_reject("SOME_BRAND_NEW_REASON") is False
    assert X.is_soft_reject(None) is False


def test_soft_reject_list_is_recognised():
    for reason in X.SOFT_REJECT_REASONS:
        assert X.is_soft_reject(reason) is True


# ── 2. LIVE_CONFIRMED 만 쓴다 ──────────────────────────────────────────────
def test_recomputed_only_flags_are_ignored():
    live = flags(("11:03", "R"), ("11:12", "B"), ("11:24", "R"), ("11:30", "B"))
    recomputed = flags(("11:03", "R"), ("11:12", "B"), ("11:24", "R"), ("11:30", "B"),
                       source=X.RECOMPUTED_ONLY)
    now = pd.Timestamp("2026-09-22 11:45", tz=KST)
    assert X.flip_state_from_live_flags(live, now=now).flip_count == 3
    assert X.flip_state_from_live_flags(recomputed, now=now).flip_count == 0


def test_flip_count_is_direction_changes_not_flag_count():
    same = flags(("11:03", "R"), ("11:12", "R"), ("11:24", "R"))
    st = X.flip_state_from_live_flags(same, now=pd.Timestamp("2026-09-22 11:45", tz=KST))
    assert st.flag_count == 3 and st.flip_count == 0


def test_future_flag_events_are_dropped():
    ev = flags(("11:03", "R"), ("11:12", "B"), ("13:00", "R"))
    st = X.flip_state_from_live_flags(ev, now=pd.Timestamp("2026-09-22 11:45", tz=KST))
    assert st.flag_count == 2 and st.last_flag_at.strftime("%H:%M") == "11:12"


# ── 3. future leak 없음 ────────────────────────────────────────────────────
def test_no_future_leak_extra_bars_do_not_change_result():
    closes = [1900000 - 1000 * i for i in range(40)]
    full = bars3m("2026-09-22 09:00", closes)
    now = end_of(full.iloc[:25])
    truncated = full.iloc[:25].reset_index(drop=True)
    a = X.evaluate_morning_context(truncated, None, Direction.DOWN_BLUE, now=now)
    b = X.evaluate_morning_context(full, None, Direction.DOWN_BLUE, now=now)
    assert a.score == b.score and a.components == b.components
    assert a.metrics["close"] == b.metrics["close"]


def test_incomplete_last_bar_is_excluded():
    fr = bars3m("2026-09-22 09:00", [1900000, 1901000, 1902000])
    # 마지막 봉(09:06)은 09:09 에 완성된다 — 09:08 시점에는 보이면 안 된다.
    now = pd.Timestamp("2026-09-22 09:08", tz=KST)
    got = X._complete_upto(fr, now, bar_minutes=3)
    assert len(got) == 2 and got["datetime"].iloc[-1].strftime("%H:%M") == "09:03"


# ── 4. fail-open ───────────────────────────────────────────────────────────
def test_no_bars_is_fail_open():
    m = X.evaluate_morning_context(None, None, Direction.UP_RED,
                                   now=pd.Timestamp("2026-09-22 10:00", tz=KST))
    assert m.insufficient_data is True and m.action == X.ACTION_PASS


def test_no_base_decision_is_pass_not_block():
    ctx = X.build_context(now=pd.Timestamp("2026-09-22 10:00", tz=KST))
    assert ctx.final_action == X.ACTION_PASS
    assert "X1_NO_BASE_DECISION" in ctx.reasons


# ── 5. X1-1 MORNING CONTEXT ────────────────────────────────────────────────
def test_counter_trend_morning_red_is_watch_or_weak():
    """프리마켓부터 계속 하락 중인데 나오는 약한 RED -> 가점 없음, WATCH 후보."""
    pre = bars1m("2026-09-22 08:00", [1950000 - 500 * i for i in range(60)])
    down = [1920000 - 800 * i for i in range(24)]
    up = [down[-1] + 300 * i for i in range(1, 5)]
    fr = bars3m("2026-09-22 09:00", down + up)
    now = end_of(fr)
    m = X.evaluate_morning_context(fr, pre, Direction.UP_RED, now=now)
    assert m.components.get("premarket_trend_against") is True
    assert m.score < config.X1_MORNING_PASS_SCORE
    assert m.action in (X.ACTION_WATCH, X.ACTION_PASS_WEAK)


def test_with_trend_morning_flag_passes():
    pre = bars1m("2026-09-22 08:00", [1900000 + 300 * i for i in range(60)])
    fr = bars3m("2026-09-22 09:00", [1930000 + 900 * i for i in range(28)])
    m = X.evaluate_morning_context(fr, pre, Direction.UP_RED, now=end_of(fr))
    assert m.components.get("premarket_trend_align") is True
    assert m.action == X.ACTION_PASS


def test_morning_watch_release_requires_breakout_within_window():
    fr = bars3m("2026-09-22 09:00", [1900000 + 200 * i for i in range(20)])
    # WATCH 유효창(X1_MORNING_WATCH_MAX_MIN=9분) 안쪽으로 잡는다 — 3분봉 3개.
    watch_at = fr["datetime"].iloc[-3].to_pydatetime()
    ok, reason, _ = X.evaluate_morning_watch_release(
        fr, Direction.UP_RED, now=end_of(fr), watch_started_at=watch_at)
    assert ok is True and reason == "X1_WATCH_RELEASED_BREAKOUT"
    stale = (fr["datetime"].iloc[-1] - pd.Timedelta(minutes=60)).to_pydatetime()
    ok2, reason2, _ = X.evaluate_morning_watch_release(
        fr, Direction.UP_RED, now=end_of(fr), watch_started_at=stale)
    assert ok2 is False and reason2 == "X1_WATCH_EXPIRED"


# ── 6. X1-2 FLIP EXIT ──────────────────────────────────────────────────────
def _flip_exit_frame():
    hold = [1900000 + 100 * i for i in range(20)]
    drop = [hold[-1] - 4000 * i for i in range(1, 8)]
    return bars3m("2026-09-22 09:00", hold + drop)


def test_flip_exit_not_armed_without_h50_hold():
    fr = _flip_exit_frame()
    st = X.flip_state_from_live_flags(
        flags(("09:30", "R"), ("09:45", "B"), ("10:00", "R"), ("10:15", "B")),
        now=end_of(fr), bars_3m=fr)
    d = X.evaluate_flip_exit(fr, Direction.UP_RED, st, now=end_of(fr), h50_hold_seen=False)
    assert d.armed is False and d.exit_now is False


def test_flip_exit_not_armed_below_three_flips():
    fr = _flip_exit_frame()
    st = X.flip_state_from_live_flags(flags(("09:30", "R"), ("09:45", "B")),
                                      now=end_of(fr), bars_3m=fr)
    assert st.flip_count == 1
    d = X.evaluate_flip_exit(fr, Direction.UP_RED, st, now=end_of(fr), h50_hold_seen=True)
    assert d.armed is False


def test_flip_exit_fires_on_three_flips_plus_breakout():
    fr = _flip_exit_frame()
    st = X.flip_state_from_live_flags(
        flags(("09:30", "R"), ("09:45", "B"), ("10:00", "R"), ("10:15", "B")),
        now=end_of(fr), bars_3m=fr)
    assert st.flip_count == 3
    d = X.evaluate_flip_exit(fr, Direction.UP_RED, st, now=end_of(fr),
                             h50_hold_seen=True, etf_move_pct=1.2)
    assert d.armed is True
    assert d.score >= config.X1_FLIP_EXIT_SCORE_MIN
    assert d.exit_now is True


def test_flip_exit_never_reverses():
    """청산만 답한다 — 신규진입/reverse 를 시사하는 필드가 없어야 한다."""
    d = X.X1FlipExitDecision()
    assert not any("reverse" in f or "entry" in f for f in d.__dataclass_fields__)


# ── 7. X1-3 AFTERNOON RE-ENTRY (AR1) ───────────────────────────────────────
SAME_DIR = config.TW2_3SLOT_REJECT_SAME_DIRECTION_AFTERNOON_2ND


def test_ar1_grants_stack_exemption_when_only_stack_fails():
    from app.trading.macd2 import teg_gate as T
    teg = FakeTeg(False, teg_conditions(**{T.COND_EMA_STACK: False}))
    d = X.evaluate_afternoon_reentry(teg, base_reject_reason=SAME_DIR)
    assert d.allowed is True and d.stack_exempt is True
    assert d.reason == "X1_AR1_STACK_EXEMPT"


def test_ar1_refuses_when_two_conditions_fail():
    from app.trading.macd2 import teg_gate as T
    teg = FakeTeg(False, teg_conditions(**{T.COND_EMA_STACK: False, T.COND_VWAP: False}))
    d = X.evaluate_afternoon_reentry(teg, base_reject_reason=SAME_DIR)
    assert d.allowed is False and d.reason == "X1_AR1_MULTI_CONDITION_FAIL"


def test_ar1_refuses_when_momentum_not_confirmed():
    from app.trading.macd2 import teg_gate as T
    conds = teg_conditions(**{T.COND_EMA_STACK: False})
    conds[T.COND_MACD_GAP_EXPANDING] = False
    teg = FakeTeg(False, conds)
    d = X.evaluate_afternoon_reentry(teg, base_reject_reason=SAME_DIR)
    # gap 이 꺼지면 탈락 조건이 2개가 되므로 먼저 MULTI 에서 걸린다 — 어느 쪽이든 거부다.
    assert d.allowed is False
    assert d.reason in ("X1_AR1_MULTI_CONDITION_FAIL", "X1_AR1_MOMENTUM_NOT_CONFIRMED")


def test_ar1_is_narrow_no_plb():
    """SAME_DIRECTION_AFTERNOON 이 아닌 거절에는 절대 개입하지 않는다(PLB 금지)."""
    from app.trading.macd2 import teg_gate as T
    teg = FakeTeg(False, teg_conditions(**{T.COND_EMA_STACK: False}))
    for other in ("TW2_3SLOT_REJECT_TEG", "REJECT_LOW_QUALITY_SCORE",
                  "TW2_3SLOT_REJECT_DAILY_SLOT_CAP", None):
        d = X.evaluate_afternoon_reentry(teg, base_reject_reason=other)
        assert d.allowed is False and d.reason == "X1_AR1_OUT_OF_SCOPE"


def test_ar1_disabled_flag():
    from app.trading.macd2 import teg_gate as T
    teg = FakeTeg(False, teg_conditions(**{T.COND_EMA_STACK: False}))
    d = X.evaluate_afternoon_reentry(teg, base_reject_reason=SAME_DIR, enabled=False)
    assert d.allowed is False and d.reason == "X1_AR1_DISABLED"


def test_anchor_20260922_1215_blue_is_ar1_reentry():
    """과거 앵커 고정 — 2026-09-22 12:15 DOWN_BLUE.

    실측(z4_teg): TEG 7조건 중 price_ema_stack_aligned **하나만** 실패
    (close 1,917,000 / ema10 1,917,384 / ema20 1,917,267 = 117원 차이),
    macd_gap 2봉 +179.32 / ema_spread +247.39 / vwap 우호는 전부 참.
    threshold 튜닝 금지 — 행동 확인용 fixture 다.
    """
    from app.trading.macd2 import teg_gate as T
    conds = teg_conditions(**{T.COND_EMA_STACK: False})
    metrics = {"close": 1917000.0, "ema10": 1917384.0097166637,
               "ema20": 1917267.1617275702, "vwap": 1922601.7482286782,
               "gap_flag": 66.6995748199916, "gap_now": 96.14664951972088}
    teg = FakeTeg(False, conds, metrics)
    d = X.evaluate_afternoon_reentry(teg, base_reject_reason=SAME_DIR)
    assert d.allowed is True and d.stack_exempt is True
    ctx = X.build_context(now=pd.Timestamp("2026-09-22 12:15", tz=KST),
                          direction=Direction.DOWN_BLUE,
                          base_reject_reason=SAME_DIR, teg_decision=teg)
    assert ctx.final_action == X.ACTION_REENTRY


# ── 8. X1-4 FLIP BREAKOUT WATCH / LATE ENTRY ───────────────────────────────
def test_flip_watch_catches_long_congestion_window_b():
    """9/22 오후형 — 66분 span RBRBRB. 18분짜리 짧은 창으로는 못 잡는 케이스."""
    ev = flags(("11:03", "R"), ("11:12", "B"), ("11:24", "R"),
               ("11:30", "B"), ("11:36", "R"), ("12:09", "B"))
    fr = bars3m("2026-09-22 11:00", [1917000] * 25)
    st = X.evaluate_flip_watch(ev, now=pd.Timestamp("2026-09-22 12:15", tz=KST), bars_3m=fr)
    assert st.watching is True
    assert st.flag_count >= config.X1_FLIPWATCH_MIN_FLAGS_B
    assert st.seq == "RBRBRB" and st.span_min == pytest.approx(66.0)


def test_flip_watch_not_triggered_on_quiet_tape():
    ev = flags(("10:00", "R"), ("11:00", "B"))
    st = X.evaluate_flip_watch(ev, now=pd.Timestamp("2026-09-22 11:30", tz=KST))
    assert st.watching is False


def test_late_entry_fires_on_soft_reject_plus_breakout():
    box = [1917000 + (200 if i % 2 else -200) for i in range(22)]
    brk = [1910000, 1904000]
    fr = bars3m("2026-09-22 11:00", box + brk)
    watch = X.X1FlipState(flip_count=5, flag_count=6, watching=True, seq="RBRBRB",
                          last_direction=Direction.DOWN_BLUE.value,
                          box_high=max(box) + 50, box_low=min(box) - 50,
                          last_flag_at=(fr["datetime"].iloc[-3]).to_pydatetime(),
                          cluster_start=fr["datetime"].iloc[0].to_pydatetime())
    d = X.evaluate_late_entry(fr, watch, now=end_of(fr),
                              base_reject_reason="REJECT_LOW_QUALITY_SCORE",
                              etf_move_pct=0.9)
    assert d.components["box_breakout"] is True
    assert d.score >= config.X1_LATE_ENTRY_SCORE_MIN
    assert d.candidate is True
    assert d.latency_bucket is not None


def test_late_entry_latency_guard_is_provisional_and_recorded():
    box = [1917000 + (200 if i % 2 else -200) for i in range(22)]
    brk = [1910000, 1904000]
    fr = bars3m("2026-09-22 11:00", box + brk)
    old_flag = (fr["datetime"].iloc[-1] - pd.Timedelta(minutes=45)).to_pydatetime()
    watch = X.X1FlipState(flip_count=5, flag_count=6, watching=True,
                          last_direction=Direction.DOWN_BLUE.value,
                          box_high=max(box) + 50, box_low=min(box) - 50,
                          last_flag_at=old_flag)
    d = X.evaluate_late_entry(fr, watch, now=end_of(fr),
                              base_reject_reason="REJECT_LOW_QUALITY_SCORE")
    assert d.within_guard is False              # production 행동은 막힌다
    assert d.latency_min is not None            # 그러나 trace 에는 남는다
    assert d.latency_bucket == ">30m"
    ctx = X.build_context(now=end_of(fr), direction=Direction.DOWN_BLUE, bars_3m=fr,
                          base_reject_reason="REJECT_LOW_QUALITY_SCORE",
                          flag_events=flags(("11:03", "R"), ("11:12", "B"), ("11:24", "R"),
                                            ("11:30", "B"), ("11:36", "R"), ("12:09", "B")))
    assert ctx.final_action != X.ACTION_LATE_ENTRY


def test_late_entry_without_breakout_is_not_candidate():
    box = [1917000 + (200 if i % 2 else -200) for i in range(22)]
    fr = bars3m("2026-09-22 11:00", box)
    watch = X.X1FlipState(flip_count=5, flag_count=6, watching=True,
                          last_direction=Direction.DOWN_BLUE.value,
                          box_high=max(box) + 50, box_low=min(box) - 50,
                          last_flag_at=fr["datetime"].iloc[-2].to_pydatetime())
    d = X.evaluate_late_entry(fr, watch, now=end_of(fr),
                              base_reject_reason="REJECT_NOT_CONFIRMED")
    assert d.candidate is False


# ── 9. X1-5 AFTERNOON BLUE BONUS ───────────────────────────────────────────
def _morning_red_then_blue_break():
    """오전 RED 추세 -> 오후 **가속** 하락. 선형 하락은 마지막 봉에서 gap 이
    오히려 수축하므로(실측) 가속 구간이 있어야 BLUE gap 확대가 성립한다."""
    morning_up = [1900000 + 1500 * i for i in range(60)]
    fade, px = [], morning_up[-1]
    for i in range(1, 30):
        px -= 500 * i                      # 가속 하락
        fade.append(px)
    return bars3m("2026-09-22 09:00", morning_up + fade)


def test_afternoon_blue_bonus_requires_full_structure():
    fr = _morning_red_then_blue_break()
    ok, metrics = X.afternoon_blue_bonus(fr, Direction.DOWN_BLUE, now=end_of(fr))
    assert ok is True and metrics["reason"] == "X1_AFTERNOON_BLUE_BONUS"
    assert metrics["gap_now"] < metrics["gap_prev"]      # BLUE 방향 확대
    assert metrics["close"] < metrics["vwap"]


def test_afternoon_blue_bonus_denied_before_cutoff():
    fr = bars3m("2026-09-22 09:00", [1900000 - 500 * i for i in range(40)])
    ok, _ = X.afternoon_blue_bonus(fr, Direction.DOWN_BLUE,
                                   now=pd.Timestamp("2026-09-22 11:00", tz=KST))
    assert ok is False


def test_afternoon_blue_bonus_denied_when_morning_not_red():
    down_all = [1950000 - 900 * i for i in range(90)]
    fr = bars3m("2026-09-22 09:00", down_all)
    ok, metrics = X.afternoon_blue_bonus(fr, Direction.DOWN_BLUE, now=end_of(fr))
    assert ok is False and metrics.get("reason") == "MORNING_NOT_RED"


def test_afternoon_blue_bonus_alone_never_approves_entry():
    """가점은 점수만 올린다 — 단독으로 진입을 승인하지 않는다."""
    fr = _morning_red_then_blue_break()
    ctx = X.build_context(now=end_of(fr), direction=Direction.DOWN_BLUE, bars_3m=fr,
                          base_reject_reason="TW2_3SLOT_REJECT_DAILY_SLOT_CAP")
    assert ctx.afternoon_blue_bonus is False or ctx.final_action == X.ACTION_BLOCK
    assert ctx.final_action != X.ACTION_LATE_ENTRY


# ── 10. trace 계약 ─────────────────────────────────────────────────────────
def test_trace_has_every_documented_field():
    ctx = X.build_context(now=pd.Timestamp("2026-09-22 12:15", tz=KST))
    trace = ctx.as_trace()
    for key in ("x1_final_action", "morning_score", "flip_count", "flip_span_min",
                "box_high", "box_low", "box_breakout", "flip_exit_score",
                "afternoon_reentry_ar1", "late_entry_score",
                "x1_afternoon_blue_bonus"):
        assert key in trace


def test_final_action_is_always_a_known_enum():
    allowed = {X.ACTION_PASS, X.ACTION_PASS_WEAK, X.ACTION_WATCH, X.ACTION_EXIT,
               X.ACTION_REENTRY, X.ACTION_LATE_ENTRY, X.ACTION_BLOCK}
    fr = bars3m("2026-09-22 09:00", [1900000 + 100 * i for i in range(30)])
    for reason in [None, "REJECT_NOT_CONFIRMED", SAME_DIR,
                   "TW2_3SLOT_REJECT_DAILY_SLOT_CAP"]:
        ctx = X.build_context(now=end_of(fr), direction=Direction.UP_RED, bars_3m=fr,
                              base_reject_reason=reason)
        assert ctx.final_action in allowed


# ── 11. 사후 보완 (demo replay 에서 드러난 두 건) ──────────────────────────
def test_unlisted_reject_reason_is_labelled_separately():
    """hard 목록에도 soft 목록에도 없는 사유는 되살리지 않되, **다른 라벨**로 남긴다.

    SOFT 목록 누락을 사후에 찾기 위한 신호다(예: TW2_REJECT_VWAP_VETO).
    """
    fr = bars3m("2026-09-22 11:00", [1910000 + 500 * i for i in range(30)])
    watch = X.X1FlipState(flip_count=5, flag_count=6, watching=True,
                          last_direction=Direction.UP_RED.value,
                          box_high=1915000, box_low=1905000,
                          last_flag_at=pd.Timestamp("2026-09-22 11:30", tz=KST))
    d = X.evaluate_late_entry(fr, watch, now=end_of(fr),
                              base_reject_reason="TW2_REJECT_VWAP_VETO")
    assert d.candidate is False
    assert d.reason == "X1_LATE_UNLISTED_REASON_NOT_RESURRECTED"
    hard = X.evaluate_late_entry(fr, watch, now=end_of(fr),
                                 base_reject_reason="TW2_3SLOT_REJECT_DAILY_SLOT_CAP")
    assert hard.reason == "X1_LATE_HARD_REJECT_NOT_RESURRECTED"


def test_flip_since_narrows_flip_count_for_held_position():
    """보유 판정의 flip 은 **포지션 진입/첫 H50 HOLD 이후**부터 센다(사양 §2)."""
    ev = flags(("09:00", "R"), ("09:27", "B"), ("11:03", "R"), ("11:12", "B"),
               ("11:24", "R"), ("11:30", "B"))
    now = pd.Timestamp("2026-09-22 11:45", tz=KST)
    whole_day = X.flip_state_from_live_flags(ev, now=now)
    since_1100 = X.flip_state_from_live_flags(
        ev, now=now, since=pd.Timestamp("2026-09-22 11:00", tz=KST))
    assert whole_day.flip_count == 5
    assert since_1100.flip_count == 3
    fr = bars3m("2026-09-22 09:00", [1900000 + 100 * i for i in range(50)])
    narrow = X.build_context(now=now, bars_3m=fr, flag_events=ev,
                             held_direction=Direction.UP_RED, h50_hold_seen=True,
                             flip_since=pd.Timestamp("2026-09-22 11:00", tz=KST))
    assert narrow.flip.flip_count == 3
    wide = X.build_context(now=now, bars_3m=fr, flag_events=ev,
                           held_direction=Direction.UP_RED, h50_hold_seen=True)
    assert wide.flip.flip_count == 5
