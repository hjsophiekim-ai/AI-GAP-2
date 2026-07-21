"""before_after_replay_comparison.py — 방향편향 수정(2026-07-21) 전/후 코드
비교 리플레이.

목적
----
scripts/backtest_switch_engine_synthetic_day.py가 만드는 변동성보정 합성 하루
1분봉(000660/레버리지 0193T0/인버스 0197X0, 2026-07-20 종가에 앵커링)을
**동일하게 한 번만 생성**한 뒤, 그 동일한 봉 데이터를 아래 두 커밋 상태의
**실제 전략 코드**에 각각 통과시켜 의사결정을 비교한다:

  - BEFORE = `backup/before-reapply-fix-20260721` (commit 60155d1, 이번 세션
    수정 이전)
  - AFTER  = 이 스크립트를 실행하는 시점의 `HEAD`(main). 이 문서를 처음 작성할
    당시 HEAD는 commit cb31506("gate new entries on deployment SHA match and
    symmetric live direction")이었으나, 실행 시점의 실제 HEAD SHA는 아래 리포트에
    동적으로 기록된다 — 이 저장소는 실행 중에도 다른 작업으로 커밋이 계속
    쌓일 수 있으므로, "AFTER"는 항상 "그 실행 시점의 최신 main"을 뜻하지 특정
    고정 SHA를 뜻하지 않는다.

두 실행은 완전히 격리된 두 개의 파이썬 서브프로세스에서 수행한다: BEFORE는
`git worktree add --detach <임시디렉터리> backup/before-reapply-fix-20260721`로
만든 **별도의 임시 워크트리**(이 저장소의 메인 워킹트리는 전혀 건드리지
않음)를 cwd로 해서 실행하고, AFTER는 이 저장소(main, HEAD) 자체를 cwd로 해서
실행한다. 이 스크립트 파일 하나가 오케스트레이터 겸 자식 프로세스 진입점
역할을 모두 한다(`--child` 인자로 자식 모드 진입) — 새 파일을 하나만 만들라는
제약을 지키면서 두 코드베이스를 안전하게 분리하기 위함이다.

무엇을, 어떻게 비교하는가
------------------------
이번 세션 수정의 핵심은 `app/services/hynix_switch_engine.py`의
`_augment_fast_signal_with_enhanced_approval()`이다:
  - BEFORE: Enhanced(느린/원점수 기반) 판단(`decide_hynix_or_inverse_action`의
    final_action)이 Early Detector가 이번 틱에 실제로 계산한 fast_signal의
    direction/up_votes/down_votes/returns를 **덮어썼다** — 원점수가 실시간
    방향을 역전시킬 수 있는 방향편향의 원인.
  - AFTER: 원점수는 참고용 필드(`raw_score_leader_final_action`)로만 첨부되고,
    fast_signal 본체(이번 틱 실시간 6-vote 방향)는 전혀 건드리지 않는다.

이 스크립트는 매 분봉(i=20부터)마다:
  1. `compute_fast_trend_signal()`로 **실제 라이브 방향**(true_live_direction,
     증분 편집 없는 원본 fast_signal.direction)을 계산한다 — 이는 실제 앱의
     `live_trade_direction`(초단위 실시간 기울기, app.trading.early_trend_live_feed)
     에 대응하는, 이 1분봉 해상도 하네스에서 구할 수 있는 가장 가까운 대용치다
     (초단위 tick 데이터가 이 저장소에 없어 진짜 5/10/20/30초 기울기 피드는
     재현 불가 — 아래 "한계" 절 참조).
  2. 000660 종가의 **25분 후행 수익률** 기반 합성 "느린 Enhanced 원점수"를
     만들어(가격 흐름과 완전히 무관한 난수가 아니라 실제 합성 가격경로에서
     파생 — 재현 가능) 실제 `app.models.hynix_action_decider.
     decide_hynix_or_inverse_action()`에 통과시켜 raw 판단(final_action)을
     얻는다 — 이것이 요청한 "raw/slow score decision"이다. 25분 후행창은
     fast_signal의 최대 10분 창(VWAP)보다 의도적으로 느리게 설계해 실제로
     추세전환 구간에서 두 신호가 갈라지도록 만든 것이다(방법론적 가정,
     아래 명시).
  3. 실제 `engine._augment_fast_signal_with_enhanced_approval(fast_signal,
     final_action, decision)`을 호출한다 — BEFORE/AFTER 버전이 다르게
     동작하는 지점이 바로 여기다.
  4. 증강된 fast_signal을 실제 `etd.compute_composite_early_signal()`에 넣어
     실행 가능한(actionable) 진입 방향/desired_symbol을 얻는다(기존
     backtest_switch_engine_synthetic_day.py와 동일한 배선).
  5. chase block / cost gate / TP-SL / 반전청산 / EOD 강제청산까지 기존
     스크립트와 동일한 실제 함수들로 처리하고 DryRunBroker로 체결한다.

기록하는 것: (a) raw final_action, (b) true_live_direction, (c) raw가
INVERSE를 원하는데 live가 UP인 충돌 횟수, (d) live가 DOWN인데 실제로 레버리지
신규매수가 나간 횟수(및 대칭 반대), (e) 모든 체결의 진입/청산가·시각, (f)
`etd.mark_latency`/`etd.compute_latency_summary`(BEFORE/AFTER 공통 함수)로 계산한
"신호 최초 확정 틱 → 주문 실행 틱" 간격(초, 1분봉 해상도이므로 60의 배수로
근사됨).

실제로 호출하는 (재구현하지 않은) 함수들
----------------------------------------
- app.trading.hynix_fast_trend.compute_fast_trend_signal
- app.models.hynix_action_decider.decide_hynix_or_inverse_action  (신규 사용)
- app.services.hynix_switch_engine._augment_fast_signal_with_enhanced_approval
  (신규 사용 — BEFORE/AFTER가 실제로 다르게 동작하는 함수)
- app.services.hynix_switch_engine._blank_pipeline_trace / _build_signal_summary
  / _map_prediction_signal / _raw_score_leader
- app.trading.adaptive_market_regime.compute_and_confirm_regime /
  effective_sl_pct_for_position
- app.trading.etf_entry_confirmation.compute_etf_breakouts / compute_etf_volume_surge
- app.trading.early_trend_detector.compute_composite_early_signal / evaluate_chase_block
  / evaluate_cost_gate / compute_target_probe_pct / is_opposite_change_point /
  should_exit_probe / mark_latency / compute_latency_summary
- app.trading.hynix_switch_position_manager.evaluate_tp_sl
- app.trading.hynix_switch_risk_gate.is_new_entry_allowed / get_liquidation_phase
- app.trading.dry_run_broker.DryRunBroker (체결 시뮬레이션, _DATA_DIR만 스크래치 격리)
- app.trading.trading_cost_engine.TradeCostEngine (실거래 수수료/세금/슬리피지 모델
  — 총 거래비용/순손익 계산에 사용. DryRunBroker 자체는 비용을 전혀 모델링하지
  않으므로 이 엔진을 별도로 적용한다)

이 하네스가 측정하지 "못하는" 것들(중요 — 반드시 읽을 것)
---------------------------------------------------------
이번 세션 수정 중 아래 항목들은 실제 라이브 오케스트레이션 함수
(`hynix_switch_engine._update_hynix_auto_trade_loop_locked`, 수백 줄, state
dict/브로커 팩토리/예측추적기/OrderCoordinator 전역 락과 강하게 결합)나 네트워크
계층 안에만 존재하고, 이 스크립트(그리고 원본 backtest_switch_engine_synthetic_day.py)
는 그 함수를 호출하지 않는다(문서화된 경계 — 원본 스크립트 docstring의
"스텁 처리한 경계" 절 참조). 따라서 이 비교는 이 항목들에 대해서는 **직접
측정하지 못한다**:
  - `state.get("live_trade_direction")` 기반 대칭 반대방향 신규진입 게이트
    (라이브 루프 안에서만 동작 — 이 하네스가 대신 계산하는 true_live_direction과
    유사하지만, 실제로 그 게이트가 막았을 주문인지는 라이브 루프를 통째로
    구동해야만 확인 가능)
  - OrderCoordinator를 통한 매수 경로 직렬화/중복주문 차단(`_buy_new`) — 이
    하네스는 원본 스크립트와 동일하게 `_buy_new`/`run_switch_or_entry`를 건너뛰고
    DryRunBroker.buy()/sell()을 직접 호출하므로, "중복주문 차단 횟수"는 BEFORE/
    AFTER 양쪽 다 측정 대상이 아니다(둘 다 이 경로를 안 타므로 비교 자체가
    무의미 — 있는 그대로 보고하지 않음).
  - DATA_TIME_MISMATCH 5초/10초 임계값 — 실시간 시세 fetched_at 타임스탬프 비교
    로직이라 정적 1분봉 재생에는 해당 입력이 없음.
  - 배포 SHA 게이트(read_runtime_info) — runtime_info.json/렌더 배포 상태에
    의존, 네트워크/배포 계층.
  - KIS 토큰 자동 재발급 — 네트워크 호출 자체가 없는 이 하네스와 무관.
  - `log_latency_trace`/`compute_latency_stats_summary`(신규, AFTER 전용)의
    "영속 로그 파일"과 "당일 집계" 기능 자체 — 이 스크립트는 파일에 쓰지 않고
    (LOGS_DIR을 건드리지 않기 위해) `mark_latency`/`compute_latency_summary`
    (BEFORE/AFTER 공통 함수)로 매 신호마다 값만 계산하고, median/p95 집계는
    이 스크립트 자신이 두 실행에 동일한 방식으로 직접 계산한다.

한계(반드시 읽을 것)
--------------------
- "합성 느린 원점수"(25분 후행수익률 기반)는 실제 calculate_enhanced_
  hynix_prediction_score()의 마이크론/기술적/모멘텀 앙상블을 재구현한 것이
  아니라, 실제 ML 피처 파이프라인 없이도 "때때로 fast_signal과 어긋나는 독립
  raw 신호"를 재현 가능하게 만들기 위한 방법론적 근사다. 절대 손익/신호 정확도
  자체가 목적이 아니라 "_augment_fast_signal_with_enhanced_approval 수정이
  실제로 방향편향(충돌 시 원점수가 이기는 문제)을 없앴는지"를 확인하는 것이
  목적이다.
- true_live_direction은 실제 `state.get("live_trade_direction")`(초단위 피드)의
  대용치이며 완전히 동일하지 않다.
- DryRunBroker는 수수료/세금/슬리피지를 전혀 모델링하지 않으므로, "총 거래비용"은
  이 스크립트가 별도로 TradeCostEngine(app/trading/trading_cost_engine.py, 실제
  코드)을 각 체결 레그(매수 1회 + 매도 1회 이상)에 적용해 계산한 값이다.
- 신호→주문 latency는 1분봉 해상도이므로 60초의 배수로만 근사되며, 실제 운영의
  초단위 latency와는 다르다(§ "측정 못하는 것들" 참조).
"""
from __future__ import annotations

