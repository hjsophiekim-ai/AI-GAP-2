"""hynix_execution_ledger.py — 하이닉스⇄인버스 자동매매의 단일 거래 원장(Source of Truth).

기존에는 매수/스위칭/강제청산이 `hynix_auto_trade_log_*.csv`에, Dynamic Exit AI의
청산이 `exit_engine_log.csv`에 각각 따로 기록되어(레거시 TP/SL과 강제청산도 별도
필드 구성) "하나의 CSV만 보면 절반만 보이는" 문제가 있었다(2026-07-10 실측 — UI의
"최근 매도 8,685원"이 exit_engine_log에만 있고 export CSV에는 없었던 사고).

이 모듈은 모든 주문 실행 경로(신규진입/스위칭/레거시 TP·SL/강제청산/Dynamic Exit AI)가
공통으로 거치는 `app.trading.hynix_switch_position_manager._record_order()`에서
호출되는 단일 기록 지점이다. UI의 오늘 거래횟수/실현손익/최근 매수·매도가는 반드시
이 원장 + broker.get_positions()만으로 계산해야 한다(여러 CSV/state 필드를 따로
집계하지 말 것).
"""

from __future__ import annotations

import csv
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd

from app.logger import logger
from app.utils.time_utils import kst_now
from app.utils.data_paths import LOGS_DIR, EXECUTION_LEDGER_PATH as _LEDGER_PATH

ROOT = Path(__file__).resolve().parent.parent.parent

# 레거시(원장 도입 이전) 로그 — 백필 전용으로만 읽는다.
_LEGACY_TRADE_LOG_PATH = LOGS_DIR / "hynix_auto_trade_log_{date}.csv"
_LEGACY_EXIT_LOG_PATH = LOGS_DIR / "exit_engine_log.csv"

LEDGER_COLUMNS = [
    "trade_id", "parent_trade_id", "timestamp", "mode", "environment", "strategy_name",
    "signal_source", "action", "symbol", "requested_qty", "executed_qty", "requested_price",
    "executed_price", "before_qty", "after_qty", "cash_before", "cash_after", "realized_pnl",
    "fees", "tax", "success", "order_id", "position_confirmed", "is_test_order",
    # Adaptive Fusion(섹션 12) — 각 모델 확률/합의도/기대값/목표비중을 원장에 함께
    # 남겨 전략별 손익 분리 집계와 사후 분석이 가능하게 한다. 기존 원장(위 24개
    # 컬럼)에는 없던 컬럼이므로 _migrate_ledger_schema_if_needed()가 기존 파일의
    # 헤더를 안전하게 확장한 뒤에만 새 컬럼으로 기록한다.
    "active_probability", "prediction_v2_probability", "cycle_probability",
    "fused_probability", "prediction_v2_weight", "dominant_model", "model_agreement",
    "expected_value", "target_position_pct",
    # 실거래 비용 반영(docs/requirements.md 섹션 2) — realized_pnl은 이제 GrossPnL이
    # 아니라 NetPnL(수수료/거래세/슬리피지 차감 후)의 alias다. 아래 6개 필드는 모든
    # 체결(BUY/SELL 모두)에 항상 숫자(0.0 포함)로 기록되어야 하며 NaN/빈 값을 허용하지
    # 않는다(2026-07-13 사용자 검증 — 이전에는 이 필드들이 전부 비어 있었다).
    "gross_pnl", "buy_fee", "sell_fee", "transaction_tax", "slippage_cost", "net_pnl",
    # 2026-07-22 — WEIGHTED_ORDER_CONTROLLER 신규 BUY 감사 필드 (전략 A 일치 검증용)
    "actual_entry_engine", "entry_path", "weighted_evidence", "expected_net_edge",
    "reward_risk", "direction_episode_id", "decision_snapshot_id", "deployed_git_sha",
]

SIGNAL_SOURCE_ENHANCED_REGIME_SWITCH = "ENHANCED_REGIME_SWITCH"
SIGNAL_SOURCE_WEIGHTED_ORDER_CONTROLLER = "WEIGHTED_ORDER_CONTROLLER"
SIGNAL_SOURCE_PREDICTION_V2 = "PREDICTION_V2"
SIGNAL_SOURCE_CYCLE_AI = "CYCLE_AI"
SIGNAL_SOURCE_DYNAMIC_EXIT = "DYNAMIC_EXIT"
SIGNAL_SOURCE_FORCED_LIQUIDATION = "FORCED_LIQUIDATION"
SIGNAL_SOURCE_TEST = "TEST"
# 아래 3개는 Active Strategy/Adaptive Fusion이 "실제로 어떤 전략이 이번 주문을
# 지배했는지" 정직하게 남기기 위한 값이다(2026-07-13 사용자 요청) —
#   ACTIVE_ONLY: Adaptive Fusion이 꺼져있거나, Prediction V2가 아직 SHADOW라서
#                (검증 전) 실제로는 ACTIVE_FUSION 신호만 주문에 반영된 경우.
#   ADAPTIVE_FUSION: Prediction V2가 ADVISORY/LIVE_VALIDATED 상태로 실제 확률/
#                    비중/진입시점에 유의미하게 반영된 경우.
#   PREDICTION_V2_ASSISTED: ADAPTIVE_FUSION 중에서도 dominant_model이 PREDICTION_V2인
#                           경우(Prediction V2가 이번 결정을 실질적으로 주도).
SIGNAL_SOURCE_ACTIVE_ONLY = "ACTIVE_ONLY"
SIGNAL_SOURCE_ADAPTIVE_FUSION = "ADAPTIVE_FUSION"
SIGNAL_SOURCE_PREDICTION_V2_ASSISTED = "PREDICTION_V2_ASSISTED"
# 하위호환(레거시 코드가 참조할 수 있음) — 새 코드는 SIGNAL_SOURCE_ACTIVE_ONLY를 쓴다.
SIGNAL_SOURCE_ACTIVE_STRATEGY_MOCK = "ACTIVE_STRATEGY_MOCK"
# 요구사항(2026-07-16) — KIS에 실제 체결/보유가 확인되는데 원장에는 없는 체결을
# 사후 복구(backfill)한 행임을 표시한다(정상 실시간 기록과 구분해 사후 분석 가능).
SIGNAL_SOURCE_KIS_RECONCILE_BACKFILL = "KIS_RECONCILE_BACKFILL"


def get_ledger_path() -> Path:
    """원장 writer/reader가 반드시 공유해야 하는 단일 경로 조회 함수(요구사항 2026-07-16
    — writer와 UI reader가 서로 다른 경로를 참조해 "브로커에는 보유 중인데 원장은
    0건"으로 보이는 사고를 막는다). $AI_GAP_DATA_DIR/logs/hynix_execution_ledger.csv."""
    return _LEDGER_PATH


def _new_trade_id() -> str:
    return uuid.uuid4().hex[:16]


def _migrate_ledger_schema_if_needed() -> None:
    """기존 원장 파일의 헤더가 현재 LEDGER_COLUMNS보다 컬럼이 적으면(예: Adaptive
    Fusion 컬럼 추가 이전 데이터) 기존 행은 그대로 보존한 채 누락된 컬럼만 빈 값으로
    채워 헤더를 확장한다. 실거래 원장이므로 데이터 유실 없이 헤더만 확장해야 한다 —
    기존 컬럼 순서/값은 절대 변경하지 않는다.
    """
    if not _LEDGER_PATH.exists():
        return
    try:
        with _LEDGER_PATH.open("r", encoding="utf-8-sig", newline="") as fh:
            first_line = fh.readline()
        existing_header = next(csv.reader([first_line])) if first_line else []
        if existing_header == LEDGER_COLUMNS:
            return  # 이미 최신 스키마
        if not existing_header:
            return  # 빈 파일 — append 시 자연스럽게 새 헤더로 시작됨

        df = pd.read_csv(_LEDGER_PATH, dtype=str)
        for col in LEDGER_COLUMNS:
            if col not in df.columns:
                df[col] = ""
        df = df[LEDGER_COLUMNS]
        df.to_csv(_LEDGER_PATH, index=False, encoding="utf-8-sig")
        logger.info(
            "[ExecutionLedger] 원장 스키마 마이그레이션 완료(%d개 컬럼 → %d개, 기존 %d행 보존)",
            len(existing_header), len(LEDGER_COLUMNS), len(df),
        )
    except Exception as exc:
        logger.error("[ExecutionLedger] 원장 스키마 마이그레이션 실패(원본은 그대로 유지됨): %s", exc)


