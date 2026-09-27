"""STRATEGY MODE — 전략 선택의 단일 진실원천 (2026-09-27).

사용자가 고르는 것은 **모드 하나**뿐이다::

    [ N1 ]   기존 전략            = N1 + C1 + SMART + AR1
    [ P3 ]   Adaptive CHOP        = 위 BASE + SHADOW detector + B3 + Y3 + P3 rescue

C1 / SMART / AR1 / SHADOW / B3 / Y3 / P3 rescue 를 **개별로 조합하지 않는다**.
그 조합들은 검증된 적이 없고, 조합 실수 하나가 전략을 조용히 다른 것으로
바꿔 놓기 때문이다. 아래 :func:`derive` 가 모드 하나에서 하위 플래그 전부를
결정하고, :func:`apply` 가 state 에 그대로 찍는다.

P3 는 **N1 을 대체하는 진입전략이 아니다.** 두 모드의 진입은 완전히 같다
(N1 + C1 + SMART + AR1). 차이는 청산 레이어 하나뿐이다 --
P3 모드에서 ``entry_regime == CHOP`` 인 포지션만 B3/Y3/P3 를 탄다. TREND /
WARMUP 으로 진입한 포지션은 두 모드에서 완전히 동일하게 관리된다.

AR1 은 토글이 없다 -- N1 이 켜지면 N1 내부 규칙으로 자동 동작한다
(``time_window_3slot.afternoon_reentry_exception_enabled``). 그래서 아래
derive 결과에서도 AR1 은 "N1 이 켜졌는가" 의 다른 이름일 뿐이다.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from app.trading.macd2 import config

logger = logging.getLogger(__name__)

MODE_N1 = "N1"
MODE_P3 = "P3"
ALL_MODES = (MODE_N1, MODE_P3)

#: 애매하거나 충돌된 조합은 **항상 여기로** 떨어진다.
DEFAULT_MODE = MODE_N1

#: 모드가 소유하는 전략 토글들. 여기 없는 토글(예: 09:03 예약매수)은
#: 모드와 무관하므로 건드리지 않는다.
_STRATEGY_FLAGS = (
    "time_window_2_filter_enabled",
    "time_window_teg_filter_enabled",
    "time_window_3slot_filter_enabled",
    "time_window_twf_filter_enabled",
    "time_window_x2lite_filter_enabled",
    "time_window_h50_filter_enabled",
    "time_window_n1_filter_enabled",
)


def normalize(mode: Any) -> str:
    """입력을 유효한 모드 문자열로. 모르는 값은 기본 모드로 떨어진다."""
    value = str(mode or "").strip().upper()
    return value if value in ALL_MODES else DEFAULT_MODE


def derive(mode: Any) -> dict[str, bool]:
    """모드 하나에서 하위 기능 전부를 결정한다. **여기가 유일한 규칙이다.**

    반환 키는 UI 의 Advanced(읽기전용) 표시와 :func:`apply` 가 함께 쓴다.
    """
    m = normalize(mode)
    base = {
        # 진입 -- 두 모드가 완전히 같다.
        "N1": True,
        "C1": True,
        "SMART": True,
        "AR1": True,           # N1 내부 규칙(별도 토글 없음)
        # 청산 레이어 -- 여기만 다르다.
        "SHADOW": False,
        "B3": False,
        "Y3": False,
        "P3_RESCUE": False,
    }
    if m == MODE_P3:
        base.update({"SHADOW": True, "B3": True, "Y3": True, "P3_RESCUE": True})
    return base


def current(state) -> str:
    """지금 state 의 모드. 저장돼 있지 않으면 레거시 토글에서 추론한다."""
    stored = getattr(state, "strategy_mode", None)
    if stored:
        return normalize(stored)
    return infer_from_legacy(state)[0]


def infer_from_legacy(state) -> tuple[str, str]:
    """``strategy_mode`` 가 없던 시절의 state 를 모드로 옮긴다 (migration).

    반환 ``(mode, reason)``. 규칙은 보수적이다 -- **애매하면 N1** 이다.

      · N1 + C1 + SMART 가 전부 켜져 있고 P3 가 꺼져 있으면      -> N1
      · 위에 더해 P3 까지 켜져 있으면                              -> P3
      · 다른 전략(TW2/TWF/X2-lite/H50 등)이 켜져 있거나 조합이
        어긋나면                                                   -> N1 (안전)
    """
    n1 = bool(getattr(state, "time_window_n1_filter_enabled", False))
    c1 = bool(getattr(state, "c1_peak_protection_enabled", False))
    smart = bool(getattr(state, "smart_sizing_enabled", False))
    p3 = bool(getattr(state, "p3_enabled", False))
    others = [f for f in _STRATEGY_FLAGS
              if f != "time_window_n1_filter_enabled" and bool(getattr(state, f, False))]
    if others:
        return DEFAULT_MODE, f"CONFLICT_OTHER_STRATEGY:{','.join(others)}"
    if not n1:
        return DEFAULT_MODE, "N1_OFF"
    if p3 and c1 and smart:
        return MODE_P3, "FULL_P3_STACK"
    if p3:
        return DEFAULT_MODE, "P3_WITHOUT_BASE"
    if c1 and smart:
        return MODE_N1, "BASE_COMPLETE"
    return DEFAULT_MODE, f"PARTIAL_BASE:c1={c1},smart={smart}"


def apply(state, mode: Any, *, changed_by: str = "ui", now_iso: Optional[str] = None) -> str:
    """모드를 state 에 찍는다 -- 하위 토글 전부를 모드 기준으로 **정규화**한다.

    레거시 토글이 충돌하는 값을 들고 있어도 여기서 전부 덮어쓴다. 그래서
    "개별 토글 조합으로 전략이 꼬이는" 경로가 남지 않는다.

    주문을 내지 않고 포지션도 건드리지 않는다 -- 이미 열려 있는 포지션은 진입
    당시의 스냅샷(``p3_entry_regime``)으로 계속 관리된다.
    """
    m = normalize(mode)
    flags = derive(m)

    # ① 전략 tier -- N1 만 켜고 나머지 상호배타 전략은 전부 끈다.
    for flag in _STRATEGY_FLAGS:
        setattr(state, flag, flag == "time_window_n1_filter_enabled")
    state.time_window_n1_filter_version = config.N1_3SLOT_FILTER_VERSION

    # ② overlay
    state.c1_peak_protection_enabled = bool(flags["C1"])
    state.c1_peak_protection_version = config.C1_FILTER_VERSION
    state.smart_sizing_enabled = bool(flags["SMART"])
    # legacy 미러 — 구버전으로 롤백해도 사이징 토글이 살아남는다
    # (service.set_smart_sizing_enabled 와 같은 관례).
    state.p2_sizing_enabled = bool(flags["SMART"])

    # ③ P3 스택 -- SHADOW/B3/Y3/rescue 는 p3_enabled 하나로 함께 켜고 끈다.
    was_p3 = bool(getattr(state, "p3_enabled", False))
    state.p3_enabled = bool(flags["SHADOW"])
    state.p3_version = config.P3_FILTER_VERSION
    if was_p3 and not state.p3_enabled:
        # P3 -> N1 로 내려올 때 정리하는 것은 **섀도우 진행중 장부 하나**뿐이다.
        #
        # 이미 열려 있는 포지션의 진입 regime 스냅샷(p3_entry_regime 등)은
        # 지우지 않는다 -- 사용자 요구 §11-4 "기존 보유포지션은 진입 시 mode
        # snapshot 유지". 그 값은 포지션이 실제로 닫힐 때
        # worker._clear_position_scoped_state 가 정리한다.
        #
        # 다만 **관리권은 즉시 내려놓는다**: p3_enabled 가 False 가 되는 순간
        # p3_stack.governs_position 이 False 가 되어 B3 가 그 포지션에서
        # 손을 떼고 기존 N1/C1 래더가 다시 맡는다. 기록은 남기되, 사용자가 끈
        # 기능이 계속 주문을 내는 일은 없어야 하기 때문이다.
        #
        # 섀도우 **완료거래 ledger** 도 남겨 둔다(다시 켰을 때 WARMUP 을
        # 처음부터 쌓지 않도록).
        state.p3_shadow = None

    state.strategy_mode = m
    state.strategy_mode_at = now_iso
    state.strategy_mode_by = str(changed_by or "ui")
    return m


def migrate(state) -> Optional[str]:
    """``strategy_mode`` 가 비어 있는 state 를 한 번 옮긴다.

    이미 값이 있으면 아무 것도 하지 않고 ``None`` 을 돌려준다. 옮겼으면
    선택된 모드를 돌려주고 로그를 한 줄 남긴다(무엇을 보고 골랐는지 포함).

    **다른 전략(TW2 / TWF / X2-lite / H50)을 쓰고 있던 state 는 옮기지
    않는다.** 그 경우 ``strategy_mode`` 를 비워 둔 채 ``None`` 을 돌려주고,
    기존 전략이 그대로 돌아간다 -- 모드 도입이 사용자가 고른 전략을 조용히
    N1 으로 바꿔 버리면 안 되기 때문이다. 모드는 사용자가 새 UI 에서
    [N1]/[P3] 를 직접 누를 때 비로소 붙는다.
    """
    if getattr(state, "strategy_mode", None):
        return None
    mode, reason = infer_from_legacy(state)
    if reason.startswith("CONFLICT_OTHER_STRATEGY") or reason == "N1_OFF":
        logger.info("[MACD2][MODE] strategy_mode migration 보류 (reason=%s) "
                    "-- 기존 전략을 그대로 둔다", reason)
        return None
    logger.info("[MACD2][MODE] strategy_mode migration -> %s (reason=%s, "
                "n1=%s c1=%s smart=%s p3=%s)", mode, reason,
                getattr(state, "time_window_n1_filter_enabled", False),
                getattr(state, "c1_peak_protection_enabled", False),
                getattr(state, "smart_sizing_enabled", False),
                getattr(state, "p3_enabled", False))
    state.strategy_mode = mode
    state.strategy_mode_by = f"migration:{reason}"
    return mode


def restore(state) -> Optional[str]:
    """재시작 복원 -- 저장된 모드가 **여전히 유효한지**만 확인한다.

    여기서 하위 플래그를 다시 쓰지 않는다. 한때 그렇게 했는데, 그러면 사용자가
    레거시 토글로 N1/C1/SMART 를 직접 끈 순간 다음 ``load_state()`` 가 그것을
    되돌리고, UI 는 "위젯은 OFF / state 는 ON" 을 보고 setter 를 다시 불러
    무한 rerun 에 빠진다(2026-09-27 AppTest 40초 타임아웃으로 확인).

    그래서 계약을 뒤집었다 -- **플래그가 운영 상태이고, 모드는 그 선택의
    기록**이다. 저장된 플래그 조합이 모드와 어긋나면(사용자가 손으로 다른
    조합을 만든 것이다) 모드 기록만 지우고 플래그는 그대로 둔다.

    P3 모드가 재시작만으로 N1 이 되지 않는 이유는 이 함수가 아니라
    ``deserialize`` 자체다: ``apply`` 가 N1/C1/SMART/P3 를 전부 켜고 각
    필터 버전을 현행으로 찍어 두므로, 버전 불일치 초기화도 상호배타 양보도
    걸리지 않는다(tests/macd2/test_p3_worker.py 가 이 경로를 잠근다).
    """
    stored = getattr(state, "strategy_mode", None)
    if not stored:
        return migrate(state)
    mode = normalize(stored)
    inferred, reason = infer_from_legacy(state)
    if inferred != mode:
        logger.info("[MACD2][MODE] 저장된 모드 %s 와 실제 토글 조합이 어긋난다 "
                    "(reason=%s) -- 모드 기록만 지우고 토글은 그대로 둔다",
                    mode, reason)
        state.strategy_mode = None
        state.strategy_mode_by = f"cleared:{reason}"
        return None
    return mode


def execution_layer(state) -> str:
    """지금 **실제로** 무엇으로 거래하고 있는가 -- UI 의 EXECUTION 줄.

    P3 모드라도 detector 가 준비되지 않았거나(WARMUP) 오류면 실거래는 BASE 다.
    모드와 실행계층을 나눠 보여 주는 이유가 이것이다.
    """
    from app.trading.macd2 import chop_regime

    if current(state) != MODE_P3:
        return "BASE"
    regime = getattr(state, "p3_last_regime", None)
    if regime == chop_regime.REGIME_CHOP:
        return "P3"
    return "BASE"


def shadow_status(state) -> str:
    """UI 의 SHADOW 줄 -- READY / WARMUP / ERROR / OFF."""
    from app.trading.macd2 import chop_regime

    if current(state) != MODE_P3:
        return "OFF"
    regime = getattr(state, "p3_last_regime", None)
    if regime in (chop_regime.REGIME_CHOP, chop_regime.REGIME_TREND):
        return "READY"
    sample = int(getattr(state, "p3_last_shadow_sample", 0) or 0)
    if regime == chop_regime.REGIME_WARMUP:
        return f"WARMUP {sample}/{int(config.P3_DETECTOR_WINDOW)}"
    return "ERROR"