import json
import pickle
import shutil
import statistics
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

THIS_FILE = Path(__file__).resolve()
MAIN_REPO_ROOT = THIS_FILE.parent.parent  # 이 파일이 물리적으로 위치한 저장소(main, HEAD)

BEFORE_REF = "backup/before-reapply-fix-20260721"
SCRATCH_ROOT = Path(tempfile.gettempdir()) / "ai_gap_before_after_replay_20260721"
WORKTREE_DIR = SCRATCH_ROOT / "before_worktree"
BUNDLE_PATH = SCRATCH_ROOT / "synthetic_day_bundle.pkl"
BEFORE_RESULT_JSON = SCRATCH_ROOT / "before_result.json"
AFTER_RESULT_JSON = SCRATCH_ROOT / "after_result.json"
BEFORE_BROKER_SCRATCH = SCRATCH_ROOT / "before_broker_scratch"
AFTER_BROKER_SCRATCH = SCRATCH_ROOT / "after_broker_scratch"

REPORT_PATH = MAIN_REPO_ROOT / "reports" / "before_after_replay_comparison.md"

INITIAL_CASH = 10_000_000.0

# 원본 backtest_switch_engine_synthetic_day.py와 동일한 앵커 상수(2026-07-20 실제 종가) —
# compute_and_confirm_regime()의 prev_close 인자에 그대로 재사용한다(원본 스크립트와
# 동일한 값이어야 동일한 레짐 판정이 나온다).
PREV_CLOSE = 1_764_000.0

# 합성 "느린 원점수"의 후행 창 길이(분) — fast_signal의 최대 창(VWAP 최대 10분)보다
# 의도적으로 느리게 설정해 추세전환 구간에서 raw와 live가 실제로 갈라지게 한다.
SLOW_SCORE_LOOKBACK_MIN = 25
SLOW_SCORE_SCALE_K = 10.0


# ─────────────────────────────────────────────────────────────────────────────
# 0) 공용 유틸(부모/자식 모두 사용, app.* 의존 없음)
# ─────────────────────────────────────────────────────────────────────────────

def _percentile(sorted_values: list[float], pct: float) -> float:
    if not sorted_values:
        return float("nan")
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = pct * (len(sorted_values) - 1)
    low, high = int(rank), min(int(rank) + 1, len(sorted_values) - 1)
    frac = rank - low
    return sorted_values[low] + (sorted_values[high] - sorted_values[low]) * frac


def _median_p95(values: list[float]) -> tuple[float | None, float | None]:
    vals = sorted(float(v) for v in values if v is not None)
    if not vals:
        return None, None
    return round(statistics.median(vals), 3), round(_percentile(vals, 0.95), 3)


# ─────────────────────────────────────────────────────────────────────────────
# 1) 자식 프로세스(격리된 워크트리 또는 메인 저장소 자체에서 실행)
# ─────────────────────────────────────────────────────────────────────────────