def record_execution(
    action: str, symbol: str, requested_qty: int, executed_qty: int,
    requested_price: Optional[float], executed_price: Optional[float],
    success: bool, mode: str = "mock", environment: Optional[str] = None,
    strategy_name: str = "hynix_switch", signal_source: str = SIGNAL_SOURCE_ENHANCED_REGIME_SWITCH,
    before_qty: Optional[int] = None, after_qty: Optional[int] = None,
    cash_before: Optional[float] = None, cash_after: Optional[float] = None,
    realized_pnl: Optional[float] = None, fees: float = 0.0, tax: float = 0.0,
    order_id: str = "", position_confirmed: Optional[bool] = None,
    is_test_order: bool = False, parent_trade_id: Optional[str] = None,
    now: Optional[datetime] = None,
    active_probability: Optional[float] = None, prediction_v2_probability: Optional[float] = None,
    cycle_probability: Optional[float] = None, fused_probability: Optional[float] = None,
    prediction_v2_weight: Optional[float] = None, dominant_model: Optional[str] = None,
    model_agreement: Optional[float] = None, expected_value: Optional[float] = None,
    target_position_pct: Optional[float] = None,
    gross_pnl: float = 0.0, buy_fee: float = 0.0, sell_fee: float = 0.0,
    transaction_tax: float = 0.0, slippage_cost: float = 0.0, net_pnl: float = 0.0,
    raise_on_failure: bool = False,
    actual_entry_engine: Optional[str] = None, entry_path: Optional[str] = None,
    weighted_evidence: Optional[str] = None, expected_net_edge: Optional[float] = None,
    reward_risk: Optional[float] = None, direction_episode_id: Optional[str] = None,
    decision_snapshot_id: Optional[str] = None, deployed_git_sha: Optional[str] = None,
) -> str:
    """단일 체결/시도를 원장에 append하고 새로 발급한 trade_id를 반환한다.

    raise_on_failure=True면 CSV 쓰기 실패 시 예외를 삼키지 않고 그대로 올린다 —
    record_confirmed_fill()이 이걸로 LEDGER_WRITE_FAILED를 감지한다(기본값 False는
    기존 호출부(_record_order 등)의 하위호환을 위해 계속 예외를 삼키고 로그만 남긴다).

    실패한 시도(success=False)도 기록한다 — "왜 이번엔 주문이 안 나갔는지" 추적을
    위해 스킵/실패 사유까지 원장에 남기는 것이 목적이다. Adaptive Fusion 관련
    확률 필드(active_probability 등)는 해당 전략 경로에서만 채워지고 다른
    signal_source는 빈 값으로 남지만, 거래비용 필드(gross_pnl/buy_fee/sell_fee/
    transaction_tax/slippage_cost/net_pnl)는 모든 체결에서 반드시 숫자(기본 0.0)로
    기록된다 — NaN/빈 값 금지(2026-07-13 사용자 검증 이슈 수정).
    """
    _migrate_ledger_schema_if_needed()
    trade_id = _new_trade_id()
    now = now or kst_now()
    try:
        effective_success = bool(success) and int(executed_qty or 0) > 0
    except Exception:
        effective_success = False
    row = {
        "trade_id": trade_id, "parent_trade_id": parent_trade_id or "",
        "timestamp": now.isoformat(timespec="seconds"), "mode": mode,
        "environment": environment or ("REAL" if mode == "real" else "MOCK"),
        "strategy_name": strategy_name, "signal_source": signal_source,
        "action": action, "symbol": symbol,
        "requested_qty": requested_qty, "executed_qty": executed_qty,
        "requested_price": requested_price, "executed_price": executed_price,
        "before_qty": before_qty, "after_qty": after_qty,
        "cash_before": cash_before, "cash_after": cash_after,
        "realized_pnl": realized_pnl, "fees": fees, "tax": tax,
        "success": effective_success, "order_id": order_id or "",
        "position_confirmed": position_confirmed, "is_test_order": bool(is_test_order),
        "active_probability": active_probability, "prediction_v2_probability": prediction_v2_probability,
        "cycle_probability": cycle_probability, "fused_probability": fused_probability,
        "prediction_v2_weight": prediction_v2_weight, "dominant_model": dominant_model,
        "model_agreement": model_agreement, "expected_value": expected_value,
        "target_position_pct": target_position_pct,
        "gross_pnl": gross_pnl, "buy_fee": buy_fee, "sell_fee": sell_fee,
        "transaction_tax": transaction_tax, "slippage_cost": slippage_cost, "net_pnl": net_pnl,
        "actual_entry_engine": actual_entry_engine or "",
        "entry_path": entry_path or "",
        "weighted_evidence": weighted_evidence or "",
        "expected_net_edge": expected_net_edge if expected_net_edge is not None else "",
        "reward_risk": reward_risk if reward_risk is not None else "",
        "direction_episode_id": direction_episode_id or "",
        "decision_snapshot_id": decision_snapshot_id or "",
        "deployed_git_sha": deployed_git_sha or "",
    }
    try:
        _LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
        is_new = not _LEDGER_PATH.exists()
        with _LEDGER_PATH.open("a", newline="", encoding="utf-8-sig") as fh:
            writer = csv.DictWriter(fh, fieldnames=LEDGER_COLUMNS)
            if is_new:
                writer.writeheader()
            writer.writerow(row)
    except Exception as exc:
        logger.error("[ExecutionLedger] 원장 기록 실패(중대 — 손익/거래횟수 집계에 영향): %s", exc)
        if raise_on_failure:
            raise
    return trade_id


def load_ledger(date_str: Optional[str] = None) -> pd.DataFrame:
    """원장 CSV를 읽어(date_str이 주어지면 그 날짜만) DataFrame으로 반환한다."""
    if not _LEDGER_PATH.exists():
        return pd.DataFrame(columns=LEDGER_COLUMNS)
    try:
        df = pd.read_csv(_LEDGER_PATH)
    except Exception as exc:
        logger.error("[ExecutionLedger] 원장 로드 실패: %s", exc)
        return pd.DataFrame(columns=LEDGER_COLUMNS)
    if df.empty:
        return df
    # 마이그레이션 이전 원장(Adaptive Fusion 컬럼 없음)을 읽어도 KeyError 없이
    # 항상 LEDGER_COLUMNS 전체를 갖도록 보장한다(파일 자체는 건드리지 않음 —
    # 실제 파일 마이그레이션은 record_execution()의 다음 기록 시점에 이루어진다).
    for col in LEDGER_COLUMNS:
        if col not in df.columns:
            df[col] = None
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    df = df.dropna(subset=["timestamp"])
    if date_str:
        df = df[df["timestamp"].dt.strftime("%Y%m%d") == date_str]
    return df.sort_values("timestamp").reset_index(drop=True)


def _position_delta_from_row(row) -> Optional[int]:
    try:
        before = row.get("before_qty")
        after = row.get("after_qty")
        if pd.isna(before) or pd.isna(after):
            return None
        return int(after) - int(before)
    except Exception:
        return None


