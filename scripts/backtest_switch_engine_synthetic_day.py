"""backtest_switch_engine_synthetic_day.py — 하이닉스⇄레버리지/인버스 Enhanced
스위칭 엔진(app/services/hynix_switch_engine.py 및 최근 5개 커밋으로 수정된
early_trend_detector/exit_order_coordinator/dynamic_exit_watcher/
hynix_switch_position_manager/hynix_auto_trade_scheduler)을, 가상의 하루 동안
실제 판단 함수로 구동해 보는 백테스트.

사용법:
    python scripts/backtest_switch_engine_synthetic_day.py

출력:
    reports/hynix_switch_engine_synthetic_backtest.md

방법과 한계(반드시 읽을 것) — scripts/backtest_adaptive_fusion.py와 동일한 취지:
  하이닉스/레버리지(0193T0)/인버스(0197X0) 세 종목 모두, 하루 전체(09:00~15:30)
  분량의 실제 1분봉 아카이브가 이 저장소에 없다. data/cache/hynix_*_minute_1m.csv는
  수집 사이클마다 통째로 덮어써지는 "최근 30분 캐시"일 뿐이고(app/data_sources/
  hynix_inverse_collector.py, hynix_long_collector.py의 df.to_csv(..., index=False)),
  KIS 분봉 API 자체도 "오늘자"만 반환해 과거 특정 거래일 전체를 재구성할 방법이
  없다. 따라서 이 백테스트는 "과거 실제 하루의 재생"이 아니라:

    (1) data/hynix/hynix_daily.csv의 실제 일별 변동성 통계로 보정한 가상의 하루
        1분봉 가격경로(추세 → 횡보 → 반전 3구간)를 만들고,
    (2) 그 하루의 시가를 실제 마지막 종가(2026-07-20 종가 1,764,000원)에 앵커링하며,
    (3) 레버리지/인버스 ETF는 데이터가 실제로 남아있는 2026-07-20 14:45~15:14
        구간의 관측 가격대(레버리지 13,135~13,560원, 인버스 12,070~12,410원)에
        앵커링한 뒤 하이닉스 일중수익률의 ±2배 + 추적오차 노이즈로 움직이게 하고,
    (4) 이 합성 분봉을 "최근 5개 커밋으로 수정된 실제 코드"에 그대로 통과시켜
        BUY/SELL/HOLD/스위칭 판단을 받는다.

  절대수익률 자체의 정확한 예측이 목적이 아니라 "최근 수정된 판단/청산 로직이
  실제로 신호를 만들고 체결까지 이어지는지, 몇 번 거래하고 손익이 어떻게 나는지"를
  확인하는 실행 가능성 점검 도구다.

이 스크립트가 실제로 호출하는 (재구현하지 않은) 함수들:
  - app.trading.hynix_fast_trend.compute_fast_trend_signal
  - app.trading.adaptive_market_regime.compute_and_confirm_regime /
    effective_sl_pct_for_position
  - app.trading.etf_entry_confirmation.compute_etf_breakouts /
    compute_etf_volume_surge  (2026-07-20 "genuine ETF minute-bar" 커밋에서 도입)
  - app.trading.early_trend_detector.compute_composite_early_signal /
    evaluate_chase_block / evaluate_cost_gate / compute_target_probe_pct /
    stage_for_elapsed_seconds / is_opposite_change_point / should_exit_probe
    (2026-07-20 3개 커밋: "use ETF bars for early chase block",
    "distinguish raw score leader from actionable trading signal"의 실질 로직,
    "keep full exits for confirmed reversals while filtering micro noise")
  - app.services.hynix_switch_engine._raw_score_leader / _build_signal_summary /
    _blank_pipeline_trace / _map_prediction_signal
    (commit 23d0e2e "distinguish raw score leader from actionable trading signal")
  - app.trading.hynix_switch_position_manager.evaluate_tp_sl
  - app.trading.hynix_switch_risk_gate.is_new_entry_allowed / get_liquidation_phase
  - app.trading.dry_run_broker.DryRunBroker (주문 체결 시뮬레이션 — 실제 브로커/
    네트워크 호출 없음)

스텁 처리한 경계 (그 이유):
  - DryRunBroker는 생성자에서 data/orders/{오늘날짜}_dry_portfolio.json을 읽고
    쓴다(app/utils/data_paths.ORDERS_DIR). 실계좌/모의계좌 상태 파일과 절대
    섞이지 않도록, 이 스크립트는 dry_run_broker 모듈의 _DATA_DIR만 스크래치
    디렉터리로 monkeypatch한 뒤 브로커를 생성한다 — 브로커의 buy()/sell() 판단
    로직 자체는 전혀 건드리지 않는다.
  - app.trading.hynix_switch_position_manager.run_switch_or_entry/
    run_liquidation_if_needed와 그 하위의 _buy_new/_sell_all_or_ratio는
    app.services.hynix_execution_ledger(실거래 원장 CSV)에 실제로 기록을 남기고
    exit_order_coordinator의 프로세스 전역 락 상태를 공유한다 — 라이브 앱과
    상태를 공유하는 이 전체 경로는 호출하지 않고, 대신 그 안에서 실제 진입/청산
    "판단"을 내리는 하위 순수 함수(evaluate_tp_sl/should_exit_probe/
    evaluate_chase_block 등)만 직접 호출하고 체결 자체는 격리된 DryRunBroker로
    수행한다.
  - signal_symbol_agreement(000660과 실거래 ETF 방향의 일치 여부)는 실제로는
    hynix_switch_engine._early_signal_symbol_agreement()가 계산하지만 그 함수는
    라이브 상태 dict 구조에 강하게 결합돼 있어, 이 스크립트는 보수적으로 None
    (판단 보류)을 그대로 넘긴다 — compute_composite_early_signal은 None을
    "불일치 아님"으로 처리하도록 이미 설계되어 있다(실제 코드 그대로).
"""
from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

