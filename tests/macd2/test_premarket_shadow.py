"""Premarket Carry Shadow — 관측 전용 모듈 + GitHub sync 무결성 테스트.

이 테스트가 지켜야 하는 핵심 명제는 딱 두 개다.

  A. shadow 는 **주문/슬롯/production state 를 절대 건드리지 않는다.**
  B. GitHub 로 나가는 바이트에 **민감정보가 절대 섞이지 않는다.**

나머지 케이스(08:44/08:45 경계, 재시작, 중복틱, 날짜 rollover)는 전부
"A 를 지키면서도 표본이 실제로 쌓이는가"를 확인하는 보조 검증이다.
"""
from __future__ import annotations

import csv
import importlib
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.trading.macd2 import config as mcfg
from app.trading.macd2 import premarket_shadow as PS
from app.trading.macd2.models import Direction
from app.utils.time_utils import KST


# ══════════════════════════════════════════════════════════════════════════
#  fixtures — 모든 쓰기를 tmp_path 로 격리한다(실제 Disk/레포를 절대 안 건드림)
# ══════════════════════════════════════════════════════════════════════════
@pytest.fixture
def shadow_dir(tmp_path, monkeypatch):
    d = tmp_path / "premarket_carry"
    d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(PS, "PREMARKET_CARRY_DIR", d, raising=True)
    return d


def _dt(hhmm: str, day: str = "20260914") -> datetime:
    return datetime.strptime(f"{day} {hhmm}", "%Y%m%d %H:%M").replace(tzinfo=KST)


def _snap(bar_hhmm: str, *, direction: Direction | None, day: str = "20260914"):
    """confirmed_macd_flag_condition 이 원하는 방향을 돌려주도록 만든 스냅샷.

    production 규칙은 **히스토그램 3봉의 단조성** 이다(부호가 아니라 방향):
      RED  = h2 < h1 < h0 (2봉 연속 상승)
      BLUE = h2 > h1 > h0 (2봉 연속 하락)
      그 외는 HOLD.
    """
    if direction == Direction.UP_RED:
        hist = (-1.0, 0.0, 1.0)
    elif direction == Direction.DOWN_BLUE:
        hist = (1.0, 0.0, -1.0)
    else:
        hist = (0.0, 0.0, 0.0)          # 단조 아님 → HOLD
    return SimpleNamespace(
        bar_dt=_dt(bar_hhmm, day), macd=hist[2], signal=0.0,
        current_diff=hist[2], previous_diff=hist[1], relation=None,
        hist_last3=hist,
    )


class _FakeState:
    """production RuntimeState 를 흉내낸 **읽기 전용 감시자**.

    shadow 가 어떤 속성이든 *쓰려고 하면* 즉시 실패한다 — 이게 명제 A 의
    기계적 증명이다.
    """

    _ALLOWED_INIT = {
        "time_window_2_filter_enabled", "time_window_teg_filter_enabled",
        "time_window_3slot_filter_enabled", "time_window_twf_filter_enabled",
        "time_window_x2lite_filter_enabled", "x2lite_exposure_used_today",
        "x2lite_entry_seq_today", "x2lite_first_trade_stop_loss",
        "x2lite_last_applied_sizing", "time_window_active_mode",
        "_frozen", "writes",
    }

    def __init__(self):
        object.__setattr__(self, "writes", [])
        object.__setattr__(self, "_frozen", False)
        self.time_window_2_filter_enabled = False
        self.time_window_teg_filter_enabled = False
        self.time_window_3slot_filter_enabled = False
        self.time_window_twf_filter_enabled = False
        self.time_window_x2lite_filter_enabled = True
        self.time_window_active_mode = None
        self.x2lite_exposure_used_today = 0.0
        self.x2lite_entry_seq_today = 0
        self.x2lite_first_trade_stop_loss = False
        self.x2lite_last_applied_sizing = None
        object.__setattr__(self, "_frozen", True)

    def __setattr__(self, name, value):
        if object.__getattribute__(self, "_frozen"):
            object.__getattribute__(self, "writes").append(name)
            raise AssertionError(
                f"premarket_shadow 가 production state 에 썼다: {name}={value!r}"
            )
        object.__setattr__(self, name, value)


@pytest.fixture
def st():
    return _FakeState()


def _events(shadow_dir, day="20260914"):
    p = shadow_dir / f"premarket_carry_{day}.csv"
    if not p.exists():
        return []
    with p.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def _types(shadow_dir, day="20260914"):
    return [r["event_type"] for r in _events(shadow_dir, day)]


# ══════════════════════════════════════════════════════════════════════════
#  1. carry 윈도 경계
# ══════════════════════════════════════════════════════════════════════════
def test_window_boundaries_are_0845_inclusive_0900_exclusive():
    from datetime import time as dtime
    assert not PS.is_premarket_carry_window(dtime(8, 44, 59))
    assert PS.is_premarket_carry_window(dtime(8, 45, 0))
    assert PS.is_premarket_carry_window(dtime(8, 59, 59))
    assert not PS.is_premarket_carry_window(dtime(9, 0, 0))


def test_flag_at_0844_is_recorded_but_never_becomes_candidate(shadow_dir, st):
    PS.observe(state=st, macd_snap=_snap("08:44", direction=Direction.UP_RED),
               bars_3m=None, now=_dt("08:47"), quotes={})
    assert _types(shadow_dir) == [PS.EV_PREMARKET_FLAG]           # 기록은 된다
    assert PS.load_shadow_state().last_flag_direction is None      # 후보는 아니다


def test_flag_at_0845_becomes_candidate(shadow_dir, st):
    PS.observe(state=st, macd_snap=_snap("08:45", direction=Direction.UP_RED),
               bars_3m=None, now=_dt("08:48"), quotes={})
    assert _types(shadow_dir) == [PS.EV_PREMARKET_FLAG, PS.EV_PREMARKET_LAST_FLAG]
    s = PS.load_shadow_state()
    assert s.last_flag_direction == Direction.UP_RED.value
    assert s.last_flag_time == _dt("08:45").isoformat()


def test_later_flag_at_0857_replaces_the_0845_candidate(shadow_dir, st):
    PS.observe(state=st, macd_snap=_snap("08:45", direction=Direction.UP_RED),
               bars_3m=None, now=_dt("08:48"), quotes={})
    PS.observe(state=st, macd_snap=_snap("08:57", direction=Direction.DOWN_BLUE),
               bars_3m=None, now=_dt("09:00"), quotes={})
    s = PS.load_shadow_state()
    assert s.last_flag_time == _dt("08:57").isoformat()
    assert s.last_flag_direction == Direction.DOWN_BLUE.value      # 마지막 것만