def _normalize_order_id(order_id) -> Optional[str]:
    """order_id를 dedup 비교용으로 정규화한다. CSV 왕복 시 pandas가 숫자형
    order_id의 앞자리 0을 지워버릴 수 있어(예: "0000012345" -> 12345), 숫자면
    int로 정규화해 기록 시점/재조회 시점 표현이 항상 같은 키로 비교되게 한다."""
    if order_id is None:
        return None
    try:
        if pd.isna(order_id):
            return None
    except Exception:
        pass
    text = str(order_id).strip()
    if not text:
        return None
    try:
        return str(int(float(text)))
    except Exception:
        return text


def _dedup_key(
    mode: str, symbol: str, action: str, order_id: Optional[str],
    timestamp, executed_qty, executed_price, position_delta,
) -> tuple:
    """요구사항(2026-07-16) — account + mode + date + symbol + side + order_no/fill_no로
    식별하고, 주문번호가 없으면 timestamp + qty + price + position_delta로 식별한다."""
    normalized_order_id = _normalize_order_id(order_id)
    if normalized_order_id:
        return ("order", mode, symbol, action, normalized_order_id)
    ts_key = timestamp.isoformat(timespec="seconds") if hasattr(timestamp, "isoformat") else str(timestamp)
    try:
        price_key = round(float(executed_price), 4) if executed_price is not None else None
    except Exception:
        price_key = None
    try:
        qty_key = int(executed_qty) if executed_qty is not None else None
    except Exception:
        qty_key = None
    return ("synthetic", mode, symbol, action, ts_key, qty_key, price_key, position_delta)


def _existing_dedup_keys(date_str: str) -> set:
    df = load_ledger(date_str)
    keys: set = set()
    if df.empty:
        return keys
    for _, row in df.iterrows():
        delta = _position_delta_from_row(row)
        order_id = row.get("order_id")
        mode = row.get("mode")
        symbol = row.get("symbol")
        action = row.get("action")
        normalized_order_id = _normalize_order_id(order_id)
        if normalized_order_id:
            keys.add(("order", mode, symbol, action, normalized_order_id))
        keys.add(_dedup_key(mode, symbol, action, None, row.get("timestamp"), row.get("executed_qty"), row.get("executed_price"), delta))
    return keys


def record_confirmed_fill(
    *, action: str, symbol: str, executed_qty: int, executed_price: Optional[float],
    mode: str, before_qty: int, after_qty: int, order_id: str = "",
    now: Optional[datetime] = None, signal_source: str = SIGNAL_SOURCE_ENHANCED_REGIME_SWITCH,
    strategy_name: str = "hynix_switch", realized_pnl: Optional[float] = None,
    cash_before: Optional[float] = None, cash_after: Optional[float] = None,
    fees: float = 0.0, tax: float = 0.0, gross_pnl: float = 0.0, buy_fee: float = 0.0,
    sell_fee: float = 0.0, transaction_tax: float = 0.0, slippage_cost: float = 0.0, net_pnl: float = 0.0,
    requested_qty: Optional[int] = None, requested_price: Optional[float] = None,
    parent_trade_id: Optional[str] = None,
    active_probability: Optional[float] = None, prediction_v2_probability: Optional[float] = None,
    cycle_probability: Optional[float] = None, fused_probability: Optional[float] = None,
    prediction_v2_weight: Optional[float] = None, dominant_model: Optional[str] = None,
    model_agreement: Optional[float] = None, expected_value: Optional[float] = None,
    target_position_pct: Optional[float] = None,
    actual_entry_engine: Optional[str] = None, entry_path: Optional[str] = None,
    weighted_evidence: Optional[str] = None, expected_net_edge: Optional[float] = None,
    reward_risk: Optional[float] = None, direction_episode_id: Optional[str] = None,
    decision_snapshot_id: Optional[str] = None, deployed_git_sha: Optional[str] = None,
) -> dict:
    """모든 실제 체결 확정 경로(신규매수/부분매도/전량매도/스위칭/Dynamic Exit/
    Big Trend Holding/KIS 잔고 재조회로 체결이 확인되는 경로)가 반드시 거쳐야 하는
    단일 기록 지점(요구사항 2026-07-16). 호출자는 반드시 state.position을 갱신하기
    전에 이 함수를 호출해야 한다 — 원장 기록이 먼저, 상태 갱신은 그 다음이다.

    이미 기록된 체결(order_id 또는 timestamp+qty+price+position_delta)은 중복
    기록하지 않는다(idempotent — 같은 체결이 재확인 사이클에서 다시 넘어와도 안전).

    Returns: {"recorded": bool, "duplicate": bool, "trade_id": str|None, "error": str|None}
    원장 쓰기 자체가 실패해도 예외를 던지지 않는다 — 호출자가 "error"를 보고
    LEDGER_WRITE_FAILED critical alert를 세팅해야 한다(체결 자체는 취소하지 않음).
    """
    now = now or kst_now()
    if int(executed_qty or 0) <= 0:
        return {"recorded": False, "duplicate": False, "trade_id": None, "error": "ZERO_QTY_FILL"}
    position_delta = None
    try:
        position_delta = int(after_qty) - int(before_qty)
    except Exception:
        position_delta = None

    key = _dedup_key(mode, symbol, action, order_id, now, executed_qty, executed_price, position_delta)
    if key in _existing_dedup_keys(now.strftime("%Y%m%d")):
        return {"recorded": False, "duplicate": True, "trade_id": None, "error": None}

    try:
        trade_id = record_execution(
            action=action, symbol=symbol,
            requested_qty=requested_qty if requested_qty is not None else executed_qty,
            executed_qty=executed_qty,
            requested_price=requested_price if requested_price is not None else executed_price,
            executed_price=executed_price, success=True, mode=mode, strategy_name=strategy_name,
            signal_source=signal_source, before_qty=before_qty, after_qty=after_qty,
            cash_before=cash_before, cash_after=cash_after, realized_pnl=realized_pnl,
            fees=fees, tax=tax, order_id=order_id, position_confirmed=True,
            is_test_order=False, now=now, gross_pnl=gross_pnl, buy_fee=buy_fee, sell_fee=sell_fee,
            transaction_tax=transaction_tax, slippage_cost=slippage_cost, net_pnl=net_pnl,
            parent_trade_id=parent_trade_id,
            active_probability=active_probability, prediction_v2_probability=prediction_v2_probability,
            cycle_probability=cycle_probability, fused_probability=fused_probability,
            prediction_v2_weight=prediction_v2_weight, dominant_model=dominant_model,
            model_agreement=model_agreement, expected_value=expected_value,
            target_position_pct=target_position_pct,
            actual_entry_engine=actual_entry_engine, entry_path=entry_path,
            weighted_evidence=weighted_evidence, expected_net_edge=expected_net_edge,
            reward_risk=reward_risk, direction_episode_id=direction_episode_id,
            decision_snapshot_id=decision_snapshot_id, deployed_git_sha=deployed_git_sha,
            raise_on_failure=True,
        )
        return {"recorded": True, "duplicate": False, "trade_id": trade_id, "error": None}
    except Exception as exc:
        logger.error("[ExecutionLedger] record_confirmed_fill 기록 실패(LEDGER_WRITE_FAILED): %s", exc)
        return {"recorded": False, "duplicate": False, "trade_id": None, "error": str(exc)}


def compute_ledger_net_quantity(symbol: str, mode: str, date_str: Optional[str] = None) -> int:
    """원장 기준 오늘 순수량(BUY 누적 - SELL 누적, 성공/운영거래만)을 계산한다.
    KIS 실제 보유수량과 대조해 LEDGER_POSITION_MISMATCH를 판단하는 데 쓰인다."""
    df = load_ledger(date_str or kst_now().strftime("%Y%m%d"))
    if df.empty:
        return 0
    live = df[(df["success"] == True) & (df["is_test_order"] != True) & (df["mode"] == mode) & (df["symbol"] == symbol)]  # noqa: E712
    if live.empty:
        return 0
    buy_qty = pd.to_numeric(live[live["action"] == "BUY"]["executed_qty"], errors="coerce").fillna(0).sum()
    sell_qty = pd.to_numeric(live[live["action"] == "SELL"]["executed_qty"], errors="coerce").fillna(0).sum()
    return int(buy_qty - sell_qty)