# ── 실제 전략 코드 import (재구현 없음) ──────────────────────────────────────
from app.trading.hynix_fast_trend import compute_fast_trend_signal  # noqa: E402
from app.trading.adaptive_market_regime import (  # noqa: E402
    compute_and_confirm_regime, effective_sl_pct_for_position,
)
from app.trading.etf_entry_confirmation import (  # noqa: E402
    compute_etf_breakouts, compute_etf_volume_surge,
)
import app.trading.early_trend_detector as etd  # noqa: E402
import app.services.hynix_switch_engine as engine  # noqa: E402
from app.trading.hynix_switch_position_manager import evaluate_tp_sl  # noqa: E402
from app.trading.hynix_switch_risk_gate import is_new_entry_allowed, get_liquidation_phase  # noqa: E402
from app.trading.hynix_symbols import (  # noqa: E402
    LONG_SYMBOL, LONG_NAME, SHORT_SYMBOL as INVERSE_SYMBOL, SHORT_NAME as INVERSE_NAME,
)
import app.trading.dry_run_broker as dry_run_broker_module  # noqa: E402
from app.trading.dry_run_broker import DryRunBroker  # noqa: E402

RNG = np.random.default_rng(20260721)

SESSION_START = "09:00"
SESSION_END = "15:30"
SYNTHETIC_DATE = datetime(2026, 7, 21)  # 2026-07-20 종가 다음 거래일 가정(가상)
PREV_CLOSE = 1_764_000.0  # data/hynix/hynix_daily.csv 마지막 실제 종가(2026-07-20)

# 2026-07-20 14:45~15:14 실측 캐시(data/cache/hynix_long_minute_1m.csv /
# hynix_inverse_minute_1m.csv)에서 관측된 가격대로 ETF 시가를 앵커링한다.
LEVERAGE_ANCHOR_OPEN = 13_300.0
INVERSE_ANCHOR_OPEN = 12_250.0

INITIAL_CASH = 10_000_000.0


# ─────────────────────────────────────────────────────────────────────────────
# 1) 실제 일별 변동성 통계 로드 (scripts/backtest_adaptive_fusion.py와 동일 기법)
# ─────────────────────────────────────────────────────────────────────────────

def _load_daily_returns_and_df() -> tuple[np.ndarray, pd.DataFrame]:
    path = ROOT / "data" / "hynix" / "hynix_daily.csv"
    df = pd.read_csv(path)
    df = df.sort_values("date")
    closes = df["close"].astype(float).to_numpy()
    rets = np.diff(closes) / closes[:-1]
    return rets, df


def _classify_regime(day_return: float, vol: float) -> str:
    if day_return <= -1.5 * vol:
        return "급락장"
    if day_return >= 1.5 * vol:
        return "급등장"
    return "횡보장"


