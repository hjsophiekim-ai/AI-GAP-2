"""오전 손절 -1.3% -> -1.5% (2026-11-02, 사용자 결정) — 계약 고정.

잠그는 축
---------
 1   값 자체 — config / 모듈 상수 / override 가 전부 -1.5 로 일치한다
 2   경계 — -1.49 버팀 / -1.50·-1.51 손절. 구 임계구간(-1.30~-1.49)은 더 이상 안 걸린다
 3   공유 범위 — N1 계열과 X2-lite 계열만 바뀐다. 그 외 모드는 한 값도 안 바뀐다
 4   **안 바뀐 손절들** — TW2·TEGv2(-1.7%) / TWF(-1.4%) / B3(-1.0%). 오후는 2026-11-05 에 -1.2 -> -1.5 로 별도 변경
 5   env 로 되돌릴 수 있다 (MACD2_X2LITE_MORNING_STOP_LOSS)
 6   손절 외 래더(TP1/TP2/trailing/after-TP1)는 건드리지 않았다
 7   worker 경로 — 완성 3분봉 종가가 -1.5% 를 넘겨야 손절한다(틱 아님)

거래단위 손익 검증은 이 파일이 아니라 재생 하네스다 — 58영업일 실데이터
(2026-08-03~10-30, CHOP+MU Assist) 전량 재생에서 총손익/PF/MDD/최악일/승률/
거래수 **전부 불변(Δ 0)**. 재현은 docs/MACD2_STOPLOSS_15.md 참조.
"""
from __future__ import annotations

import importlib

import pytest

from app.trading.macd2 import config
from app.trading.macd2 import time_window_3slot as tw3
from app.trading.macd2 import time_window_position_manager as pm

MORNING = "MORNING"


def _morning(net, *, mode=tw3.MODE_N1_3SLOT, tp1_done=False, peak=0.0):
    """그 모드의 override 를 실제로 거쳐 오전 래더를 판정한다."""
    ov = tw3.exit_overrides(mode)
    return pm.evaluate_position(
        session=MORNING, net_return_pct=net, tp1_done=tp1_done, peak_net_return=peak,
        tp2_pct_override=tw3.morning_tp2_pct_override(mode), **ov,
    )


# ── 1. 값 ────────────────────────────────────────────────────────────────
def test_value_is_minus_1_5_everywhere():
    assert config.X2LITE_MORNING_STOP_LOSS == pytest.approx(-0.015)
    for mode in (tw3.MODE_N1_3SLOT, tw3.MODE_X2LITE_3SLOT, tw3.MODE_X2LITE_H50_3SLOT):
        assert tw3.exit_overrides(mode)["stop_loss_pct_override"] == pytest.approx(-1.5), mode


# ── 2. 경계 ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize("mode", [tw3.MODE_N1_3SLOT, tw3.MODE_X2LITE_3SLOT,
                                  tw3.MODE_X2LITE_H50_3SLOT])
def test_boundary_149_holds_150_exits(mode):
    assert _morning(-1.49, mode=mode).exit_reason is None
    assert _morning(-1.50, mode=mode).exit_reason == config.EXIT_TW_STOP_LOSS
    assert _morning(-1.51, mode=mode).exit_reason == config.EXIT_TW_STOP_LOSS


@pytest.mark.parametrize("net", [-1.30, -1.35, -1.40, -1.45, -1.49])
def test_old_threshold_band_no_longer_stops(net):
    """이 변경의 전부 — 구 임계구간에서 버틴다."""
    assert _morning(net).exit_reason is None


# ── 3~4. 공유 범위 / 안 바뀐 손절들 ──────────────────────────────────────
def test_only_n1_and_x2lite_families_changed():
    for mode in (tw3.MODE_TW2_3SLOT, tw3.MODE_TWF_3SLOT, None, "MU_MACD"):
        ov = tw3.exit_overrides(mode)["stop_loss_pct_override"]
        assert ov != pytest.approx(-1.5), mode
    # TWF 는 자기 값(-1.4%) 그대로
    assert tw3.exit_overrides(tw3.MODE_TWF_3SLOT)["stop_loss_pct_override"] == \
        pytest.approx(config.TWF_MORNING_STOP_LOSS * 100.0)


def test_other_stop_losses_are_untouched():
    """-1.3% 가 아니었던 손절들은 이번 변경 대상이 아니다 — 값을 그대로 잠근다."""
    assert config.MORNING_STOP_LOSS == pytest.approx(-0.017)       # TW2 / TEGv2 / MU_MACD
    assert config.AFTERNOON_STOP_LOSS == pytest.approx(-0.015)     # 오후 래더(override 없음) — 2026-11-05 -1.2 -> -1.5
    assert config.TWF_MORNING_STOP_LOSS == pytest.approx(-0.014)   # TW TEG 3-SLOT
    assert config.P3_B3_SL_PCT == pytest.approx(1.0)               # B3 (CHOP 각인 진입)
    assert pm.MORNING_STOP_LOSS_PCT == pytest.approx(-1.7)
    assert pm.AFTERNOON_STOP_LOSS_PCT == pytest.approx(-1.5)