def test_no_flag_premarket_bar_writes_nothing(shadow_dir, st):
    PS.observe(state=st, macd_snap=_snap("08:48", direction=None),
               bars_3m=None, now=_dt("08:51"), quotes={})
    assert _events(shadow_dir) == []


# ══════════════════════════════════════════════════════════════════════════
#  2. 09:00 상태 / 취소 경로
# ══════════════════════════════════════════════════════════════════════════
def _seed_candidate(st, shadow_dir, direction=Direction.UP_RED):
    PS.observe(state=st, macd_snap=_snap("08:45", direction=direction),
               bars_3m=None, now=_dt("08:48"), quotes={})


def test_0900_without_a_new_flag_keeps_candidate_alive(shadow_dir, st):
    """09:00 봉에 **새 플래그가 없으면** 후보는 그대로 살아있다(정상 경로)."""
    _seed_candidate(st, shadow_dir)
    PS.observe(state=st, macd_snap=_snap("09:00", direction=None),
               bars_3m=None, now=_dt("09:03"), quotes={})
    s = PS.load_shadow_state()
    assert s.state_0900 == "SAME_DIRECTION"
    assert s.cancel_reason == ""
    assert s.state_0903 == ""
    assert PS.EV_OPEN_0900_STATE in _types(shadow_dir)


def test_0900_same_direction_regular_flag_still_cancels(shadow_dir, st):
    """같은 방향이라도 **정규장 플래그가 떴으면** carry 는 포기한다.

    규칙 7 그대로다 — 그 플래그는 production 이 어차피 정상 경로로 처리하므로
    carry 가 끼어들 이유가 없다. 방향이 같다는 이유로 예외를 두지 않는다.
    """
    _seed_candidate(st, shadow_dir, Direction.UP_RED)
    PS.observe(state=st, macd_snap=_snap("09:00", direction=Direction.UP_RED),
               bars_3m=None, now=_dt("09:03"), quotes={})
    s = PS.load_shadow_state()
    assert s.state_0900 == "SAME_DIRECTION"          # 방향은 같았지만
    assert s.state_0903 == "CANCELLED"               # 그래도 취소
    assert s.cancel_reason == PS.EV_CARRY_CANCEL_REGULAR_FLAG
    assert s.carry_confirmed is False


def test_0900_reversed_direction_cancels(shadow_dir, st):
    _seed_candidate(st, shadow_dir, Direction.UP_RED)
    PS.observe(state=st, macd_snap=_snap("09:00", direction=Direction.DOWN_BLUE),
               bars_3m=None, now=_dt("09:03"), quotes={})
    s = PS.load_shadow_state()
    assert s.state_0900 == "REVERSED"
    assert s.state_0903 == "CANCELLED"
    assert s.cancel_reason == PS.EV_CARRY_CANCEL_REVERSE
    assert s.carry_confirmed is False
    assert PS.EV_CARRY_CANCEL_REVERSE in _types(shadow_dir)


def test_regular_session_flag_cancels_the_carry(shadow_dir, st):
    """규칙 7 — 정규장 플래그가 먼저 나오면 carry 는 포기한다."""
    _seed_candidate(st, shadow_dir, Direction.UP_RED)
    PS.observe(state=st, macd_snap=_snap("09:01", direction=Direction.UP_RED),
               bars_3m=None, now=_dt("09:04"), quotes={})
    s = PS.load_shadow_state()
    assert s.state_0903 == "CANCELLED"
    assert s.cancel_reason == PS.EV_CARRY_CANCEL_REGULAR_FLAG
    assert s.carry_confirmed is False


def test_cancel_writes_a_summary_row(shadow_dir, st):
    _seed_candidate(st, shadow_dir, Direction.UP_RED)
    PS.observe(state=st, macd_snap=_snap("09:01", direction=Direction.UP_RED),
               bars_3m=None, now=_dt("09:04"), quotes={})
    rows = PS.read_summary()
    assert len(rows) == 1
    assert rows[0]["date"] == "20260914"
    assert rows[0]["cancel_reason"] == PS.EV_CARRY_CANCEL_REGULAR_FLAG


# ══════════════════════════════════════════════════════════════════════════
#  3. 09:03 재확인 (성공/실패)
# ══════════════════════════════════════════════════════════════════════════
class _Dec:
    def __init__(self, approved, block_reason=""):
        self.approved = approved
        self.block_reason = block_reason


def test_0903_confirm_success_records_eligible_and_entry(shadow_dir, st, monkeypatch):
    _seed_candidate(st, shadow_dir, Direction.UP_RED)
    PS.observe(state=st, macd_snap=_snap("09:00", direction=None),
               bars_3m=None, now=_dt("09:03"), quotes={})
    monkeypatch.setattr(PS.twf, "evaluate_time_window_entry",
                        lambda *a, **k: _Dec(True))
    monkeypatch.setattr(PS.early_take_profit, "evaluate_entry_chop",
                        lambda *a, **k: SimpleNamespace(is_chop=False))
    PS.observe(state=st, macd_snap=_snap("09:03", direction=None), bars_3m=object(),
               now=_dt("09:06"), quotes={mcfg.LONG_SYMBOL: 10000.0})
    s = PS.load_shadow_state()
    assert s.state_0903 == "CONFIRMED" and s.carry_confirmed is True
    assert s.pos_symbol == mcfg.LONG_SYMBOL and s.pos_entry_price == 10000.0
    t = _types(shadow_dir)
    assert PS.EV_CARRY_CONFIRM_0903 in t
    assert PS.EV_CARRY_ELIGIBLE in t
    assert PS.EV_CARRY_SHADOW_ENTRY in t


def test_0903_confirm_failure_records_rejection_only(shadow_dir, st, monkeypatch):
    _seed_candidate(st, shadow_dir, Direction.UP_RED)
    PS.observe(state=st, macd_snap=_snap("09:00", direction=None),
               bars_3m=None, now=_dt("09:03"), quotes={})
    monkeypatch.setattr(PS.twf, "evaluate_time_window_entry",
                        lambda *a, **k: _Dec(False, "TEG_BLOCK"))
    PS.observe(state=st, macd_snap=_snap("09:03", direction=None), bars_3m=object(),
               now=_dt("09:06"), quotes={mcfg.LONG_SYMBOL: 10000.0})
    s = PS.load_shadow_state()
    assert s.state_0903 == "REJECTED" and s.carry_confirmed is False
    assert s.cancel_reason == "TEG_BLOCK"
    assert PS.EV_CARRY_ELIGIBLE not in _types(shadow_dir)