def reconcile_symbol_with_kis(
    symbol: str, mode: str, broker_qty: int, avg_price: Optional[float], *,
    broker=None, now: Optional[datetime] = None,
) -> dict:
    """요구사항 5 — 매 사이클 KIS 실제 보유수량과 원장 순수량을 비교하고, 불일치하면
    KIS 당일체결조회로 누락된 체결을 backfill한다.

    KIS 당일체결조회가 없거나(broker에 get_today_fills 미구현) 실패하거나 그 델타를
    설명하는 체결을 찾지 못하면, 브로커가 보고하는 현재 평단가를 이용해 근사치로 1건
    backfill한다(signal_source=KIS_RECONCILE_BACKFILL로 표시 — 실시간 정상 기록과
    구분됨). 이미 채워 넣은 체결은 record_confirmed_fill()의 dedup으로 중복되지 않는다.
    """
    now = now or kst_now()
    date_str = now.strftime("%Y%m%d")
    ledger_qty = compute_ledger_net_quantity(symbol, mode, date_str)
    delta = int(broker_qty) - int(ledger_qty)
    result = {
        "symbol": symbol, "kis_quantity": int(broker_qty), "ledger_quantity": int(ledger_qty),
        "mismatch": delta != 0, "backfilled": [], "error": None,
    }
    if delta == 0:
        return result

    action = "BUY" if delta > 0 else "SELL"
    remaining = abs(delta)

    fills: list = []
    if broker is not None and hasattr(broker, "get_today_fills"):
        try:
            fetched = broker.get_today_fills(symbol=symbol)
            if fetched.get("ok"):
                fills = [f for f in (fetched.get("fills") or []) if f.get("side") == action]
            else:
                result["error"] = fetched.get("error")
        except Exception as exc:
            result["error"] = str(exc)

    for fill in fills:
        if remaining <= 0:
            break
        qty = int(fill.get("quantity") or 0)
        if qty <= 0:
            continue
        qty = min(qty, remaining)
        try:
            fill_ts = datetime.strptime(str(fill.get("timestamp")), "%Y%m%d%H%M%S")
        except Exception:
            fill_ts = now
        before = ledger_qty if action == "BUY" else ledger_qty
        after = before + qty if action == "BUY" else before - qty
        outcome = record_confirmed_fill(
            action=action, symbol=symbol, executed_qty=qty, executed_price=fill.get("price"),
            mode=mode, before_qty=before, after_qty=after, order_id=fill.get("order_id") or "",
            now=fill_ts, signal_source=SIGNAL_SOURCE_KIS_RECONCILE_BACKFILL,
        )
        if outcome.get("recorded"):
            result["backfilled"].append({"source": "kis_today_fills", **outcome})
            ledger_qty = after
            remaining -= qty

    if remaining > 0:
        # KIS 당일체결로 델타를 전부 설명하지 못했다(API 미지원/실패/조회 누락) —
        # 브로커가 보고하는 현재 평단가로 근사치 1건을 backfill해 최소한 순수량은
        # 맞춘다. 다음 사이클에 같은 delta가 이미 해소돼 있으므로 재실행돼도
        # dedup(timestamp+qty+price+position_delta)이 겹치지 않게 before/after를
        # 그대로 재사용해도 안전하다.
        if action == "SELL" and int(broker_qty) <= 0 and int(ledger_qty) > 0:
            result["mismatch_code"] = "LEDGER_BROKER_MISMATCH"
            result["requires_fill_query"] = True
            result["error"] = result.get("error") or "broker flat but ledger still has positive net quantity"
            return result
        before = ledger_qty
        after = before + remaining if action == "BUY" else before - remaining
        outcome = record_confirmed_fill(
            action=action, symbol=symbol, executed_qty=remaining, executed_price=avg_price,
            mode=mode, before_qty=before, after_qty=after, order_id="",
            now=now, signal_source=SIGNAL_SOURCE_KIS_RECONCILE_BACKFILL,
        )
        if outcome.get("recorded"):
            result["backfilled"].append({"source": "approximate_avg_price", **outcome})
        elif outcome.get("error"):
            result["error"] = outcome.get("error")

    return result


def compute_trade_counters(date_str: Optional[str] = None, include_test: bool = False) -> dict:
    """오늘 주문체결/매수체결/매도체결/왕복거래/테스트거래/운영거래 건수를 원장 기준으로 계산한다.

    "왕복거래"는 포지션이 0 → 보유 → 0으로 돌아온 횟수(심볼 무관, 순서대로 카운트)로 정의한다.
    """
    df = load_ledger(date_str)
    if df.empty:
        return {
            "order_fill_count": 0, "buy_fill_count": 0, "sell_fill_count": 0,
            "round_trip_count": 0, "test_order_count": 0, "live_order_count": 0,
        }
    filled = df[df["success"] == True].copy()  # noqa: E712
    if "executed_qty" in filled.columns:
        filled = filled[pd.to_numeric(filled["executed_qty"], errors="coerce").fillna(0) > 0]
    if "signal_source" in filled.columns:
        filled = filled[filled["signal_source"] != SIGNAL_SOURCE_KIS_RECONCILE_BACKFILL]
    if not include_test:
        live = filled[filled["is_test_order"] != True]  # noqa: E712
    else:
        live = filled
    test_count = int((filled["is_test_order"] == True).sum())  # noqa: E712

    buy_count = int((live["action"] == "BUY").sum())
    sell_count = int((live["action"] == "SELL").sum())

    # 왕복거래: after_qty가 0으로 떨어질 때마다 1회의 "청산 완료"로 카운트한다
    # (해당 시점까지 최소 1건의 BUY가 선행했다는 전제 — after_qty 컬럼이 없는 과거
    # 백필 데이터는 셀 수 없으므로 0 처리).
    round_trips = 0
    if "after_qty" in live.columns:
        closes = live[(live["action"] == "SELL") & (pd.to_numeric(live["after_qty"], errors="coerce") == 0)]
        round_trips = int(len(closes))

    return {
        "order_fill_count": int(len(live)), "buy_fill_count": buy_count, "sell_fill_count": sell_count,
        "round_trip_count": round_trips, "test_order_count": test_count,
        "live_order_count": int(len(live)),
    }


def compute_realized_pnl_breakdown(date_str: Optional[str] = None) -> dict:
    """원장의 realized_pnl 합계 + 거래별 상세 내역을 반환한다(section 12: 손익 재구성)."""
    df = load_ledger(date_str)
    if df.empty:
        return {"total_realized_pnl": 0.0, "trades": []}
    live = _successful_operating_trades(df)
    live = live[live["action"] == "SELL"].copy()
    live["realized_pnl"] = pd.to_numeric(live["realized_pnl"], errors="coerce").fillna(0.0)
    trades = live[[
        "trade_id", "timestamp", "symbol", "executed_qty", "executed_price", "realized_pnl", "signal_source",
    ]].to_dict("records")
    return {"total_realized_pnl": round(float(live["realized_pnl"].sum()), 2), "trades": trades}


