"""청산이 실행된 tick 에서도 확정 플래그 탐지/기록은 수행된다 (2026-09-16).

구멍
----
``run_once`` 의 보유포지션 리스크관리 분기는 청산을 실행하면
``_advance_confirmed_primary`` **이전에** return 했다. 그래서 청산이 발동한
tick 에 마침 새 크로스오버가 확정되면 그 플래그가 통째로 사라졌다 --
크로스오버는 봉 단위 일회성 사건이라 다시 만들 수 없고, 같은-방향 억제가
다음 플래그까지 연쇄로 삼킨다. reconcile 블록 경로는 2026-08-31 에 정확히
같은 이유로 이미 보강됐는데(탐지는 주문 건강도에 의존하면 안 된다) 이
분기만 빠져 있었다.

계약
----
  * 청산 동작(가격/사유/수량/슬롯)은 **한 값도 바뀌지 않는다**
  * 청산 여부와 무관하게 그 tick 의 완성봉 crossover 는 탐지·기록된다
  * 이 보강 경로는 **주문을 내지 않는다**
  * 같은 봉 재평가 시 중복 0
"""
from __future__ import annotations

import ast
import inspect
import textwrap

import pytest

from app.trading.macd2 import worker


def _code_of(fn) -> str:
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    node = tree.body[0]
    if node.body and isinstance(node.body[0], ast.Expr) and isinstance(
            node.body[0].value, ast.Constant):
        node.body = node.body[1:]
    return ast.unparse(node)


def _run_once_src() -> str:
    return inspect.getsource(worker.run_once)


# ══════════════════════════════════════════════════════════════════════════
# A. 청산 tick 에서 탐지가 수행되는가
# ══════════════════════════════════════════════════════════════════════════
def test_exit_branch_detects_before_returning():
    """청산 분기가 _advance_confirmed_primary 를 호출한 뒤에 return 해야 한다."""
    src = _run_once_src()
    i = src.index("_advance_held_position_risk_management(")
    # 이 분기의 return 까지 구간
    j = src.index("return result", i)
    window = src[i:j]
    assert "_advance_confirmed_primary" in window, \
        "청산 tick 이 탐지 없이 반환한다 -- 그 봉의 플래그가 사라진다"


def test_exit_branch_also_replays_unevaluated_bars():
    """청산 tick 도 늦게 완성된 미평가 봉을 따라잡아야 한다."""
    src = _run_once_src()
    i = src.index("_advance_held_position_risk_management(")
    j = src.index("return result", i)
    assert "_replay_unevaluated_completed_bars" in src[i:j], \
        "청산 tick 에서 미평가 봉 따라잡기가 빠졌다"


def test_exit_branch_records_the_flag():
    """탐지만 하고 원장에 안 남기면 반쪽이다."""
    src = _run_once_src()
    i = src.index("_advance_held_position_risk_management(")
    j = src.index("return result", i)
    assert "_propagate_confirmed_flag_without_orders" in src[i:j], \
        "청산 tick 에서 플래그가 원장/T+3 후보로 전파되지 않는다"


# ══════════════════════════════════════════════════════════════════════════
# B. 청산 tick 의 보강이 주문을 내지 않는가
# ══════════════════════════════════════════════════════════════════════════
def test_the_preservation_helper_never_orders():
    code = _code_of(worker._propagate_confirmed_flag_without_orders)
    for forbidden in ("execute_signal(", "buy_market(", "sell_market("):
        assert forbidden not in code, f"플래그 전파 헬퍼가 {forbidden} 를 부른다"
    tree = ast.parse(textwrap.dedent(
        inspect.getsource(worker._propagate_confirmed_flag_without_orders)))
    args = [a.arg for a in tree.body[0].args.args + tree.body[0].args.kwonlyargs]
    assert "broker" not in args, "플래그 전파 헬퍼가 broker 를 인자로 받는다"


def test_replay_helper_never_orders():
    code = _code_of(worker._replay_unevaluated_completed_bars)
    for forbidden in ("execute_signal", "buy_market", "sell_market",
                      "broker", "_set_pending_signal", "order_executor"):
        assert forbidden not in code, f"복원 경로가 {forbidden} 를 건드린다"


# ══════════════════════════════════════════════════════════════════════════
# C. 청산 동작 자체는 그대로인가
# ══════════════════════════════════════════════════════════════════════════
def test_the_exit_itself_runs_before_any_detection_work():
    """보강은 청산 **뒤에** 붙어야 한다 -- 청산 판단/실행에 영향을 주면 안 된다."""
    src = _run_once_src()
    call = src.index("_advance_held_position_risk_management(")
    det = src.index("_advance_confirmed_primary", call)
    assert call < det, "탐지가 청산보다 먼저 끼어들었다"


def test_risk_management_signature_is_untouched():
    """청산 함수 자체는 손대지 않았다 -- 가격/사유/수량 로직 불변."""
    args = [a.arg for a in ast.parse(textwrap.dedent(
        inspect.getsource(worker._advance_held_position_risk_management))
    ).body[0].args.kwonlyargs]
    assert args == ["broker", "state", "market_data", "now", "quotes", "pos", "result"], \
        f"청산 함수 시그니처가 바뀌었다: {args}"


def test_preservation_failure_cannot_break_an_exit_tick():
    """이미 체결된 청산 tick 을 기록 실패가 예외로 만들면 안 된다."""
    src = _run_once_src()
    i = src.index("_advance_held_position_risk_management(")
    j = src.index("return result", i)
    window = src[i:j]
    assert "try:" in window and "except Exception:" in window, \
        "청산 tick 보강이 try/except 로 감싸여 있지 않다"


# ══════════════════════════════════════════════════════════════════════════
# D. 청산만 있고 crossover 가 없으면 아무 것도 기록하지 않는가
# ══════════════════════════════════════════════════════════════════════════
def test_no_flag_row_when_there_is_no_crossover_on_the_exit_tick():
    """HOLD 면 전파하지 않는다 -- 없던 플래그를 만들면 안 된다."""
    src = _run_once_src()
    i = src.index("_advance_held_position_risk_management(")
    j = src.index("return result", i)
    window = src[i:j]
    assert "!= Direction.HOLD" in window, \
        "청산 tick 에서 HOLD 도 플래그로 기록될 수 있다"
