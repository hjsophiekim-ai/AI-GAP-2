"""확정 MACD 탐지는 어떤 경로에서도 스킵되지 않는다 (2026-09-16).

불변식
------
    "방향이 바뀌었는데 탐지가 없다" = 0

크로스오버는 봉 하나짜리 일회성 사건이라 지나가면 다시 만들 수 없다. 게다가
``evaluate_macd_crossover`` 는 ``last_detected_direction`` 과 같은 방향을
억제하므로, 한 번 놓치면 **다음 같은 방향 플래그까지 연쇄로 삼켜진다**
(2026-08-31 / 2026-09-11 실사고가 정확히 그 형태였다).

이 파일은 "주문을 냈거나 tick 을 조기 종료하는" 모든 경로에서 탐지가 살아
있는지를 고정한다. 검사 대상 경로:

  1. 09:03 예약매수와 같은 tick
  2. reconcile 블록 tick
  3. restart / catch-up tick
  4. 예약매수 보호구간(09:03~09:10) tick
  5. 평범한 tick (대조군)

핵심 사실 두 가지도 소스로 못박는다:
  - 탐지(_advance_confirmed_primary)는 예약매수 분기보다 **먼저** 호출된다
  - 예약매수 체결 경로는 탐지 상태(last_detected_direction / latest_primary_
    flag / last_confirmed_bar_ts)를 건드리지 않는다
"""
from __future__ import annotations

import inspect
from datetime import datetime, timedelta

import pytest

from app.trading.macd2 import config, worker
from app.trading.macd2.models import Direction, RuntimeState
from app.trading.macd2.signal_engine import evaluate_macd_crossover

KST = config.KST


class _Snap:
    def __init__(self, prev: float, curr: float, bar_dt: datetime):
        self.previous_diff = float(prev)
        self.current_diff = float(curr)
        self.macd = 0.0
        self.signal = 0.0
        self.hist_last3 = [prev, prev, curr]
        self.bar_dt = bar_dt
        self.relation = None


def _state() -> RuntimeState:
    s = RuntimeState()
    s.session_date = "20260916"
    return s


# ══════════════════════════════════════════════════════════════════════════
# A. 순서 — 탐지가 주문 경로보다 먼저다
# ══════════════════════════════════════════════════════════════════════════
def test_detection_runs_before_the_scheduled_entry_branch():
    """예약매수가 탐지를 선점하면 그 봉의 플래그가 통째로 사라진다."""
    src = inspect.getsource(worker.run_once)
    det = src.index("_advance_confirmed_primary(state, macd_snap, now)")
    sched = src.index("_scheduled_entry_should_fire(state, now)")
    assert det < sched, "탐지가 예약매수 분기보다 뒤에 있다 -- 선점 위험"


def test_every_executing_early_return_preserves_the_flag():
    """주문을 내고 조기 return 하는 분기는 전부 _preserve_confirmed_flag 를
    호출해야 한다. 호출 없이 return 하면 그 tick 의 새 플래그가 유실된다."""
    src = inspect.getsource(worker.run_once)
    for marker in ("_scheduled_entry_should_fire", "_premarket_carry_should_fire"):
        i = src.index(marker)
        window = src[i:i + 1400]
        assert "_preserve_confirmed_flag" in window, f"{marker} 분기에 플래그 보존이 없다"


def test_scheduled_entry_execution_does_not_touch_detection_state():
    """체결 경로가 탐지 상태를 갱신하면 다음 봉 판정이 오염된다."""
    src = inspect.getsource(worker._apply_switch_outcome)
    for field in ("last_detected_direction", "latest_primary_flag",
                  "last_confirmed_bar_ts"):
        assert f"state.{field} =" not in src, f"_apply_switch_outcome 이 {field} 를 바꾼다"
    src2 = inspect.getsource(worker._execute_scheduled_entry)
    for field in ("last_detected_direction", "latest_primary_flag",
                  "last_confirmed_bar_ts"):
        assert f"state.{field} =" not in src2, f"_execute_scheduled_entry 가 {field} 를 바꾼다"


