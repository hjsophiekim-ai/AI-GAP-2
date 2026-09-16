"""제로크로스 불변식 + 2026-09-16 실서버 값 replay.

실서버 bar_ledger (2026-09-16)
------------------------------
    08:45  gap=-475.893972  prev_gap=-966.743318  dir=HOLD   prev_state=DOWN_BLUE
    09:00  gap=+557.525896  prev_gap=-292.112502  dir=HOLD   prev_state=DOWN_BLUE  <-- 오류
    09:03  gap=+1389.045533 prev_gap=+621.343560  dir=HOLD   prev_state=DOWN_BLUE

09:00 은 prev_gap<0 -> gap>0 이고 직전 방향이 DOWN_BLUE 이므로 **UP_RED 여야
한다**. 이 파일은 (1) 판정 함수 자체는 정상임을 실서버 값으로 못박고,
(2) 한 번 놓친 방향이 이후 같은 방향 플래그를 연쇄로 삼키는 구조
(09:57 BLUE 누락)를 재현한다.

MACD 계산식/3분봉 resample/T+3/quality/TEG/H50/X2-lite/W1a 는 건드리지 않았다.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.trading.macd2 import config, worker
from app.trading.macd2.models import Direction, RuntimeState
from app.trading.macd2.signal_engine import evaluate_macd_crossover

KST = config.KST
DAY = datetime(2026, 9, 16, tzinfo=KST)


class _Snap:
    """bar_ledger._resolve_gap 이 읽는 것과 같은 두 필드를 가진 스냅샷."""

    def __init__(self, prev_gap: float, gap: float, bar_dt: datetime):
        self.previous_diff = float(prev_gap)
        self.current_diff = float(gap)
        self.macd = 0.0
        self.signal = 0.0
        self.hist_last3 = [prev_gap, prev_gap, gap]
        self.bar_dt = bar_dt
        self.relation = None
        self.completed_3m_count = 60


# ══════════════════════════════════════════════════════════════════════════
# A. 불변식 — 사용자 지정 계약
# ══════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("prev_gap,gap", [
    (-292.112502, 557.525896),      # 실서버 09:00
    (-475.893972, 69.862000),       # 로컬 replay 값
    (-0.0001, 0.0001), (-1000.0, 1.0), (-1.0, 1000.0), (0.0, 0.5),
])
def test_blue_state_plus_upward_zero_cross_must_be_up_red(prev_gap, gap):
    """prev_gap <= 0 AND gap > 0 AND prev_state == DOWN_BLUE -> 반드시 UP_RED."""
    snap = _Snap(prev_gap, gap, DAY.replace(hour=9))
    d = evaluate_macd_crossover(snap, Direction.DOWN_BLUE)
    assert d == Direction.UP_RED, f"prev={prev_gap} gap={gap} 에서 {d} 가 나왔다"
    assert bool(d.value in ("UP_RED", "DOWN_BLUE")) is True, "confirmed_cross 가 False"


@pytest.mark.parametrize("prev_gap,gap", [
    (292.112502, -557.525896), (0.0001, -0.0001),
    (1000.0, -1.0), (1.0, -1000.0), (0.0, -0.5),
])
def test_red_state_plus_downward_zero_cross_must_be_down_blue(prev_gap, gap):
    """대칭: prev_gap >= 0 AND gap < 0 AND prev_state == UP_RED -> DOWN_BLUE."""
    snap = _Snap(prev_gap, gap, DAY.replace(hour=9, minute=57))
    d = evaluate_macd_crossover(snap, Direction.UP_RED)
    assert d == Direction.DOWN_BLUE, f"prev={prev_gap} gap={gap} 에서 {d} 가 나왔다"


def test_the_only_way_to_get_hold_on_a_real_cross_is_a_same_direction_state():
    """제로크로스가 있는데 HOLD 가 나오는 경우는 **직전 방향이 같을 때뿐**이다.

    실서버 09:00 행(prev_state=DOWN_BLUE, dir=HOLD)은 이 성질과 모순이다."""
    snap = _Snap(-292.112502, 557.525896, DAY.replace(hour=9))
    assert evaluate_macd_crossover(snap, Direction.DOWN_BLUE) == Direction.UP_RED
    assert evaluate_macd_crossover(snap, None) == Direction.UP_RED
    assert evaluate_macd_crossover(snap, Direction.UP_RED) == Direction.HOLD  # 유일한 HOLD


# ══════════════════════════════════════════════════════════════════════════
# B. session-open 첫 완성봉에서도 동일해야 한다
# ══════════════════════════════════════════════════════════════════════════
def test_session_open_first_bar_has_no_special_suppression():
    """09:00(정규장 첫 완성봉)이라고 억제하는 게이트가 있으면 안 된다."""
    state = RuntimeState()
    state.session_date = "20260916"
    state.last_detected_direction = Direction.DOWN_BLUE
    snap = _Snap(-292.112502, 557.525896, DAY.replace(hour=9, minute=0))
    now = DAY.replace(hour=9, minute=3, second=30)

    d = worker._advance_confirmed_primary(state, snap, now)

    assert d == Direction.UP_RED, "session-open 첫 봉이 억제됐다"
    assert state.last_detected_direction == Direction.UP_RED
    assert state.latest_primary_flag == Direction.UP_RED
    assert state.latest_primary_signal_id


def test_no_premarket_to_regular_boundary_gate_exists():
    """08:50~09:00 경계를 이유로 cross 를 막는 코드가 없는지 소스로 확인."""
    import ast, inspect, textwrap

    def _code_only(fn):
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        node = tree.body[0]
        if (node.body and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)):
            node.body = node.body[1:]           # docstring 제외
        return ast.unparse(node)

    src = _code_only(worker._advance_confirmed_primary)
    for token in ("SESSION_OPEN", "premarket", "PREMARKET"):
        assert token not in src, f"탐지 함수가 {token} 을 참조한다 -- 경계 게이트 의심"
    eng = _code_only(evaluate_macd_crossover)
    for token in ("SESSION_OPEN", "premarket", "bar_dt"):
        assert token not in eng, f"판정 함수가 {token} 을 참조한다"


# ══════════════════════════════════════════════════════════════════════════
# C. 2026-09-16 실서버 값 replay
# ══════════════════════════════════════════════════════════════════════════
REAL_ROWS = [
    ("08:45", -966.743318, -475.893972),
    ("09:00", -292.112502, 557.525896),
    ("09:03", 621.343560, 1389.045533),
]


def test_replay_of_the_real_server_rows_produces_exactly_one_red():
    """08:45 BLUE state -> 09:00 UP_RED 1회 -> 09:03 중복 0건."""
    state = RuntimeState()
    state.session_date = "20260916"
    state.last_detected_direction = Direction.DOWN_BLUE

    detected = []
    for label, prev_gap, gap in REAL_ROWS:
        hh, mm = (int(x) for x in label.split(":"))
        snap = _Snap(prev_gap, gap, DAY.replace(hour=hh, minute=mm))
        now = snap.bar_dt + timedelta(minutes=3, seconds=30)
        d = worker._advance_confirmed_primary(state, snap, now)
        if d != Direction.HOLD:
            detected.append((label, d.value))

    assert detected == [("09:00", "UP_RED")], (
        f"09:00 UP_RED 1건만 나와야 한다 -- 실제: {detected}")
    assert state.last_detected_direction == Direction.UP_RED
    assert state.latest_primary_flag == Direction.UP_RED


# ══════════════════════════════════════════════════════════════════════════
# D. 연쇄 유실 — 09:57 BLUE 누락의 구조
# ══════════════════════════════════════════════════════════════════════════
def test_a_missed_flag_poisons_every_later_same_direction_flag():
    """09:00 RED 를 놓쳐 상태가 DOWN_BLUE 로 고착되면, 09:57 의 진짜 BLUE 가
    '같은 방향 반복' 으로 억제된다 -- 오늘 09:57 BLUE 누락의 구조다.

    이 테스트는 **결함을 재현**하는 것이 목적이다: 상태가 오염되면 이후
    플래그가 사라진다는 것을 고정한다."""
    # (1) 정상: 09:00 RED 를 잡았으면 09:57 BLUE 가 정상 탐지된다
    ok = RuntimeState(); ok.session_date = "20260916"
    ok.last_detected_direction = Direction.DOWN_BLUE
    worker._advance_confirmed_primary(
        ok, _Snap(-292.112502, 557.525896, DAY.replace(hour=9)),
        DAY.replace(hour=9, minute=3, second=30))
    assert ok.last_detected_direction == Direction.UP_RED
    d_ok = worker._advance_confirmed_primary(
        ok, _Snap(300.0, -400.0, DAY.replace(hour=9, minute=57)),
        DAY.replace(hour=10, minute=0, second=30))
    assert d_ok == Direction.DOWN_BLUE, "정상 상태인데 09:57 BLUE 가 안 나왔다"

    # (2) 오염: 09:00 RED 를 놓쳐 DOWN_BLUE 고착 -> 09:57 BLUE 가 삼켜진다
    bad = RuntimeState(); bad.session_date = "20260916"
    bad.last_detected_direction = Direction.DOWN_BLUE          # 고착된 상태
    d_bad = worker._advance_confirmed_primary(
        bad, _Snap(300.0, -400.0, DAY.replace(hour=9, minute=57)),
        DAY.replace(hour=10, minute=0, second=30))
    assert d_bad == Direction.HOLD, (
        "이 테스트의 전제(같은 방향 반복 억제)가 성립하지 않는다")


def test_stored_direction_can_contradict_the_bar_sign():
    """오늘 09:03 행의 모순을 그대로 표현한다: gap=+1389(>0) 인데 저장된
    방향은 DOWN_BLUE. BLUE 상태는 gap<0 을 뜻하므로 **증명 가능하게 stale**
    이다. 현재 엔진은 이 모순을 감지하지도, 복구하지도 않는다."""
    state = RuntimeState(); state.session_date = "20260916"
    state.last_detected_direction = Direction.DOWN_BLUE
    snap = _Snap(621.343560, 1389.045533, DAY.replace(hour=9, minute=3))
    d = worker._advance_confirmed_primary(
        state, snap, DAY.replace(hour=9, minute=6, second=30))
    assert d == Direction.HOLD                     # 크로스가 없으니 HOLD 는 맞다
    # 그러나 저장된 방향은 현재 gap 부호와 모순인 채로 남는다
    assert snap.current_diff > 0
    assert state.last_detected_direction == Direction.DOWN_BLUE, (
        "현행 동작 고정 -- 모순이 남는다는 사실 자체를 기록한다")


# ══════════════════════════════════════════════════════════════════════════
# E. restart catch-up 과 live 는 같은 입력을 써야 한다 (2026-09-16 1·2순위)
# ══════════════════════════════════════════════════════════════════════════
def test_catchup_uses_the_same_completed_bar_filter_as_live():
    """catch-up 이 불완전 봉을 EMA 에 넣으면 live 와 다른 macd/gap 이 나온다.

    EMA 는 누적이라 한 봉 차이가 이후 모든 봉을 바꾸고, catch-up 은 그 판정으로
    last_confirmed_bar_ts 에 도장을 찍어 live 의 재평가를 영구히 막는다."""
    import ast, inspect, textwrap
    src = inspect.getsource(worker.initialize_strategy_session)
    tree = ast.parse(textwrap.dedent(src))
    code = ast.unparse(tree.body[0])
    i = code.index("resample_completed_3m")
    window = code[i:i + 400]
    assert "filter_complete_3m_bars" in window, (
        "initialize_strategy_session 이 완성봉 필터 없이 프레임을 만든다 -- "
        "live 와 다른 MACD 로 detection state 를 덮어쓴다")


def test_every_frame_builder_in_worker_applies_the_filter():
    """worker 안에서 resample 한 뒤 필터를 안 거는 곳이 하나도 없어야 한다."""
    from pathlib import Path as _Path
    src = _Path(worker.__file__).read_text(encoding="utf-8")
    lines = src.split("\n")
    offenders = []
    for n, ln in enumerate(lines):
        if "resample_completed_3m(" in ln and not ln.strip().startswith("#"):
            nxt = " ".join(lines[n + 1:n + 16])
            if "filter_complete_3m_bars" not in nxt:
                offenders.append(n + 1)
    assert not offenders, f"완성봉 필터가 빠진 resample 호출: line {offenders}"


def test_catchup_stamping_cannot_hide_a_bar_from_live_forever():
    """catch-up 이 도장을 찍은 봉은 live 가 다시 평가하지 않는다 -- 그래서
    catch-up 의 판정이 반드시 live 와 같아야 한다는 것이 이 구조의 전제다.
    현행 동작을 고정한다(도장 자체는 중복 방지를 위해 필요하다)."""
    state = RuntimeState(); state.session_date = "20260916"
    state.last_detected_direction = Direction.DOWN_BLUE
    snap = _Snap(-292.112502, 557.525896, DAY.replace(hour=9))
    state.last_confirmed_bar_ts = snap.bar_dt.isoformat()      # catch-up 이 찍은 도장
    d = worker._advance_confirmed_primary(
        state, snap, DAY.replace(hour=9, minute=3, second=30))
    assert d == Direction.HOLD, "중복 방지 도장이 동작하지 않는다"
    assert state.last_detected_direction == Direction.DOWN_BLUE
