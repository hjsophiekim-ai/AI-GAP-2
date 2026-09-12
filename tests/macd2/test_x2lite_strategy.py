"""X2-lite — 독립 전략 프리셋 검증 (2026-09-12 사용자 요청).

X2-lite 는 TW TEG 3-SLOT(=F, 배포 전략)과 **진입이 100% 동일**하고 청산
파라미터만 다른 자매 전략이다. 이 파일은 두 축을 증명한다:

  A. **F 회귀 0건** — X2-lite 를 켜지 않은 모든 모드(TW2/TEGv2/TW2 3-SLOT/
     TW TEG 3-SLOT/MU_MACD·입양 포지션)의 청산 파라미터가 한 값도 바뀌지
     않는다. worker 의 TP2 조건식을 헬퍼로 옮긴 것이 유일한 실질 위험이라
     그 반환값을 모드별로 직접 잠근다.
  B. **X2-lite 사양** — 진입 parity / TP1 20% / TP2 5% / trailing 2.8% /
     오전 SL -1.3% / ETP 1.5→1.0 / 반대신호 / 강제청산 / 슬롯 카운터 /
     왕복거래 표시.

경계값(2.79 vs 2.80 등)은 순수 래더 함수로 정확히 찍고, 시나리오는 전부
**실제 worker.run_once() + FakeBroker + 격리 tmp 원장**(conftest autouse)으로
end-to-end 검증한다. 하네스는 기존 test_tw2_3slot_worker_regression /
test_early_take_profit_worker 의 것을 그대로 재사용한다(중복 인프라 없음).
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.trading.macd2 import (
    config,
    early_take_profit as etp,
    ledger,
    state_store,
    time_window_3slot as tw3,
    time_window_position_manager as pm,
    worker,
)
from app.trading.macd2.models import Direction, PositionSnapshot, RuntimeState
from app.trading.macd2.service import Macd2Service
from app.trading.macd2.worker import run_once
from app.ui import macd2_summary
from tests.macd2.fake_broker import FakeBroker
from tests.macd2.test_tw2_3slot_worker_regression import (
    _1m_frame,
    _BOOTSTRAP_NOW,
    _PRIOR_DAY,
    _SESSION_START_NOW,
    _approved,
    _rejected,
    _patch_common,
    _prime_3slot_pending,
    _quality,
    _sine_1m_closes,
    _teg,
)
from tests.macd2.test_early_take_profit_worker import _market, _seed_completed_bar

KST = config.KST
MODE = tw3.MODE_X2LITE_3SLOT
F_MODE = tw3.MODE_TWF_3SLOT


# ════════════════════════════════════════════════════════════════════════════
# A. F 회귀 — 기존 모드의 청산 파라미터가 한 값도 바뀌지 않는다
# ════════════════════════════════════════════════════════════════════════════
def test_existing_modes_exit_overrides_are_byte_identical():
    """X2-lite 도입으로 늘어난 2개 키는 기존 모드에서 항상 None 이어야 한다."""
    for mode in (None, "TW2", "TEG", "TEGv2", tw3.MODE_TW2_3SLOT, "MU_MACD"):
        ov = tw3.exit_overrides(mode)
        assert ov["stop_loss_pct_override"] is None, mode
        assert ov["after_tp1_stop_pct_override"] is None, mode
        assert ov["afternoon_tp_pct_override"] is None, mode
        assert ov["tp1_sell_ratio_override"] is None, mode
        assert ov["trailing_stop_pct_override"] is None, mode

    f = tw3.exit_overrides(F_MODE)
    assert f["stop_loss_pct_override"] == pytest.approx(config.TWF_MORNING_STOP_LOSS * 100.0)
    assert f["after_tp1_stop_pct_override"] == pytest.approx(config.TWF_MORNING_AFTER_TP1_STOP * 100.0)
    assert f["afternoon_tp_pct_override"] == pytest.approx(config.TWF_AFTERNOON_TP * 100.0)
    # F 는 TP1 분할/trailing 을 override 하지 않는다 -- 모듈 상수 그대로.
    assert f["tp1_sell_ratio_override"] is None
    assert f["trailing_stop_pct_override"] is None


def test_morning_tp2_override_helper_reproduces_the_old_inline_condition():
    """worker 에서 헬퍼로 옮긴 TP2 조건식이 기존 5개 모드에서 동일값을 준다.

    옛 식: (TW2_MORNING_TP2*100) if mode in ("TW2","TEG","TEGv2") + MODES_3SLOT
    — 당시 MODES_3SLOT 은 (TW2_3SLOT, TWF_3SLOT) 두 개였다.
    """
    legacy_modes = ("TW2", "TEG", "TEGv2", tw3.MODE_TW2_3SLOT, tw3.MODE_TWF_3SLOT)
    for mode in legacy_modes:
        assert tw3.morning_tp2_pct_override(mode) == pytest.approx(config.TW2_MORNING_TP2 * 100.0), mode
    for mode in (None, "", "MU_MACD", "NO_FILTER"):
        assert tw3.morning_tp2_pct_override(mode) is None, mode
    # X2-lite 만 5.0%
    assert tw3.morning_tp2_pct_override(MODE) == pytest.approx(config.X2LITE_MORNING_TP2 * 100.0)
    assert tw3.morning_tp2_pct_override(MODE) == pytest.approx(5.0)


def test_ladder_without_overrides_is_unchanged():
    """override 인자를 주지 않으면 모듈 상수 그대로 — MU_MACD 경로 보호."""
    d = pm.evaluate_morning_position(net_return_pct=3.0, tp1_done=False)
    assert d.exit_reason == config.EXIT_TW_TP1_PARTIAL
    assert d.sell_fraction == pytest.approx(pm.MORNING_TP1_SELL_RATIO)
    assert d.sell_fraction == pytest.approx(0.50)

    d2 = pm.evaluate_morning_position(net_return_pct=2.0, tp1_done=True, peak_net_return=4.0)
    assert d2.exit_reason == config.EXIT_TW_TRAILING_STOP  # 모듈상수 trailing 2.0%
    d3 = pm.evaluate_morning_position(net_return_pct=2.01, tp1_done=True, peak_net_return=4.0)
    assert d3.exit_reason is None

    t = pm.evaluate_take_profit_immediate(session="MORNING", net_return_pct=3.0, tp1_done=False)
    assert t.sell_fraction == pytest.approx(0.50)


def test_f_mode_ladder_thresholds_unchanged_end_to_end():
    """F(TW TEG 3-SLOT) 의 래더 임계값이 전부 기존 값 그대로인지 한 번에 확인."""
    # TP1 50%
    d = _f(3.0)
    assert d.exit_reason == config.EXIT_TW_TP1_PARTIAL
    assert d.sell_fraction == pytest.approx(0.50)
    # TP2 6.0% (5.9 는 아직 아님)
    assert _f(5.9, tp1_done=True).exit_reason != config.EXIT_TW_TP2_FULL
    assert _f(6.0, tp1_done=True).exit_reason == config.EXIT_TW_TP2_FULL
    # 오전 손절 -1.4% (부동소수: config 값은 -1.4000000000000001)
    assert tw3.exit_overrides(F_MODE)["stop_loss_pct_override"] == pytest.approx(-1.4)
    assert _f(-1.39).exit_reason is None
    assert _f(-1.41).exit_reason == config.EXIT_TW_STOP_LOSS
    # trailing 2.0%
    assert _f(2.01, tp1_done=True, peak=4.0).exit_reason is None
    assert _f(2.00, tp1_done=True, peak=4.0).exit_reason == config.EXIT_TW_TRAILING_STOP


def test_etp_thresholds_unchanged_for_every_non_x2lite_mode():
    for flag in ("time_window_3slot_filter_enabled", "time_window_twf_filter_enabled"):
        s = state_store.default_state()
        setattr(s, flag, True)
        assert etp.thresholds(s) == (config.EARLY_TP_TRIGGER_PCT, config.EARLY_TP_FLOOR_PCT)
    s = state_store.default_state()
    assert etp.thresholds(s) == (config.EARLY_TP_TRIGGER_PCT, config.EARLY_TP_FLOOR_PCT)
    # evaluate() 도 인자를 안 주면 기존 config 값을 쓴다.
    assert etp.evaluate(entry_chop=True, peak_net_return_pct=1.5, net_return_pct=0.8).exit_reason == config.EXIT_EARLY_TAKE_PROFIT
    assert etp.evaluate(entry_chop=True, peak_net_return_pct=1.5, net_return_pct=0.81).exit_reason is None


# ════════════════════════════════════════════════════════════════════════════
# B-0. X2-lite 사양이 연구값과 정확히 일치하는가
# ════════════════════════════════════════════════════════════════════════════
def test_x2lite_params_match_the_researched_spec():
    ov = tw3.exit_overrides(MODE)
    assert ov["tp1_sell_ratio_override"] == pytest.approx(0.20)
    assert ov["trailing_stop_pct_override"] == pytest.approx(2.8)
    assert ov["stop_loss_pct_override"] == pytest.approx(-1.30)
    assert ov["after_tp1_stop_pct_override"] == pytest.approx(2.0)
    assert ov["afternoon_tp_pct_override"] == pytest.approx(3.0)
    assert tw3.morning_tp2_pct_override(MODE) == pytest.approx(5.0)
    # TP1 트리거와 trailing 트리거는 F 와 동일 (override 없음)
    assert pm.MORNING_TP1_PCT == pytest.approx(3.0)
    assert pm.MORNING_TRAILING_TRIGGER_PCT == pytest.approx(3.5)
    assert config.X2LITE_EARLY_TP_TRIGGER_PCT == pytest.approx(1.5)
    assert config.X2LITE_EARLY_TP_FLOOR_PCT == pytest.approx(1.0)


def test_x2lite_shares_the_tw_teg_entry_rule():
    """진입이 F 와 100% 동일 — CHOP 후보 TEGv2 추가요구 규칙까지 공유한다."""
    assert tw3.requires_chop_teg_gate(MODE) is True
    assert tw3.requires_chop_teg_gate(F_MODE) is True
    assert tw3.requires_chop_teg_gate(tw3.MODE_TW2_3SLOT) is False
    assert MODE in tw3.MODES_3SLOT


# ── 순수 래더 경계값 ────────────────────────────────────────────────────────
def _ladder(mode, net, *, tp1_done=False, peak=0.0, session="MORNING"):
    return pm.evaluate_position(
        session=session, net_return_pct=net, tp1_done=tp1_done, peak_net_return=peak,
        tp2_pct_override=tw3.morning_tp2_pct_override(mode), **tw3.exit_overrides(mode),
    )


def _x2(net, *, tp1_done=False, peak=0.0):
    return _ladder(MODE, net, tp1_done=tp1_done, peak=peak)


def _f(net, *, tp1_done=False, peak=0.0):
    return _ladder(F_MODE, net, tp1_done=tp1_done, peak=peak)


def test_x2lite_tp1_sells_exactly_20pct():
    d = _x2(3.0)
    assert d.exit_reason == config.EXIT_TW_TP1_PARTIAL
    assert d.sell_fraction == pytest.approx(0.20)
    assert d.tp1_done is True
    assert _x2(2.99).exit_reason is None


def test_x2lite_tp2_fires_at_5pct_not_6pct():
    assert _x2(4.99, tp1_done=True).exit_reason != config.EXIT_TW_TP2_FULL
    assert _x2(5.00, tp1_done=True).exit_reason == config.EXIT_TW_TP2_FULL
    assert _x2(5.00, tp1_done=True).sell_fraction == pytest.approx(1.0)
    # F 는 같은 5.0% 에서 아직 TP2 가 아니다 (6.0%)
    assert _f(5.0, tp1_done=True).exit_reason != config.EXIT_TW_TP2_FULL


def test_x2lite_trailing_stop_boundary_279_holds_280_exits():
    """고점 3.5% 이상 -> trailing 활성. 되돌림 2.80% 지점에서만 청산."""
    assert _x2(2.81, tp1_done=True, peak=4.0).exit_reason is None       # 2.79% 되돌림
    assert _x2(2.80, tp1_done=True, peak=4.0).exit_reason == config.EXIT_TW_TRAILING_STOP
    assert _x2(2.79, tp1_done=True, peak=4.0).exit_reason == config.EXIT_TW_TRAILING_STOP
    # trailing 트리거(3.5%) 미달이면 after-TP1 stop(+2.0%) 가지로 간다
    d = _x2(2.5, tp1_done=True, peak=3.0)
    assert d.exit_reason is None and d.label == "HOLD_AFTER_TP1"
    assert _x2(2.0, tp1_done=True, peak=3.0).exit_reason == config.EXIT_TW_AFTER_TP1_STOP


def test_x2lite_morning_stop_loss_boundary_129_holds_130_exits():
    assert _x2(-1.29).exit_reason is None
    assert _x2(-1.30).exit_reason == config.EXIT_TW_STOP_LOSS
    assert _x2(-1.31).exit_reason == config.EXIT_TW_STOP_LOSS


def test_x2lite_afternoon_ladder_is_unchanged_except_tp():
    aft = _ladder(MODE, 3.0, session="AFTERNOON")
    assert aft.exit_reason == config.EXIT_TW_AFTERNOON_TP
    assert _ladder(MODE, 2.99, session="AFTERNOON").exit_reason != config.EXIT_TW_AFTERNOON_TP
    # 오후 손절 -1.2% / breakeven / profit-lock 은 모듈 상수 그대로
    assert pm.evaluate_afternoon_position(net_return_pct=-1.19).exit_reason is None
    assert pm.evaluate_afternoon_position(net_return_pct=-1.20).exit_reason == config.EXIT_TW_STOP_LOSS
    assert pm.AFTERNOON_STOP_LOSS_PCT == pytest.approx(config.AFTERNOON_STOP_LOSS * 100.0)
    assert pm.evaluate_afternoon_position(net_return_pct=0.2, peak_net_return=1.6).exit_reason == config.EXIT_TW_BREAKEVEN_STOP
    assert pm.evaluate_afternoon_position(net_return_pct=1.0, peak_net_return=2.1).exit_reason == config.EXIT_TW_PROFIT_LOCK_STOP


def test_x2lite_etp_state_machine_exactly_15_to_10():
    trig, flr = config.X2LITE_EARLY_TP_TRIGGER_PCT, config.X2LITE_EARLY_TP_FLOOR_PCT
    # MFE +1.49% -> 미armed
    d = etp.evaluate(entry_chop=True, peak_net_return_pct=1.49, net_return_pct=0.5,
                     trigger_pct=trig, floor_pct=flr)
    assert d.armed is False and d.exit_reason is None
    # MFE +1.50% -> armed, 현재 +1.01% 면 HOLD
    d = etp.evaluate(entry_chop=True, peak_net_return_pct=1.50, net_return_pct=1.01,
                     trigger_pct=trig, floor_pct=flr)
    assert d.armed is True and d.exit_reason is None
    # armed 이후 +1.00% 이하 -> 발동
    d = etp.evaluate(entry_chop=True, peak_net_return_pct=1.50, net_return_pct=1.00,
                     trigger_pct=trig, floor_pct=flr)
    assert d.armed is True and d.exit_reason == config.EXIT_EARLY_TAKE_PROFIT
    assert d.sell_fraction == pytest.approx(1.0)
    # 진입CHOP 이 아니면 대상 아님 (F 와 동일)
    assert etp.evaluate(entry_chop=False, peak_net_return_pct=5.0, net_return_pct=0.0,
                        trigger_pct=trig, floor_pct=flr).exit_reason is None
    # "+1.5% 도달 즉시 매도"가 아니다 — 도달 순간 수익이 1.5%면 floor 위라 HOLD
    assert etp.evaluate(entry_chop=True, peak_net_return_pct=1.5, net_return_pct=1.5,
                        trigger_pct=trig, floor_pct=flr).exit_reason is None


def _x2lite_flags(s: RuntimeState) -> RuntimeState:
    """default_state() 는 TW2 3-SLOT 이 기본 ON 이다 — X2-lite 만 남긴다
    (production 에서는 service setter 가 같은 일을 한다)."""
    s.time_window_2_filter_enabled = False
    s.time_window_teg_filter_enabled = False
    s.time_window_3slot_filter_enabled = False
    s.time_window_twf_filter_enabled = False
    s.time_window_x2lite_filter_enabled = True
    return s


def test_x2lite_etp_is_auto_on_without_the_manual_toggle():
    s = _x2lite_flags(state_store.default_state())
    assert s.early_tp_filter_enabled is False          # 수동 토글은 꺼져 있는데
    assert etp.is_enabled(s) is True                   # 자동 ON
    assert etp.thresholds(s) == (pytest.approx(1.5), pytest.approx(1.0))
    s.time_window_position_active = True
    s.time_window_active_mode = MODE
    assert etp.is_active(s) is True


def test_x2lite_etp_never_double_applies_with_the_manual_toggle():
    """수동 토글이 켜져 있어도 임계값은 X2-lite 것 하나뿐이고, service 가
    토글 자체를 꺼 준다 — 두 번 적용될 경로가 존재하지 않는다."""
    s = _x2lite_flags(state_store.default_state())
    s.early_tp_filter_enabled = True
    assert etp.thresholds(s) == (pytest.approx(1.5), pytest.approx(1.0))
    # floor 0.8 (F 의 값) 로는 절대 발동하지 않는다는 것을 같이 못박는다
    trig, flr = etp.thresholds(s)
    assert flr != config.EARLY_TP_FLOOR_PCT


# ════════════════════════════════════════════════════════════════════════════
# B-1. 상호배타 / 상태 저장
# ════════════════════════════════════════════════════════════════════════════
def test_service_mutual_exclusion_and_builtin_etp_forces_manual_toggle_off():
    svc = Macd2Service()
    svc.set_time_window_twf_filter_enabled(True, changed_by="t")
    svc.set_early_tp_filter_enabled(True, changed_by="t")
    s = state_store.load_state()
    assert s.time_window_twf_filter_enabled is True and s.early_tp_filter_enabled is True

    res = svc.set_time_window_x2lite_filter_enabled(True, changed_by="t")
    assert res["ok"] is True
    s = state_store.load_state()
    assert s.time_window_x2lite_filter_enabled is True
    assert s.time_window_twf_filter_enabled is False          # F 가 꺼졌다
    assert s.time_window_3slot_filter_enabled is False
    assert s.time_window_2_filter_enabled is False
    assert s.time_window_teg_filter_enabled is False
    assert s.early_tp_filter_enabled is False                 # 내장이라 수동토글 해제
    assert tw3.active_3slot_mode(s) == MODE

    # 반대로 F 를 다시 켜면 X2-lite 가 꺼진다
    svc.set_time_window_twf_filter_enabled(True, changed_by="t")
    s = state_store.load_state()
    assert s.time_window_x2lite_filter_enabled is False
    assert s.time_window_twf_filter_enabled is True
    assert tw3.active_3slot_mode(s) == F_MODE


def test_state_roundtrip_persists_x2lite_toggle():
    s = state_store.default_state()
    assert s.time_window_x2lite_filter_enabled is False       # 기본 OFF
    _x2lite_flags(s)
    s.time_window_x2lite_filter_version = config.X2LITE_3SLOT_FILTER_VERSION
    state_store.save_state(s)
    back = state_store.load_state()
    assert back.time_window_x2lite_filter_enabled is True
    assert tw3.active_3slot_mode(back) == MODE


# ════════════════════════════════════════════════════════════════════════════
# B-2. worker.run_once() end-to-end
# ════════════════════════════════════════════════════════════════════════════
def _fresh_x2lite_state(*, budget: float = 10_000_000.0) -> RuntimeState:
    s = state_store.default_state()
    s.auto_trade_on = True
    s.budget = budget
    s.time_window_2_filter_enabled = False
    s.time_window_teg_filter_enabled = False
    s.time_window_3slot_filter_enabled = False
    s.time_window_twf_filter_enabled = False
    s.time_window_x2lite_filter_enabled = True
    return s


def _fresh_f_state(*, budget: float = 10_000_000.0) -> RuntimeState:
    s = state_store.default_state()
    s.auto_trade_on = True
    s.budget = budget
    s.time_window_2_filter_enabled = False
    s.time_window_teg_filter_enabled = False
    s.time_window_3slot_filter_enabled = False
    s.time_window_twf_filter_enabled = True
    return s


def _price_for_net(symbol: str, entry_price: float, qty: int, target_net: float,
                   *, side: str = "at_least") -> float:
    """원하는 net% 가 나오는 현재가(수수료/세금 포함).

    ``side="at_least"`` -> net >= target 을 보장하는 가장 낮은 가격,
    ``side="at_most"``  -> net <= target 을 보장하는 가장 높은 가격.
    경계값 테스트에서 부동소수 오차로 반대쪽에 떨어지는 것을 막는다."""
    lo, hi = entry_price * 0.80, entry_price * 1.30
    for _ in range(300):
        mid = (lo + hi) / 2.0
        if worker._net_return_pct(symbol, entry_price, mid, qty) < target_net:
            lo = mid
        else:
            hi = mid
    price = hi if side == "at_least" else lo
    got = worker._net_return_pct(symbol, entry_price, price, qty)
    if side == "at_least":
        assert got >= target_net, f"{got} < {target_net}"
    else:
        assert got <= target_net, f"{got} > {target_net}"
    return price


def _held_state(mode: str, *, now, entry_price=10_000.0, qty=1000, tp1_done=False,
                peak=0.0, entry_chop=False, bar_close=None) -> RuntimeState:
    s = _fresh_x2lite_state() if mode == MODE else _fresh_f_state()
    s.position = PositionSnapshot(symbol=config.INVERSE_SYMBOL, quantity=qty,
                                  avg_price=entry_price, entry_at=now)
    s.time_window_position_active = True
    s.time_window_active_mode = mode
    s.time_window_entry_session = "MORNING"
    s.time_window_tp1_done = tp1_done
    s.time_window_peak_net_return = peak
    s.early_tp_peak_net_return = peak
    s.time_window_entry_chop = entry_chop
    s.tw2_3slot_slots_used_today = 1
    _seed_completed_bar(s, now=now, price=(entry_price if bar_close is None else bar_close))
    return s


def _broker_with(symbol_price: float, *, entry_price=10_000.0, qty=1000) -> FakeBroker:
    b = FakeBroker(cash=10_000_000.0,
                   quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: symbol_price})
    b.buy_market(config.INVERSE_SYMBOL, qty, "seed-order")
    b._positions[config.INVERSE_SYMBOL].avg_price = entry_price
    return b


def _sell_rows():
    rows = [r for r in ledger.load_execution_ledger(limit=10_000)
            if str(r.get("side", "")).upper() == "SELL"]
    return rows


# ── 진입 parity (F vs X2-lite) ──────────────────────────────────────────────
def _isolate_ledger(monkeypatch, tmp_path, tag: str) -> None:
    """한 테스트 안에서 두 전략을 각각 돌리려면 원장/상태를 **실행 단위로**
    갈라야 한다 — 같은 원장을 공유하면 두 번째 실행의 동일 signal_id 가
    중복실행 방지에 걸려 진입이 통째로 사라진다(그것이 정상 동작이다)."""
    d = tmp_path / tag
    d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(state_store, "STATE_DIR_PATH", d)
    monkeypatch.setattr(state_store, "STATE_PATH", d / "macd2_runtime.json")
    monkeypatch.setattr(ledger, "LOGS_DIR_PATH", d)
    monkeypatch.setattr(ledger, "SIGNAL_LEDGER_PATH", d / "macd2_signal_ledger.csv")
    monkeypatch.setattr(ledger, "EXECUTION_LEDGER_PATH", d / "macd2_execution_ledger.csv")


def _run_entry_sequence(state_factory, monkeypatch, svc, now0, directions):
    """같은 플래그 시퀀스를 흘려 넣고 체결된 진입을 (시각, 방향, 종목, 슬롯, 수량)
    으로 수집한다."""
    state = state_factory()
    broker = FakeBroker(cash=10_000_000.0,
                        quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})
    _patch_common(monkeypatch, entry_decision=_approved(),
                  quality_decision=_quality(True, 5), teg_decision=_teg(True))
    entries = []
    for i, direction in enumerate(directions):
        now = now0 + timedelta(minutes=3 * i)
        _prime_3slot_pending(state, direction, before=now - timedelta(minutes=3 * (i + 1)))
        before_qty = state.position.quantity if state.position else 0
        before_sym = state.position.symbol if state.position else None
        run_once(broker=broker, market_data=svc, state=state, now=now)
        pos = state.position
        if pos is not None and (pos.symbol != before_sym or pos.quantity != before_qty):
            entries.append((now.isoformat(), direction.value if hasattr(direction, "value") else str(direction),
                            pos.symbol, int(state.tw2_3slot_slots_used_today), int(pos.quantity)))
    return entries, state


def test_entry_parity_f_vs_x2lite_is_exactly_zero_diff(tmp_path, monkeypatch):
    """같은 입력에 대해 F 와 X2-lite 의 진입 집합이 완전히 동일해야 한다."""
    svc, now0 = _market()
    dirs = [Direction.UP_RED, Direction.DOWN_BLUE, Direction.UP_RED,
            Direction.DOWN_BLUE, Direction.UP_RED, Direction.DOWN_BLUE]

    _isolate_ledger(monkeypatch, tmp_path, "f")
    f_entries, f_state = _run_entry_sequence(_fresh_f_state, monkeypatch, svc, now0, dirs)
    svc2, now02 = _market()
    _isolate_ledger(monkeypatch, tmp_path, "x2lite")
    x_entries, x_state = _run_entry_sequence(_fresh_x2lite_state, monkeypatch, svc2, now02, dirs)

    assert f_entries == x_entries, (
        "진입 집합이 다르다 — X2-lite 는 청산만 다른 전략이어야 한다\n"
        f"F   : {f_entries}\nX2L : {x_entries}"
    )
    assert len(f_entries) > 0, "하네스가 진입을 한 건도 만들지 못했다"
    # 슬롯 카운터/일일 cap 도 동일
    assert f_state.tw2_3slot_slots_used_today == x_state.tw2_3slot_slots_used_today
    assert f_state.tw2_3slot_morning_count == x_state.tw2_3slot_morning_count
    assert f_state.tw2_3slot_afternoon_count == x_state.tw2_3slot_afternoon_count


def test_x2lite_daily_cap_is_still_3(monkeypatch):
    svc, now0 = _market()
    dirs = [Direction.UP_RED, Direction.DOWN_BLUE] * 4
    entries, state = _run_entry_sequence(_fresh_x2lite_state, monkeypatch, svc, now0, dirs)
    assert state.tw2_3slot_slots_used_today <= config.TW2_3SLOT_DAILY_CAP
    assert len(entries) <= config.TW2_3SLOT_DAILY_CAP


# ── TP1 20% ────────────────────────────────────────────────────────────────
def test_e2e_tp1_sells_exactly_20pct_and_keeps_80(monkeypatch):
    entry, qty = 10_000.0, 1000
    price = _price_for_net(config.INVERSE_SYMBOL, entry, qty, 3.0)
    svc, now0 = _market(inverse_price=price)
    state = _held_state(MODE, now=now0, entry_price=entry, qty=qty)
    broker = _broker_with(price, entry_price=entry, qty=qty)
    _patch_common(monkeypatch)

    result = run_once(broker=broker, market_data=svc, state=state, now=now0)

    assert any(a.startswith(config.EXIT_TW_TP1_PARTIAL) for a in result.actions), result.actions
    assert state.position is not None
    assert state.position.quantity == 800, f"잔량 800주여야 한다 (got {state.position.quantity})"
    assert broker._positions[config.INVERSE_SYMBOL].quantity == 800
    assert state.time_window_tp1_done is True

    rows = [r for r in _sell_rows() if r.get("exit_reason") == config.EXIT_TW_TP1_PARTIAL]
    assert len(rows) == 1, rows
    r = rows[0]
    assert int(r["position_before"]) == 1000
    assert int(r["executed_qty"]) == 200
    assert int(r["position_after"]) == 800
    assert r["exit_reason"] == config.EXIT_TW_TP1_PARTIAL
    assert r.get("net_pnl") not in (None, "")


def test_e2e_f_still_sells_50pct_at_tp1(monkeypatch):
    """같은 조건에서 F 는 여전히 50% — 회귀 확인."""
    entry, qty = 10_000.0, 1000
    price = _price_for_net(config.INVERSE_SYMBOL, entry, qty, 3.0)
    svc, now0 = _market(inverse_price=price)
    state = _held_state(F_MODE, now=now0, entry_price=entry, qty=qty)
    broker = _broker_with(price, entry_price=entry, qty=qty)
    _patch_common(monkeypatch)

    run_once(broker=broker, market_data=svc, state=state, now=now0)
    assert state.position.quantity == 500


# ── TP2 5% ─────────────────────────────────────────────────────────────────
def test_e2e_tp2_at_5pct_closes_remaining_and_never_double_sells(monkeypatch):
    entry, qty = 10_000.0, 800          # TP1 이후 잔량 80% 상황
    price = _price_for_net(config.INVERSE_SYMBOL, entry, qty, 5.0)
    svc, now0 = _market(inverse_price=price)
    state = _held_state(MODE, now=now0, entry_price=entry, qty=qty, tp1_done=True, peak=3.2)
    broker = _broker_with(price, entry_price=entry, qty=qty)
    _patch_common(monkeypatch)

    result = run_once(broker=broker, market_data=svc, state=state, now=now0)

    assert any(a.startswith(config.EXIT_TW_TP2_FULL) for a in result.actions), result.actions
    assert state.position is None
    assert config.INVERSE_SYMBOL not in broker._positions
    assert state.time_window_position_active is False
    rows = [r for r in _sell_rows() if r.get("exit_reason") == config.EXIT_TW_TP2_FULL]
    assert len(rows) == 1
    assert int(rows[0]["position_after"]) == 0
    assert int(rows[0]["executed_qty"]) == 800

    # 이후 틱에서 추가 매도 주문이 나가지 않는다
    sells_before = len([o for o in broker.orders if o.side.upper() == "SELL"])
    run_once(broker=broker, market_data=svc, state=state, now=now0 + timedelta(minutes=3))
    assert len([o for o in broker.orders if o.side.upper() == "SELL"]) == sells_before


def test_e2e_f_does_not_exit_at_5pct(monkeypatch):
    entry, qty = 10_000.0, 800
    price = _price_for_net(config.INVERSE_SYMBOL, entry, qty, 5.0)
    svc, now0 = _market(inverse_price=price)
    # 완성봉 종가도 같은 +5% 로 둔다 — 그렇지 않으면 after-TP1 stop 이 먼저 걸려
    # "TP2 가 아니라서 살아남았는지"를 검증할 수 없다.
    state = _held_state(F_MODE, now=now0, entry_price=entry, qty=qty, tp1_done=True,
                        peak=3.2, bar_close=price)
    broker = _broker_with(price, entry_price=entry, qty=qty)
    _patch_common(monkeypatch)

    result = run_once(broker=broker, market_data=svc, state=state, now=now0)
    assert not any(a.startswith(config.EXIT_TW_TP2_FULL) for a in result.actions), (
        f"F 의 TP2 는 6.0% 이므로 +5.0% 에서 나가면 안 된다 — {result.actions!r}")
    assert state.position is not None


# ── trailing 2.8% ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("net,should_exit", [(2.81, False), (2.80, True)])
def test_e2e_trailing_28_boundary(monkeypatch, net, should_exit):
    entry, qty = 10_000.0, 800
    price = _price_for_net(config.INVERSE_SYMBOL, entry, qty, net)
    svc, now0 = _market(inverse_price=price)
    # 완성봉 종가가 판정 기준 — bar_close 를 같은 가격으로 심는다.
    state = _held_state(MODE, now=now0, entry_price=entry, qty=qty,
                        tp1_done=True, peak=4.0, bar_close=price)
    broker = _broker_with(price, entry_price=entry, qty=qty)
    _patch_common(monkeypatch)

    result = run_once(broker=broker, market_data=svc, state=state, now=now0)
    fired = any(a.startswith(config.EXIT_TW_TRAILING_STOP) for a in result.actions)
    assert fired is should_exit, f"net={net} actions={result.actions!r}"
    if should_exit:
        assert state.position is None


# ── 오전 손절 -1.30% ───────────────────────────────────────────────────────
@pytest.mark.parametrize("net,should_exit", [(-1.29, False), (-1.30, True)])
def test_e2e_morning_stop_loss_130_boundary(monkeypatch, net, should_exit):
    entry, qty = 10_000.0, 1000
    price = _price_for_net(config.INVERSE_SYMBOL, entry, qty, net)
    svc, now0 = _market(inverse_price=price)
    state = _held_state(MODE, now=now0, entry_price=entry, qty=qty, bar_close=price)
    broker = _broker_with(price, entry_price=entry, qty=qty)
    _patch_common(monkeypatch)

    result = run_once(broker=broker, market_data=svc, state=state, now=now0)
    fired = any(a.startswith(config.EXIT_TW_STOP_LOSS) for a in result.actions)
    assert fired is should_exit, f"판정 net={net} actions={result.actions!r}"
    if should_exit:
        assert state.position is None
        rows = [r for r in _sell_rows() if r.get("exit_reason") == config.EXIT_TW_STOP_LOSS]
        assert len(rows) == 1
        # 판정 threshold(-1.30%)와 실제 체결 net% 는 별개다 — 체결가가 더 나쁠 수 있다.
        assert float(rows[0]["net_pnl"]) <= 0.0
        assert int(rows[0]["position_after"]) == 0


def test_e2e_x2lite_does_not_stop_out_where_f_would(monkeypatch):
    """-1.35% 지점: X2-lite(-1.3) 는 이미 손절, F(-1.4) 는 아직 보유."""
    entry, qty = 10_000.0, 1000
    price = _price_for_net(config.INVERSE_SYMBOL, entry, qty, -1.35)
    for mode, expect_exit in ((MODE, True), (F_MODE, False)):
        svc, now0 = _market(inverse_price=price)
        state = _held_state(mode, now=now0, entry_price=entry, qty=qty, bar_close=price)
        broker = _broker_with(price, entry_price=entry, qty=qty)
        _patch_common(monkeypatch)
        result = run_once(broker=broker, market_data=svc, state=state, now=now0)
        fired = any(a.startswith(config.EXIT_TW_STOP_LOSS) for a in result.actions)
        assert fired is expect_exit, f"mode={mode} actions={result.actions!r}"


# ── ETP 1.5 -> 1.0 (worker 경로) ───────────────────────────────────────────
@pytest.mark.parametrize("peak,bar_net,should_exit", [
    (1.49, 0.50, False),   # 미armed
    (1.50, 1.01, False),   # armed, floor 위 -> HOLD
    (1.50, 1.00, True),    # armed, floor 도달 -> 발동
])
def test_e2e_etp_state_machine_through_worker(monkeypatch, peak, bar_net, should_exit):
    entry, qty = 10_000.0, 800
    bar_price = _price_for_net(config.INVERSE_SYMBOL, entry, qty, bar_net)
    svc, now0 = _market(inverse_price=bar_price)
    state = _held_state(MODE, now=now0, entry_price=entry, qty=qty,
                        peak=peak, entry_chop=True, bar_close=bar_price)
    broker = _broker_with(bar_price, entry_price=entry, qty=qty)
    _patch_common(monkeypatch)

    result = run_once(broker=broker, market_data=svc, state=state, now=now0)
    fired = any(a.startswith(config.EXIT_EARLY_TAKE_PROFIT) for a in result.actions)
    assert fired is should_exit, f"peak={peak} bar_net={bar_net} actions={result.actions!r}"
    if should_exit:
        assert state.position is None
        rows = [r for r in _sell_rows() if r.get("exit_reason") == config.EXIT_EARLY_TAKE_PROFIT]
        assert len(rows) == 1 and int(rows[0]["position_after"]) == 0
        # 원장 진단 필드가 **실제 판정에 쓰인 X2-lite 임계값**을 기록해야 한다
        # (config 의 F 값 1.5/0.8 이 아니라).
        assert float(rows[0]["early_tp_trigger_pct"]) == pytest.approx(1.5)
        assert float(rows[0]["early_tp_floor_pct"]) == pytest.approx(1.0)
        assert float(rows[0]["early_tp_floor_pct"]) != pytest.approx(config.EARLY_TP_FLOOR_PCT)


def test_e2e_f_etp_floor_08_does_not_fire_where_x2lite_does(monkeypatch):
    """+0.9% 되돌림: X2-lite(floor 1.0) 는 발동, F(floor 0.8, 수동토글 ON) 는 미발동."""
    entry, qty = 10_000.0, 800
    bar_price = _price_for_net(config.INVERSE_SYMBOL, entry, qty, 0.9)

    svc, now0 = _market(inverse_price=bar_price)
    s = _held_state(MODE, now=now0, entry_price=entry, qty=qty, peak=2.0,
                    entry_chop=True, bar_close=bar_price)
    _patch_common(monkeypatch)
    r = run_once(broker=_broker_with(bar_price, entry_price=entry, qty=qty),
                 market_data=svc, state=s, now=now0)
    assert any(a.startswith(config.EXIT_EARLY_TAKE_PROFIT) for a in r.actions), r.actions

    svc2, now02 = _market(inverse_price=bar_price)
    s2 = _held_state(F_MODE, now=now02, entry_price=entry, qty=qty, peak=2.0,
                     entry_chop=True, bar_close=bar_price)
    s2.early_tp_filter_enabled = True
    r2 = run_once(broker=_broker_with(bar_price, entry_price=entry, qty=qty),
                  market_data=svc2, state=s2, now=now02)
    assert not any(a.startswith(config.EXIT_EARLY_TAKE_PROFIT) for a in r2.actions), r2.actions


def test_e2e_production_ladder_wins_over_etp(monkeypatch):
    """TP2 와 ETP 조건이 동시에 성립하면 TP2 가 이긴다 (기존 우선순위 유지)."""
    entry, qty = 10_000.0, 800
    price = _price_for_net(config.INVERSE_SYMBOL, entry, qty, 5.2)
    svc, now0 = _market(inverse_price=price)
    state = _held_state(MODE, now=now0, entry_price=entry, qty=qty,
                        tp1_done=True, peak=5.2, entry_chop=True, bar_close=price)
    broker = _broker_with(price, entry_price=entry, qty=qty)
    _patch_common(monkeypatch)

    result = run_once(broker=broker, market_data=svc, state=state, now=now0)
    assert any(a.startswith(config.EXIT_TW_TP2_FULL) for a in result.actions), result.actions
    assert not any(a.startswith(config.EXIT_EARLY_TAKE_PROFIT) for a in result.actions)


# ── 강제청산 / 슬롯 카운터 ─────────────────────────────────────────────────
def test_e2e_forced_liquidation_closes_everything_without_consuming_a_slot(monkeypatch):
    entry, qty = 10_000.0, 800
    svc, _ = _market(inverse_price=entry)
    now = _SESSION_START_NOW.replace(hour=15, minute=0, second=5, microsecond=0)
    state = _held_state(MODE, now=now, entry_price=entry, qty=qty)
    slots_before = state.tw2_3slot_slots_used_today
    broker = _broker_with(entry, entry_price=entry, qty=qty)
    _patch_common(monkeypatch)

    result = run_once(broker=broker, market_data=svc, state=state, now=now)

    assert any(config.EXIT_FORCED_LIQUIDATION in a for a in result.actions), result.actions
    assert state.position is None
    assert config.INVERSE_SYMBOL not in broker._positions
    assert state.tw2_3slot_slots_used_today == slots_before
    rows = [r for r in _sell_rows() if config.EXIT_FORCED_LIQUIDATION in str(r.get("exit_reason"))]
    assert rows and int(rows[-1]["position_after"]) == 0


def test_exits_never_increment_the_slot_counter(monkeypatch):
    """TP1 부분익절이 슬롯을 추가로 소비하지 않는다."""
    entry, qty = 10_000.0, 1000
    price = _price_for_net(config.INVERSE_SYMBOL, entry, qty, 3.0)
    svc, now0 = _market(inverse_price=price)
    state = _held_state(MODE, now=now0, entry_price=entry, qty=qty)
    before = state.tw2_3slot_slots_used_today
    broker = _broker_with(price, entry_price=entry, qty=qty)
    _patch_common(monkeypatch)

    run_once(broker=broker, market_data=svc, state=state, now=now0)
    assert state.tw2_3slot_slots_used_today == before


# ── 반대신호 청산 ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("whipsaw_reason", sorted(config.TW_WHIPSAW_REJECT_REASONS))
def test_e2e_whipsaw_reversal_holds_under_x2lite_exactly_like_f(monkeypatch, whipsaw_reason):
    """휩쏘로 분류된 반대신호는 보유 유지 — 매도 주문 0건, 슬롯 불변."""
    entry, qty = 10_000.0, 800
    svc, now0 = _market(inverse_price=entry)
    state = _held_state(MODE, now=now0, entry_price=entry, qty=qty)
    state.tw2_3slot_morning_count = 1
    broker = _broker_with(entry, entry_price=entry, qty=qty)
    _patch_common(monkeypatch, entry_decision=_rejected(whipsaw_reason))
    _prime_3slot_pending(state, Direction.UP_RED, before=_PRIOR_DAY)
    orders_before = len(broker.orders)

    result = run_once(broker=broker, market_data=svc, state=state, now=now0)

    assert any(a.startswith("TW2_3SLOT_WHIPSAW_HOLD") for a in result.actions), result.actions
    assert not any(a.startswith("TW2_3SLOT_SELL_ONLY") for a in result.actions)
    assert state.position is not None and state.position.quantity == qty
    assert state.time_window_position_active is True
    assert len(broker.orders) == orders_before
    assert state.tw2_3slot_slots_used_today == 1


def test_e2e_non_whipsaw_reversal_liquidates_under_x2lite_exactly_like_f(monkeypatch):
    """정상 반대신호(품질 미달 거절)는 기존 포지션을 청산한다 — 신규진입은 별개."""
    entry, qty = 10_000.0, 800
    svc, now0 = _market(inverse_price=entry)
    state = _held_state(MODE, now=now0, entry_price=entry, qty=qty)
    broker = _broker_with(entry, entry_price=entry, qty=qty)
    _patch_common(monkeypatch, entry_decision=_rejected(config.TW_REJECT_LOW_QUALITY_SCORE))
    _prime_3slot_pending(state, Direction.UP_RED, before=_PRIOR_DAY)

    result = run_once(broker=broker, market_data=svc, state=state, now=now0)

    assert any(a.startswith("TW2_3SLOT_SELL_ONLY") for a in result.actions), result.actions
    assert state.position is None
    assert config.INVERSE_SYMBOL not in broker._positions
    assert state.time_window_position_active is False
    # 청산은 슬롯을 되돌려주지도 더 쓰지도 않는다
    assert state.tw2_3slot_slots_used_today == 1


def test_e2e_approved_reversal_switches_and_consumes_a_new_slot_under_x2lite(monkeypatch):
    """승인된 반대신호는 청산 + 신규 반대방향 진입(=슬롯 +1). 청산과 진입은 별개 판정."""
    entry, qty = 10_000.0, 800
    svc, now0 = _market(inverse_price=entry)
    state = _held_state(MODE, now=now0, entry_price=entry, qty=qty)
    state.tw2_3slot_morning_count = 1
    broker = _broker_with(entry, entry_price=entry, qty=qty)
    _patch_common(monkeypatch, entry_decision=_approved(),
                  quality_decision=_quality(True, 5), teg_decision=_teg(True))
    _prime_3slot_pending(state, Direction.UP_RED, before=_PRIOR_DAY)

    result = run_once(broker=broker, market_data=svc, state=state, now=now0)

    assert any(a.startswith("TW2_3SLOT_SWITCH") for a in result.actions), result.actions
    assert state.position is not None and state.position.symbol == config.LONG_SYMBOL
    assert config.INVERSE_SYMBOL not in broker._positions
    assert state.time_window_active_mode == MODE, "스위치 후에도 모드가 유지돼야 한다"
    assert state.tw2_3slot_slots_used_today == 2


# ── 왕복거래 UI 표시 ───────────────────────────────────────────────────────
def test_round_trip_counts_05_then_10_for_one_x2lite_position(monkeypatch):
    entry, qty = 10_000.0, 1000
    price1 = _price_for_net(config.INVERSE_SYMBOL, entry, qty, 3.0)
    svc, now0 = _market(inverse_price=price1)
    state = _held_state(MODE, now=now0, entry_price=entry, qty=qty)
    broker = _broker_with(price1, entry_price=entry, qty=qty)
    _patch_common(monkeypatch)
    run_once(broker=broker, market_data=svc, state=state, now=now0)
    assert state.position is not None and state.position.quantity == 800

    rows = _sell_rows()
    assert macd2_summary.count_round_trips(rows, has_open_position=True) == pytest.approx(0.5)
    assert macd2_summary.has_in_progress_round_trip(rows, has_open_position=True) is True
    assert macd2_summary.format_round_trips(0.5, in_progress=True) == "0.5회 (진행중)"

    # 잔량까지 TP2 로 청산 -> 1.0회 (SELL 레그가 2개여도 2회가 아니다)
    price2 = _price_for_net(config.INVERSE_SYMBOL, entry, 800, 5.0)
    svc2, _ = _market(inverse_price=price2)
    now1 = now0 + timedelta(minutes=3)
    _seed_completed_bar(state, now=now1, price=price2)
    broker.set_quote(config.INVERSE_SYMBOL, price2)
    run_once(broker=broker, market_data=svc2, state=state, now=now1)
    assert state.position is None

    rows = _sell_rows()
    assert len(rows) >= 2, rows
    assert macd2_summary.count_round_trips(rows, has_open_position=False) == pytest.approx(1.0)
    assert macd2_summary.has_in_progress_round_trip(rows, has_open_position=False) is False