# ══════════════════════════════════════════════════════════════════════════
# B. 불변식 — 방향이 바뀌면 반드시 탐지된다
# ══════════════════════════════════════════════════════════════════════════
#: 2026-09-16 실데이터: 08:00 봉과 09:00 봉의 실제 prev/curr diff.
#: (9/15+9/16 연속 시리즈로 production 함수를 재생해 얻은 값)
REAL_0916 = [
    ("08:00", 339.1939, -821.0274, Direction.DOWN_BLUE),
    ("09:00", -475.8940, 69.8620, Direction.UP_RED),
]


def test_todays_real_bars_produce_both_flags():
    """오늘 실데이터 replay: 08:00 BLUE 와 09:00 RED 가 **둘 다** 나와야 한다.

    사고 당시 09:00 RED 는 원장에 없었다. 억제(same-direction dedup) 때문이
    아니라는 것을 여기서 못박는다 -- 직전 방향이 DOWN_BLUE 이므로 RED 는
    억제 대상이 아니다."""
    last = None
    got = []
    for label, prev, curr, expect in REAL_0916:
        hh, mm = (int(x) for x in label.split(":"))
        snap = _Snap(prev, curr, datetime(2026, 9, 16, hh, mm, tzinfo=KST))
        d = evaluate_macd_crossover(snap, last)
        assert d == expect, f"{label} 에서 {expect.value} 가 아니라 {d.value}"
        last = d
        got.append((label, d.value))
    assert got == [("08:00", "DOWN_BLUE"), ("09:00", "UP_RED")]


def test_red_after_blue_is_never_suppressed():
    """방향 전환은 절대 억제 대상이 아니다 (억제는 '같은 방향 반복'만)."""
    snap = _Snap(-475.894, 69.862, datetime(2026, 9, 16, 9, 0, tzinfo=KST))
    assert evaluate_macd_crossover(snap, Direction.DOWN_BLUE) == Direction.UP_RED
    assert evaluate_macd_crossover(snap, None) == Direction.UP_RED
    # 같은 방향 반복만 HOLD
    assert evaluate_macd_crossover(snap, Direction.UP_RED) == Direction.HOLD


@pytest.mark.parametrize(
    "tick_kind",
    ["평범한 tick", "예약매수 tick", "reconcile 블록 tick",
     "restart/catch-up tick", "보호구간 tick"],
)
def test_direction_change_is_always_detected_regardless_of_tick_kind(tick_kind):
    """_advance_confirmed_primary 는 tick 종류와 무관하게 같은 답을 낸다.

    이 함수는 reconcile/restart/예약매수 상태를 **아예 읽지 않는다** --
    그것이 "탐지는 주문 건강도에 의존하지 않는다"(2026-08-31 fix)의 핵심이다.
    상태를 어떻게 흔들어도 09:00 RED 는 나와야 한다."""
    s = _state()
    s.last_detected_direction = Direction.DOWN_BLUE
    if tick_kind == "예약매수 tick":
        s.scheduled_entry_armed_direction = Direction.DOWN_BLUE
        s.scheduled_entry_armed_at = datetime(2026, 9, 16, 8, 5, tzinfo=KST).isoformat()
    elif tick_kind == "reconcile 블록 tick":
        s.order_block_reason = "POSITION_MISMATCH"
    elif tick_kind == "restart/catch-up tick":
        s.session_date = None
        s.worker_instance_id = "restarted"
    elif tick_kind == "보호구간 tick":
        s.scheduled_entry_protected = True

    now = datetime(2026, 9, 16, 9, 3, 30, tzinfo=KST)
    snap = _Snap(-475.894, 69.862, datetime(2026, 9, 16, 9, 0, tzinfo=KST))
    direction = worker._advance_confirmed_primary(s, snap, now)

    assert direction == Direction.UP_RED, f"{tick_kind} 에서 RED 가 탐지되지 않았다"
    assert s.last_detected_direction == Direction.UP_RED
    assert s.latest_primary_flag == Direction.UP_RED
    assert s.latest_primary_signal_id, "플래그 signal_id 가 남지 않았다"


