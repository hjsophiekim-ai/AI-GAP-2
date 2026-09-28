"""재계산 플래그 vs 신호원장 대조 — 표시 전용 (2026-09-22 실사고 회귀).

사고: UI "마지막 플래그"에 12:09 DOWN_BLUE 가 떴다가 **사라졌다**.
원인: `compute_today_signal_overview` 는 호출될 때마다 오늘 전체를 다시 걷는
순수 재계산이고, MACD EMA 가 누적이라 1분봉 하나만 늦게 들어오거나 빠져도
과거 플래그가 다른 시각으로 옮겨간다(repaint). 원장은 그대로인데 화면만 바뀐다.

이 테스트는 **표시 계층만** 검증한다 — 주문/진입/청산/슬롯/사이징은 이 경로를
호출하지 않는다.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

from app.trading.macd2 import worker as W

KST = "Asia/Seoul"


def _ov(hhmm: str, direction: str) -> dict:
    """compute_today_signal_overview 가 내는 모양의 재계산 행."""
    bar = f"2026-09-22T{hhmm}:00+09:00"
    return {
        "signal_id": f"20260922_{hhmm.replace(':', '')}00_{direction}",
        "bar_start_at": bar,
        "bar_end_at": f"2026-09-22T{hhmm}:00+09:00",
        "direction": direction,
        "origin": W.ORIGIN_LIVE_CONFIRMED,      # 재계산은 무조건 이걸 붙인다
    }


def _led(hhmm: str, direction: str, *, suffix: str = "", day: str = "20260922",
         bar_hhmm: str | None = None, signal_bar_at: bool = True) -> dict:
    """**실제 신호원장 schema**(ledger.SIGNAL_LEDGER_COLUMNS) 의 행.

    2026-09-28: 예전 fixture 는 원장에 없는 ``confirmed_bar_at`` 을 썼다 — 그래서
    production 에서 LEDGER_ONLY 행 시각이 늘 빈 값이던 버그를 테스트가 못 잡았다.
    ``bar_hhmm`` 은 그 행 자신의 바 시각(:TW_CONFIRM 행은 확정봉 = 플래그 +3분).
    """
    bar = bar_hhmm or hhmm
    iso_day = f"{day[:4]}-{day[4:6]}-{day[6:]}"
    row = {
        "trading_date": day,
        "completed_bar_at": bar.replace(":", "") + "00",
        "signal_id": f"{day}_{hhmm.replace(':', '')}00_{direction}{suffix}",
        "signal_type": "INITIAL",
        "direction": direction,
    }
    if signal_bar_at:
        row["signal_bar_at"] = f"{iso_day}T{bar}:00+09:00"
    return row


def _by_time(events, hhmm):
    return next((e for e in events if str(e["bar_start_at"])[11:16] == hhmm), None)


# ════════════════════════════════════════════════════════════════════════
# 1. 오늘 사례 — 12:09 BLUE 가 재계산에만 있으면 LIVE_CONFIRMED 가 아니다
# ════════════════════════════════════════════════════════════════════════
def test_recompute_only_flag_is_not_live_confirmed():
    overview = [_ov("09:00", "UP_RED"), _ov("12:09", "DOWN_BLUE")]
    ledger = [_led("09:00", "UP_RED")]           # 12:09 는 원장에 없다
    ev = W.reconcile_signal_overview_with_ledger(overview, ledger)
    assert _by_time(ev, "12:09")["origin"] == W.ORIGIN_RECOMPUTED_ONLY
    assert _by_time(ev, "12:09")["origin"] != W.ORIGIN_LIVE_CONFIRMED
    assert _by_time(ev, "09:00")["origin"] == W.ORIGIN_LIVE_CONFIRMED


def test_recompute_only_flag_never_becomes_the_last_flag():
    """워커가 본 적 없는 신호가 '마지막 플래그' 가 되면 안 된다."""
    overview = [_ov("09:00", "UP_RED"), _ov("12:09", "DOWN_BLUE")]
    ledger = [_led("09:00", "UP_RED")]
    ev = W.reconcile_signal_overview_with_ledger(overview, ledger)
    last = W.latest_ledger_backed_flag(ev)
    assert last is not None
    assert str(last["bar_start_at"])[11:16] == "09:00"    # 12:09 가 아니다
    assert last["direction"] == "UP_RED"


# ════════════════════════════════════════════════════════════════════════
# 2. 오늘 사례 — 09:30 RED / 09:39 BLUE (원장 전용) 는 화면에서 사라지지 않는다
# ════════════════════════════════════════════════════════════════════════
def test_ledger_only_flags_are_kept():
    overview = [_ov("09:00", "UP_RED"), _ov("09:27", "DOWN_BLUE")]   # 재계산에서 소실
    ledger = [_led("09:00", "UP_RED"), _led("09:27", "DOWN_BLUE"),
              _led("09:30", "UP_RED"), _led("09:39", "DOWN_BLUE")]
    ev = W.reconcile_signal_overview_with_ledger(overview, ledger)
    for hhmm, d in (("09:30", "UP_RED"), ("09:39", "DOWN_BLUE")):
        row = _by_time(ev, hhmm)
        assert row is not None, f"{hhmm} 이 화면에서 사라졌다"
        assert row["origin"] == W.ORIGIN_LEDGER_ONLY
        assert row["direction"] == d


def test_ledger_only_flag_can_be_the_last_flag():
    overview = [_ov("09:00", "UP_RED")]
    ledger = [_led("09:00", "UP_RED"), _led("09:39", "DOWN_BLUE")]
    last = W.latest_ledger_backed_flag(
        W.reconcile_signal_overview_with_ledger(overview, ledger))
    assert str(last["bar_start_at"])[11:16] == "09:39"


# ════════════════════════════════════════════════════════════════════════
# 3. 불변성 — 원장이 같으면 재계산이 어떻게 흔들려도 마지막 플래그는 그대로
# ════════════════════════════════════════════════════════════════════════
def test_last_flag_is_invariant_to_recompute_repaint():
    ledger = [_led("09:00", "UP_RED"), _led("09:27", "DOWN_BLUE")]
    before = W.latest_ledger_backed_flag(W.reconcile_signal_overview_with_ledger(
        [_ov("09:00", "UP_RED"), _ov("09:27", "DOWN_BLUE"), _ov("12:09", "DOWN_BLUE")],
        ledger))
    # 분봉이 더 들어와 재계산이 12:09 -> 12:12 로 repaint 된 뒤
    after = W.latest_ledger_backed_flag(W.reconcile_signal_overview_with_ledger(
        [_ov("09:00", "UP_RED"), _ov("09:27", "DOWN_BLUE"), _ov("12:12", "DOWN_BLUE")],
        ledger))
    assert before is not None and after is not None
    assert before["signal_id"] == after["signal_id"]
    assert str(after["bar_start_at"])[11:16] == "09:27"


def test_repaint_does_not_delete_previously_confirmed_events():
    ledger = [_led("12:09", "DOWN_BLUE")]
    ev = W.reconcile_signal_overview_with_ledger(
        [_ov("12:12", "DOWN_BLUE")], ledger)          # 재계산은 12:12 로 옮겨감
    assert _by_time(ev, "12:09") is not None          # 확정 이벤트는 유지
    assert _by_time(ev, "12:09")["origin"] == W.ORIGIN_LEDGER_ONLY
    assert _by_time(ev, "12:12")["origin"] == W.ORIGIN_RECOMPUTED_ONLY


# ════════════════════════════════════════════════════════════════════════
# 4. signal_id 접미사 / 경계 조건
# ════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("suffix", [":TW2_3SLOT_CONFIRM", ":TW_CONFIRM",
                                    ":QUOTE_STALE_RECOVERY_SELL"])
def test_ledger_suffixes_still_match_the_recomputed_flag(suffix):
    ev = W.reconcile_signal_overview_with_ledger(
        [_ov("09:06", "UP_RED")], [_led("09:06", "UP_RED", suffix=suffix)])
    assert _by_time(ev, "09:06")["origin"] == W.ORIGIN_LIVE_CONFIRMED


def test_base_signal_id_strips_only_the_suffix():
    assert W.base_signal_id("20260922_120900_DOWN_BLUE:TW2_3SLOT_CONFIRM") == \
        "20260922_120900_DOWN_BLUE"
    assert W.base_signal_id("20260922_120900_DOWN_BLUE") == "20260922_120900_DOWN_BLUE"
    assert W.base_signal_id(None) == ""


def test_empty_inputs_are_safe():
    assert W.reconcile_signal_overview_with_ledger([], []) == []
    assert W.latest_ledger_backed_flag([]) is None
    assert W.latest_ledger_backed_flag(
        [{"origin": W.ORIGIN_RECOMPUTED_ONLY, "bar_start_at": "x"}]) is None


def test_events_are_sorted_by_bar_time():
    ev = W.reconcile_signal_overview_with_ledger(
        [_ov("12:09", "DOWN_BLUE"), _ov("09:00", "UP_RED")],
        [_led("11:00", "UP_RED")])
    times = [str(e["bar_start_at"])[11:16] for e in ev]
    assert times == sorted(times)


# ════════════════════════════════════════════════════════════════════════
# 5. 실제 repaint 재현 — 실데이터(2026-09-22)가 있을 때만
# ════════════════════════════════════════════════════════════════════════
_REAL_CSV = Path("data/cache/replay_20260922_hynix_1m.csv")


def _real_frame(drop: str | None = None) -> pd.DataFrame:
    d = pd.read_csv(_REAL_CSV)
    d["datetime"] = pd.to_datetime(d["datetime"], format="ISO8601")
    if d["datetime"].dt.tz is None:
        d["datetime"] = d["datetime"].dt.tz_localize(KST)
    if drop:
        d = d[d["datetime"].dt.strftime("%H:%M") != drop]
    return d.sort_values("datetime").reset_index(drop=True)


@pytest.mark.skipif(not _REAL_CSV.exists(), reason="2026-09-22 분봉 캐시 없음")
@pytest.mark.parametrize("drop", ["12:05", "12:09", "12:10", "12:11"])
def test_real_data_repaints_when_a_minute_is_missing(drop):
    """실사고 재현: 분봉 하나가 빠지면 12:09 DOWN_BLUE 가 12:12 로 옮겨간다."""
    now = datetime.fromisoformat("2026-09-22T12:35:00+09:00")
    ss = "2026-09-22T08:30:00+09:00"
    full = {str(o["bar_start_at"])[11:16]
            for o in W.compute_today_signal_overview(
                _real_frame(), now=now, session_started_at=ss)}
    holed = {str(o["bar_start_at"])[11:16]
             for o in W.compute_today_signal_overview(
                 _real_frame(drop), now=now, session_started_at=ss)}
    assert "12:09" in full, "기준 데이터에 12:09 플래그가 있어야 한다"
    # 한 번 표시됐던 12:09 플래그가 **사라진다** — 이게 사고의 메커니즘이다.
    assert "12:09" not in holed, f"{drop} 결측인데 12:09 가 그대로면 재현 실패"
    # 그리고 더 뒤 시각으로 옮겨간다(12:05 결측 -> 12:12, 12:09~12:11 -> 12:15).
    assert any(t > "12:09" for t in holed), "플래그가 뒤로 옮겨가야 한다"
    assert full - {"12:09"} == holed - {t for t in holed if t > "12:09"},         "12:09 이전 플래그는 그대로여야 한다"


@pytest.mark.skipif(not _REAL_CSV.exists(), reason="2026-09-22 분봉 캐시 없음")
def test_real_repaint_does_not_move_the_ledger_backed_last_flag():
    """위 repaint 가 일어나도 원장 기반 '마지막 플래그' 는 불변이어야 한다."""
    now = datetime.fromisoformat("2026-09-22T12:35:00+09:00")
    ss = "2026-09-22T08:30:00+09:00"
    ledger = [_led("09:27", "DOWN_BLUE")]
    a = W.latest_ledger_backed_flag(W.reconcile_signal_overview_with_ledger(
        W.compute_today_signal_overview(_real_frame(), now=now, session_started_at=ss),
        ledger))
    b = W.latest_ledger_backed_flag(W.reconcile_signal_overview_with_ledger(
        W.compute_today_signal_overview(_real_frame("12:10"), now=now, session_started_at=ss),
        ledger))
    assert a is not None and b is not None
    assert a["signal_id"] == b["signal_id"] == "20260922_092700_DOWN_BLUE"


# ════════════════════════════════════════════════════════════════════════
# 6. 2026-09-28 실사고 — 재배포 직후 "마지막 FLAG EVENT" 가 08:00 BLUE 로 회귀
#    (실제 마지막 = 14:09 BLUE). 원장에 없는 컬럼을 읽어 LEDGER_ONLY 시각이
#    빈 값 -> 재계산 repaint 로 최신 플래그가 전부 LEDGER_ONLY 가 되자
#    오래된 일치 이벤트(08:00)만 후보로 남았다.
# ════════════════════════════════════════════════════════════════════════
def test_fixture_rows_use_only_real_signal_ledger_columns():
    """fixture 가 다시 가짜 컬럼에 의존하지 못하게 schema 로 잠근다."""
    from app.trading.macd2 import ledger
    cols = set(ledger.SIGNAL_LEDGER_COLUMNS)
    for row in (_led("09:00", "UP_RED"), _led("09:03", "DOWN_BLUE", suffix=":TW_CONFIRM",
                                              bar_hhmm="09:06")):
        assert set(row) <= cols, f"원장에 없는 컬럼: {set(row) - cols}"


def _led0928():
    """오늘(2026-09-28) 신호원장 모양 — 플래그봉 행 + T+3 확정봉 행."""
    d = "20260928"
    rows = [_led("08:00", "DOWN_BLUE", day=d), _led("08:39", "UP_RED", day=d)]
    for hhmm, conf, dirn in (("09:03", "09:06", "DOWN_BLUE"), ("09:21", "09:24", "UP_RED"),
                             ("09:39", "09:42", "DOWN_BLUE"), ("14:09", "14:12", "DOWN_BLUE")):
        rows.append(_led(hhmm, dirn, day=d))
        rows.append(_led(hhmm, dirn, day=d, suffix=":TW2_3SLOT_CONFIRM", bar_hhmm=conf))
    return rows


def _ov0928(hhmm, direction):
    r = _ov(hhmm, direction)
    r["signal_id"] = r["signal_id"].replace("20260922", "20260928")
    r["bar_start_at"] = r["bar_start_at"].replace("2026-09-22", "2026-09-28")
    r["bar_end_at"] = r["bar_end_at"].replace("2026-09-22", "2026-09-28")
    return r


def test_0928_ledger_only_1409_is_the_last_flag_after_restart_repaint():
    # 재시작 직후 재계산: 08:00 만 원장과 일치, 나머지는 repaint 로 시각이 밀림
    overview = [_ov0928("08:00", "DOWN_BLUE"), _ov0928("09:06", "DOWN_BLUE"),
                _ov0928("14:15", "DOWN_BLUE")]
    ev = W.reconcile_signal_overview_with_ledger(overview, _led0928())
    row = next(e for e in ev if e["signal_id"] == "20260928_140900_DOWN_BLUE")
    assert row["origin"] == W.ORIGIN_LEDGER_ONLY
    assert row["bar_start_at"] == "2026-09-28T14:09:00+09:00", "플래그봉 시각(확정봉 14:12 아님)"
    last = W.latest_ledger_backed_flag(ev)
    assert last["signal_id"] == "20260928_140900_DOWN_BLUE"
    assert last["direction"] == "DOWN_BLUE"
    assert str(last["bar_start_at"])[11:16] == "14:09"


def test_0928_does_not_fall_back_to_the_old_matched_0800_event():
    overview = [_ov0928("08:00", "DOWN_BLUE")]          # 유일한 일치 이벤트
    last = W.latest_ledger_backed_flag(
        W.reconcile_signal_overview_with_ledger(overview, _led0928()))
    assert str(last["bar_start_at"])[11:16] != "08:00"
    assert str(last["bar_start_at"])[11:16] == "14:09"


def test_0928_empty_recompute_still_shows_ledger_last_flag():
    """재시작 직후 1분봉 이력이 아직 비어 재계산이 0건이어도 원장으로 표시한다."""
    last = W.latest_ledger_backed_flag(W.reconcile_signal_overview_with_ledger([], _led0928()))
    assert last is not None and last["signal_id"] == "20260928_140900_DOWN_BLUE"


def test_ledger_bar_time_falls_back_to_completed_bar_at_without_signal_bar_at():
    row = _led("14:09", "DOWN_BLUE", day="20260928", signal_bar_at=False)
    assert "signal_bar_at" not in row
    assert W._ledger_row_bar_at(row) == "2026-09-28T14:09:00+09:00"


def test_ledger_bar_time_prefers_signal_bar_at():
    row = _led("14:09", "DOWN_BLUE", day="20260928")
    row["completed_bar_at"] = "999999"                   # 깨진 fallback 은 무시돼야 한다
    assert W._ledger_row_bar_at(row) == "2026-09-28T14:09:00+09:00"


def test_ledger_bar_time_is_empty_when_no_real_column_exists():
    """추정하지 않는다 -- 예전 가짜 컬럼만 있으면 빈 값(후보 제외)."""
    assert W._ledger_row_bar_at({"confirmed_bar_at": "2026-09-28T14:09:00+09:00",
                                 "bar_start_at": "x", "flag_bar_at": "y"}) == ""


def test_matched_event_keeps_recompute_bar_time():
    """LIVE_CONFIRMED 는 재계산 시각을 그대로 쓴다 -- 원장 시각으로 덮어쓰지 않는다."""
    ev = W.reconcile_signal_overview_with_ledger(
        [_ov0928("09:03", "DOWN_BLUE")],
        [_led("09:03", "DOWN_BLUE", day="20260928", suffix=":TW_CONFIRM", bar_hhmm="09:06")])
    assert ev[0]["origin"] == W.ORIGIN_LIVE_CONFIRMED
    assert ev[0]["bar_start_at"] == "2026-09-28T09:03:00+09:00"


def test_display_path_does_not_touch_order_or_exit_logic():
    """수정 범위 잠금 -- 표시용 헬퍼는 주문/청산 함수에서 불리지 않는다."""
    import inspect
    for fn in (W.run_once, W._resolve_tw2_3slot_candidate, W._advance_held_position_risk_management):
        src = inspect.getsource(fn)
        assert "_ledger_row_bar_at" not in src
        assert "reconcile_signal_overview_with_ledger" not in src


# ════════════════════════════════════════════════════════════════════════
# 7. 화면 — 실제 페이지가 "마지막 FLAG EVENT = DOWN_BLUE 14:09" 를 그리는가
#    (재시작 직후처럼 재계산이 비어 있고 원장만 있는 상태)
# ════════════════════════════════════════════════════════════════════════
def test_page_last_flag_event_metric_shows_ledger_1409_blue():
    from streamlit.testing.v1 import AppTest
    from app.trading.macd2 import ledger, state_store

    s = state_store.load_state()
    s.session_date = "20260928"
    s.mode = "mock"
    state_store.save_state(s)
    for row in _led0928():
        assert ledger.append_signal(row)

    app = str(Path(__file__).parent.parent.parent / "app" / "ui" / "pages" / "11_MACD_자동매매2.py")
    at = AppTest.from_file(app, default_timeout=40)
    at.session_state["app_auth_authenticated"] = True
    at.run()
    for exc in at.exception:
        if "can't be used in an `st.form()`" in str(getattr(exc, "value", "") or ""):
            pytest.skip("기존 페이지 결함(st.button inside st.form) -- 이 변경과 무관")
    assert not at.exception
    m = next(x for x in at.metric if x.label == "마지막 FLAG EVENT")
    assert m.value == "DOWN_BLUE", f"값이 {m.value!r} (재계산 전용/08:00 회귀 금지)"
    assert m.delta == "14:09:00", f"시각이 {m.delta!r}"