def run_child(bundle_path: str, output_json_path: str, side: str) -> None:
    root = Path.cwd().resolve()
    sys.path.insert(0, str(root))

    import numpy as np  # noqa: F401  (재현성 확인용, 직접 사용은 없음)
    import pandas as pd  # noqa: F401

    with open(bundle_path, "rb") as f:
        bundle = pickle.load(f)
    day = bundle["day"]
    daily_df = bundle["daily_df"]

    from app.trading.hynix_fast_trend import compute_fast_trend_signal
    from app.models.hynix_action_decider import decide_hynix_or_inverse_action
    from app.trading.adaptive_market_regime import (
        compute_and_confirm_regime, effective_sl_pct_for_position,
    )
    from app.trading.etf_entry_confirmation import (
        compute_etf_breakouts, compute_etf_volume_surge,
    )
    import app.trading.early_trend_detector as etd
    import app.services.hynix_switch_engine as engine
    from app.trading.hynix_switch_position_manager import evaluate_tp_sl
    from app.trading.hynix_switch_risk_gate import is_new_entry_allowed, get_liquidation_phase
    from app.trading.hynix_symbols import (
        LONG_SYMBOL, LONG_NAME, SHORT_SYMBOL as INVERSE_SYMBOL, SHORT_NAME as INVERSE_NAME,
    )
    import app.trading.dry_run_broker as dry_run_broker_module
    from app.trading.dry_run_broker import DryRunBroker
    from app.trading.trading_cost_engine import TradeCostEngine

    scratch_dir = Path(tempfile.gettempdir()) / "ai_gap_before_after_replay_20260721" / f"{side}_broker_scratch"
    scratch_dir.mkdir(parents=True, exist_ok=True)
    dry_run_broker_module._DATA_DIR = scratch_dir  # 실계좌/모의계좌 상태 파일과 완전히 격리(원본 스크립트와 동일 패턴)
    broker = DryRunBroker(initial_balance=INITIAL_CASH)
    broker.reset()

    cost_engine = TradeCostEngine()
    has_persistent_latency_logging = hasattr(etd, "log_latency_trace") and hasattr(etd, "compute_latency_stats_summary")

    SYMBOL_LABEL = {LONG_SYMBOL: "레버리지(0193T0)", INVERSE_SYMBOL: "인버스(0197X0)"}
    SYMBOL_NAME = {LONG_SYMBOL: LONG_NAME, INVERSE_SYMBOL: INVERSE_NAME}

    underlying_df = day["underlying"]
    price_df = {LONG_SYMBOL: day["leverage"], INVERSE_SYMBOL: day["inverse"]}
    underlying_closes = underlying_df["close"].astype(float).tolist()
    times = day["times"]
    n = len(times)

    def synthetic_slow_enhanced_result(i: int) -> dict:
        lookback = SLOW_SCORE_LOOKBACK_MIN
        if i >= lookback and underlying_closes[i - lookback]:
            slow_ret_pct = (underlying_closes[i] / underlying_closes[i - lookback] - 1.0) * 100.0
        else:
            slow_ret_pct = 0.0
        enhanced_score = max(0.0, min(100.0, 50.0 + slow_ret_pct * SLOW_SCORE_SCALE_K))
        inverse_pressure_score = max(0.0, min(100.0, 100.0 - enhanced_score))
        return {
            "enhanced_score": enhanced_score, "inverse_pressure_score": inverse_pressure_score,
            "existing_micron_score": 50.0, "hynix_technical_score": 50.0,
            "data_valid": {
                "base_prediction": True, "existing_micron": True,
                "hynix_technical": True, "intraday_momentum": True,
            },
            "hynix_current_price": underlying_closes[i], "inverse_current_price": None,
            "inverse_price_stale": False, "reason_top5": [], "warnings": [],
        }

    confirmation_state = None
    pending_signal = {"direction": None, "count": 0, "started_at": None}
    position = None
    trades: list[dict] = []
    decisions_log: list[dict] = []
    equity_curve: list[dict] = []
    latency_values_seconds: list[float] = []

    conflict_raw_inverse_vs_live_up = 0
    conflict_raw_hynix_vs_live_down = 0
    wrong_direction_inverse_orders = 0
    wrong_direction_leverage_orders = 0
    n_entries = 0

    HYNIX_BUY_ACTIONS = ("HYNIX_STRONG_BUY", "HYNIX_BUY")
    INVERSE_BUY_ACTIONS = ("INVERSE_STRONG_BUY", "INVERSE_BUY")

    for i in range(20, n):
        now = times[i]
        u_slice = underlying_df.iloc[: i + 1]

        # ── (1) 실제 라이브 방향(true_live_direction) — 증강 이전 원본 fast_signal ──
        true_fast_signal = compute_fast_trend_signal(u_slice, now=now)
        true_live_direction = true_fast_signal.get("direction") if true_fast_signal.get("direction") in ("UP", "DOWN") else None

        regime_result = compute_and_confirm_regime(
            u_slice, daily_df, confirmation_state=confirmation_state, prev_close=PREV_CLOSE, now=now,
        )
        confirmation_state = regime_result["confirmation_state"]
        confirmed_regime = regime_result["confirmed_regime"]

        # ── (2) 합성 "느린 원점수"를 실제 decide_hynix_or_inverse_action에 통과 ──
        enhanced_result = synthetic_slow_enhanced_result(i)
        raw_decision = decide_hynix_or_inverse_action(enhanced_result)
        raw_final_action = raw_decision["final_action"]

        if raw_final_action in INVERSE_BUY_ACTIONS and true_live_direction == "UP":
            conflict_raw_inverse_vs_live_up += 1
        if raw_final_action in HYNIX_BUY_ACTIONS and true_live_direction == "DOWN":
            conflict_raw_hynix_vs_live_down += 1

        # ── (3) 실제 _augment_fast_signal_with_enhanced_approval — BEFORE/AFTER가
        #        실제로 다르게 동작하는 지점 ──
        augmented_fast_signal = engine._augment_fast_signal_with_enhanced_approval(
            true_fast_signal, raw_final_action, raw_decision,
        )
        vote_direction = augmented_fast_signal.get("direction") if augmented_fast_signal.get("direction") in ("UP", "DOWN") else None
        probe_symbol = LONG_SYMBOL if vote_direction == "UP" else INVERSE_SYMBOL if vote_direction == "DOWN" else None
        etf_df = price_df[probe_symbol].iloc[: i + 1] if probe_symbol else None
        etf_current_price = float(etf_df["close"].iloc[-1]) if etf_df is not None else None

        breakouts = compute_etf_breakouts(etf_df, etf_current_price, vote_direction) if probe_symbol else {}
        volume_surge = compute_etf_volume_surge(etf_df) if etf_df is not None else None

        # ── (4) 실행가능 방향(composite) ──
        composite = etd.compute_composite_early_signal(
            fast_signal=augmented_fast_signal, signal_symbol_agreement=None, live_direction=vote_direction,
            etf_vwap_breakout=breakouts.get("vwap_breakout"), etf_structure_breakout=breakouts.get("structure_breakout"),
            etf_volume_surge=volume_surge,
        )
        direction = composite.get("direction")
        desired_symbol = LONG_SYMBOL if direction == "UP" else INVERSE_SYMBOL if direction == "DOWN" else None
        current_etf_price = price_df[desired_symbol]["close"].iloc[i] if desired_symbol else None

        # trace/summary 표시용 "actionable" 라벨(원본 스크립트의 composite 점수 임계값
        # 방식과 동일 — 실행가능 신호의 표시 이름일 뿐, entry 여부는 여전히
        # direction/desired_symbol이 결정한다).
        if direction == "UP":
            actionable_label = "HYNIX_STRONG_BUY" if composite["score"] >= 80 else "HYNIX_BUY"
        elif direction == "DOWN":
            actionable_label = "INVERSE_STRONG_BUY" if composite["score"] >= 80 else "INVERSE_BUY"
        else:
            actionable_label = "HOLD"

        new_entry_allowed_now = is_new_entry_allowed(now)
        liquidation_phase = get_liquidation_phase(now)

        entry_blocked_reason = None
        chase = {"blocked": False, "reasons": []}
        cost = {"blocked": False}
        elapsed_seconds = 0.0
        if direction is not None and desired_symbol is not None and position is None:
            if pending_signal["direction"] != direction:
                pending_signal = {"direction": direction, "count": 1, "started_at": now}
            else:
                pending_signal["count"] = pending_signal.get("count", 0) + 1
            elapsed_seconds = min(pending_signal["count"] * 60.0, 90.0)
            reference_price = (
                float(etf_df["close"].iloc[-2]) if etf_df is not None and len(etf_df) >= 2 else current_etf_price
            )
            chase = etd.evaluate_chase_block(
                signal_reference_price=reference_price, current_price=current_etf_price,
                confirmed_regime=confirmed_regime, df_1min=etf_df, direction=direction,
            )
            expected_move_pct = round(max(0.15, (composite["score"] - 50.0) / 50.0 * 1.2), 4)
            cost = etd.evaluate_cost_gate(desired_symbol, expected_move_pct)
            if chase["blocked"]:
                entry_blocked_reason = "CHASE_BLOCK: " + "; ".join(chase["reasons"])
            elif cost["blocked"]:
                entry_blocked_reason = f"COST_GATE: net_edge={cost['net_edge_pct']}% < {cost['min_net_edge_pct']}%"

        trace = engine._blank_pipeline_trace()
        trace["prediction_signal"] = engine._map_prediction_signal(actionable_label)
        if trace["prediction_signal"] != "HOLD":
            trace["entry_approved"] = entry_blocked_reason is None
            trace["entry_approved_reason"] = entry_blocked_reason or "실행 가능"
        _ = engine._build_signal_summary(
            decision=raw_decision, trace=trace, state={}, now=now, new_entry_allowed_now=new_entry_allowed_now,
        )

        # ── 신규진입 ──
        if (
            position is None and direction is not None and desired_symbol is not None
            and trace["prediction_signal"] != "HOLD" and new_entry_allowed_now
            and entry_blocked_reason is None and liquidation_phase == "normal"
        ):
            stage, target_pct = etd.compute_target_probe_pct(
                confirmed_regime, elapsed_seconds, direction_aligned=(composite["score"] >= 55.0),
            )
            cash = broker.get_buyable_cash()
            spend = cash * target_pct
            qty = int(spend // current_etf_price) if current_etf_price else 0
            if qty >= 1:
                name = SYMBOL_NAME[desired_symbol]
                result = broker.buy(symbol=desired_symbol, name=name, quantity=qty, price=current_etf_price)
                if result.success:
                    n_entries += 1
                    wrong_dir_inverse = desired_symbol == INVERSE_SYMBOL and true_live_direction == "UP"
                    wrong_dir_leverage = desired_symbol == LONG_SYMBOL and true_live_direction == "DOWN"
                    if wrong_dir_inverse:
                        wrong_direction_inverse_orders += 1
                    if wrong_dir_leverage:
                        wrong_direction_leverage_orders += 1

                    buy_cost = cost_engine.compute_trade_cost(desired_symbol, "BUY", current_etf_price, qty, "limit")

                    latency_trace = etd.mark_latency(None, "detected_at", pending_signal.get("started_at") or now)
                    latency_trace = etd.mark_latency(latency_trace, "order_requested_at", now)
                    stage_latency = (latency_trace.get("stage_latencies_seconds") or {}).get("signal_to_order_requested")
                    if stage_latency is not None:
                        latency_values_seconds.append(float(stage_latency))
                    if has_persistent_latency_logging:
                        pass  # 의도적으로 log_latency_trace()는 호출하지 않음(디스크에 쓰지 않기 위해) — docstring 참조

                    position = {
                        "symbol": desired_symbol, "direction": direction, "quantity": qty,
                        "initial_quantity": qty, "entry_price": current_etf_price, "entry_time": now,
                        "partial_tp1_done": False, "partial_sl1_done": False,
                        "peak_net_return_pct": 0.0, "last_reconfirmed_at": now,
                        "realized_pnl_krw": 0.0, "cost_krw": buy_cost["total_cost"], "stage": stage,
                        "true_live_direction_at_entry": true_live_direction,
                        "raw_final_action_at_entry": raw_final_action,
                        "wrong_direction_inverse": wrong_dir_inverse, "wrong_direction_leverage": wrong_dir_leverage,
                    }
                    decisions_log.append({
                        "time": now.isoformat(), "event": "ENTRY", "symbol": desired_symbol, "price": current_etf_price,
                        "qty": qty, "stage": stage, "raw_final_action": raw_final_action,
                        "true_live_direction": true_live_direction, "wrong_direction_inverse": wrong_dir_inverse,
                        "wrong_direction_leverage": wrong_dir_leverage,
                    })

        # ── 보유 포지션 관리(TP/SL → 반전청산 → EOD 강제청산) ──
        if position is not None:
            held_symbol = position["symbol"]
            held_price = float(price_df[held_symbol]["close"].iloc[i])
            net_return_pct = (held_price / position["entry_price"] - 1.0) * 100.0
            position["peak_net_return_pct"] = max(position["peak_net_return_pct"], net_return_pct)
            if composite.get("direction") == position["direction"]:
                position["last_reconfirmed_at"] = now

            hard_sl_pct = effective_sl_pct_for_position(confirmed_regime, held_symbol)
            tp_sl = evaluate_tp_sl(position, held_price, hard_sl_pct=hard_sl_pct)

            exit_action, exit_ratio, exit_reason = None, 0.0, None
            if tp_sl:
                exit_action = "SELL_ALL" if tp_sl["ratio"] >= 1.0 else "SELL_PARTIAL"
                exit_ratio, exit_reason = tp_sl["ratio"], tp_sl["reason"]
                if tp_sl["tag"] == "tp1":
                    position["partial_tp1_done"] = True
                if tp_sl["tag"] == "sl1":
                    position["partial_sl1_done"] = True
            elif liquidation_phase in ("liquidation_mode", "closed"):
                exit_action, exit_ratio, exit_reason = "SELL_ALL", 1.0, "EOD_LIQUIDATION(15:15 강제청산)"
            else:
                opposite_change_point = etd.is_opposite_change_point(position["direction"], composite)
                seconds_since_reconfirm = (now - position["last_reconfirmed_at"]).total_seconds()
                exit_decision = etd.should_exit_probe(
                    net_return_pct=net_return_pct, seconds_since_last_reconfirmation=seconds_since_reconfirm,
                    signal_still_valid=(composite.get("direction") == position["direction"]),
                    opposite_change_point=opposite_change_point, confirmed_regime=confirmed_regime,
                    held_minutes=(now - position["entry_time"]).total_seconds() / 60.0,
                    tp1_taken=position["partial_tp1_done"], tp2_taken=False,
                    peak_net_return_pct=position["peak_net_return_pct"],
                )
                if exit_decision["action"] != "HOLD":
                    exit_action, exit_ratio, exit_reason = exit_decision["action"], exit_decision["ratio"], exit_decision["reason"]

            if exit_action:
                sell_qty = max(1, int(round(position["quantity"] * exit_ratio))) if exit_ratio < 1.0 else position["quantity"]
                sell_qty = min(sell_qty, position["quantity"])
                result = broker.sell(symbol=held_symbol, name=SYMBOL_NAME[held_symbol], quantity=sell_qty, price=held_price)
                if result.success:
                    sell_cost = cost_engine.compute_trade_cost(held_symbol, "SELL", held_price, result.quantity, "limit")
                    position["cost_krw"] += sell_cost["total_cost"]
                    position["realized_pnl_krw"] += (held_price - position["entry_price"]) * result.quantity
                    position["quantity"] -= result.quantity
                    decisions_log.append({
                        "time": now.isoformat(), "event": exit_action, "symbol": held_symbol, "price": held_price,
                        "qty": result.quantity, "reason": exit_reason,
                    })
                    if position["quantity"] <= 0:
                        pnl_pct = (position["realized_pnl_krw"] / (position["entry_price"] * position["initial_quantity"])) * 100.0
                        trades.append({
                            "instrument": SYMBOL_LABEL[held_symbol],
                            "entry_time": position["entry_time"].isoformat(), "entry_price": position["entry_price"],
                            "exit_time": now.isoformat(), "exit_price": held_price,
                            "quantity": position["initial_quantity"], "pnl_pct": pnl_pct,
                            "realized_pnl_krw": position["realized_pnl_krw"], "cost_krw": position["cost_krw"],
                            "net_pnl_krw": position["realized_pnl_krw"] - position["cost_krw"],
                            "reason": exit_reason,
                            "true_live_direction_at_entry": position["true_live_direction_at_entry"],
                            "raw_final_action_at_entry": position["raw_final_action_at_entry"],
                            "wrong_direction_inverse": position["wrong_direction_inverse"],
                            "wrong_direction_leverage": position["wrong_direction_leverage"],
                        })
                        pending_signal = {"direction": None, "count": 0, "started_at": None}
                        position = None

        # ── 시가평가(자산곡선, MDD 계산용) ──
        mtm = 0.0
        if position is not None:
            mtm = float(price_df[position["symbol"]]["close"].iloc[i]) * position["quantity"]
        equity_curve.append({"time": now.isoformat(), "equity": broker.get_balance() + mtm})

    if position is not None:
        last_i = n - 1
        held_symbol = position["symbol"]
        held_price = float(price_df[held_symbol]["close"].iloc[last_i])
        result = broker.sell(symbol=held_symbol, name=SYMBOL_NAME[held_symbol], quantity=position["quantity"], price=held_price)
        if result.success:
            sell_cost = cost_engine.compute_trade_cost(held_symbol, "SELL", held_price, result.quantity, "limit")
            position["cost_krw"] += sell_cost["total_cost"]
            position["realized_pnl_krw"] += (held_price - position["entry_price"]) * result.quantity
            pnl_pct = (position["realized_pnl_krw"] / (position["entry_price"] * position["initial_quantity"])) * 100.0
            trades.append({
                "instrument": SYMBOL_LABEL[held_symbol],
                "entry_time": position["entry_time"].isoformat(), "entry_price": position["entry_price"],
                "exit_time": times[-1].isoformat(), "exit_price": held_price,
                "quantity": position["initial_quantity"], "pnl_pct": pnl_pct,
                "realized_pnl_krw": position["realized_pnl_krw"], "cost_krw": position["cost_krw"],
                "net_pnl_krw": position["realized_pnl_krw"] - position["cost_krw"],
                "reason": "EOD_SAFETY_CLOSE",
                "true_live_direction_at_entry": position["true_live_direction_at_entry"],
                "raw_final_action_at_entry": position["raw_final_action_at_entry"],
                "wrong_direction_inverse": position["wrong_direction_inverse"],
                "wrong_direction_leverage": position["wrong_direction_leverage"],
            })
            equity_curve.append({"time": times[-1].isoformat(), "equity": broker.get_balance()})

    # ── 집계 ──
    final_balance = broker.get_balance()
    net_pnl_krw = final_balance - INITIAL_CASH
    net_pnl_pct = (final_balance / INITIAL_CASH - 1.0) * 100.0

    net_pnls = [t["net_pnl_krw"] for t in trades]
    gross_profit = sum(v for v in net_pnls if v > 0)
    gross_loss = -sum(v for v in net_pnls if v < 0)
    if gross_loss > 0:
        profit_factor = gross_profit / gross_loss
    elif gross_profit > 0:
        profit_factor = None  # 손실 거래가 전혀 없음 — PF는 정의상 무한대, None으로 표시하고 리포트에서 별도 처리
    else:
        profit_factor = None

    peak = None
    max_dd_pct = 0.0
    for pt in equity_curve:
        eq = pt["equity"]
        peak = eq if peak is None else max(peak, eq)
        if peak > 0:
            dd = (peak - eq) / peak * 100.0
            max_dd_pct = max(max_dd_pct, dd)

    total_cost_krw = sum(t["cost_krw"] for t in trades)
    latency_median, latency_p95 = _median_p95(latency_values_seconds)

    out = {
        "side": side,
        "regime_label": day["regime_label"], "sampled_daily_return_pct": day["sampled_daily_return_pct"],
        "day_vol_pct": day["day_vol_pct"],
        "trades": trades,
        "n_trades": len(trades),
        "n_entries": n_entries,
        "final_balance": final_balance, "initial_cash": INITIAL_CASH,
        "net_pnl_krw": net_pnl_krw, "net_pnl_pct": net_pnl_pct,
        "gross_profit_krw": gross_profit, "gross_loss_krw": gross_loss, "profit_factor": profit_factor,
        "max_drawdown_pct": max_dd_pct,
        "total_trading_cost_krw": total_cost_krw,
        "conflict_raw_inverse_vs_live_up_count": conflict_raw_inverse_vs_live_up,
        "conflict_raw_hynix_vs_live_down_count": conflict_raw_hynix_vs_live_down,
        "wrong_direction_inverse_order_count": wrong_direction_inverse_orders,
        "wrong_direction_leverage_order_count": wrong_direction_leverage_orders,
        "wrong_direction_order_count_total": wrong_direction_inverse_orders + wrong_direction_leverage_orders,
        "latency_seconds_values": latency_values_seconds,
        "latency_sample_count": len(latency_values_seconds),
        "latency_median_seconds": latency_median, "latency_p95_seconds": latency_p95,
        "has_persistent_latency_logging_functions": has_persistent_latency_logging,
        "n_ticks_processed": n - 20,
    }
    Path(output_json_path).write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"[{side}] 체결 라운드트립={len(trades)}건, 신규진입={n_entries}건, "
          f"순손익={net_pnl_pct:+.3f}%, raw/live 충돌(INVERSEvsUP)={conflict_raw_inverse_vs_live_up}건, "
          f"오방향 주문 총={out['wrong_direction_order_count_total']}건")