# ─────────────────────────────────────────────────────────────────────────────
# 2) 합성 1분봉 하루 생성 (추세 → 횡보 → 반전 3구간, 실제 변동성 통계로 진폭 보정)
# ─────────────────────────────────────────────────────────────────────────────

def _session_minutes() -> list[datetime]:
    start = datetime.combine(SYNTHETIC_DATE.date(), datetime.strptime(SESSION_START, "%H:%M").time())
    end = datetime.combine(SYNTHETIC_DATE.date(), datetime.strptime(SESSION_END, "%H:%M").time())
    minutes = []
    t = start
    while t <= end:
        minutes.append(t)
        t += timedelta(minutes=1)
    return minutes


def _build_underlying_path(day_vol_pct: float, true_direction: int, n: int) -> np.ndarray:
    """3구간(추세/횡보/반전) 누적수익률(%) 경로. 실제 변동성 크기로 보정하되,
    구조는 스위칭 엔진의 STRONG_UP/DOWN 확정·반전청산·체이스블록 로직을 실제로
    발동시키기 위해 일부러 "뚜렷한 추세(구간1) → 휩쏘 횡보(구간2) → 반대방향
    뚜렷한 추세(구간3)"를 포함한다(스크립트 상단 docstring 참조). 실측
    변동성(day_vol_pct)은 각 구간의 총 이동폭 크기를 정하는 스케일로만 쓰이고,
    추세 구간의 봉간 노이즈는 방향성이 실제로 15/30분 추세·스윙구조 확인
    조건(app/trading/adaptive_market_regime.py의 STRONG_UP/DOWN 게이트)을 통과할
    만큼 충분히 작게(추세 대비 낮은 봉간 노이즈) 유지한다 — 그래야 순수 랜덤워크
    노이즈에 추세가 파묻혀 하루 종일 RANGE로만 분류되는 것을 피할 수 있다."""
    p1, p2 = int(n * 0.38), int(n * 0.24)
    p3 = n - p1 - p2

    # 구간별 "총 이동폭"(%) — 실측 일별 변동성의 배수로 스케일링(과거 실측
    # 하루 변동폭 예: 2026-07-14 하루 고저폭 약 15%, 2026-07-13 약 15% 등 실제로
    # 큰 변동성 종목이므로 하루 안에서도 수 %대 추세 이동은 비현실적이지 않음).
    move1_pct = true_direction * max(2.0, min(6.0, day_vol_pct * 0.9))
    move3_pct = -true_direction * max(2.2, min(6.5, day_vol_pct * 1.05))

    def _trend_segment(total_move_pct: float, length: int, noise_frac: float) -> np.ndarray:
        drift_per_bar = total_move_pct / length
        noise_std = abs(drift_per_bar) * noise_frac
        return RNG.normal(drift_per_bar, max(noise_std, 1e-6), length)

    seg1 = _trend_segment(move1_pct, p1, noise_frac=0.55)
    # 구간2(횡보/휩쏘) — 방향성 없는 순수 노이즈로 VWAP 교차·스윙반전을 유도.
    chop_noise_scale = max(abs(move1_pct), abs(move3_pct)) / max(p1, p3) * 1.8
    seg2 = RNG.normal(0.0, chop_noise_scale, p2)
    seg3 = _trend_segment(move3_pct, p3, noise_frac=0.55)

    per_bar_ret_pct = np.concatenate([seg1, seg2, seg3])
    return np.cumsum(per_bar_ret_pct)  # percent


def _ohlcv_from_close_path(times: list[datetime], closes: np.ndarray, tick: float,
                            vol_lo: float, vol_hi: float, intrabar_noise_pct: float) -> pd.DataFrame:
    n = len(times)
    opens = np.empty(n)
    opens[0] = closes[0]
    opens[1:] = closes[:-1]
    highs = np.maximum(opens, closes) * (1 + np.abs(RNG.normal(0, intrabar_noise_pct, n)))
    lows = np.minimum(opens, closes) * (1 - np.abs(RNG.normal(0, intrabar_noise_pct, n)))
    volumes = RNG.uniform(vol_lo, vol_hi, n).astype(int)

    def _round_tick(x):
        return np.round(x / tick) * tick

    df = pd.DataFrame({
        "datetime": times,
        "open": _round_tick(opens),
        "high": _round_tick(highs),
        "low": _round_tick(lows),
        "close": _round_tick(closes),
        "volume": volumes,
    })
    return df