def test_afternoon_ladder_stops_at_1_5():
    """2026-11-05: 오후 손절 -1.2 -> -1.5 (사용자 결정). 구 임계구간(-1.20~-1.49)은 더 이상 안 걸린다."""
    assert pm.evaluate_afternoon_position(net_return_pct=-1.20).exit_reason is None
    assert pm.evaluate_afternoon_position(net_return_pct=-1.49).exit_reason is None
    assert pm.evaluate_afternoon_position(net_return_pct=-1.50).exit_reason == config.EXIT_TW_STOP_LOSS
    assert pm.evaluate_afternoon_position(net_return_pct=-1.60).exit_reason == config.EXIT_TW_STOP_LOSS


# ── 5. env 롤백 ─────────────────────────────────────────────────────────
def test_env_can_roll_back(monkeypatch):
    monkeypatch.setenv("MACD2_X2LITE_MORNING_STOP_LOSS", "-0.013")
    cfg = importlib.reload(config)
    try:
        assert cfg.X2LITE_MORNING_STOP_LOSS == pytest.approx(-0.013)
    finally:
        monkeypatch.delenv("MACD2_X2LITE_MORNING_STOP_LOSS", raising=False)
        importlib.reload(config)
    assert config.X2LITE_MORNING_STOP_LOSS == pytest.approx(-0.015)


# ── 6. 손절 외 래더 불변 ────────────────────────────────────────────────
def test_rest_of_the_ladder_is_unchanged():
    ov = tw3.exit_overrides(tw3.MODE_N1_3SLOT)
    assert ov["after_tp1_stop_pct_override"] == pytest.approx(config.X2LITE_MORNING_AFTER_TP1_STOP * 100.0)
    assert ov["tp1_sell_ratio_override"] == pytest.approx(config.X2LITE_MORNING_TP1_SELL_RATIO)
    assert ov["trailing_stop_pct_override"] == pytest.approx(config.N1_MORNING_TRAILING_STOP * 100.0)
    assert ov["afternoon_tp_pct_override"] == pytest.approx(config.N1_AFTERNOON_TP * 100.0)
    # TP 가지는 손절선과 무관하게 그대로 발동한다 (N1 의 추세 TP2 는 5% 가 아니다)
    tp2 = tw3.morning_tp2_pct_override(tw3.MODE_N1_3SLOT)
    assert _morning(3.0).exit_reason == config.EXIT_TW_TP1_PARTIAL
    assert _morning(tp2).exit_reason == config.EXIT_TW_TP2_FULL
    assert _morning(tp2 - 0.01).exit_reason == config.EXIT_TW_TP1_PARTIAL


# ── 7. worker 경로 — 틱이 아니라 완성 3분봉 종가 ────────────────────────
def test_worker_stops_on_completed_bar_close_only(monkeypatch, tmp_path):
    """-1.6% 봉 종가면 손절, -1.4% 봉 종가면 버틴다(= 구 임계값이면 걸렸을 값)."""
    from tests.macd2.test_fixed_exit_15 import ENTRY, _run, _sells
    from app.trading.macd2 import strategy_mode
    from app.trading.macd2.worker import _net_return_pct

    def _px_for(net_pct):
        """net_return_pct 가 net_pct 이하가 되는 첫 가격(1원 단위 하향 탐색)."""
        p = ENTRY
        while p > ENTRY * 0.95:
            if _net_return_pct(config.INVERSE_SYMBOL, ENTRY, p, 10) <= net_pct:
                return p
            p -= 1.0
        raise AssertionError("가격을 못 찾았다")

    deep = _px_for(-1.6)
    _st, orders, result = _run(monkeypatch, tmp_path / "deep", mode=strategy_mode.MODE_N1,
                              on=False, price=deep, bar_close=deep)
    assert f"{config.EXIT_TW_STOP_LOSS}:{config.INVERSE_SYMBOL}" in result.actions
    assert len(_sells(orders)) == 1

    shallow = _px_for(-1.4)
    _st2, orders2, result2 = _run(monkeypatch, tmp_path / "shallow", mode=strategy_mode.MODE_N1,
                                  on=False, price=shallow, bar_close=shallow)
    assert not any(a.startswith(config.EXIT_TW_STOP_LOSS) for a in result2.actions)
    assert _sells(orders2) == []