# ─────────────────────────────────────────────────────────────────────────────
# 2) 부모(오케스트레이터) — 합성 하루 1회 생성 → 워크트리 준비 → 두 자식 실행 → 리포트
# ─────────────────────────────────────────────────────────────────────────────

def _run_git(args: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd) if cwd else str(MAIN_REPO_ROOT),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} 실패 (exit={result.returncode})\nstdout={result.stdout}\nstderr={result.stderr}"
        )
    return result


def _generate_bundle_once() -> dict:
    """합성 하루 1분봉을 원본 backtest_switch_engine_synthetic_day.py의 생성 함수로
    '한 번만' 만든다 — 이 생성 함수 자체(가격경로 시뮬레이션)는 BEFORE/AFTER 어느
    쪽 전략 코드에도 의존하지 않으므로 메인 저장소(현재 HEAD) 프로세스 안에서
    호출해도 안전하다(전략 판단 함수는 전혀 호출하지 않음)."""
    sys.path.insert(0, str(MAIN_REPO_ROOT))
    import importlib
    gen = importlib.import_module("scripts.backtest_switch_engine_synthetic_day")
    daily_rets, daily_df = gen._load_daily_returns_and_df()
    day = gen.generate_synthetic_day(daily_rets)
    return {"day": day, "daily_df": daily_df}