def generate_synthetic_day(daily_rets: np.ndarray) -> dict:
    vol = float(np.std(daily_rets)) if len(daily_rets) > 1 else 0.02
    sampled_ret = float(RNG.choice(daily_rets)) if len(daily_rets) else RNG.normal(0.0, vol)
    regime = _classify_regime(sampled_ret, vol)
    true_direction = 1 if regime == "급등장" else (-1 if regime == "급락장" else int(RNG.choice([1, -1])))
    day_vol_pct = vol * 100.0

    times = _session_minutes()
    n = len(times)
    cum_ret_pct = _build_underlying_path(day_vol_pct, true_direction, n)
    underlying_close = PREV_CLOSE * (1 + cum_ret_pct / 100.0)
    underlying_df = _ohlcv_from_close_path(
        times, underlying_close, tick=1000.0, vol_lo=8_000, vol_hi=22_000, intrabar_noise_pct=0.0028,
    )

    cum_ret_frac = cum_ret_pct / 100.0
    lev_tracking_noise = np.cumsum(RNG.normal(0.0, 0.0006, n))
    inv_tracking_noise = np.cumsum(RNG.normal(0.0, 0.0006, n))
    lev_close = LEVERAGE_ANCHOR_OPEN * (1 + 2.0 * cum_ret_frac + lev_tracking_noise)
    inv_close = INVERSE_ANCHOR_OPEN * (1 - 2.0 * cum_ret_frac + inv_tracking_noise)

    leverage_df = _ohlcv_from_close_path(
        times, lev_close, tick=5.0, vol_lo=150_000, vol_hi=900_000, intrabar_noise_pct=0.0012,
    )
    inverse_df = _ohlcv_from_close_path(
        times, inv_close, tick=5.0, vol_lo=150_000, vol_hi=900_000, intrabar_noise_pct=0.0012,
    )

    return {
        "regime_label": regime, "true_direction": true_direction, "day_vol_pct": day_vol_pct,
        "sampled_daily_return_pct": sampled_ret * 100.0,
        "underlying": underlying_df, "leverage": leverage_df, "inverse": inverse_df, "times": times,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 3) 드라이버 루프 — 실제 판단 함수들을 분봉마다 그대로 호출
# ─────────────────────────────────────────────────────────────────────────────

SYMBOL_LABEL = {LONG_SYMBOL: "레버리지(0193T0)", INVERSE_SYMBOL: "인버스(0197X0)"}
SYMBOL_NAME = {LONG_SYMBOL: LONG_NAME, INVERSE_SYMBOL: INVERSE_NAME}