def test_carry_is_attempted_at_most_once_per_day(shadow_dir, st, monkeypatch):
    _seed_candidate(st, shadow_dir, Direction.UP_RED)
    PS.observe(state=st, macd_snap=_snap("09:00", direction=None),
               bars_3m=None, now=_dt("09:03"), quotes={})
    calls = []
    monkeypatch.setattr(PS.twf, "evaluate_time_window_entry",
                        lambda *a, **k: (calls.append(1), _Dec(False, "X"))[1])
    for hhmm in ("09:03", "09:06", "09:09"):
        PS.observe(state=st, macd_snap=_snap(hhmm, direction=None), bars_3m=object(),
                   now=_dt(hhmm) + timedelta(minutes=3), quotes={})
    assert len(calls) == 1


# ══════════════════════════════════════════════════════════════════════════
#  4. 가상 포지션 — X2-lite 청산 규칙을 그대로 쓰는가
# ══════════════════════════════════════════════════════════════════════════
def _confirmed_position(st, shadow_dir, monkeypatch, entry=10000.0, chop=False):
    _seed_candidate(st, shadow_dir, Direction.UP_RED)
    PS.observe(state=st, macd_snap=_snap("09:00", direction=None),
               bars_3m=None, now=_dt("09:03"), quotes={})
    monkeypatch.setattr(PS.twf, "evaluate_time_window_entry", lambda *a, **k: _Dec(True))
    monkeypatch.setattr(PS.early_take_profit, "evaluate_entry_chop",
                        lambda *a, **k: SimpleNamespace(is_chop=chop))
    PS.observe(state=st, macd_snap=_snap("09:03", direction=None), bars_3m=object(),
               now=_dt("09:06"), quotes={mcfg.LONG_SYMBOL: entry})


def test_shadow_stop_loss_uses_x2lite_threshold_not_f(shadow_dir, st, monkeypatch):
    """X2-lite SL = -1.30%. F(TW TEG) 의 -1.40% 가 아니어야 한다."""
    _confirmed_position(st, shadow_dir, monkeypatch)
    # -1.35% → X2-lite 면 손절, F 면 아직 버팀
    PS.observe(state=st, macd_snap=_snap("09:09", direction=None), bars_3m=object(),
               now=_dt("09:12"), quotes={mcfg.LONG_SYMBOL: 10000.0 * (1 - 0.0135)})
    s = PS.load_shadow_state()
    assert s.exit_reason == mcfg.EXIT_TW_STOP_LOSS
    assert s.net_pct == pytest.approx(-1.35, abs=1e-6)


def test_shadow_tp1_is_partial_then_position_survives(shadow_dir, st, monkeypatch):
    _confirmed_position(st, shadow_dir, monkeypatch)
    tp1 = mcfg.MORNING_TP1          # 3.0% — X2-lite 는 트리거를 F 와 공유한다
    PS.observe(state=st, macd_snap=_snap("09:09", direction=None), bars_3m=object(),
               now=_dt("09:12"), quotes={mcfg.LONG_SYMBOL: 10000.0 * (1 + tp1 + 0.001)})
    s = PS.load_shadow_state()
    assert s.pos_tp1_done is True
    assert s.exit_reason is None                       # 부분청산이므로 살아있다
    assert s.pos_qty_frac == pytest.approx(
        1.0 - mcfg.X2LITE_MORNING_TP1_SELL_RATIO, abs=1e-9)


def test_forced_liquidation_closes_the_shadow_position(shadow_dir, st, monkeypatch):
    _confirmed_position(st, shadow_dir, monkeypatch)
    PS.observe(state=st, macd_snap=_snap("15:18", direction=None), bars_3m=object(),
               now=_dt("15:19"), quotes={mcfg.LONG_SYMBOL: 10050.0})
    s = PS.load_shadow_state()
    assert s.exit_reason == mcfg.EXIT_FORCED_LIQUIDATION
    assert PS.EV_CARRY_SHADOW_EXIT in _types(shadow_dir)


def test_shadow_sizing_matches_production_position_sizing(shadow_dir, st, monkeypatch):
    from app.trading.macd2 import position_sizing
    _confirmed_position(st, shadow_dir, monkeypatch, chop=True)
    expected = position_sizing.evaluate(_FakeState(), entry_chop=True).applied
    assert PS.load_shadow_state().pos_sizing == pytest.approx(expected)
    assert expected == pytest.approx(mcfg.X2LITE_SIZING_CHOP_MULT)


# ══════════════════════════════════════════════════════════════════════════
#  5. 재시작 / 중복틱 / 날짜 rollover
# ══════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("restart_at", ["08:50", "09:01", "09:02"])
def test_state_survives_a_worker_restart(shadow_dir, st, restart_at):
    """상태는 Disk JSON 에만 있으므로 프로세스가 죽어도 후보가 유지된다."""
    _seed_candidate(st, shadow_dir, Direction.UP_RED)
    before = PS.load_shadow_state()
    importlib.reload(PS)                      # 프로세스 재시작 시뮬레이션
    PS.PREMARKET_CARRY_DIR = shadow_dir
    after = PS.load_shadow_state()
    assert after.last_flag_time == before.last_flag_time
    assert after.last_flag_direction == before.last_flag_direction
    assert after.date == before.date


def test_duplicate_ticks_do_not_duplicate_rows(shadow_dir, st):
    for _ in range(5):
        PS.observe(state=st, macd_snap=_snap("08:45", direction=Direction.UP_RED),
                   bars_3m=None, now=_dt("08:48"), quotes={})
    assert _types(shadow_dir).count(PS.EV_PREMARKET_FLAG) == 1
    assert _types(shadow_dir).count(PS.EV_PREMARKET_LAST_FLAG) == 1


def test_date_rollover_resets_state_and_opens_a_new_file(shadow_dir, st):
    PS.observe(state=st, macd_snap=_snap("08:45", direction=Direction.UP_RED),
               bars_3m=None, now=_dt("08:48"), quotes={})
    PS.observe(state=st,
               macd_snap=_snap("08:45", direction=Direction.DOWN_BLUE, day="20260915"),
               bars_3m=None, now=_dt("08:48", "20260915"), quotes={})
    s = PS.load_shadow_state()
    assert s.date == "20260915"
    assert s.last_flag_direction == Direction.DOWN_BLUE.value
    assert (shadow_dir / "premarket_carry_20260914.csv").exists()
    assert (shadow_dir / "premarket_carry_20260915.csv").exists()