def compute_strategy_real_stats(signal_sources, date_str: Optional[str] = None) -> dict:
    """실제로 체결된(원장 기준) 특정 signal_source(들)의 성과를 hynix_strategy_shadow_tracker의
    가상 포트폴리오 통계와 같은 스키마로 반환한다 — ADAPTIVE_FUSION처럼 실제 공통 주문이
    나가는 전략을 가상 전략들과 나란히 비교하기 위함(섹션 18/19)."""
    if isinstance(signal_sources, str):
        signal_sources = [signal_sources]
    empty = {
        "trade_count": 0, "win_rate": None, "total_return_pct": 0.0, "profit_factor": None,
        "max_drawdown_pct": None, "avg_holding_minutes": None, "hynix_pnl_krw": 0.0, "inverse_pnl_krw": 0.0,
    }
    df = load_ledger(date_str)
    if df.empty:
        return empty
    live = _successful_operating_trades(df)
    live = live[live["signal_source"].isin(signal_sources)].copy()
    sells = live[live["action"] == "SELL"].copy()
    if sells.empty:
        return empty
    sells["realized_pnl"] = pd.to_numeric(sells["realized_pnl"], errors="coerce")
    sells["executed_qty"] = pd.to_numeric(sells["executed_qty"], errors="coerce")
    sells["executed_price"] = pd.to_numeric(sells["executed_price"], errors="coerce")

    pnl = sells["realized_pnl"].dropna()
    wins, losses = pnl[pnl > 0], pnl[pnl < 0]
    gross_profit, gross_loss = float(wins.sum()), float(abs(losses.sum()))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (float("inf") if gross_profit > 0 else None)
    win_rate = round(len(wins) / len(pnl) * 100.0, 2) if len(pnl) else None

    # entry_cost = executed_qty*executed_price - realized_pnl(=진입원가 근사, entry_price*qty)
    entry_cost = sells["executed_qty"] * sells["executed_price"] - sells["realized_pnl"]
    pct = (sells["realized_pnl"] / entry_cost.replace(0, pd.NA)) * 100.0
    total_return_pct = round(float(pct.dropna().sum()), 4)

    cum = pnl.cumsum()
    drawdown = cum - cum.cummax()
    max_dd = round(float(drawdown.min()), 2) if not drawdown.empty else 0.0

    hynix_pnl = float(pd.to_numeric(sells[sells["symbol"] == "0193T0"]["realized_pnl"], errors="coerce").sum())
    inverse_pnl = float(pd.to_numeric(sells[sells["symbol"] == "0197X0"]["realized_pnl"], errors="coerce").sum())

    return {
        "trade_count": int(len(sells)), "win_rate": win_rate, "total_return_pct": total_return_pct,
        "profit_factor": profit_factor, "max_drawdown_pct": max_dd, "avg_holding_minutes": None,
        "hynix_pnl_krw": round(hynix_pnl, 2), "inverse_pnl_krw": round(inverse_pnl, 2),
    }


def compute_performance_stats(date_str: Optional[str] = None) -> dict:
    """명세 13절 — 운영 UI 통계(왕복거래 수/체결 수/평균 진입비중/평균 보유시간/
    평균 거래수익률/승률/Profit Factor/최대 장중 손실/전략별·BUY-INVERSE별·
    시험진입/Scale-in별 손익). TEST 주문은 전부 제외한다."""
    df = load_ledger(date_str)
    empty = {
        "round_trip_count": 0, "order_fill_count": 0, "avg_entry_pct": None, "avg_holding_minutes": None,
        "avg_trade_return_pct": None, "win_rate": None, "profit_factor": None, "max_intraday_drawdown_krw": None,
        "cumulative_realized_pnl": 0.0, "pnl_by_signal_source": {}, "pnl_by_symbol": {},
        "test_entry_pnl": 0.0, "scale_in_pnl": 0.0,
    }
    if df.empty:
        return empty

    live = _successful_operating_trades(df)
    if live.empty:
        return empty
    live["realized_pnl"] = pd.to_numeric(live["realized_pnl"], errors="coerce")

    sells = live[live["action"] == "SELL"].copy()
    pnl_values = sells["realized_pnl"].dropna()
    wins = pnl_values[pnl_values > 0]
    losses = pnl_values[pnl_values < 0]
    gross_profit = float(wins.sum())
    gross_loss = float(abs(losses.sum()))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (float("inf") if gross_profit > 0 else None)
    win_rate = round(len(wins) / len(pnl_values) * 100.0, 2) if len(pnl_values) > 0 else None

    counters = compute_trade_counters(date_str)

    avg_entry_pct = None  # requested_qty*executed_price 대비 예산비중은 호출부(브로커 예산)가 있어야 정확 — 원장만으로는 근사 생략

    # 평균 보유시간: 같은 심볼의 연속된 BUY→SELL 쌍을 순서대로 매칭(간단 FIFO 근사).
    holding_minutes: list = []
    open_time = {}
    for _, row in live.sort_values("timestamp").iterrows():
        sym = row["symbol"]
        if row["action"] == "BUY" and sym not in open_time:
            open_time[sym] = row["timestamp"]
        elif row["action"] == "SELL" and sym in open_time:
            delta = (row["timestamp"] - open_time[sym]).total_seconds() / 60.0
            holding_minutes.append(delta)
            after_qty = pd.to_numeric(row.get("after_qty"), errors="coerce")
            if pd.notna(after_qty) and after_qty <= 0:
                del open_time[sym]

    pnl_by_source = sells.groupby("signal_source")["realized_pnl"].sum().round(2).to_dict()
    pnl_by_symbol = sells.groupby("symbol")["realized_pnl"].sum().round(2).to_dict()

    cum = pd.to_numeric(sells.sort_values("timestamp")["realized_pnl"], errors="coerce").fillna(0.0).cumsum()
    running_peak = cum.cummax()
    drawdown = (cum - running_peak)
    max_dd = round(float(drawdown.min()), 2) if not drawdown.empty else 0.0

    return {
        "round_trip_count": counters["round_trip_count"], "order_fill_count": counters["live_order_count"],
        "avg_entry_pct": avg_entry_pct,
        "avg_holding_minutes": round(sum(holding_minutes) / len(holding_minutes), 1) if holding_minutes else None,
        "avg_trade_return_pct": None,
        "win_rate": win_rate, "profit_factor": profit_factor, "max_intraday_drawdown_krw": max_dd,
        "cumulative_realized_pnl": round(float(pnl_values.sum()), 2) if len(pnl_values) else 0.0,
        "pnl_by_signal_source": pnl_by_source, "pnl_by_symbol": pnl_by_symbol,
        "test_entry_pnl": 0.0, "scale_in_pnl": round(float(pnl_by_source.get("ACTIVE_STRATEGY_MOCK", 0.0)), 2),
    }


def compute_cost_breakdown_stats(date_str: Optional[str] = None) -> dict:
    """오늘 체결의 거래비용 총계(총 매수수수료/총 매도수수료/총 거래세/총 슬리피지/
    Gross 실현손익/Net 실현손익)를 반환한다 — UI '거래 성과 통계'에서 비용 breakdown을
    보여주기 위함(2026-07-13 사용자 요청). TEST 주문은 제외한다."""
    stats = calculate_daily_net_pnl_from_ledger(date_str)
    return {
        "total_buy_fee": stats["total_buy_fee"],
        "total_sell_fee": stats["total_sell_fee"],
        "total_transaction_tax": stats["total_transaction_tax"],
        "total_slippage_cost": stats["total_slippage_cost"],
        "total_commission": round(stats["total_buy_fee"] + stats["total_sell_fee"], 2),
        "total_trading_cost": stats["total_trading_cost"],
        "gross_realized_pnl": stats["gross_realized_pnl"],
        "net_realized_pnl": stats["net_realized_pnl"],
    }


def _bool_series(series: pd.Series) -> pd.Series:
    if series.dtype == bool:
        return series.fillna(False)
    return series.astype(str).str.strip().str.lower().isin(("true", "1", "yes", "y"))