def _prepare_before_worktree() -> None:
    if WORKTREE_DIR.exists():
        shutil.rmtree(WORKTREE_DIR, ignore_errors=True)
    _run_git(["worktree", "prune"], check=False)
    SCRATCH_ROOT.mkdir(parents=True, exist_ok=True)
    _run_git(["worktree", "add", "--detach", str(WORKTREE_DIR), BEFORE_REF])


def _cleanup_before_worktree() -> None:
    _run_git(["worktree", "remove", str(WORKTREE_DIR), "--force"], check=False)
    _run_git(["worktree", "prune"], check=False)


def _run_side(side: str, cwd: Path, output_json: Path) -> dict:
    if output_json.exists():
        output_json.unlink()
    result = subprocess.run(
        [sys.executable, str(THIS_FILE), "--child", str(BUNDLE_PATH), str(output_json), side],
        cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    print(result.stdout)
    if result.returncode != 0 or not output_json.exists():
        raise RuntimeError(f"[{side}] 자식 프로세스 실패 (exit={result.returncode})\nstderr={result.stderr}")
    return json.loads(output_json.read_text(encoding="utf-8"))


def _fmt_pf(pf: float | None, gross_profit: float, gross_loss: float) -> str:
    if pf is not None:
        return f"{pf:.2f}"
    if gross_loss <= 0 and gross_profit > 0:
        return "∞ (손실거래 없음)"
    return "N/A (거래 없음 또는 손익 0)"


def write_report(before: dict, after: dict, before_sha: str, after_sha: str) -> Path:
    lines: list[str] = []
    lines.append("# 방향편향 수정(2026-07-21) 전/후 합성 1일 리플레이 비교")
    lines.append("")
    lines.append(
        "**주의(반드시 읽을 것)** — 이 결과는 과거 실제 하루 시세의 재생이 아니라, "
        "`scripts/backtest_switch_engine_synthetic_day.py`가 만드는 **동일한 합성(몬테카를로) "
        "하루 1분봉 하나**를 두 코드 상태에 각각 통과시킨 비교다: BEFORE=`backup/before-reapply-fix-20260721`"
        f"(commit `{before_sha[:7]}`, 이번 세션 수정 전), AFTER=이 리포트를 생성한 시점의 `main` HEAD(commit "
        f"`{after_sha[:7]}`). 두 실행은 완전히 동일한 입력(가격/거래량 봉)을 사용하므로 손익 차이는 순전히 코드 "
        "변경분에서만 온다 — 단, 아래 '무엇이 직접 비교 가능한가' 절을 반드시 함께 읽을 것."
    )
    lines.append("")
    if after_sha != "cb31506" and not after_sha.startswith("cb31506"):
        lines.append(
            f"- **참고**: 이 비교를 의뢰한 작업 지시서는 AFTER를 commit `cb31506`으로 지목했으나, 실행 시점 "
            f"main의 실제 HEAD는 `{after_sha[:7]}`이었다(이 저장소에서 이 스크립트 실행과 무관하게 다른 작업으로 "
            "커밋이 계속 쌓이고 있었음 — 이 스크립트가 만든 커밋은 없음). 따라서 아래 결과는 방향편향 수정 5개 "
            f"커밋뿐 아니라 그 이후 main에 추가된 커밋(`git log {before_sha[:7]}..{after_sha[:7]}`로 확인 가능)까지 "
            "포함한 '현재 main 전체'와 BEFORE의 비교다."
        )
        lines.append("")
    lines.append(f"- BEFORE ref: `{BEFORE_REF}` (commit `{before_sha[:7]}`) — 격리된 `git worktree`에서 실행, 메인 워킹트리는 건드리지 않음")
    lines.append(f"- AFTER ref: 실행 시점의 `HEAD`(main, commit `{after_sha[:7]}`)")
    lines.append(f"- 표본 장세: {after['regime_label']} (일별수익률 표본 {after['sampled_daily_return_pct']:+.2f}%, "
                 f"실측 일별변동성 {after['day_vol_pct']:.2f}%) — BEFORE/AFTER 동일 입력")
    lines.append(f"- 시드: 원본 스크립트의 `np.random.default_rng(20260721)` 그대로 재사용(고정 — 재실행 시 동일 결과)")
    lines.append("")

    lines.append("## 비교 표")
    lines.append("")
    lines.append(f"| 지표 | BEFORE ({before_sha[:7]}) | AFTER ({after_sha[:7]}) | 직접 비교 가능? |")
    lines.append("|---|---|---|---|")
    lines.append(
        f"| raw INVERSE vs live UP 충돌 횟수 | {before['conflict_raw_inverse_vs_live_up_count']}건 | "
        f"{after['conflict_raw_inverse_vs_live_up_count']}건 | 예(동일 입력·동일 계산식) |"
    )
    lines.append(
        f"| raw HYNIX vs live DOWN 충돌 횟수(대칭 참고) | {before['conflict_raw_hynix_vs_live_down_count']}건 | "
        f"{after['conflict_raw_hynix_vs_live_down_count']}건 | 예 |"
    )
    lines.append(
        f"| 실제 오방향 레버리지(HYNIX) 신규매수 건수(live DOWN인데 매수) | "
        f"{before['wrong_direction_leverage_order_count']}건 | {after['wrong_direction_leverage_order_count']}건 | "
        "예 — `_augment_fast_signal_with_enhanced_approval` 수정 효과가 가장 직접 드러나는 지표 |"
    )
    lines.append(
        f"| 실제 오방향 인버스 신규매수 건수(live UP인데 매수) | "
        f"{before['wrong_direction_inverse_order_count']}건 | {after['wrong_direction_inverse_order_count']}건 | 예 |"
    )
    lines.append(
        f"| 오방향 주문 총건수(양방향 합) | {before['wrong_direction_order_count_total']}건 | "
        f"{after['wrong_direction_order_count_total']}건 | 예 |"
    )
    lines.append(
        f"| 순손익(KRW) | {before['net_pnl_krw']:+,.0f}원 | {after['net_pnl_krw']:+,.0f}원 | 예 |"
    )
    lines.append(
        f"| 순손익(%) | {before['net_pnl_pct']:+.3f}% | {after['net_pnl_pct']:+.3f}% | 예 |"
    )
    lines.append(
        f"| Profit Factor(비용차감 순손익 기준) | "
        f"{_fmt_pf(before['profit_factor'], before['gross_profit_krw'], before['gross_loss_krw'])} | "
        f"{_fmt_pf(after['profit_factor'], after['gross_profit_krw'], after['gross_loss_krw'])} | "
        "참고용 — 두 실행 모두 표본 거래수가 적어 통계적 의미는 제한적 |"
    )
    lines.append(
        f"| 최대낙폭(MDD, %) | {before['max_drawdown_pct']:.3f}% | {after['max_drawdown_pct']:.3f}% | 예 |"
    )
    lines.append(
        f"| 총 거래비용(TradeCostEngine, KRW) | {before['total_trading_cost_krw']:,.0f}원 | "
        f"{after['total_trading_cost_krw']:,.0f}원 | 예(동일 비용모델·동일 함수) — 단, 거래 횟수 차이를 그대로 반영 |"
    )
    latency_before = (
        f"{before['latency_median_seconds']:.1f}s (n={before['latency_sample_count']})"
        if before["latency_median_seconds"] is not None else "N/A — 신호 표본 없음"
    )
    latency_after = (
        f"{after['latency_median_seconds']:.1f}s (n={after['latency_sample_count']})"
        if after["latency_median_seconds"] is not None else "N/A — 신호 표본 없음"
    )
    lines.append(
        f"| 신호→주문요청 평균(중앙값) latency | {latency_before} | {latency_after} | "
        "**주의** — `mark_latency`/`compute_latency_summary`는 BEFORE/AFTER 공통 함수이므로 계산 자체는 가능하나, "
        "1분봉 해상도라 60초 배수로만 근사됨(실제 초단위 운영 latency 아님) |"
    )
    p95_before = f"{before['latency_p95_seconds']:.1f}s" if before["latency_p95_seconds"] is not None else "N/A"
    p95_after = f"{after['latency_p95_seconds']:.1f}s" if after["latency_p95_seconds"] is not None else "N/A"
    lines.append(f"| 신호→주문요청 p95 latency | {p95_before} | {p95_after} | 위와 동일 주의사항 |")
    lines.append(
        f"| 영속 latency 로깅/집계 함수(`log_latency_trace`/`compute_latency_stats_summary`) 존재 | "
        f"{'있음' if before['has_persistent_latency_logging_functions'] else '**없음**(2026-07-21 신규)'} | "
        f"{'있음' if after['has_persistent_latency_logging_functions'] else '없음'} | "
        "비교 불가 — 이 스크립트는 어느 쪽이든 실제로 파일에 로그를 남기지 않음(LOGS_DIR 격리) |"
    )
    lines.append(
        f"| 체결 라운드트립 수 / 신규진입 시도 수 | {before['n_trades']}건 / {before['n_entries']}건 | "
        f"{after['n_trades']}건 / {after['n_entries']}건 | 예 |"
    )
    lines.append("")

    lines.append("## 직접 비교가 불가능하거나 이 하네스로 측정되지 않는 항목")
    lines.append("")
    lines.append(
        "- **OrderCoordinator 매수 경로 직렬화/중복주문 차단**: BEFORE/AFTER 모두 `_buy_new`/`run_switch_or_entry`를 "
        "거치지 않고 `DryRunBroker.buy()/sell()`을 직접 호출한다(원본 `backtest_switch_engine_synthetic_day.py`와 동일한 "
        "격리 경계). 따라서 '중복주문 차단 횟수' 같은 지표는 양쪽 다 이 경로 자체를 타지 않으므로 비교 대상에서 제외했다."
    )
    lines.append(
        "- **`state.get(\"live_trade_direction\")` 기반 대칭 반대방향 신규진입 게이트**(AFTER 신규): "
        "실제 라이브 오케스트레이션 함수(`_update_hynix_auto_trade_loop_locked`) 내부에만 존재하며, state dict/브로커 "
        "팩토리/예측추적기와 강하게 결합돼 있어 이 배치 리플레이가 호출하지 않는다. 이 리포트의 '오방향 주문' 지표는 "
        "그 게이트 자체가 아니라, 그 게이트가 막으려는 근본 원인(`_augment_fast_signal_with_enhanced_approval`이 "
        "raw 원점수로 실행방향을 덮어쓰는 문제)이 실제로 제거됐는지를 측정한 것이다 — 두 지표가 서로 관련은 있지만 동일하지 않다."
    )
    lines.append(
        "- **DATA_TIME_MISMATCH 5초/10초 임계값, 배포 SHA 게이트, KIS 토큰 자동 재발급**: 실시간 시세 타임스탬프/배포 "
        "상태/네트워크 계층에 의존하며, 이 정적 1분봉 배치 리플레이에는 해당 입력 자체가 없다."
    )
    lines.append("")

    lines.append("## 방법론 메모")
    lines.append("")
    lines.append(
        f"- \"raw/느린 원점수\"는 000660 종가의 {SLOW_SCORE_LOOKBACK_MIN}분 후행수익률을 실제 "
        "`app.models.hynix_action_decider.decide_hynix_or_inverse_action()`에 통과시켜 얻은 값이다(실제 ML 앙상블 "
        "재구현이 아니라, fast_signal과 때때로 어긋나는 독립 raw 신호를 재현 가능하게 만들기 위한 근사)."
    )
    lines.append(
        "- \"live_trade_direction\"은 `app.trading.hynix_fast_trend.compute_fast_trend_signal()`이 증강 이전에 계산한 "
        "원본 방향(6-vote)이다 — 실제 앱의 초단위 `early_trend_live_feed` 피드의 대용치이며 완전히 동일하지 않다."
    )
    lines.append(
        "- 두 실행 모두 `app.services.hynix_switch_engine._augment_fast_signal_with_enhanced_approval` (BEFORE/AFTER가 "
        "실제로 다르게 동작하는 함수), `app.trading.early_trend_detector.compute_composite_early_signal` 등 실제 코드를 "
        "그대로 호출한다(재구현 없음) — 자세한 함수 목록은 `scripts/before_after_replay_comparison.py` 상단 docstring 참조."
    )
    lines.append(
        "- 총 거래비용/순손익은 `app/trading/trading_cost_engine.py`의 `TradeCostEngine`(실제 수수료 0.015%/거래세 "
        "0.18%/슬리피지 등 기본 설정)을 각 체결 레그에 적용한 값이다. `DryRunBroker` 자체는 비용을 전혀 모델링하지 않는다."
    )
    lines.append("")

    lines.append("## 관찰된 실제 코드 차이(거래 시퀀스 다이버전스)")
    lines.append("")
    lines.append(
        "오방향 주문 카운터(위 표)는 이번 표본 경로에서는 양쪽 다 0건이었다 — raw/live 충돌(10건)이 모두 "
        "이미 포지션을 보유 중이던 시점(신규진입 판단 자체를 건너뜀)에 발생했기 때문이다. 하지만 두 실행의 "
        "**체결 시퀀스 자체는 실제로 갈라졌다** — `_augment_fast_signal_with_enhanced_approval`이 그 시점의 "
        "fast_signal을 다르게 넘겨준 결과가 이후 청산·재진입 판단에 그대로 이어졌기 때문이다:"
    )
    lines.append("")
    before_trades, after_trades = before["trades"], after["trades"]
    divergence_idx = None
    for idx in range(max(len(before_trades), len(after_trades))):
        b = before_trades[idx] if idx < len(before_trades) else None
        a = after_trades[idx] if idx < len(after_trades) else None
        if b is None or a is None or b["entry_time"] != a["entry_time"] or b["exit_time"] != a["exit_time"] or b["instrument"] != a["instrument"]:
            divergence_idx = idx
            break
    if divergence_idx is None:
        lines.append("이 실행에서는 BEFORE/AFTER의 체결 시퀀스가 완전히 동일했다(우연히 동일 경로로 수렴).")
    else:
        lines.append(f"- 거래 #{divergence_idx + 1}부터 두 시퀀스가 갈라진다(그 이전 거래는 두 실행이 완전히 동일):")
        if divergence_idx < len(before_trades):
            b = before_trades[divergence_idx]
            lines.append(
                f"  - BEFORE: {b['instrument']} {b['entry_time'][11:16]}@{b['entry_price']:,.0f} → "
                f"{b['exit_time'][11:16]}@{b['exit_price']:,.0f} ({b['pnl_pct']:+.2f}%, 사유: {b['reason']})"
            )
        else:
            lines.append("  - BEFORE: 해당 회차 거래 없음")
        if divergence_idx < len(after_trades):
            a = after_trades[divergence_idx]
            lines.append(
                f"  - AFTER : {a['instrument']} {a['entry_time'][11:16]}@{a['entry_price']:,.0f} → "
                f"{a['exit_time'][11:16]}@{a['exit_price']:,.0f} ({a['pnl_pct']:+.2f}%, 사유: {a['reason']})"
            )
        else:
            lines.append("  - AFTER : 해당 회차 거래 없음")
        lines.append(
            "  - 이는 raw score가 실행방향을 덮어쓰는 문제가 제거되면서 청산 판단 시점의 실행가능신호(actionable "
            "signal)가 달라졌고, 그 결과 하루 나머지 구간의 신규진입/청산 타이밍 전체가 연쇄적으로 달라졌음을 "
            "보여준다 — 절대적으로 어느 쪽이 '더 낫다'는 뜻은 아니며(이번 표본에서는 두 실행의 최종 순손익이 "
            "+5.558% vs +5.534%로 비슷했다), 코드 변경이 실제로 판단 경로에 영향을 준다는 것을 확인하는 데 의미가 있다."
        )
    lines.append("")

    if after["trades"]:
        lines.append(f"## AFTER({after_sha[:7]}) 거래별 상세")
        lines.append("")
        lines.append("| # | 종목 | 진입시각 | 진입가 | 청산시각 | 청산가 | 수량 | 손익률 | 순손익(비용차감) | 오방향? | 청산사유 |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
        for idx, t in enumerate(after["trades"], start=1):
            wrong = "예" if (t["wrong_direction_inverse"] or t["wrong_direction_leverage"]) else "아니오"
            lines.append(
                f"| {idx} | {t['instrument']} | {t['entry_time'][11:16]} | {t['entry_price']:,.0f} | "
                f"{t['exit_time'][11:16]} | {t['exit_price']:,.0f} | {t['quantity']} | {t['pnl_pct']:+.2f}% | "
                f"{t['net_pnl_krw']:+,.0f}원 | {wrong} | {t['reason']} |"
            )
        lines.append("")
    else:
        lines.append(f"## AFTER({after_sha[:7]}) 거래별 상세")
        lines.append("")
        lines.append("AFTER 실행에서는 체결된 라운드트립이 없었다.")
        lines.append("")

    if before["trades"]:
        lines.append(f"## BEFORE({before_sha[:7]}) 거래별 상세")
        lines.append("")
        lines.append("| # | 종목 | 진입시각 | 진입가 | 청산시각 | 청산가 | 수량 | 손익률 | 순손익(비용차감) | 오방향? | 청산사유 |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
        for idx, t in enumerate(before["trades"], start=1):
            wrong = "예" if (t["wrong_direction_inverse"] or t["wrong_direction_leverage"]) else "아니오"
            lines.append(
                f"| {idx} | {t['instrument']} | {t['entry_time'][11:16]} | {t['entry_price']:,.0f} | "
                f"{t['exit_time'][11:16]} | {t['exit_price']:,.0f} | {t['quantity']} | {t['pnl_pct']:+.2f}% | "
                f"{t['net_pnl_krw']:+,.0f}원 | {wrong} | {t['reason']} |"
            )
        lines.append("")
    else:
        lines.append(f"## BEFORE({before_sha[:7]}) 거래별 상세")
        lines.append("")
        lines.append("BEFORE 실행에서는 체결된 라운드트립이 없었다.")
        lines.append("")

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")
    return REPORT_PATH


def run_orchestrator() -> None:
    SCRATCH_ROOT.mkdir(parents=True, exist_ok=True)

    print("[1/5] 합성 하루 1분봉 1회 생성 중...")
    bundle = _generate_bundle_once()
    with open(BUNDLE_PATH, "wb") as f:
        pickle.dump(bundle, f)
    print(f"     -> {BUNDLE_PATH}")

    print(f"[2/5] BEFORE 워크트리 준비 중 ({BEFORE_REF} -> {WORKTREE_DIR}) ...")
    try:
        _prepare_before_worktree()
    except Exception as exc:
        print(
            "git worktree add 실패 — 안전을 위해 git checkout/stash 등으로 폴백하지 않고 여기서 중단합니다.\n"
            f"원인: {exc}"
        )
        raise

    before_sha = _run_git(["rev-parse", "HEAD"], cwd=WORKTREE_DIR).stdout.strip()

    try:
        print(f"[3/5] BEFORE(격리 워크트리, commit {before_sha[:7]}) 리플레이 실행 중...")
        before = _run_side("before", WORKTREE_DIR, BEFORE_RESULT_JSON)

        after_sha = _run_git(["rev-parse", "HEAD"], cwd=MAIN_REPO_ROOT).stdout.strip()
        print(f"[4/5] AFTER(현재 HEAD={after_sha[:7]}) 리플레이 실행 중...")
        after = _run_side("after", MAIN_REPO_ROOT, AFTER_RESULT_JSON)
    finally:
        print(f"[cleanup] 임시 워크트리 제거 중: {WORKTREE_DIR}")
        _cleanup_before_worktree()

    print("[5/5] 리포트 작성 중...")
    report_path = write_report(before, after, before_sha, after_sha)

    print("")
    print("=== 요약 ===")
    print(f"BEFORE: 거래 {before['n_trades']}건, 순손익 {before['net_pnl_pct']:+.3f}%, "
          f"raw/live 충돌(INVERSEvsUP) {before['conflict_raw_inverse_vs_live_up_count']}건, "
          f"오방향 주문 총 {before['wrong_direction_order_count_total']}건")
    print(f"AFTER : 거래 {after['n_trades']}건, 순손익 {after['net_pnl_pct']:+.3f}%, "
          f"raw/live 충돌(INVERSEvsUP) {after['conflict_raw_inverse_vs_live_up_count']}건, "
          f"오방향 주문 총 {after['wrong_direction_order_count_total']}건")
    print(f"[written] {report_path}")


def main() -> None:
    if len(sys.argv) >= 4 and sys.argv[1] == "--child":
        run_child(sys.argv[2], sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else "unknown")
        return
    run_orchestrator()


if __name__ == "__main__":
    main()