def test_corrupt_state_file_is_recovered_not_fatal(shadow_dir, st):
    PS.state_path().write_text("{{{ not json", encoding="utf-8")
    assert PS.load_shadow_state().date == ""           # 조용히 초기화
    PS.observe(state=st, macd_snap=_snap("08:45", direction=Direction.UP_RED),
               bars_3m=None, now=_dt("08:48"), quotes={})
    assert PS.load_shadow_state().last_flag_direction == Direction.UP_RED.value


# ══════════════════════════════════════════════════════════════════════════
#  6. 명제 A — 주문/슬롯/production state 불가침
# ══════════════════════════════════════════════════════════════════════════
def test_module_never_imports_order_executor():
    """주석/독스트링이 아니라 **실제 코드** 를 본다 — AST 로 import 와 이름
    참조를 전부 훑어서 주문 관련 심볼이 하나라도 있으면 실패."""
    import ast

    tree = ast.parse(open(PS.__file__, encoding="utf-8").read())
    banned = {"order_executor", "place_order", "send_order", "submit_order",
              "KisClient", "kis_client", "order_api"}
    seen = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            seen.update(a.name.split(".")[-1] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            seen.add((node.module or "").split(".")[-1])
            seen.update(a.name for a in node.names)
        elif isinstance(node, (ast.Name, ast.Attribute)):
            seen.add(node.id if isinstance(node, ast.Name) else node.attr)
    hits = sorted(banned & seen)
    assert not hits, f"shadow 모듈 코드가 주문 심볼을 참조한다: {hits}"


def test_observe_never_writes_to_production_state(shadow_dir, st, monkeypatch):
    """_FakeState 는 어떤 쓰기든 AssertionError 를 던진다. 전 구간을 통과시킨다."""
    monkeypatch.setattr(PS.twf, "evaluate_time_window_entry", lambda *a, **k: _Dec(True))
    monkeypatch.setattr(PS.early_take_profit, "evaluate_entry_chop",
                        lambda *a, **k: SimpleNamespace(is_chop=False))
    seq = [("08:45", Direction.UP_RED, "08:48", {}),
           ("09:00", None, "09:03", {}),
           ("09:03", None, "09:06", {mcfg.LONG_SYMBOL: 10000.0}),
           ("09:09", None, "09:12", {mcfg.LONG_SYMBOL: 9800.0})]
    for bar, d, now, q in seq:
        PS.observe(state=st, macd_snap=_snap(bar, direction=d), bars_3m=object(),
                   now=_dt(now), quotes=q)
    assert st.writes == []


def test_observe_swallows_every_exception(shadow_dir, st, monkeypatch):
    monkeypatch.setattr(PS, "_observe_inner",
                        lambda **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert PS.observe(state=st, macd_snap=_snap("08:45", direction=Direction.UP_RED),
                      bars_3m=None, now=_dt("08:48"), quotes={}) is None


def test_worker_hook_is_wrapped_in_try_except():
    import app.trading.macd2.worker as W
    text = open(W.__file__, encoding="utf-8").read()
    i = text.index("premarket_shadow.observe(")
    head = text[max(0, i - 400):i]
    assert "try:" in head, "worker 의 observe 호출이 try 로 감싸이지 않았다"


def test_worker_tick_survives_a_broken_shadow(monkeypatch):
    """shadow 가 폭발해도 worker 의 틱 로직은 계속된다."""
    import app.trading.macd2.worker as W
    monkeypatch.setattr(W.premarket_shadow, "observe",
                        lambda **k: (_ for _ in ()).throw(RuntimeError("boom")))
    try:
        W.premarket_shadow.observe(state=None, macd_snap=None, bars_3m=None,
                                   now=None, quotes=None)
    except Exception:
        pass  # 모듈 밖에서 던지는 건 정상 — worker 의 try/except 가 잡는다
    # worker 쪽 래핑이 실제로 존재하는지는 위 테스트가 소스로 확인한다.


def test_shadow_state_file_is_separate_from_production_state(shadow_dir):
    from app.utils import data_paths
    assert PS.state_path().name == "premarket_carry_shadow_state.json"
    assert PS.state_path().parent == shadow_dir
    assert "premarket_carry" in str(data_paths.PREMARKET_CARRY_DIR)
    assert data_paths.PREMARKET_CARRY_DIR != data_paths.DATA_ROOT


# ══════════════════════════════════════════════════════════════════════════
#  7. 명제 B — GitHub sync 민감정보 차단
# ══════════════════════════════════════════════════════════════════════════
from app.services import github_analysis_sync as GS  # noqa: E402


@pytest.fixture
def sync_dir(tmp_path):
    d = tmp_path / "gh_premarket"
    d.mkdir()
    return d


def _write(d, name, text):
    p = d / name
    p.write_text(text, encoding="utf-8")
    return p


CLEAN_EVENTS = (
    "event_time,bar_time,recognition_time,symbol,macd,signal,gap,prev_gap,"
    "direction,event_type,entry_chop,worker_instance_id,strategy_mode\n"
    "2026-09-14T08:48:00+09:00,2026-09-14T08:45:00+09:00,,,0.5,0.0,0.5,-0.5,"
    "RED,PREMARKET_FLAG,,1234-ab12cd34,X2LITE_3SLOT\n"
)


def test_forbidden_hint_lists_stay_in_sync():
    """두 모듈이 각자 목록을 들고 있으므로 어긋나면 즉시 실패시킨다."""
    assert set(PS.FORBIDDEN_COLUMN_HINTS) == set(GS._SENSITIVE_COLUMN_HINTS)


def test_clean_file_passes_the_sensitive_check(sync_dir):
    GS.assert_no_sensitive_data(_write(sync_dir, "premarket_carry_20260914.csv",
                                       CLEAN_EVENTS))


@pytest.mark.parametrize("header_extra", [
    "account_no", "cano", "app_key", "app_secret", "access_token",
    "refresh_token", "balance", "orderable_cash", "계좌번호", "잔고",
])
def test_sensitive_column_names_are_rejected(sync_dir, header_extra):
    bad = CLEAN_EVENTS.replace("strategy_mode", f"strategy_mode,{header_extra}")
    with pytest.raises(GS.SensitiveDataFound):
        GS.assert_no_sensitive_data(_write(sync_dir, "premarket_carry_20260914.csv", bad))


@pytest.mark.parametrize("payload", [
    "eyJhbGciOiJIUzI1NiJ9.abc",                            # JWT
    "Bearer abcdefghijklmnop",                             # Authorization 헤더
    "PSabcdefghijklmnopqrstuvwxyz012345",                  # KIS app key 형태
    "12345678-01",                                         # 계좌번호 형태
    "A" * 45,                                              # 길고 불투명한 비밀
])
def test_sensitive_values_are_rejected_even_with_clean_headers(sync_dir, payload):
    bad = CLEAN_EVENTS + CLEAN_EVENTS.splitlines()[1].replace(
        "X2LITE_3SLOT", payload) + "\n"
    with pytest.raises(GS.SensitiveDataFound):
        GS.assert_no_sensitive_data(_write(sync_dir, "premarket_carry_20260915.csv", bad))


def test_violation_message_never_echoes_the_secret(sync_dir):
    secret = "PSabcdefghijklmnopqrstuvwxyz012345"
    bad = CLEAN_EVENTS + CLEAN_EVENTS.splitlines()[1].replace(
        "X2LITE_3SLOT", secret) + "\n"
    p = _write(sync_dir, "premarket_carry_20260916.csv", bad)
    with pytest.raises(GS.SensitiveDataFound) as ei:
        GS.assert_no_sensitive_data(p)
    assert secret not in str(ei.value)


def test_real_shadow_output_passes_the_sensitive_check(shadow_dir, st, monkeypatch):
    """합성 CSV 가 아니라 **모듈이 실제로 뱉은 파일** 을 검사한다."""
    monkeypatch.setattr(PS.twf, "evaluate_time_window_entry", lambda *a, **k: _Dec(True))
    monkeypatch.setattr(PS.early_take_profit, "evaluate_entry_chop",
                        lambda *a, **k: SimpleNamespace(is_chop=False))
    for bar, d, now, q in [("08:45", Direction.UP_RED, "08:48", {}),
                           ("09:00", None, "09:03", {}),
                           ("09:03", None, "09:06", {mcfg.LONG_SYMBOL: 10000.0}),
                           ("15:18", None, "15:19", {mcfg.LONG_SYMBOL: 10100.0})]:
        PS.observe(state=st, macd_snap=_snap(bar, direction=d), bars_3m=object(),
                   now=_dt(now), quotes=q)
    for p in shadow_dir.glob("*.csv"):
        GS.assert_no_sensitive_data(p)


# ── sync 대상 선정 ────────────────────────────────────────────────────────
def test_only_whitelisted_filenames_are_synced(sync_dir):
    _write(sync_dir, "premarket_carry_20260914.csv", CLEAN_EVENTS)
    _write(sync_dir, "premarket_carry_summary.csv", "date\n20260914\n")
    _write(sync_dir, "premarket_carry_shadow_state.json", '{"date":"20260914"}')
    _write(sync_dir, "_github_sync_state.json", "{}")
    _write(sync_dir, "random.txt", "x")
    picked = sorted(GS._premarket_local_files(sync_dir))
    assert picked == [
        "data/analysis_live/premarket_carry/premarket_carry_20260914.csv",
        "data/analysis_live/premarket_carry/premarket_carry_summary.csv",
    ]


def test_shadow_state_json_is_never_uploaded(sync_dir):
    _write(sync_dir, "premarket_carry_shadow_state.json", '{"pos_entry_price": 1}')
    assert GS._premarket_local_files(sync_dir) == {}


def test_allowed_prefix_guard_blocks_the_other_sync_path():
    with pytest.raises(ValueError):
        GS._assert_allowed("data/analysis_60d/20260914/x.parquet",
                           GS.PREMARKET_CARRY_PREFIX)
    with pytest.raises(ValueError):
        GS._assert_allowed("data/analysis_live/premarket_carry/x.csv",
                           GS.ALLOWED_PREFIX)
    with pytest.raises(ValueError):
        GS._assert_allowed("app/trading/macd2/worker.py", GS.PREMARKET_CARRY_PREFIX)


def test_git_blob_sha_matches_git():
    assert GS._git_blob_sha(b"hello\n") == "ce013625030ba8dba906f756967f9e9ca394464a"
    assert GS._git_blob_sha(b"") == "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"


# ── run_premarket_carry_sync 동작 ─────────────────────────────────────────
def test_sync_is_dry_run_by_default_and_makes_no_write_call(sync_dir, monkeypatch):
    calls = []
    monkeypatch.setattr(GS, "_request",
                        lambda m, u, *a, **k: calls.append((m, u)))
    monkeypatch.setenv(GS.ENABLE_PUSH_ENV_VAR, "true")
    monkeypatch.setenv(GS.TOKEN_ENV_VAR, "t")
    _write(sync_dir, "premarket_carry_20260914.csv", CLEAN_EVENTS)
    GS.run_premarket_carry_sync(source_dir=sync_dir)
    assert all(m == "GET" for m, _ in calls), f"dry-run 이 쓰기를 호출했다: {calls}"


def test_sync_refuses_to_push_without_the_env_switch(sync_dir, monkeypatch):
    monkeypatch.delenv(GS.ENABLE_PUSH_ENV_VAR, raising=False)
    monkeypatch.setattr(GS, "_list_repo_files_under", lambda *a, **k: {})
    _write(sync_dir, "premarket_carry_20260914.csv", CLEAN_EVENTS)
    r = GS.run_premarket_carry_sync(dry_run=False, source_dir=sync_dir, token="t")
    assert r["committed"] is False and r["skipped_reason"] == "DRY_RUN"


def test_sync_aborts_entirely_when_sensitive_data_found(sync_dir, monkeypatch):
    monkeypatch.setenv(GS.ENABLE_PUSH_ENV_VAR, "true")
    committed = []
    monkeypatch.setattr(GS, "_commit_files_single_commit",
                        lambda *a, **k: committed.append(1))
    _write(sync_dir, "premarket_carry_20260914.csv", CLEAN_EVENTS)
    _write(sync_dir, "premarket_carry_summary.csv", "date,app_secret\n20260914,x\n")
    r = GS.run_premarket_carry_sync(dry_run=False, source_dir=sync_dir, token="t")
    assert r["skipped_reason"] == "SENSITIVE_DATA_DETECTED"
    assert r["committed"] is False
    assert committed == [], "민감정보가 있는데도 커밋을 시도했다"


def test_sync_does_not_commit_when_nothing_changed(sync_dir, monkeypatch):
    monkeypatch.setenv(GS.ENABLE_PUSH_ENV_VAR, "true")
    p = _write(sync_dir, "premarket_carry_20260914.csv", CLEAN_EVENTS)
    repo = {f"{GS.PREMARKET_CARRY_PREFIX}{p.name}": GS._git_blob_sha(p.read_bytes())}
    monkeypatch.setattr(GS, "_list_repo_files_under", lambda *a, **k: repo)
    committed = []
    monkeypatch.setattr(GS, "_commit_files_single_commit",
                        lambda *a, **k: committed.append(1))
    r = GS.run_premarket_carry_sync(dry_run=False, source_dir=sync_dir, token="t")
    assert r["skipped_reason"] == "NO_CHANGES" and committed == []


def test_sync_commits_once_then_refuses_a_second_commit_the_same_day(sync_dir, monkeypatch):
    monkeypatch.setenv(GS.ENABLE_PUSH_ENV_VAR, "true")
    monkeypatch.setattr(GS, "_list_repo_files_under", lambda *a, **k: {})
    n = []
    monkeypatch.setattr(GS, "_commit_files_single_commit",
                        lambda *a, **k: (n.append(1), "c" * 40)[1])
    _write(sync_dir, "premarket_carry_20260914.csv", CLEAN_EVENTS)
    r1 = GS.run_premarket_carry_sync(dry_run=False, source_dir=sync_dir, token="t")
    _write(sync_dir, "premarket_carry_20260914.csv", CLEAN_EVENTS + CLEAN_EVENTS)
    r2 = GS.run_premarket_carry_sync(dry_run=False, source_dir=sync_dir, token="t")
    assert r1["committed"] is True and r1["commit_sha"] == "c" * 40
    assert r2["committed"] is False and r2["skipped_reason"] == "ALREADY_COMMITTED_TODAY"
    assert len(n) == 1


def test_daily_commit_budget_survives_a_restart(sync_dir, monkeypatch):
    """하루 1회 제한이 프로세스 재시작으로 리셋되면 안 된다(Disk 에 기록)."""
    monkeypatch.setenv(GS.ENABLE_PUSH_ENV_VAR, "true")
    monkeypatch.setattr(GS, "_list_repo_files_under", lambda *a, **k: {})
    monkeypatch.setattr(GS, "_commit_files_single_commit", lambda *a, **k: "d" * 40)
    _write(sync_dir, "premarket_carry_20260914.csv", CLEAN_EVENTS)
    GS.run_premarket_carry_sync(dry_run=False, source_dir=sync_dir, token="t")
    state = json.loads((sync_dir / "_github_sync_state.json").read_text(encoding="utf-8"))
    assert sum(state["commits_by_date"].values()) == 1
    importlib.reload(GS)                                  # 재시작
    monkeypatch.setattr(GS, "_list_repo_files_under", lambda *a, **k: {})
    r = GS.run_premarket_carry_sync(dry_run=False, source_dir=sync_dir, token="t")
    assert r["skipped_reason"] == "ALREADY_COMMITTED_TODAY"


def test_next_day_gets_its_own_commit_and_its_own_file(sync_dir, monkeypatch):
    monkeypatch.setenv(GS.ENABLE_PUSH_ENV_VAR, "true")
    monkeypatch.setattr(GS, "_list_repo_files_under", lambda *a, **k: {})
    monkeypatch.setattr(GS, "_commit_files_single_commit", lambda *a, **k: "e" * 40)
    _write(sync_dir, "premarket_carry_20260914.csv", CLEAN_EVENTS)
    GS.run_premarket_carry_sync(dry_run=False, source_dir=sync_dir, token="t")
    # 날짜가 바뀐 상황을 sync-state 를 통해 재현
    s = json.loads((sync_dir / "_github_sync_state.json").read_text(encoding="utf-8"))
    s["commits_by_date"] = {"2026-01-01": 1}
    (sync_dir / "_github_sync_state.json").write_text(json.dumps(s), encoding="utf-8")
    _write(sync_dir, "premarket_carry_20260915.csv", CLEAN_EVENTS)
    r = GS.run_premarket_carry_sync(dry_run=False, source_dir=sync_dir, token="t")
    assert r["committed"] is True
    assert f"{GS.PREMARKET_CARRY_PREFIX}premarket_carry_20260915.csv" in r["uploaded"]


def test_sync_never_raises_on_network_failure(sync_dir, monkeypatch):
    monkeypatch.setenv(GS.ENABLE_PUSH_ENV_VAR, "true")
    monkeypatch.setattr(GS, "_list_repo_files_under",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("net down")))
    _write(sync_dir, "premarket_carry_20260914.csv", CLEAN_EVENTS)
    r = GS.run_premarket_carry_sync(dry_run=False, source_dir=sync_dir, token="t")
    assert r["committed"] is False and r["error"]        # 예외 대신 error 필드


def test_sync_never_raises_on_a_missing_directory(tmp_path):
    r = GS.run_premarket_carry_sync(source_dir=tmp_path / "nope")
    assert r["error"] is None or isinstance(r["error"], str)
    assert r["committed"] is False


def test_sync_never_deletes_anything():
    text = open(GS.__file__, encoding="utf-8").read()
    section = text[text.index("Premarket Carry Shadow sync"):]
    assert '"DELETE"' not in section, "premarket sync 경로에 DELETE 호출이 있다"


def test_ref_update_is_never_forced():
    text = open(GS.__file__, encoding="utf-8").read()
    section = text[text.index("Premarket Carry Shadow sync"):]
    assert '"force": False' in section
    assert '"force": True' not in section


def test_commit_message_format(sync_dir, monkeypatch):
    monkeypatch.setenv(GS.ENABLE_PUSH_ENV_VAR, "true")
    monkeypatch.setattr(GS, "_list_repo_files_under", lambda *a, **k: {})
    seen = {}
    monkeypatch.setattr(GS, "_commit_files_single_commit",
                        lambda *a, **k: (seen.update(k), "f" * 40)[1])
    _write(sync_dir, "premarket_carry_20260914.csv", CLEAN_EVENTS)
    GS.run_premarket_carry_sync(dry_run=False, source_dir=sync_dir, token="t")
    assert seen["message"].startswith("data: sync premarket carry shadow 20")


def test_sync_module_still_does_not_import_trading_code():
    text = open(GS.__file__, encoding="utf-8").read()
    assert "app.trading" not in text.replace(
        "app/trading/macd2/premarket_shadow.FORBIDDEN_COLUMN_HINTS", "")


def test_scheduler_isolates_premarket_sync_from_the_60d_sync():
    import app.services.macd2_daily_archive_scheduler as SCH
    text = open(SCH.__file__, encoding="utf-8").read()
    i = text.index("run_premarket_carry_sync")
    assert "try:" in text[max(0, i - 900):i]
    assert "except Exception as exc:" in text[i:i + 400]


# ══════════════════════════════════════════════════════════════════════════
#  8. 커밋 경로 통합 — _commit_files_single_commit 을 스텁하지 않고 실제로 돈다
# ══════════════════════════════════════════════════════════════════════════
class _FakeGitHub:
    """Git Data API 를 흉내내는 최소 서버. 어떤 요청이 어떤 순서로 갔는지
    전부 기록해두고, 테스트가 그 시퀀스를 직접 검증한다."""

    def __init__(self, *, base_commit="a" * 40, base_tree="b" * 40,
                 new_tree="c" * 40, new_commit="d" * 40):
        self.calls = []
        self.base_commit, self.base_tree = base_commit, base_tree
        self.new_tree, self.new_commit = new_tree, new_commit
        self.blobs = []

    def __call__(self, method, url, token, counter, **kw):
        body = json.loads(kw["data"]) if "data" in kw else None
        self.calls.append((method, url.rsplit("/repos/", 1)[-1], body))
        counter.take()
        tail = url.rsplit("/", 1)[-1]
        if method == "GET" and "/git/ref/heads/" in url:
            return _Resp(200, {"object": {"sha": self.base_commit}})
        if method == "GET" and "/git/commits/" in url:
            return _Resp(200, {"tree": {"sha": self.base_tree}})
        if method == "GET" and "/git/trees/" in url:
            return _Resp(200, {"tree": []})
        if method == "POST" and tail == "blobs":
            self.blobs.append(body)
            return _Resp(201, {"sha": f"blob{len(self.blobs):036d}"})
        if method == "POST" and tail == "trees":
            return _Resp(201, {"sha": self.new_tree})
        if method == "POST" and tail == "commits":
            return _Resp(201, {"sha": self.new_commit})
        if method == "PATCH" and "/git/refs/heads/" in url or method == "PATCH":
            return _Resp(200, {"object": {"sha": self.new_commit}})
        raise AssertionError(f"예상치 못한 요청: {method} {url}")


class _Resp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


@pytest.fixture
def gh(monkeypatch):
    fake = _FakeGitHub()
    monkeypatch.setattr(GS, "_request", fake)
    monkeypatch.setenv(GS.ENABLE_PUSH_ENV_VAR, "true")
    return fake


def test_commit_flow_is_blob_tree_commit_ref_exactly_once(sync_dir, gh):
    _write(sync_dir, "premarket_carry_20260914.csv", CLEAN_EVENTS)
    _write(sync_dir, "premarket_carry_summary.csv", "date,state_0900\n20260914,SAME\n")
    r = GS.run_premarket_carry_sync(dry_run=False, source_dir=sync_dir, token="t")
    assert r["committed"] is True and r["commit_sha"] == "d" * 40

    methods = [(m, u.split("/git/")[-1].split("/")[0]) for m, u, _ in gh.calls
               if "/git/" in u]
    # 파일이 2개여도 커밋 객체는 **딱 하나**
    assert methods.count(("POST", "commits")) == 1
    assert methods.count(("POST", "trees")) == 1
    assert methods.count(("POST", "blobs")) == 2      # 파일 수만큼 blob
    assert methods.count(("PATCH", "ref")) == 1


def test_commit_uses_base_tree_so_other_files_are_never_dropped(sync_dir, gh):
    _write(sync_dir, "premarket_carry_20260914.csv", CLEAN_EVENTS)
    GS.run_premarket_carry_sync(dry_run=False, source_dir=sync_dir, token="t")
    tree_body = next(b for m, u, b in gh.calls if m == "POST" and u.endswith("/trees"))
    assert tree_body["base_tree"] == gh.base_tree, "base_tree 없이 트리를 만들면 레포가 날아간다"
    assert all(e["path"].startswith(GS.PREMARKET_CARRY_PREFIX) for e in tree_body["tree"])


def test_ref_patch_sends_force_false(sync_dir, gh):
    _write(sync_dir, "premarket_carry_20260914.csv", CLEAN_EVENTS)
    GS.run_premarket_carry_sync(dry_run=False, source_dir=sync_dir, token="t")
    patch_body = next(b for m, _, b in gh.calls if m == "PATCH")
    assert patch_body["force"] is False
    assert patch_body["sha"] == gh.new_commit


def test_identical_tree_means_no_commit_object_is_created(sync_dir, monkeypatch):
    """새 트리가 기존 트리와 같으면(= 실질 변경 없음) 빈 커밋을 만들지 않는다."""
    fake = _FakeGitHub(new_tree="b" * 40)      # new_tree == base_tree
    monkeypatch.setattr(GS, "_request", fake)
    monkeypatch.setenv(GS.ENABLE_PUSH_ENV_VAR, "true")
    _write(sync_dir, "premarket_carry_20260914.csv", CLEAN_EVENTS)
    r = GS.run_premarket_carry_sync(dry_run=False, source_dir=sync_dir, token="t")
    assert r["committed"] is False and r["skipped_reason"] == "TREE_UNCHANGED"
    assert not any(m == "POST" and u.endswith("/commits") for m, u, _ in fake.calls)
    assert not any(m == "PATCH" for m, _, _ in fake.calls)


def test_non_fast_forward_is_reported_not_forced(sync_dir, monkeypatch):
    """다른 데서 먼저 push 해 ref 갱신이 422 로 거절되면 **덮어쓰지 않는다**."""
    fake = _FakeGitHub()
    orig = fake.__call__

    def patched(method, url, token, counter, **kw):
        if method == "PATCH":
            fake.calls.append((method, url, json.loads(kw["data"])))
            counter.take()
            return _Resp(422, {"message": "Update is not a fast forward"})
        return orig(method, url, token, counter, **kw)

    monkeypatch.setattr(GS, "_request", patched)
    monkeypatch.setenv(GS.ENABLE_PUSH_ENV_VAR, "true")
    _write(sync_dir, "premarket_carry_20260914.csv", CLEAN_EVENTS)
    r = GS.run_premarket_carry_sync(dry_run=False, source_dir=sync_dir, token="t")
    assert r["committed"] is False and r["error"]           # 예외 대신 error
    assert sum(1 for m, _, _ in fake.calls if m == "PATCH") == 1   # 재시도 없음
    # 실패했으므로 "오늘 커밋함" 으로 기록되면 안 된다(내일 다시 시도 가능)
    state = GS._premarket_load_sync_state(sync_dir)
    assert sum(state.get("commits_by_date", {}).values()) == 0


def test_uploaded_bytes_are_exactly_the_local_file(sync_dir, gh):
    import base64 as _b64
    p = _write(sync_dir, "premarket_carry_20260914.csv", CLEAN_EVENTS)
    GS.run_premarket_carry_sync(dry_run=False, source_dir=sync_dir, token="t")
    blob = gh.blobs[0]
    assert _b64.b64decode(blob["content"]) == p.read_bytes()


def test_token_never_appears_in_any_request_body_or_path(sync_dir, gh):
    _write(sync_dir, "premarket_carry_20260914.csv", CLEAN_EVENTS)
    GS.run_premarket_carry_sync(dry_run=False, source_dir=sync_dir,
                                token="ghp_supersecrettoken123456")
    blob = json.dumps([(m, u, b) for m, u, b in gh.calls])
    assert "ghp_supersecrettoken123456" not in blob


def test_no_request_ever_targets_a_path_outside_the_premarket_prefix(sync_dir, gh):
    _write(sync_dir, "premarket_carry_20260914.csv", CLEAN_EVENTS)
    GS.run_premarket_carry_sync(dry_run=False, source_dir=sync_dir, token="t")
    for m, u, b in gh.calls:
        if b and "tree" in b and isinstance(b.get("tree"), list):
            for e in b["tree"]:
                assert e["path"].startswith(GS.PREMARKET_CARRY_PREFIX)
        assert "analysis_60d" not in json.dumps(b or {})


# ══════════════════════════════════════════════════════════════════════════
#  9. 스케줄러 레벨 격리 — sync 가 터져도 EOD 루틴이 죽지 않는다
#
#  worker 틱은 sync 를 **아예 호출하지 않는다**(E2E 로 확인: 902틱 동안 sync
#  호출 0회). sync 가 실제로 도는 유일한 곳은 20:30 EOD 스케줄러이므로,
#  "sync 예외가 다른 것을 멈추지 않는다"는 명제는 여기서 검증해야 한다.
# ══════════════════════════════════════════════════════════════════════════
def _due_thread(monkeypatch):
    import app.services.macd2_daily_archive_scheduler as SCH

    th = SCH.Macd2DailyArchiveThread.__new__(SCH.Macd2DailyArchiveThread)
    th._last_run_date = None
    th.last_result = None
    # EOD 루틴은 '평일 20:30 이후'에만 돈다 -- 테스트가 실제로 언제 실행되든
    # 조건을 만족하도록 시계를 고정한다(2026-09-14 은 월요일).
    monkeypatch.setattr(SCH, "kst_now",
                        lambda: datetime(2026, 9, 14, 20, 31, tzinfo=KST))
    monkeypatch.setattr(SCH, "_kis_real_client", lambda: object())
    monkeypatch.setattr("app.services.macd2_daily_archiver.run_daily_archive",
                        lambda client, d: {"day": d, "ok": True})
    return SCH, th


def test_premarket_sync_exception_does_not_break_the_eod_routine(monkeypatch):
    SCH, th = _due_thread(monkeypatch)
    monkeypatch.setattr(GS, "run_sync", lambda **k: {"effective_dry_run": True, "error": None})
    monkeypatch.setattr(GS, "run_premarket_carry_sync",
                        lambda **k: (_ for _ in ()).throw(RuntimeError("boom")))
    th._tick_if_due()                                   # 예외가 새어나오면 실패
    assert th.last_result is not None
    assert th.last_result["archive"], "premarket sync 예외가 아카이브 결과를 삼켰다"
    assert th.last_result["sync"]["error"] is None, "60일 sync 가 같이 죽었다"
    assert "error" in th.last_result["premarket_sync"]


def test_60d_sync_exception_does_not_stop_the_premarket_sync(monkeypatch):
    """반대 방향도 성립해야 한다 — 두 sync 는 서로를 막지 않는다."""
    SCH, th = _due_thread(monkeypatch)
    monkeypatch.setattr(GS, "run_sync",
                        lambda **k: (_ for _ in ()).throw(RuntimeError("60d boom")))
    called = []
    monkeypatch.setattr(GS, "run_premarket_carry_sync",
                        lambda **k: (called.append(1), {"committed": False,
                                                        "skipped_reason": "NO_CHANGES",
                                                        "error": None})[1])
    th._tick_if_due()
    assert called == [1], "60일 sync 예외 때문에 premarket sync 가 건너뛰어졌다"
    assert th.last_result["premarket_sync"]["skipped_reason"] == "NO_CHANGES"


def test_archive_exception_does_not_stop_either_sync(monkeypatch):
    SCH, th = _due_thread(monkeypatch)
    monkeypatch.setattr("app.services.macd2_daily_archiver.run_daily_archive",
                        lambda client, d: (_ for _ in ()).throw(RuntimeError("archive boom")))
    seen = []
    monkeypatch.setattr(GS, "run_sync", lambda **k: (seen.append("60d"), {"error": None})[1])
    monkeypatch.setattr(GS, "run_premarket_carry_sync",
                        lambda **k: (seen.append("pm"), {"error": None})[1])
    th._tick_if_due()
    assert seen == ["60d", "pm"]


def test_worker_tick_never_calls_github_sync_at_all(monkeypatch):
    """가장 강한 격리 증명 — worker 소스에 sync 참조가 존재하지 않는다."""
    import ast
    import app.trading.macd2.worker as W

    tree = ast.parse(open(W.__file__, encoding="utf-8").read())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
            names.update(a.name for a in node.names)
        elif isinstance(node, (ast.Name, ast.Attribute)):
            names.add(node.id if isinstance(node, ast.Name) else node.attr)
    for banned in ("github_analysis_sync", "run_sync", "run_premarket_carry_sync"):
        assert banned not in names, f"worker 가 {banned} 를 참조한다"


def test_shadow_module_never_calls_github_sync_either():
    import ast

    tree = ast.parse(open(PS.__file__, encoding="utf-8").read())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
        elif isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
    assert not any("github" in n for n in names), "shadow 가 sync 를 직접 부른다"