def _successful_operating_trades(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df.copy()
    success = _bool_series(df["success"]) if "success" in df.columns else pd.Series(False, index=df.index)
    is_test = _bool_series(df["is_test_order"]) if "is_test_order" in df.columns else pd.Series(False, index=df.index)
    qty_ok = pd.to_numeric(df["executed_qty"], errors="coerce").fillna(0) > 0 if "executed_qty" in df.columns else pd.Series(False, index=df.index)
    source_ok = df["signal_source"] != SIGNAL_SOURCE_KIS_RECONCILE_BACKFILL if "signal_source" in df.columns else pd.Series(True, index=df.index)
    return df[success & ~is_test & qty_ok & source_ok].copy()


def calculate_daily_net_pnl_from_ledger(
    date_str: Optional[str] = None,
    starting_equity: float = 10_000_000.0,
) -> dict:
    """Single source of truth for daily realized PnL and UI trade rows.

    Cost and PnL are intentionally sourced only from successful operating SELL
    rows. BUY rows are displayed and counted, but their costs are counted only
    when the matching SELL row carries them as realized round-trip cost.
    """
    df = load_ledger(date_str)
    empty = {
        "ledger_raw_row_count": 0,
        "operating_trade_count": 0,
        "display_row_count": 0,
        "buy_fill_count": 0,
        "sell_fill_count": 0,
        "round_trip_count": 0,
        "gross_realized_pnl": 0.0,
        "total_buy_fee": 0.0,
        "total_sell_fee": 0.0,
        "total_transaction_tax": 0.0,
        "total_slippage_cost": 0.0,
        "total_commission": 0.0,
        "total_trading_cost": 0.0,
        "net_realized_pnl": 0.0,
        "starting_equity": float(starting_equity),
        "net_daily_return_pct": 0.0,
        "trades": pd.DataFrame(columns=LEDGER_COLUMNS),
    }
    if df.empty:
        return empty

    live = _successful_operating_trades(df)
    empty["ledger_raw_row_count"] = int(len(df))
    if live.empty:
        return empty

    live = live.sort_values("timestamp").reset_index(drop=True)
    sells = live[live["action"] == "SELL"].copy()

    # 거래비용 엔진 도입(2026-07-13) 이전에 체결된 SELL 행은 gross_pnl/net_pnl이 아예
    # NaN이다(그 필드 자체가 아직 없었음) — 이걸 그대로 fillna(0.0)하면 해당 거래의
    # 실제 손익(특히 손실)이 Gross/Net 합계에서 조용히 0으로 사라진다("일부 거래는
    # 손익이 보이는데 일부 손실 거래 금액이 안 보인다"는 2026-07-15 사용자 리포트의
    # 원인). 그 시절엔 수수료를 별도 추적하지 않았으므로, 레거시 realized_pnl(당시
    # 유일하게 기록된 손익 필드)을 gross/net 둘 다의 근사치로 되살린다 — 수수료는
    # 여전히 0으로 표시되지만(위 UI 캡션이 이미 이 사실을 알림), 손익 금액 자체는
    # 더 이상 사라지지 않는다.
    legacy_pnl = pd.to_numeric(sells.get("realized_pnl"), errors="coerce")
    gross_pnl_filled = pd.to_numeric(sells["gross_pnl"], errors="coerce")
    net_pnl_filled = pd.to_numeric(sells["net_pnl"], errors="coerce")
    gross_pnl_filled = gross_pnl_filled.where(gross_pnl_filled.notna(), legacy_pnl)
    net_pnl_filled = net_pnl_filled.where(net_pnl_filled.notna(), legacy_pnl)

    gross = float(gross_pnl_filled.fillna(0.0).sum())
    buy_fee = float(pd.to_numeric(sells["buy_fee"], errors="coerce").fillna(0.0).sum())
    sell_fee = float(pd.to_numeric(sells["sell_fee"], errors="coerce").fillna(0.0).sum())
    tax = float(pd.to_numeric(sells["transaction_tax"], errors="coerce").fillna(0.0).sum())
    slippage = float(pd.to_numeric(sells["slippage_cost"], errors="coerce").fillna(0.0).sum())
    net_field_sum = float(net_pnl_filled.fillna(0.0).sum())
    total_cost = buy_fee + sell_fee + tax + slippage
    net = gross - total_cost
    # Row-level costs are rounded before writing. Reconcile sub-cent drift against
    # the SELL row net_pnl total so Gross - Cost and Net always agree in the UI.
    if sells["net_pnl"].notna().any() and round(net, 2) != round(net_field_sum, 2):
        reconciled_total_cost = gross - net_field_sum
        if abs(reconciled_total_cost - total_cost) <= 0.05:
            slippage = reconciled_total_cost - buy_fee - sell_fee - tax
            total_cost = reconciled_total_cost
            net = net_field_sum

    after_qty = pd.to_numeric(sells.get("after_qty"), errors="coerce") if "after_qty" in sells.columns else pd.Series([], dtype=float)
    round_trips = int((after_qty == 0).sum()) if not sells.empty else 0
    net_return = (net / starting_equity * 100.0) if starting_equity else 0.0

    return {
        "ledger_raw_row_count": int(len(df)),
        "operating_trade_count": int(len(live)),
        "display_row_count": int(len(live)),
        "buy_fill_count": int((live["action"] == "BUY").sum()),
        "sell_fill_count": int((live["action"] == "SELL").sum()),
        "round_trip_count": round_trips,
        "total_buy_fee": round(buy_fee, 2),
        "total_sell_fee": round(sell_fee, 2),
        "total_transaction_tax": round(tax, 2),
        "total_slippage_cost": round(slippage, 2),
        "total_commission": round(buy_fee + sell_fee, 2),
        "total_trading_cost": round(total_cost, 2),
        "gross_realized_pnl": round(gross, 2),
        "net_realized_pnl": round(net, 2),
        "starting_equity": float(starting_equity),
        "net_daily_return_pct": round(net_return, 6),
        "trades": live,
    }


def compute_current_position_detail(symbol: Optional[str], total_equity: Optional[float] = None) -> dict:
    """현재 보유 중인 포지션의 평균매수가/최초진입시각/최근추가매수시각/총투자금액/
    포지션비중을 원장에서 재구성한다(section: 보유 포지션 상세 표시).

    "현재 보유 중인 에피소드"는 원장 전체(날짜 무관 — 당일청산 원칙이라 보통 당일
    이지만, 안전하게 전체 기간에서 마지막으로 0→보유로 전환된 이후 아직 0으로
    돌아오지 않은 구간)로 정의한다. symbol이 없거나(포지션 없음) 매칭되는 원장
    기록이 없으면 has_position=False만 반환한다.
    """
    empty = {
        "has_position": False, "avg_buy_price": None, "first_entry_time": None,
        "last_add_time": None, "total_invested_krw": None, "position_pct": None,
        "buy_count_in_position": 0,
    }
    if not symbol:
        return empty

    df = load_ledger(None)
    if df.empty:
        return empty
    live = df[(df["success"] == True) & (df["is_test_order"] != True) & (df["symbol"] == symbol)]  # noqa: E712
    live = live.sort_values("timestamp").reset_index(drop=True)
    if live.empty:
        return empty

    # 가장 최근에 시작된(0→보유 전환) 후 아직 0으로 청산되지 않은 구간의 시작 위치를 찾는다.
    episode_start_pos = None
    for pos, row in live.iterrows():
        before_qty = pd.to_numeric(row.get("before_qty"), errors="coerce")
        after_qty = pd.to_numeric(row.get("after_qty"), errors="coerce")
        if row["action"] == "BUY" and (pd.isna(before_qty) or before_qty == 0):
            episode_start_pos = pos
        elif row["action"] == "SELL" and pd.notna(after_qty) and after_qty == 0:
            episode_start_pos = None

    if episode_start_pos is None:
        return empty

    episode_buys = live.iloc[episode_start_pos:]
    episode_buys = episode_buys[episode_buys["action"] == "BUY"]
    if episode_buys.empty:
        return empty

    qtys = pd.to_numeric(episode_buys["executed_qty"], errors="coerce").fillna(0)
    prices = pd.to_numeric(episode_buys["executed_price"], errors="coerce").fillna(0)
    total_qty = float(qtys.sum())
    total_cost = float((qtys * prices).sum())

    return {
        "has_position": True,
        "avg_buy_price": round(total_cost / total_qty, 2) if total_qty > 0 else None,
        "first_entry_time": episode_buys.iloc[0]["timestamp"].isoformat(),
        "last_add_time": episode_buys.iloc[-1]["timestamp"].isoformat(),
        "total_invested_krw": round(total_cost, 2),
        "buy_count_in_position": int(len(episode_buys)),
        "position_pct": round(total_cost / total_equity * 100, 2) if total_equity else None,
    }


def reconcile_execution_ledger(date_str: Optional[str] = None, broker=None) -> dict:
    """원장과 broker.get_positions()를 대조해 UI 표시값과의 불일치를 점검한다.

    Returns
    -------
    dict: counters, pnl, broker_position, ledger_final_position, position_match(bool),
          mismatches(list[str]) — 1개라도 있으면 UI에 빨간 경고를 표시해야 한다.
    """
    date_str = date_str or kst_now().strftime("%Y%m%d")
    counters = compute_trade_counters(date_str)
    pnl = compute_realized_pnl_breakdown(date_str)

    df = load_ledger(date_str)
    live = df[(df["success"] == True) & (df["is_test_order"] != True)] if not df.empty else df  # noqa: E712

    ledger_position = {"symbol": None, "quantity": 0}
    if not live.empty:
        last_by_symbol = live.groupby("symbol").tail(1)
        for _, row in last_by_symbol.iterrows():
            after_qty = pd.to_numeric(row.get("after_qty"), errors="coerce")
            if pd.notna(after_qty) and after_qty > 0:
                ledger_position = {"symbol": row["symbol"], "quantity": int(after_qty)}

    mismatches: list = []
    broker_position = None
    if broker is not None:
        try:
            positions = broker.get_positions()
            broker_position = positions[0] if positions else {"symbol": None, "quantity": 0}
        except Exception as exc:
            mismatches.append(f"broker.get_positions() 조회 실패: {exc}")

    position_match = True
    if broker_position is not None:
        b_symbol = broker_position.get("symbol") if isinstance(broker_position, dict) else getattr(broker_position, "symbol", None)
        b_qty = (broker_position.get("quantity") if isinstance(broker_position, dict) else getattr(broker_position, "quantity", 0)) or 0
        if b_symbol != ledger_position["symbol"] or int(b_qty) != ledger_position["quantity"]:
            position_match = False
            mismatches.append(
                f"포지션 불일치: 원장={ledger_position}, broker={{'symbol': {b_symbol}, 'quantity': {b_qty}}}"
            )

    return {
        "date": date_str, "counters": counters, "pnl": pnl,
        "ledger_final_position": ledger_position, "broker_position": broker_position,
        "position_match": position_match, "mismatches": mismatches,
    }


# =============================================================================
# 거래비용 재구성(섹션 8) — TradeCostEngine 도입 이전에 기록된 완료 왕복거래를
# 실제 수수료/거래세/슬리피지 기준으로 재계산한다. reconstruct_*는 순수 리포트
# (파일을 건드리지 않음), backfill_trading_costs_into_ledger는 그 결과를 실제
# 원장 파일에 반영한다(과거 행의 gross_pnl/buy_fee/sell_fee/transaction_tax/
# slippage_cost/net_pnl/realized_pnl만 갱신 — 다른 컬럼/행은 절대 건드리지 않음).
# =============================================================================

def _match_round_trips_fifo(live: pd.DataFrame) -> list:
    """심볼별 FIFO로 BUY→SELL을 매칭해 완료된 왕복거래 목록을 만든다(Scale-in은
    수량가중평균 매수가로 합산). 매도수량이 남은 매수보다 크면 잘라서(min) 매칭한다."""
    from app.trading.trading_cost_engine import TradeCostEngine

    cost_engine = TradeCostEngine()
    trades: list = []
    open_positions: dict = {}

    for _, row in live.sort_values("timestamp").iterrows():
        symbol = row["symbol"]
        action = row["action"]
        qty = pd.to_numeric(row.get("executed_qty"), errors="coerce")
        price = pd.to_numeric(row.get("executed_price"), errors="coerce")
        if pd.isna(qty) or pd.isna(price) or qty <= 0:
            continue

        if action == "BUY":
            if symbol not in open_positions:
                open_positions[symbol] = {"price": float(price), "qty": float(qty)}
            else:
                prev = open_positions[symbol]
                total_qty = prev["qty"] + qty
                prev["price"] = (prev["price"] * prev["qty"] + float(price) * float(qty)) / total_qty
                prev["qty"] = total_qty
        elif action == "SELL" and symbol in open_positions:
            entry = open_positions[symbol]
            sell_qty = min(float(qty), entry["qty"])
            if sell_qty <= 0:
                continue
            cost = cost_engine.compute_net_pnl(symbol, entry_price=entry["price"], exit_price=float(price), quantity=int(sell_qty))
            trades.append({
                "trade_id": row["trade_id"], "symbol": symbol, "buy_price": entry["price"], "sell_price": float(price),
                "quantity": int(sell_qty), "gross_pnl": cost["gross_pnl"], "buy_fee": cost["buy_fee"],
                "sell_fee": cost["sell_fee"], "transaction_tax": cost["transaction_tax"],
                "slippage_cost": cost["slippage"], "net_pnl": cost["net_pnl"], "sell_timestamp": row["timestamp"],
            })
            entry["qty"] -= sell_qty
            if entry["qty"] <= 0:
                del open_positions[symbol]

    return trades


def reconstruct_trade_costs_for_date(date_str: Optional[str] = None) -> dict:
    """섹션 8 — 완료된 왕복거래를 TradeCostEngine으로 재계산한 표+합계를 반환한다
    (파일을 수정하지 않는 순수 리포트). trade_no는 1부터 매긴다."""
    df = load_ledger(date_str)
    empty = {
        "trades": [],
        "totals": {"gross_realized_pnl": 0.0, "total_commission": 0.0, "total_tax": 0.0, "total_slippage": 0.0, "net_realized_pnl": 0.0},
    }
    if df.empty:
        return empty
    live = df[(df["success"] == True) & (df["is_test_order"] != True)].copy()  # noqa: E712
    if live.empty:
        return empty

    trades = _match_round_trips_fifo(live)
    for i, t in enumerate(trades, start=1):
        t["trade_no"] = i

    totals = {
        "gross_realized_pnl": round(sum(t["gross_pnl"] for t in trades), 2),
        "total_commission": round(sum(t["buy_fee"] + t["sell_fee"] for t in trades), 2),
        "total_tax": round(sum(t["transaction_tax"] for t in trades), 2),
        "total_slippage": round(sum(t["slippage_cost"] for t in trades), 2),
        "net_realized_pnl": round(sum(t["net_pnl"] for t in trades), 2),
    }
    return {"trades": trades, "totals": totals}


def backfill_trading_costs_into_ledger(date_str: Optional[str] = None) -> dict:
    """reconstruct_trade_costs_for_date()의 결과를 실제 원장 CSV 파일에 반영한다 —
    해당 SELL 행의 gross_pnl/buy_fee/sell_fee/transaction_tax/slippage_cost/net_pnl/
    realized_pnl/fees/tax만 갱신하고, 그 외 모든 행/컬럼은 그대로 둔다. 이미
    gross_pnl이 채워진(0이 아닌) 행은 건드리지 않는다(중복 백필 방지)."""
    _migrate_ledger_schema_if_needed()
    report = reconstruct_trade_costs_for_date(date_str)
    if not report["trades"]:
        return {"updated_rows": 0, "report": report}

    if not _LEDGER_PATH.exists():
        return {"updated_rows": 0, "report": report}

    with _LEDGER_PATH.open("r", newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.reader(fh))
    if len(rows) < 2:
        return {"updated_rows": 0, "report": report}
    header = rows[0]
    by_trade_id = {t["trade_id"]: t for t in report["trades"]}

    updated = 0
    for i in range(1, len(rows)):
        row_dict = dict(zip(header, rows[i]))
        tid = row_dict.get("trade_id")
        if tid not in by_trade_id:
            continue
        # gross_pnl과 slippage_cost가 둘 다 이미 채워져 있어야 "완료된 백필"로 본다 —
        # 스키마 마이그레이션이 컬럼명을 바꾸면서(예: slippage→slippage_cost) 값이
        # 유실될 수 있으므로, 하나라도 비어 있으면 다시 채운다(멱등 — 항상 동일한
        # 계산 결과로 덮어쓰므로 여러 번 실행해도 값이 달라지지 않는다).
        already_done = all(
            row_dict.get(col) not in (None, "", "0", "0.0")
            for col in ("gross_pnl", "slippage_cost")
        )
        if already_done:
            continue
        t = by_trade_id[tid]
        row_dict["gross_pnl"] = t["gross_pnl"]
        row_dict["buy_fee"] = t["buy_fee"]
        row_dict["sell_fee"] = t["sell_fee"]
        row_dict["transaction_tax"] = t["transaction_tax"]
        row_dict["slippage_cost"] = t["slippage_cost"]
        row_dict["net_pnl"] = t["net_pnl"]
        row_dict["realized_pnl"] = t["net_pnl"]
        row_dict["fees"] = round(t["buy_fee"] + t["sell_fee"], 2)
        row_dict["tax"] = t["transaction_tax"]
        rows[i] = [row_dict.get(col, "") for col in header]
        updated += 1

    if updated:
        with _LEDGER_PATH.open("w", newline="", encoding="utf-8-sig") as fh:
            csv.writer(fh).writerows(rows)
        logger.info("[ExecutionLedger] 거래비용 백필 완료: %d개 SELL 행 갱신", updated)

    return {"updated_rows": updated, "report": report}


# =============================================================================
# 레거시 로그 백필 (원장 도입 이전 거래를 1회성으로 재구성)
# =============================================================================

def backfill_from_legacy_logs(date_str: str, initial_cash: float = 10_000_000.0) -> dict:
    """원장 도입 이전 날짜의 hynix_auto_trade_log_*.csv + exit_engine_log.csv를 시간순으로
    합쳐 원장 형식으로 재구성한다. 이미 그 날짜 원장 데이터가 있으면 건너뛴다(중복 방지).

    E2E forced 테스트 행(reason에 "E2E forced" 포함)은 is_test_order=True로 표시하고
    운영 거래횟수/손익 계산에서 자동 제외된다(compute_* 함수들이 필터링).
    """
    existing = load_ledger(date_str)
    if not existing.empty:
        return {"skipped": True, "reason": f"{date_str} 원장에 이미 {len(existing)}건 존재 — 백필 생략"}

    rows: list = []

    trade_log_path = Path(str(_LEGACY_TRADE_LOG_PATH).format(date=date_str))
    if trade_log_path.exists():
        try:
            df = pd.read_csv(trade_log_path)
            df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
            df = df.dropna(subset=["timestamp"])
            for _, r in df.iterrows():
                reason = str(r.get("reason", ""))
                is_test = "E2E forced" in reason
                signal_source = SIGNAL_SOURCE_TEST if is_test else (
                    SIGNAL_SOURCE_FORCED_LIQUIDATION if "강제청산" in reason else SIGNAL_SOURCE_ENHANCED_REGIME_SWITCH
                )
                rows.append({
                    "timestamp": r["timestamp"], "action": r.get("action"), "symbol": str(r.get("symbol")),
                    "qty": int(r.get("quantity") or 0), "price": float(r.get("price") or 0),
                    "success": bool(r.get("success")), "reason": reason,
                    "signal_source": signal_source, "is_test_order": is_test, "source_file": "hynix_auto_trade_log",
                })
        except Exception as exc:
            logger.error("[ExecutionLedger] 레거시 trade log 백필 실패(%s): %s", trade_log_path, exc)

    if _LEGACY_EXIT_LOG_PATH.exists():
        try:
            df = pd.read_csv(_LEGACY_EXIT_LOG_PATH)
            df.columns = [c.strip() for c in df.columns]
            df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
            df = df[df["timestamp"].dt.strftime("%Y%m%d") == date_str]
            for _, r in df.iterrows():
                action = r.get("action")
                if action not in ("SELL_ALL", "SELL_PARTIAL"):
                    continue
                rows.append({
                    "timestamp": r["timestamp"], "action": "SELL", "symbol": str(r.get("symbol")),
                    "qty": None, "price": float(r.get("current_price") or 0),
                    "success": True, "reason": str(r.get("reason", "")),
                    "signal_source": SIGNAL_SOURCE_DYNAMIC_EXIT, "is_test_order": False,
                    "source_file": "exit_engine_log", "entry_price": r.get("entry_price"),
                })
        except Exception as exc:
            logger.error("[ExecutionLedger] 레거시 exit log 백필 실패: %s", exc)

    if not rows:
        return {"skipped": True, "reason": "백필할 레거시 로그 데이터 없음"}

    rows.sort(key=lambda r: r["timestamp"])

    # ── 시간순으로 재생하며 포지션/현금/실현손익을 재구성한다 ──────────────
    cash = initial_cash
    position_qty = 0
    position_symbol = None
    entry_price = None
    written = 0

    for r in rows:
        action = r["action"]
        symbol = r["symbol"]
        qty = r.get("qty")
        price = r["price"]

        if action == "BUY":
            if qty is None or qty <= 0 or not price:
                continue
            before_qty = position_qty if position_symbol == symbol else 0
            cash_before = cash
            cash -= qty * price
            if position_symbol != symbol:
                position_symbol, position_qty, entry_price = symbol, qty, price
            else:
                total_cost = (entry_price or price) * position_qty + price * qty
                position_qty += qty
                entry_price = total_cost / position_qty if position_qty else price
            record_execution(
                action="BUY", symbol=symbol, requested_qty=qty, executed_qty=qty if r["success"] else 0,
                requested_price=price, executed_price=price if r["success"] else None,
                success=r["success"], mode="mock", strategy_name="hynix_switch",
                signal_source=r["signal_source"], before_qty=before_qty,
                after_qty=position_qty if r["success"] else before_qty,
                cash_before=cash_before, cash_after=cash, is_test_order=r["is_test_order"],
                now=r["timestamp"].to_pydatetime(),
            )
            written += 1

        elif action == "SELL":
            if position_symbol != symbol or position_qty <= 0:
                continue
            sell_qty = qty if qty is not None else position_qty  # exit_engine_log는 수량 없음 → 전량으로 간주
            sell_qty = min(sell_qty, position_qty)
            before_qty = position_qty
            realized = (price - (entry_price or price)) * sell_qty
            cash_before = cash
            cash += sell_qty * price
            position_qty -= sell_qty
            if position_qty <= 0:
                position_qty, position_symbol, entry_price = 0, None, None
            record_execution(
                action="SELL", symbol=symbol, requested_qty=sell_qty, executed_qty=sell_qty if r["success"] else 0,
                requested_price=price, executed_price=price if r["success"] else None,
                success=r["success"], mode="mock", strategy_name="hynix_switch",
                signal_source=r["signal_source"], before_qty=before_qty, after_qty=position_qty,
                cash_before=cash_before, cash_after=cash, realized_pnl=round(realized, 2) if r["success"] else None,
                is_test_order=r["is_test_order"], now=r["timestamp"].to_pydatetime(),
            )
            written += 1

    return {
        "skipped": False, "rows_written": written, "final_cash": round(cash, 2),
        "final_position": {"symbol": position_symbol, "quantity": position_qty},
    }