def test_detection_does_not_read_order_health_fields():
    """탐지 함수가 주문/복구 상태를 읽으면 '주문이 막히면 탐지도 막히는'
    2026-08-31 사고 형태로 되돌아간다."""
    src = inspect.getsource(worker._advance_confirmed_primary)
    for field in ("order_block_reason", "scheduled_entry_armed_direction",
                  "scheduled_entry_protected", "position"):
        assert field not in src, f"탐지가 {field} 를 참조한다"


def test_same_bar_is_evaluated_exactly_once_but_never_zero_times():
    """같은 봉 재평가는 HOLD(중복 방지)지만, **첫 평가는 반드시 탐지**된다."""
    s = _state()
    s.last_detected_direction = Direction.DOWN_BLUE
    now = datetime(2026, 9, 16, 9, 3, 30, tzinfo=KST)
    snap = _Snap(-475.894, 69.862, datetime(2026, 9, 16, 9, 0, tzinfo=KST))

    assert worker._advance_confirmed_primary(s, snap, now) == Direction.UP_RED
    assert worker._advance_confirmed_primary(s, snap, now) == Direction.HOLD
    # 두 번째가 HOLD 여도 탐지 결과 자체는 남아 있어야 한다
    assert s.last_detected_direction == Direction.UP_RED
    assert s.latest_primary_flag == Direction.UP_RED


def test_a_bar_that_has_not_closed_yet_is_not_detected_early():
    """아직 안 닫힌 봉을 미리 읽으면 미래정보다 -- 그건 스킵이 맞다."""
    s = _state()
    s.last_detected_direction = Direction.DOWN_BLUE
    snap = _Snap(-475.894, 69.862, datetime(2026, 9, 16, 9, 0, tzinfo=KST))
    too_early = datetime(2026, 9, 16, 9, 2, 59, tzinfo=KST)   # 09:03 이전
    assert worker._advance_confirmed_primary(s, snap, too_early) == Direction.HOLD
    # 닫힌 뒤에는 정상 탐지된다 (영구 유실이 아니다)
    s.last_confirmed_bar_ts = None
    ok = datetime(2026, 9, 16, 9, 3, 1, tzinfo=KST)
    assert worker._advance_confirmed_primary(s, snap, ok) == Direction.UP_RED


# ══════════════════════════════════════════════════════════════════════════
# C. 불완전봉 -> 다음 tick 재평가 (2026-09-16 사고 추정 경로)
# ══════════════════════════════════════════════════════════════════════════
def test_a_bar_dropped_as_incomplete_is_re_evaluated_when_it_completes():
    """09:03:02 tick 에서 09:02 1분봉이 아직 안 와 09:00봉이 탈락했다면,
    다음 tick 에서 완성된 09:00봉을 **반드시** 재평가해야 한다.

    핵심은 last_confirmed_bar_ts 가 '봉 시각' 으로 찍힌다는 점이다 -- 탈락한
    봉은 애초에 macd_snap 이 되지 못하므로 도장이 찍히지 않고, 완성된 뒤
    처음 등장할 때 정상적으로 평가된다. 이 성질이 깨지면 그 플래그는
    영구 유실된다."""
    s = _state()
    s.last_detected_direction = Direction.DOWN_BLUE

    # tick 1 (09:03:02): 09:00봉은 결측으로 탈락 -> 직전 완성봉(08:45)이 snap
    t1 = datetime(2026, 9, 16, 9, 3, 2, tzinfo=KST)
    snap_0845 = _Snap(-966.7433, -475.8940, datetime(2026, 9, 16, 8, 45, tzinfo=KST))
    assert worker._advance_confirmed_primary(s, snap_0845, t1) == Direction.HOLD
    assert s.last_confirmed_bar_ts == snap_0845.bar_dt.isoformat()
    assert s.last_detected_direction == Direction.DOWN_BLUE, "탈락봉이 방향을 바꿨다"

    # tick 2 (09:03:12): 09:02 1분봉 도착 -> 09:00봉 완성 -> 반드시 탐지
    t2 = datetime(2026, 9, 16, 9, 3, 12, tzinfo=KST)
    snap_0900 = _Snap(-475.8940, 69.8620, datetime(2026, 9, 16, 9, 0, tzinfo=KST))
    assert worker._advance_confirmed_primary(s, snap_0900, t2) == Direction.UP_RED, \
        "완성된 09:00봉이 재평가되지 않았다 -- 플래그 영구 유실"
    assert s.last_detected_direction == Direction.UP_RED
    assert s.latest_primary_flag == Direction.UP_RED


