"""2026-09-21 실사고 회귀 테스트 — stale H50 이 새 포지션을 즉시 청산한 사건.

사고 요약
---------
  09:57  UP_RED T+3 승인 -> 749주 매수
  10:51  반대 DOWN_BLUE T+3 확정 -> H50 이 청산을 **보류**(HOLD 시작)
   ~     사용자가 KIS 앱에서 **수동 전량매도** (시스템 밖 청산)
         -> reconcile 이 flat 을 채택했지만 H50 상태는 아무도 지우지 않았다
  12:57  UP_RED T+3 승인 -> 740주 매수
  12:57  매수 **3초 뒤**, 126분 전에 시작된 HOLD 가 MAX_HOLD(60분) 만료로
         깨어나 새 포지션을 전량 청산. 거래원장에는 "반대신호"로 기록됐지만
         실제 MACD 반대 크로스오버는 없었다(청산 코드가 만든 합성 방향).

이 파일은 그 경로를 그대로 재현하고, 수정 후에는 **매도 주문이 0건**임을
증명한다. 전부 순수 상태 조작 + production 함수 호출이며 브로커/네트워크를
건드리지 않는다.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.trading.macd2 import (
    config,
    n1_adaptive,
    peak_protection,
    small_whipsaw_hold,
    state_store,
    worker as wk,
)
from app.trading.macd2.models import Direction, PositionSnapshot, RuntimeState

KST = config.KST


def _state() -> RuntimeState:
    s = state_store.default_state()
    s.auto_trade_on = True
    s.budget = 10_000_000.0
    s.time_window_n1_filter_enabled = True
    s.c1_peak_protection_enabled = True
    return s


def _pos(qty=749, price=11945.0, at=None) -> PositionSnapshot:
    return PositionSnapshot(symbol=config.LONG_SYMBOL, quantity=qty,
                            avg_price=price, entry_at=at or datetime.now(KST))


def _arm_h50(s: RuntimeState, *, started_at: datetime, epoch: int | None = None):
    """10:51 처럼 H50 HOLD 가 걸린 상태를 만든다."""
    s.h50_hold_active = True
    s.h50_hold_started_at = started_at.isoformat()
    s.h50_original_direction = Direction.UP_RED.value
    s.h50_trend_break_count = 0
    s.h50_last_checked_bar_ts = started_at.isoformat()
    s.h50_owner_epoch = int(s.position_epoch if epoch is None else epoch)


class _Snap:
    def __init__(self, bar_dt):
        self.bar_dt = bar_dt
        self.hist = 0.0


class _Broker:
    """매도 주문이 한 번이라도 나가면 잡아낸다."""

    def __init__(self, positions=()):
        self.sell_calls = []
        self._positions = list(positions)

    def get_positions(self):
        return list(self._positions)

    def sell(self, *a, **kw):           # pragma: no cover - 호출되면 테스트 실패
        self.sell_calls.append((a, kw))
        raise AssertionError("이 시나리오에서 매도 주문이 나가면 안 된다")


# ════════════════════════════════════════════════════════════════════════════
# TEST A — 오늘 사고 그대로
# ════════════════════════════════════════════════════════════════════════════
def test_A_manual_close_then_new_entry_does_not_trigger_stale_h50_exit():
    s = _state()
    day = datetime.now(KST).replace(hour=9, minute=57, second=0, microsecond=0)

    # 09:57 진입
    wk._begin_position_epoch(s, reason="NEW_ENTRY")
    s.position = _pos(at=day)
    s.time_window_position_active = True
    first_epoch = s.position_epoch

    # 10:51 H50 HOLD
    _arm_h50(s, started_at=day.replace(hour=10, minute=51))
    assert small_whipsaw_hold.is_holding(s)

    # 사용자가 KIS 에서 수동 전량매도 -> 브로커 flat 확인 (RECOVERED_TO_FLAT)
    s.position = None
    wk._clear_position_scoped_state(s, reason=wk.RECOVERED_TO_FLAT)
    assert not small_whipsaw_hold.is_holding(s), "시스템 밖 청산에도 HOLD 가 끝나야 한다"
    assert s.h50_owner_epoch == 0

    # 12:57 신규 진입
    wk._begin_position_epoch(s, reason="NEW_ENTRY")
    s.position = _pos(qty=740, price=12085.0, at=day.replace(hour=12, minute=57))
    s.time_window_position_active = True
    assert s.position_epoch != first_epoch

    # 진입 직후 첫 완성봉 — 과거 HOLD 가 깨어나면 안 된다
    broker = _Broker()
    out = wk._advance_h50_hold(
        broker=broker, state=s, now=day.replace(hour=12, minute=57, second=7),
        macd_snap=_Snap(day.replace(hour=12, minute=54)), bars_3m=None,
        position=s.position, result=wk.TickResult(),
    )
    assert out is None
    assert broker.sell_calls == [], "새 포지션이 stale HOLD 로 청산되면 안 된다"


# ════════════════════════════════════════════════════════════════════════════
# TEST B — RECOVERED_TO_FLAT 는 position-scoped 상태를 전부 끝낸다
# ════════════════════════════════════════════════════════════════════════════
def test_B_recovered_to_flat_clears_every_position_scoped_state():
    s = _state()
    wk._begin_position_epoch(s, reason="NEW_ENTRY")
    s.position = _pos()
    s.time_window_position_active = True
    s.time_window_active_mode = "N1_3SLOT"
    s.time_window_tp1_done = True
    s.time_window_peak_net_return = 3.2
    s.time_window_entry_chop = True
    s.early_tp_peak_net_return = 1.8
    _arm_h50(s, started_at=datetime.now(KST) - timedelta(minutes=90))
    peak_protection.note_peak(s, 5.5)
    s.c1_armed = True
    s.c1_owner_epoch = s.position_epoch
    s.whipsaw_watch_active = True
    s.whipsaw_watch_direction = Direction.DOWN_BLUE.value
    s.n1_last_eval_bar_ts = datetime.now(KST).isoformat()

    wk._clear_position_scoped_state(s, reason=wk.RECOVERED_TO_FLAT)

    assert s.time_window_position_active is False
    assert s.time_window_active_mode is None
    assert s.time_window_tp1_done is False
    assert s.time_window_peak_net_return == 0.0
    assert s.time_window_entry_chop is False
    assert s.early_tp_peak_net_return == 0.0
    assert small_whipsaw_hold.is_holding(s) is False
    assert s.h50_hold_started_at is None
    assert s.c1_armed is False
    assert s.c1_peak_net_return == 0.0
    assert s.whipsaw_watch_active is False
    assert s.n1_last_eval_bar_ts is None
    assert s.h50_owner_epoch == 0 and s.c1_owner_epoch == 0


# ════════════════════════════════════════════════════════════════════════════
# TEST C — 소유권이 다른 stale 상태는 clear 만 하고 매도하지 않는다
# ════════════════════════════════════════════════════════════════════════════
def test_C_stale_owner_epoch_clears_without_any_sell_order():
    s = _state()
    s.position_epoch = 7                       # 지금 포지션은 7번째
    s.position = _pos(at=datetime.now(KST))
    _arm_h50(s, started_at=datetime.now(KST) - timedelta(minutes=200), epoch=3)  # 3번째의 잔재
    broker = _Broker()

    out = wk._advance_h50_hold(
        broker=broker, state=s, now=datetime.now(KST), macd_snap=_Snap(datetime.now(KST)),
        bars_3m=None, position=s.position, result=wk.TickResult(),
    )
    assert out is None
    assert broker.sell_calls == []
    assert small_whipsaw_hold.is_holding(s) is False
    assert s.last_h50_stale_discarded_at is not None, "폐기 사실이 진단에 남아야 한다"


def test_C2_fallback_guard_started_before_position_entry():
    """epoch 이 우연히 일치해도 HOLD 시작이 진입보다 앞서면 stale 이다."""
    s = _state()
    s.position_epoch = 4
    entry_at = datetime.now(KST)
    s.position = _pos(at=entry_at)
    _arm_h50(s, started_at=entry_at - timedelta(minutes=90), epoch=4)
    broker = _Broker()

    out = wk._advance_h50_hold(
        broker=broker, state=s, now=entry_at + timedelta(seconds=7),
        macd_snap=_Snap(entry_at), bars_3m=None, position=s.position,
        result=wk.TickResult(),
    )
    assert out is None
    assert broker.sell_calls == []
    assert small_whipsaw_hold.is_holding(s) is False


# ════════════════════════════════════════════════════════════════════════════
# TEST F — 정상 HOLD 는 기존 동작을 그대로 유지한다
# ════════════════════════════════════════════════════════════════════════════
def test_F_same_position_hold_still_evaluates_release_normally(monkeypatch):
    s = _state()
    wk._begin_position_epoch(s, reason="NEW_ENTRY")
    entry_at = datetime.now(KST)
    s.position = _pos(at=entry_at)
    _arm_h50(s, started_at=entry_at + timedelta(minutes=1))   # 진입 이후에 시작 = 정상
    seen = {}

    def _fake_release(bars, held_dir, started, now, trend_break_count=0):
        seen["called"] = True
        return small_whipsaw_hold.ReleaseDecision(False, "TREND_UP", 0, 5.0, "HOLDING")

    monkeypatch.setattr(small_whipsaw_hold, "evaluate_release", _fake_release)
    broker = _Broker()
    out = wk._advance_h50_hold(
        broker=broker, state=s, now=entry_at + timedelta(minutes=6),
        macd_snap=_Snap(entry_at + timedelta(minutes=5)), bars_3m=None,
        position=s.position, result=wk.TickResult(),
    )
    assert seen.get("called") is True, "정상 HOLD 는 기존 해제판정을 그대로 탄다"
    assert out is None and broker.sell_calls == []
    assert small_whipsaw_hold.is_holding(s) is True


# ════════════════════════════════════════════════════════════════════════════
# TEST G — 재시작으로 디스크에서 복원돼도 새 포지션에 적용되지 않는다
# ════════════════════════════════════════════════════════════════════════════
def test_G_stale_h50_restored_from_disk_is_not_applied_to_new_position(tmp_path):
    s = _state()
    s.position_epoch = 2
    _arm_h50(s, started_at=datetime.now(KST) - timedelta(minutes=300), epoch=2)
    saved = state_store.save_state(s)
    reloaded = state_store.load_state()
    assert reloaded.h50_hold_active is True, "H50 상태는 디스크에 남는다(전제)"
    assert reloaded.h50_owner_epoch == 2

    # 재시작 후 새 포지션이 열리면 epoch 이 올라간다
    wk._begin_position_epoch(reloaded, reason="NEW_ENTRY")
    assert reloaded.h50_hold_active is False, "새 포지션 시작이 이전 HOLD 를 끝낸다"
    reloaded.position = _pos(at=datetime.now(KST))

    broker = _Broker()
    out = wk._advance_h50_hold(
        broker=broker, state=reloaded, now=datetime.now(KST),
        macd_snap=_Snap(datetime.now(KST)), bars_3m=None,
        position=reloaded.position, result=wk.TickResult(),
    )
    assert out is None and broker.sell_calls == []


# ════════════════════════════════════════════════════════════════════════════
# 청산 사유가 '반대신호' 로 뭉뚱그려지지 않는다
# ════════════════════════════════════════════════════════════════════════════
def test_h50_exit_reason_is_not_opposite_signal():
    src = __import__("inspect").getsource(wk._advance_h50_hold)
    assert "config.EXIT_H50_MAX_HOLD" in src
    assert "config.EXIT_H50_TREND_BREAK" in src
    assert config.EXIT_H50_MAX_HOLD != config.EXIT_OPPOSITE_SIGNAL
    assert config.EXIT_H50_TREND_BREAK != config.EXIT_OPPOSITE_SIGNAL
    helper = __import__("inspect").getsource(wk._execute_reversal_exit_only_for_filtered_entry)
    assert "exit_reason or config.EXIT_OPPOSITE_SIGNAL" in helper


# ════════════════════════════════════════════════════════════════════════════
# TEST D / E — reconcile 이 건강하지 않으면 신규 BUY 는 0건
# ════════════════════════════════════════════════════════════════════════════
class _MD:
    def refresh_quotes(self, symbols=None):
        return None

    def get_quote(self, symbol):
        return None

    def get_history_df(self):
        return None


def _run_entry_dispatch(monkeypatch, reconcile_result, *, position=None):
    """_execute_or_wait 를 신규진입(플랫)으로 한 번 태운다. 주문 호출을 센다."""
    s = _state()
    s.position = position
    calls = {"execute_signal": 0, "execute_exit": 0}

    monkeypatch.setattr(wk, "reconcile_position_state",
                        lambda *a, **k: reconcile_result)
    monkeypatch.setattr(wk.order_executor, "execute_signal",
                        lambda *a, **k: calls.__setitem__("execute_signal",
                                                          calls["execute_signal"] + 1))
    monkeypatch.setattr(wk.order_executor, "execute_exit",
                        lambda *a, **k: calls.__setitem__("execute_exit",
                                                          calls["execute_exit"] + 1))
    now = datetime.now(KST)
    try:
        out = wk._execute_or_wait(
            broker=_Broker(), market_data=_MD(), state=s, now=now,
            macd_snap=_Snap(now), direction=Direction.UP_RED,
            signal_id="TEST_ENTRY", signal_type="INITIAL", position=position,
            result=wk.TickResult(),
        )
    except AttributeError:
        # 게이트를 통과한 뒤 시세 스텁이 모자라 터지는 것은 이 테스트의 관심사가
        # 아니다 — 여기서 중요한 것은 '게이트가 막았는가' 와 '주문이 나갔는가'뿐.
        out = None
    return out, calls, s


@pytest.mark.parametrize("bad", ["POSITION_DATA_ERROR", "POSITION_MISMATCH"])
def test_D_position_data_error_blocks_new_buy(monkeypatch, bad):
    out, calls, s = _run_entry_dispatch(monkeypatch, bad)
    assert out is None
    assert calls["execute_signal"] == 0, "브로커 보유수량을 모르면 신규매수 금지"
    assert s.order_block_reason == bad


def test_E_unknown_reconcile_result_blocks_new_buy(monkeypatch):
    """허용목록에 없는 결과는 전부 차단된다(기본값이 '차단')."""
    out, calls, s = _run_entry_dispatch(monkeypatch, "SOME_FUTURE_RESULT")
    assert out is None
    assert calls["execute_signal"] == 0
    assert s.order_block_reason == wk.ENTRY_BLOCKED_RECONCILE_UNHEALTHY


def test_E2_match_flat_is_allowed(monkeypatch):
    """정상(플랫 확인)에서는 게이트가 막지 않는다 — 기존 동작 보존."""
    out, calls, s = _run_entry_dispatch(monkeypatch, wk.MATCH_FLAT)
    assert s.order_block_reason != wk.ENTRY_BLOCKED_RECONCILE_UNHEALTHY


def test_E3_gate_never_blocks_an_exit(monkeypatch):
    """보유 중(청산/스위치)에는 이 게이트가 적용되지 않는다 —
    매도를 막으면 보유 포지션이 무방비가 된다."""
    held = _pos()
    out, calls, s = _run_entry_dispatch(monkeypatch, "SOME_FUTURE_RESULT", position=held)
    assert s.order_block_reason != wk.ENTRY_BLOCKED_RECONCILE_UNHEALTHY


# ════════════════════════════════════════════════════════════════════════════
# UI 수동 전량매도 — 브로커를 권위로 삼는다
# ════════════════════════════════════════════════════════════════════════════
def test_manual_exit_uses_broker_holdings_when_local_state_is_flat():
    """2026-09-21: 로컬이 flat 이라고 잘못 알고 있어도 실제 보유분을 판다.

    이 버튼이 `state.position` 만 보고 NO_POSITION_TO_SELL 로 조용히 실패한 것이
    사용자가 KIS 에서 수동매도하게 만든 원인이었다."""
    import inspect
    from app.trading.macd2 import service as sv
    src = inspect.getsource(sv.Macd2Service.manual_exit)
    assert "self._broker.get_positions()" in src, "브로커 보유분을 먼저 조회해야 한다"
    assert "WORKER_NOT_RUNNING" not in src, "워커가 죽어도 매도는 가능해야 한다"
    assert "AUTO_TRADE_OFF" not in src, "자동매매 off 여도 보유분은 팔 수 있어야 한다"
    # state.position 은 진입가 보정 용도로만 남는다
    i = src.index("get_positions()")
    assert "NO_POSITION_TO_SELL" in src[i:], "정말 아무것도 없을 때만 실패한다"


# ════════════════════════════════════════════════════════════════════════════
# 수동(외부) 매도도 원장에 남는다
# ════════════════════════════════════════════════════════════════════════════
def test_external_manual_sell_is_written_to_both_ledgers(monkeypatch):
    """사용자가 KIS 앱에서 직접 판 물량도 체결원장/신호원장에 기록된다.

    2026-09-21 사고에서 수동매도가 거래원장에 보이지 않아 추적이 끊겼다.
    reconcile 이 '브로커 flat' 을 확인한 순간 백필 행을 남기는 경로가
    실제로 호출되는지 확인한다."""
    from app.trading.macd2 import ledger as lg

    calls = {"exec": [], "signal": []}
    monkeypatch.setattr(lg, "append_reconcile_backfill_sell",
                        lambda **kw: calls["exec"].append(kw) or True)
    monkeypatch.setattr(lg, "append_signal", lambda row: calls["signal"].append(row) or True)

    class _B:
        def get_quote(self, symbol):
            return 11900.0

    s = _state()
    wk._record_reconcile_discovered_sell(
        _B(), s, symbol=config.LONG_SYMBOL, sold_qty=749, entry_price=11945.0,
        position_before=749, position_after=0, now=datetime.now(KST),
        exit_reason=wk.RECOVERED_TO_FLAT,
    )
    assert len(calls["exec"]) == 1, "체결원장에 매도 백필 행이 남아야 한다"
    assert calls["exec"][0]["quantity"] == 749
    assert calls["exec"][0]["exit_price"] == 11900.0, "현재가로 체결가를 채운다"
    assert calls["exec"][0]["exit_reason"] == wk.RECOVERED_TO_FLAT
    assert len(calls["signal"]) == 1, "신호원장에도 추적 행이 남아야 한다"
    assert calls["signal"][0]["signal_type"] == "RECONCILE_DISCOVERED_SELL"


def test_recovered_to_flat_path_calls_the_backfill_before_clearing():
    """브로커 flat 확인 분기가 '백필 기록 -> 상태 정리' 순서를 지킨다."""
    import inspect
    src = inspect.getsource(wk.reconcile_position_state)
    i = src.index("_record_reconcile_discovered_sell(")
    j = src.index("_clear_position_scoped_state(state, reason=RECOVERED_TO_FLAT)")
    assert i < j, "원장 기록이 상태 정리보다 먼저 와야 한다"


def test_external_sell_reason_has_a_ui_label():
    """거래원장 화면에서 '청산 확인(정합화)' 로 보인다 — 빈칸/코드값이 아니다."""
    import io as _io
    src = _io.open("app/ui/pages/11_MACD_자동매매2.py", encoding="utf-8").read()
    assert '"RECOVERED_TO_FLAT": "청산 확인(정합화)"' in src