def run_backtest(day: dict, broker: DryRunBroker) -> dict:
    underlying_df = day["underlying"]
    price_df = {LONG_SYMBOL: day["leverage"], INVERSE_SYMBOL: day["inverse"]}
    daily_df = _load_daily_returns_and_df()[1]

    confirmation_state = None
    pending_signal = {"direction": None, "count": 0}
    position = None  # dict or None
    trades: list[dict] = []
    decisions_log: list[dict] = []

    n = len(day["times"])
    for i in range(20, n):  # classify_raw_regime은 최소 20개 봉을 요구함
        now = day["times"][i]
        u_slice = underlying_df.iloc[: i + 1]

        fast_signal = compute_fast_trend_signal(u_slice, now=now)
        regime_result = compute_and_confirm_regime(
            u_slice, daily_df, confirmation_state=confirmation_state, prev_close=PREV_CLOSE, now=now,
        )
        confirmation_state = regime_result["confirmation_state"]
        confirmed_regime = regime_result["confirmed_regime"]

        vote_direction = fast_signal.get("direction") if fast_signal.get("direction") in ("UP", "DOWN") else None
        probe_symbol = LONG_SYMBOL if vote_direction == "UP" else INVERSE_SYMBOL if vote_direction == "DOWN" else None
        etf_df = price_df[probe_symbol].iloc[: i + 1] if probe_symbol else None
        etf_current_price = float(etf_df["close"].iloc[-1]) if etf_df is not None else None

        breakouts = compute_etf_breakouts(etf_df, etf_current_price, vote_direction) if probe_symbol else {}
        volume_surge = compute_etf_volume_surge(etf_df) if etf_df is not None else None

        composite = etd.compute_composite_early_signal(
            fast_signal=fast_signal, signal_symbol_agreement=None, live_direction=vote_direction,
            etf_vwap_breakout=breakouts.get("vwap_breakout"), etf_structure_breakout=breakouts.get("structure_breakout"),
            etf_volume_surge=volume_surge,
        )
        direction = composite.get("direction")
        desired_symbol = LONG_SYMBOL if direction == "UP" else INVERSE_SYMBOL if direction == "DOWN" else None
        current_etf_price = price_df[desired_symbol]["close"].iloc[i] if desired_symbol else None

        # ── raw score leader vs 실행가능 신호 (commit 23d0e2e 로직 그대로 재사용) ──
        if direction == "UP":
            enhanced_score, inverse_pressure_score = composite["score"], max(0.0, 100.0 - composite["score"])
            final_action = "HYNIX_STRONG_BUY" if composite["score"] >= 80 else "HYNIX_BUY"
        elif direction == "DOWN":
            inverse_pressure_score, enhanced_score = composite["score"], max(0.0, 100.0 - composite["score"])
            final_action = "INVERSE_STRONG_BUY" if composite["score"] >= 80 else "INVERSE_BUY"
        else:
            enhanced_score, inverse_pressure_score, final_action = 50.0, 50.0, "HOLD"

        decision = {
            "final_action": final_action, "enhanced_score": enhanced_score,
            "inverse_pressure_score": inverse_pressure_score,
        }
        new_entry_allowed_now = is_new_entry_allowed(now)
        liquidation_phase = get_liquidation_phase(now)

        entry_blocked_reason = None
        chase = {"blocked": False, "reasons": []}
        cost = {"blocked": False}
        if direction is not None and desired_symbol is not None and position is None:
            # 실제 운영 코드는 초 단위(5/10/20/30초) 신호 신선도로 signal_reference_price를
            # 관리하지만, 이 백테스트는 1분봉 해상도만 갖는다. 그 해상도 차이를 그대로
            # "직전 신호 최초 발생 시점" 기준으로 옮기면 방향이 몇 시간째 유지될 때
            # reference_price가 몇 시간 전 가격에 영구히 고정되어 CHASE_BLOCK이 하루
            # 종일 풀리지 않는 인공적 부작용이 생긴다. 그래서 1분봉 해상도에 맞게
            # "직전 1분봉 종가"를 신호 기준가로 쓴다(신호가 매 분 재확인된다는 가정과
            # 일치) — evaluate_chase_block 자체(0.7% 이동/최근 1분 극값 로직)는 그대로
            # 실제 함수를 호출한다.
            if pending_signal["direction"] != direction:
                pending_signal = {"direction": direction, "count": 1}
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
        else:
            elapsed_seconds = 0.0

        trace = engine._blank_pipeline_trace()
        trace["prediction_signal"] = engine._map_prediction_signal(final_action)
        if trace["prediction_signal"] != "HOLD":
            trace["entry_approved"] = entry_blocked_reason is None
            trace["entry_approved_reason"] = entry_blocked_reason or "실행 가능"
        summary = engine._build_signal_summary(
            decision=decision, trace=trace, state={}, now=now, new_entry_allowed_now=new_entry_allowed_now,
        )

        # ── 신규진입 ──────────────────────────────────────────────────────────
        if (
            position is None and direction is not None and desired_symbol is not None
            and summary["actionable_signal"] != "HOLD" and new_entry_allowed_now
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
                    position = {
                        "symbol": desired_symbol, "direction": direction, "quantity": qty,
                        "initial_quantity": qty, "entry_price": current_etf_price, "entry_time": now,
                        "partial_tp1_done": False, "partial_sl1_done": False,
                        "peak_net_return_pct": 0.0, "last_reconfirmed_at": now,
                        "realized_pnl_krw": 0.0, "stage": stage,
                    }
                    decisions_log.append({
                        "time": now, "event": "ENTRY", "symbol": desired_symbol, "price": current_etf_price,
                        "qty": qty, "stage": stage, "target_pct": target_pct,
                        "raw_leader": summary["raw_score_leader"], "actionable": summary["actionable_signal"],
                    })

        # ── 보유 포지션 관리 (TP/SL → 반전청산 → EOD 강제청산 순) ───────────────
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
                    position["realized_pnl_krw"] += (held_price - position["entry_price"]) * result.quantity
                    position["quantity"] -= result.quantity
                    decisions_log.append({
                        "time": now, "event": exit_action, "symbol": held_symbol, "price": held_price,
                        "qty": result.quantity, "reason": exit_reason,
                    })
                    if position["quantity"] <= 0:
                        pnl_pct = (position["realized_pnl_krw"] / (position["entry_price"] * position["initial_quantity"])) * 100.0
                        trades.append({
                            "instrument": SYMBOL_LABEL[held_symbol],
                            "entry_time": position["entry_time"], "entry_price": position["entry_price"],
                            "exit_time": now, "exit_price": held_price,
                            "quantity": position["initial_quantity"], "pnl_pct": pnl_pct,
                            "realized_pnl_krw": position["realized_pnl_krw"], "reason": exit_reason,
                        })
                        pending_signal = {"direction": None, "count": 0}
                        position = None

    # 장 마감 시점까지 포지션이 남아있으면(이론상 위 EOD 처리에서 이미 청산되어야 함) 안전망으로 강제 종가청산
    if position is not None:
        last_i = n - 1
        held_symbol = position["symbol"]
        held_price = float(price_df[held_symbol]["close"].iloc[last_i])
        result = broker.sell(symbol=held_symbol, name=SYMBOL_NAME[held_symbol], quantity=position["quantity"], price=held_price)
        if result.success:
            position["realized_pnl_krw"] += (held_price - position["entry_price"]) * result.quantity
            pnl_pct = (position["realized_pnl_krw"] / (position["entry_price"] * position["initial_quantity"])) * 100.0
            trades.append({
                "instrument": SYMBOL_LABEL[held_symbol],
                "entry_time": position["entry_time"], "entry_price": position["entry_price"],
                "exit_time": day["times"][-1], "exit_price": held_price,
                "quantity": position["initial_quantity"], "pnl_pct": pnl_pct,
                "realized_pnl_krw": position["realized_pnl_krw"], "reason": "EOD_SAFETY_CLOSE",
            })

    return {"trades": trades, "decisions_log": decisions_log}


# ─────────────────────────────────────────────────────────────────────────────
# 4) 리포트 작성
# ─────────────────────────────────────────────────────────────────────────────

def write_report(day: dict, result: dict, broker: DryRunBroker) -> Path:
    trades = result["trades"]
    total_return_pct = (broker.get_balance() / INITIAL_CASH - 1.0) * 100.0

    lines = []
    lines.append("# 하이닉스 스위칭 엔진(app/services/hynix_switch_engine.py) 합성 1일 백테스트")
    lines.append("")
    lines.append(
        "**주의(반드시 읽을 것)**: 이 결과는 과거 실제 하루 시세의 재생이 아니라, "
        "`data/hynix/hynix_daily.csv`의 실제 일별 변동성 통계로 보정한 합성(몬테카를로) "
        "하루 1분봉을 최근 5개 커밋(`60155d1`~`57f0283`)이 반영된 **실제 현재 전략 코드**에 "
        "직접 통과시킨 결과다. 09:00~15:30 전체 분봉 아카이브가 이 저장소에 존재하지 않기 "
        "때문이다(자세한 이유는 `scripts/backtest_switch_engine_synthetic_day.py` 상단 docstring)."
    )
    lines.append("")
    lines.append(f"- 시드: `np.random.default_rng(20260721)` (고정 — 재실행 시 동일 결과)")
    lines.append(f"- 하루 시작가(하이닉스 000660) 앵커: {PREV_CLOSE:,.0f}원 (2026-07-20 실제 종가)")
    lines.append(f"- 레버리지(0193T0) 시가 앵커: {LEVERAGE_ANCHOR_OPEN:,.0f}원, 인버스(0197X0) 시가 앵커: {INVERSE_ANCHOR_OPEN:,.0f}원"
                 " (2026-07-20 14:45~15:14 실측 캐시 가격대 기준)")
    lines.append(f"- 표본 장세: {day['regime_label']} (일별수익률 표본 {day['sampled_daily_return_pct']:+.2f}%, "
                 f"실측 일별변동성 {day['day_vol_pct']:.2f}%) — 스위칭/반전청산 로직을 실제로 발동시키기 위해 "
                 "추세→횡보→반전 3구간으로 하루를 구성함(변동성 크기는 실측치로 보정)")
    lines.append("")
    lines.append("## 실제로 호출한 전략 코드(재구현 아님)")
    lines.append("")
    lines.append(
        "- `app.trading.hynix_fast_trend.compute_fast_trend_signal`\n"
        "- `app.trading.adaptive_market_regime.compute_and_confirm_regime` / `effective_sl_pct_for_position`\n"
        "- `app.trading.etf_entry_confirmation.compute_etf_breakouts` / `compute_etf_volume_surge`\n"
        "- `app.trading.early_trend_detector.compute_composite_early_signal` / `evaluate_chase_block` / "
        "`evaluate_cost_gate` / `compute_target_probe_pct` / `is_opposite_change_point` / `should_exit_probe`\n"
        "- `app.services.hynix_switch_engine._raw_score_leader` / `_build_signal_summary` / `_blank_pipeline_trace` / "
        "`_map_prediction_signal`\n"
        "- `app.trading.hynix_switch_position_manager.evaluate_tp_sl`\n"
        "- `app.trading.hynix_switch_risk_gate.is_new_entry_allowed` / `get_liquidation_phase`\n"
        "- `app.trading.dry_run_broker.DryRunBroker` (체결 시뮬레이션, `_DATA_DIR`만 스크래치 경로로 격리)"
    )
    lines.append("")
    lines.append("## 거래 요약")
    lines.append("")
    lines.append(f"- 총 체결 라운드트립(진입→완전청산) 수: **{len(trades)}건**")
    lines.append(f"- 최종 현금(브로커 잔고): {broker.get_balance():,.0f}원 (시작 {INITIAL_CASH:,.0f}원)")
    lines.append(f"- 하루 총수익률(브로커 잔고 기준): **{total_return_pct:+.3f}%**")
    lines.append("")

    if trades:
        lines.append("## 거래별 상세")
        lines.append("")
        lines.append("| # | 종목 | 진입시각 | 진입가 | 청산시각 | 청산가 | 수량 | 손익률 | 청산사유 |")
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for idx, t in enumerate(trades, start=1):
            lines.append(
                f"| {idx} | {t['instrument']} | {t['entry_time'].strftime('%H:%M')} | {t['entry_price']:,.0f} | "
                f"{t['exit_time'].strftime('%H:%M')} | {t['exit_price']:,.0f} | {t['quantity']} | "
                f"{t['pnl_pct']:+.2f}% | {t['reason']} |"
            )
        lines.append("")
    else:
        lines.append("이 합성 하루에는 체결된 라운드트립이 없었다(신규진입 시간창/체이스블록/비용게이트 등 실제 "
                     "게이트에 계속 막혔거나 신호가 발생하지 않음).")
        lines.append("")

    wins = [t for t in trades if t["pnl_pct"] > 0]
    losses = [t for t in trades if t["pnl_pct"] <= 0]
    lines.append("## 통계")
    lines.append("")
    lines.append(f"- 승: {len(wins)}건 / 패(0 이하): {len(losses)}건")
    if trades:
        lines.append(f"- 평균 손익률: {np.mean([t['pnl_pct'] for t in trades]):+.3f}%")
        lines.append(f"- 최대 손익률: {max(t['pnl_pct'] for t in trades):+.3f}% / 최소 손익률: {min(t['pnl_pct'] for t in trades):+.3f}%")
    lines.append("")

    report_path = ROOT / "reports" / "hynix_switch_engine_synthetic_backtest.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path


def main() -> None:
    daily_rets, _ = _load_daily_returns_and_df()
    day = generate_synthetic_day(daily_rets)

    scratch_dir = Path(tempfile.gettempdir()) / "ai_gap_switch_engine_backtest_scratch"
    scratch_dir.mkdir(parents=True, exist_ok=True)
    dry_run_broker_module._DATA_DIR = scratch_dir  # 실계좌/모의계좌 상태 파일과 완전히 격리
    broker = DryRunBroker(initial_balance=INITIAL_CASH)
    broker.reset()

    result = run_backtest(day, broker)
    report_path = write_report(day, result, broker)

    print(f"장세: {day['regime_label']} / 거래수: {len(result['trades'])}")
    for idx, t in enumerate(result["trades"], start=1):
        print(
            f"  #{idx} {t['instrument']} {t['entry_time'].strftime('%H:%M')}@{t['entry_price']:,.0f} -> "
            f"{t['exit_time'].strftime('%H:%M')}@{t['exit_price']:,.0f} ({t['pnl_pct']:+.2f}%) [{t['reason']}]"
        )
    print(f"총수익률: {(broker.get_balance() / INITIAL_CASH - 1.0) * 100.0:+.3f}%")
    print(f"[written] {report_path}")


if __name__ == "__main__":
    main()