def test_dropping_a_bar_never_stamps_it_as_evaluated():
    """탈락한 봉이 last_confirmed_bar_ts 를 선점하면 완성 후 재평가가
    막힌다 -- 그런 일이 없어야 한다."""
    s = _state()
    s.last_detected_direction = Direction.DOWN_BLUE
    t1 = datetime(2026, 9, 16, 9, 3, 2, tzinfo=KST)
    worker._advance_confirmed_primary(
        s, _Snap(-966.74, -475.89, datetime(2026, 9, 16, 8, 45, tzinfo=KST)), t1)
    assert s.last_confirmed_bar_ts != datetime(2026, 9, 16, 9, 0, tzinfo=KST).isoformat()


# ══════════════════════════════════════════════════════════════════════════
# D. 보호구간은 '주문만' 막는다 — FLAG EVENT / T+3 후보는 반드시 남는다
# ══════════════════════════════════════════════════════════════════════════
def test_protection_branch_preserves_the_flag_and_its_candidate():
    """보호구간 분기가 원장 행만 남기고 T+3 후보를 안 만들면, 보호가 풀릴 때
    해소할 후보가 없어 플래그가 통째로 소멸한다. reconcile 블록이 쓰는 것과
    같은 헬퍼(_propagate_confirmed_flag_without_orders)를 써야 한다."""
    src = inspect.getsource(worker.run_once)
    i = src.index("SCHEDULED_ENTRY_PROTECTION_ACTIVE")
    window = src[max(0, i - 1800):i + 400]
    assert "_propagate_confirmed_flag_without_orders" in window, \
        "보호구간이 플래그를 원장 행만으로 삼키고 있다(T+3 후보 없음)"


def test_the_propagate_helper_never_places_an_order():
    """보호구간에서 쓰는 헬퍼가 주문을 낼 수 있으면 보호가 무의미해진다."""
    import ast, textwrap
    tree = ast.parse(textwrap.dedent(
        inspect.getsource(worker._propagate_confirmed_flag_without_orders)))
    fn = tree.body[0]
    if (fn.body and isinstance(fn.body[0], ast.Expr)
            and isinstance(fn.body[0].value, ast.Constant)):
        fn.body = fn.body[1:]            # docstring 제외 -- 실제 코드만 본다
    code = ast.unparse(fn)
    for forbidden in ("execute_signal(", "buy_market(", "sell_market(",
                      "broker.buy", "broker.sell"):
        assert forbidden not in code, f"플래그 보존 헬퍼가 {forbidden} 를 호출한다"
    # 브로커 인자 자체를 받지 않는다는 것이 가장 강한 보증이다
    assert "broker" not in [a.arg for a in fn.args.args + fn.args.kwonlyargs],         "플래그 보존 헬퍼가 broker 를 인자로 받는다"


def test_protection_still_blocks_the_opposite_order_itself():
    """플래그는 남기되 주문/스위치는 보호구간 동안 계속 막혀야 한다."""
    src = inspect.getsource(worker.run_once)
    i = src.index("if scheduled_protected and pending_opposes_held:")
    assert "pass" in src[i:i + 200], "보호구간에서 반대 pending 주문이 실행된다"
