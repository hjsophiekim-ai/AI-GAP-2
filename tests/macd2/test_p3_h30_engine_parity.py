"""production p3_stack 이 연구엔진 H30 블록과 **같은 판정**을 하는가.

왜 이 파일이 필요한가
---------------------
80영업일 replay 자체는 이 저장소에서 재현할 수 없다. 봉단위 엔진(hengine5)과
그 의존물(``proj/`` = git archive 948c211, ``axlib``/``zrelax``/``common``,
80일 분봉 캐시, ``_ctx80.pkl``)이 연구 랩에만 있고 연구 번들
``research_20260928f_p3_chop_rescue`` 에는 ``scripts/`` 와 ``out/`` 만 있기
때문이다. 그래서 "523.05 -> 524.55" 를 여기서 다시 계산할 수는 없다.

대신 할 수 있는 것이 이것이다: 그 복리값을 만든 **판정 블록**을 연구 번들의
``scripts/hengine5_b3.patch`` 에서 그대로 옮겨 참조 구현으로 두고, production
``p3_stack.evaluate`` 와 격자 전체에서 같은 결정을 내는지 본다. 판정이 같으면
같은 입력에서 같은 거래가 나오고, 복리차는 그 거래들의 결과일 뿐이다.

옮긴 원본 (patch 의 tick 루프, H30 = resc_frac 0.2 + h50ext 30.0)::

    if b3["p3"] and not trig and net >= p3_trig:
        trig = True
        if el <= p3_min:                      -> P3_PARTIAL_EXIT(resc_frac) + 승격
    if ext and net >= tp:                     -> H50X_PARTIAL_EXIT(resc_frac) + 승격
    if net >= tp:                             -> GX_TP 전량
    if net <= -sl:                            -> GX_SL 전량
    if el >= hold:
        if h50ext and hold is not None and el < h50ext:
            ext = True; continue              -> 연장(주문 없음)
        if y3 and net > 0: prom = gap and etf
        if prom: 승격
        else:                                 -> GX_MAXHOLD 전량
    continue                                  -> HOLD

H30 변형에서 꺼져 있는 가지(``late``=R1/R2/R3, ``y3mode``=OR/PARTIAL,
``h50prom``, ``fs_stop``)는 b3run.py 의 ``VAR["H30"]`` 에 없으므로 옮기지 않았다.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.trading.macd2 import config, p3_stack
from app.trading.macd2.models import Direction

KST = config.KST
ENTRY = datetime(2026, 9, 28, 9, 9, tzinfo=KST)

# research_20260928f_p3_chop_rescue/scripts/b3run.py
#   "H30": dict(tp=1.0, p3=True, p3_trig=1.0, y3=True, resc_frac=0.2, h50ext=30.0)
#   공통 cfg = dict(sl=1.0, hold=20.0, p3_min=6.0, p3_trig=1.0)
ENGINE_H30 = dict(tp=1.0, sl=1.0, hold=20.0, p3_min=6.0, p3_trig=1.0,
                  resc_frac=0.2, h50ext=30.0)

# 결정 종류 -- 엔진 leg 이름과 production exit_reason 을 같은 어휘로 맞춘다.
HOLD = "HOLD"
EXTEND = "EXTEND"
RESCUE = "PARTIAL_RESCUE"        # P3_PARTIAL_EXIT
EXT_RESCUE = "PARTIAL_EXT"       # H50X_PARTIAL_EXIT
TP = "EXIT_TP"                   # GX_TP
SL = "EXIT_SL"                   # GX_SL
MAXHOLD = "EXIT_MAXHOLD"         # GX_MAXHOLD
PROMOTE = "PROMOTE"


def engine_decide(*, el: float, net: float, hold_active: bool, ext: bool,
                  trig: bool, y3_gap: bool, y3_etf: bool,
                  cfg: dict = ENGINE_H30) -> tuple[str, float]:
    """연구엔진 한 tick 의 결정. 위 docstring 의 블록을 그대로 옮겼다."""
    if not trig and net >= float(cfg["p3_trig"]):
        trig = True
        if el <= float(cfg["p3_min"]) + 1e-9:
            return RESCUE, float(cfg["resc_frac"])
    if ext and net >= float(cfg["tp"]):
        return EXT_RESCUE, float(cfg["resc_frac"])
    if net >= float(cfg["tp"]):
        return TP, 1.0
    if net <= -float(cfg["sl"]):
        return SL, 1.0
    if el >= float(cfg["hold"]) - 1e-9:
        lim = cfg.get("h50ext")
        if lim and hold_active and el < float(lim) - 1e-9:
            return EXTEND, 0.0
        prom = bool(net > 0.0 and y3_gap and y3_etf)
        if prom:
            return PROMOTE, 0.0
        return MAXHOLD, 1.0
    return HOLD, 0.0


def production_decide(*, el: float, net: float, hold_active: bool, ext: bool,
                      trig: bool, y3_gap: bool, y3_etf: bool,
                      monkeypatch) -> tuple[str, float]:
    """worker 가 한 tick 에 실제로 하는 두 번의 호출을 그대로 재현한다.

    ① 이른 체인: ``allow_max_hold=False`` (봉 없이 TP/SL 만)
    ② 그것이 HOLD 면 늦은 체인: ``allow_max_hold=True`` (완성봉 + Y3)
    """
    monkeypatch.setattr(p3_stack, "macd_gap_expanding",
                        lambda bars, now, direction: (y3_gap, "parity"))
    now = ENTRY + timedelta(minutes=el)
    kwargs = dict(
        net_return_pct=net, entry_at=ENTRY, now=now,
        direction=Direction.DOWN_BLUE,
        # 엔진의 trig 은 '+1% 최초 도달' 각인이고, production 에서 그 각인이
        # 남은 채 계속 관리되는 상태는 rescue 실패 재시도뿐이라 already_rescued
        # 와 같은 자리를 쓴다.
        already_rescued=trig, already_promoted=False,
        h50_active=hold_active, h30_active=ext,
        bars_3m=object(),
        etf_prev_price=100.0, etf_current_price=(101.0 if y3_etf else 99.0),
    )
    early = p3_stack.evaluate(allow_max_hold=False, **kwargs)
    decision = early if early.action != p3_stack.ACTION_HOLD else \
        p3_stack.evaluate(allow_max_hold=True, **kwargs)

    kind = {
        (p3_stack.ACTION_HOLD, None): HOLD,
        (p3_stack.ACTION_EXTEND, None): EXTEND,
        (p3_stack.ACTION_PARTIAL_PROMOTE, config.EXIT_P3_PARTIAL): RESCUE,
        (p3_stack.ACTION_PARTIAL_PROMOTE, config.EXIT_P3_H30_PARTIAL): EXT_RESCUE,
        (p3_stack.ACTION_EXIT, config.EXIT_B3_TP): TP,
        (p3_stack.ACTION_EXIT, config.EXIT_B3_SL): SL,
        (p3_stack.ACTION_EXIT, config.EXIT_B3_MAXHOLD): MAXHOLD,
        (p3_stack.ACTION_EXIT, config.EXIT_P3_H30_MAXHOLD): MAXHOLD,
        (p3_stack.ACTION_PROMOTE, None): PROMOTE,
    }[(decision.action, decision.exit_reason)]
    return kind, float(decision.sell_fraction)


# ── 격자 ─────────────────────────────────────────────────────────────────
#: 경계를 정확히 밟는다: 6분(rescue 창), 20분(max-hold), 30분(deadline).
ELAPSED = (0.0, 1.0, 5.9, 6.0, 6.1, 12.0, 19.9, 20.0, 20.1, 25.0,
           29.9, 30.0, 30.1, 45.0)
NETS = (-2.0, -1.01, -1.0, -0.99, -0.5, -0.01, 0.0, 0.01, 0.5,
        0.99, 1.0, 1.01, 3.26)
FLAGS = (False, True)


#: ``trig`` 은 격자에서 뺀다 -- **두 구현 모두에서 도달 불가**이기 때문이다.
#: 근거는 아래 test_trig_while_still_governed_is_unreachable 이 증명한다.
#: (엔진의 trig 은 '+1% 최초 도달' 각인인데, 그 각인이 찍히는 tick 은 반드시
#:  같은 tick 에서 rescue/ext-rescue/GX_TP 중 하나로 관리권을 끝낸다.)
TRIG = (False,)


def _reachable(el: float, ext: bool, hold_active: bool) -> bool:
    """실제로 도달 가능한 상태만 본다.

    ``ext`` 는 max-hold 시점에 H50 이 켜져 있어야만 켜지므로, 20분 전에
    ``ext=True`` 인 tick 은 두 구현 모두에서 존재하지 않는다.
    """
    return not (ext and el < float(ENGINE_H30["hold"]))


@pytest.mark.parametrize("el", ELAPSED)
@pytest.mark.parametrize("net", NETS)
def test_production_matches_the_research_engine_on_every_reachable_state(
        el, net, monkeypatch):
    checked = 0
    for hold_active in FLAGS:
        for ext in FLAGS:
            if not _reachable(el, ext, hold_active):
                continue
            for trig in TRIG:
                for y3_gap in FLAGS:
                    for y3_etf in FLAGS:
                        args = dict(el=el, net=net, hold_active=hold_active,
                                    ext=ext, trig=trig, y3_gap=y3_gap,
                                    y3_etf=y3_etf)
                        want = engine_decide(**args)
                        got = production_decide(monkeypatch=monkeypatch, **args)
                        assert got == want, f"{args}: engine={want} production={got}"
                        checked += 1
    assert checked > 0


def test_the_reference_engine_reproduces_the_two_research_anchors():
    """참조 구현 자체가 연구 출력과 맞는지 -- 옮기다 틀렸으면 여기서 걸린다."""
    # 09/28: 20분 net -0.2665, H50 활성 -> 연장 (Q2 였다면 MAXHOLD)
    assert engine_decide(el=20.0, net=-0.2665, hold_active=True, ext=False,
                         trig=False, y3_gap=False, y3_etf=False)[0] == EXTEND
    assert engine_decide(el=20.0, net=-0.2665, hold_active=False, ext=False,
                         trig=False, y3_gap=False, y3_etf=False)[0] == MAXHOLD
    # 09/28: 30분에 net>0 + Y3 -> 승격
    assert engine_decide(el=30.0, net=0.10, hold_active=True, ext=True,
                         trig=False, y3_gap=True, y3_etf=True)[0] == PROMOTE
    # 09/03: 30분에 net -0.621 -> 전량청산 (승격 아님)
    assert engine_decide(el=30.0, net=-0.621, hold_active=True, ext=True,
                         trig=False, y3_gap=True, y3_etf=True)[0] == MAXHOLD


def test_q2_ratio_is_the_one_the_research_used():
    assert float(config.P3_RESCUE_SELL_RATIO) == pytest.approx(
        float(ENGINE_H30["resc_frac"]))
    assert float(config.P3_H30_EXT_MAX_HOLD_MIN) == pytest.approx(
        float(ENGINE_H30["h50ext"]))
    assert float(config.P3_B3_MAX_HOLD_MIN) == pytest.approx(
        float(ENGINE_H30["hold"]))
    assert float(config.P3_RESCUE_MAX_MIN) == pytest.approx(
        float(ENGINE_H30["p3_min"]))
    assert float(config.P3_B3_TP_PCT) == pytest.approx(float(ENGINE_H30["tp"]))
    assert float(config.P3_B3_SL_PCT) == pytest.approx(float(ENGINE_H30["sl"]))


def test_trig_while_still_governed_is_unreachable_in_both_implementations():
    """'+1% 를 이미 밟았는데 아직 B3 관리 중' 인 tick 은 존재하지 않는다.

    엔진에서 ``trig`` 가 찍히는 것은 ``net >= p3_trig`` 인 tick 뿐이고,
    ``p3_trig == tp`` 이므로 그 tick 은 곧바로 아래 셋 중 하나로 끝난다:
    6분 이내면 rescue(승격), 연장 중이면 ext-rescue(승격), 아니면 GX_TP(청산).
    어느 쪽이든 그 다음 tick 에 이 포지션은 더 이상 관리대상이 아니다.

    production 도 같다 -- ``p3_tp_rescued`` 는 부분매도가 **체결된 뒤에만**
    켜지고, 그때 ``note_rescued`` 가 ``p3_promoted`` 도 함께 켠다. 그래서
    ``already_rescued=True and not already_promoted`` 조합이 나오지 않는다.
    """
    assert float(ENGINE_H30["p3_trig"]) == float(ENGINE_H30["tp"])
    for el in (0.0, 3.0, 6.0, 12.0, 21.0, 25.0, 35.0):
        for ext in (False, True):
            if not _reachable(el, ext, True):
                continue
            kind, _ = engine_decide(el=el, net=float(ENGINE_H30["tp"]),
                                    hold_active=True, ext=ext, trig=False,
                                    y3_gap=False, y3_etf=False)
            assert kind in (RESCUE, EXT_RESCUE, TP), (el, ext, kind)

    # production 측: note_rescued 는 두 플래그를 항상 함께 켠다.
    from app.trading.macd2 import chop_regime, state_store
    state = state_store.default_state()
    p3_stack.note_entry_regime(state, chop_regime.REGIME_CHOP)
    p3_stack.note_rescued(state, ENTRY)
    assert state.p3_tp_rescued is True and state.p3_promoted is True
