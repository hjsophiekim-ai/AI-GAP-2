"""
hynix_auto_trade_service.py — SK하이닉스 예측 기반 자동매매 (제안 + 승인 모드).

기본 흐름: generate_trade_proposal()로 제안을 만들고, 사용자가 UI에서
승인(PAPER/REAL 버튼 클릭)하면 execute_proposal()로 실제 주문을 실행한다.
run_full_auto_cycle()은 ENABLE_FULL_AUTO=true일 때만 클릭 없이 자동 실행한다.

절대 "확정 수익"/"무조건 상승" 표현을 쓰지 않으며, 데이터 검증 실패 시
임의로 추정하지 않고 주문을 막는다. 모든 판단/제안/실행은 로그로 남긴다.
"""

from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from app.logger import logger
from app.utils.time_utils import kst_now
from app.utils.data_paths import LOGS_DIR as _LOG_DIR, STATE_DIR as _STATE_DIR
from app.trading.hynix_symbols import LONG_SYMBOL, LONG_NAME, SIGNAL_SYMBOL, SIGNAL_NAME

# Actual long-side trading symbol. 000660 remains the signal-only asset and must
# not be treated as an executable/held Hynix position by this service.
HYNIX_SYMBOL = LONG_SYMBOL
HYNIX_NAME = LONG_NAME
HYNIX_SIGNAL_SYMBOL = SIGNAL_SYMBOL
HYNIX_SIGNAL_NAME = SIGNAL_NAME

# 0197X0(SOL SK하이닉스선물단일종목인버스2X)도 이 서비스의 자동매매 유니버스에 포함된다.
# 실제 매수 판단은 predict_hynix_signal(하이닉스 전용)만 하지만, 보유종목 인식/충돌감지는
# 하이닉스와 인버스를 모두 인식해야 한다(app.trading.hynix_position_common 공용 로직 사용).
from app.data_sources.hynix_inverse_collector import INVERSE_SYMBOL, INVERSE_NAME  # noqa: E402

TRADE_SYMBOLS = [HYNIX_SYMBOL, INVERSE_SYMBOL]

_ROOT = Path(__file__).resolve().parent.parent.parent
_STOP_FLAG_PATH = _STATE_DIR / "hynix_auto_trade_stopped.flag"

_ORDER_LOG_COLUMNS = [
    "timestamp", "mode", "full_auto", "action", "symbol", "name",
    "quantity", "price", "order_amount", "success", "order_id",
    "message", "error_type",
]


# ── 킬스위치 ─────────────────────────────────────────────────────────────────

def is_stopped() -> bool:
    return _STOP_FLAG_PATH.exists()


def stop_auto_trade() -> None:
    """Stop kill-switch for proposal auto-trade AND Enhanced live orders.

    Critical: UI "자동매매 정지" must set the same ``auto_trade_on=False`` that
    Enhanced order enablement and MACD mutual-exclusion read via
    ``hynix_switch_state.load_state()`` / ``set_control``. Writing only
    ``hynix_auto_trade_stopped.flag`` left Enhanced ON and caused MACD Start
    false positives (LEGACY_STRATEGY_ACTIVE).
    """
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    _STOP_FLAG_PATH.write_text(kst_now().isoformat(), encoding="utf-8")
    logger.warning("[HYNIX_AUTO] 자동매매 정지 요청됨")
    try:
        # Same write path as Enhanced checkbox OFF:
        # set_control → save_state_atomic → hynix_auto_state_{mode}.json
        # + hynix_strategy_profile_common.json under STATE_DIR (AI_GAP_DATA_DIR).
        from app.services.hynix_switch_engine import set_control

        set_control(auto_trade_on=False)
        logger.warning(
            "[HYNIX_AUTO] Enhanced auto_trade_on=False via set_control "
            "(path=%s)",
            _STATE_DIR / "hynix_auto_state_*.json",
        )
    except Exception as exc:
        logger.error("[HYNIX_AUTO] Failed to set Enhanced auto_trade_on=False: %s", exc)


def resume_auto_trade() -> None:
    if _STOP_FLAG_PATH.exists():
        _STOP_FLAG_PATH.unlink()
    logger.info("[HYNIX_AUTO] 자동매매 재개됨")


# ── 제안 생성 ────────────────────────────────────────────────────────────────

def generate_trade_proposal(mode: str = "mock") -> dict:
    """SK하이닉스 매매 제안을 생성한다. 실행하지 않고 반환만 한다."""
    from app.data_sources.auto_market_collector import collect_all
    from app.models.hynix_short_term_signal import predict_hynix_signal
    from app.trading.hynix_risk_guard import check_risk_guards
    from app.trading.hynix_position_sizer import PositionSizingContext, calculate_position_size
    from app.trading.broker_factory import create_broker
    from app.trading.hynix_position_common import get_hynix_auto_position, POSITION_CONFLICT, POSITION_HYNIX
    from app.config import get_config

    cfg = get_config()

    if is_stopped():
        proposal = _blocked_proposal("자동매매가 정지 상태입니다 (자동매매 정지 버튼으로 재개 전까지 제안을 생성하지 않습니다).")
        _log_decision(proposal, mode=mode)
        return proposal

    market_data = collect_all(mode=mode)
    signal = predict_hynix_signal(market_data)

    if signal.get("blocked"):
        proposal = _blocked_proposal(
            signal.get("block_reason") or "필수 데이터 누락",
            missing_data=signal.get("missing_data", []),
        )
        proposal["signal"] = signal
        _log_decision(proposal, mode=mode)
        return proposal

    try:
        broker = create_broker(cfg, mode=mode)
    except Exception as exc:
        proposal = _blocked_proposal(f"브로커 초기화 실패: {exc}")
        proposal["signal"] = signal
        _log_decision(proposal, mode=mode)
        return proposal

    try:
        positions = broker.get_positions()
    except Exception as exc:
        proposal = _blocked_proposal(f"계좌 조회 실패: {exc}")
        proposal["signal"] = signal
        _log_decision(proposal, mode=mode)
        return proposal

    # 요구사항(2026-07-16) — get_buyable_cash()는 조회 실패(레이트리밋/타임아웃/
    # 토큰 오류/장시작 전 TR 오류 등)와 "실제로 잔고가 0원"을 구분하지 못하고 둘 다
    # 0.0을 반환한다. 그 결과 모의계좌에 실제로 1000만원이 있어도 API가 일시적으로
    # 실패하면 "매수가능금액 0원"으로 오인해 제안 생성을 막았다(2026-07-16 사용자
    # 리포트 — 장시작 전 제안생성 시 실잔고 1006만원인데 0원으로 표시됨).
    # get_buyable_cash_status()(KisMockBroker/KisRealBroker 지원)가 있으면 그 결과로
    # "조회 실패"와 "정상 응답의 실제 0원"을 구분한다 — 실패일 때만 차단 문구를
    # "0원"이 아니라 "조회 실패"로 명확히 하고 rt_cd/msg_cd/msg1을 함께 남긴다.
    cash_status = None
    if hasattr(broker, "get_buyable_cash_status"):
        try:
            cash_status = broker.get_buyable_cash_status()
        except Exception as exc:
            cash_status = {"ok": False, "status": "EXCEPTION", "value": 0.0, "error": str(exc)}
    if cash_status is not None:
        cash = float(cash_status.get("value") or 0.0)
        if not cash_status.get("ok"):
            proposal = _blocked_proposal(
                f"매수가능금액 조회 실패({cash_status.get('status')}) — 실제 잔고가 0원이 아닐 수 있음: "
                f"rt_cd={cash_status.get('rt_cd')} msg_cd={cash_status.get('msg_cd')} "
                f"msg1={cash_status.get('msg1') or cash_status.get('error')}"
            )
            proposal["signal"] = signal
            proposal["cash_query_diagnostic"] = cash_status
            _log_decision(proposal, mode=mode)
            return proposal
        if cash <= 0:
            proposal = _blocked_proposal("매수가능금액 조회 결과 실제로 0원입니다 — 제안 생성 보류")
            proposal["signal"] = signal
            proposal["cash_query_diagnostic"] = cash_status
            _log_decision(proposal, mode=mode)
            return proposal
    else:
        cash = broker.get_buyable_cash()
        if not cash or cash <= 0:
            proposal = _blocked_proposal("매수가능금액 조회 결과 0원 — 계좌 조회 실패 가능성, 제안 생성 보류")
            proposal["signal"] = signal
            _log_decision(proposal, mode=mode)
            return proposal
    detected = get_hynix_auto_position(positions)
    if detected["current_position"] == POSITION_CONFLICT:
        proposal = _blocked_proposal(detected["error"])
        proposal["signal"] = signal
        _log_decision(proposal, mode=mode)
        return proposal
    hynix_position = detected["position"] if detected["current_position"] == POSITION_HYNIX else None
    current_position_value = hynix_position.market_value if hynix_position else 0.0
    total_equity = cash + sum(p.market_value for p in positions)

    raw = signal.get("raw_inputs", {})
    current_price = raw.get("hynix_current_price")
    daily_pnl_pct = _daily_pnl_pct(total_equity)

    minute_ts = None
    if raw.get("minute_last_bar_time"):
        try:
            minute_ts = datetime.fromisoformat(raw["minute_last_bar_time"])
        except Exception:
            minute_ts = None

    risk = check_risk_guards(
        prev_close=raw.get("hynix_prev_close"),
        current_price=current_price,
        source_prices=raw.get("current_price_sources") or {},
        minute_bar_timestamp=minute_ts,
        total_equity=total_equity,
        daily_pnl_pct=daily_pnl_pct,
    )

    ctx = PositionSizingContext(
        total_equity=total_equity,
        cash=cash,
        current_position_value=current_position_value,
        current_price=current_price,
        recent_high=signal.get("recent_high"),
        recent_low=signal.get("recent_low"),
        short_term_score=signal.get("short_term_score"),
        avg_buy_price=hynix_position.avg_price if hynix_position else None,
        mu_return_pct=raw.get("mu_regular_return"),
        sox_return_pct=raw.get("sox_return"),
        hynix_today_return_pct=raw.get("hynix_today_return_pct"),
        target_1=signal.get("target_1"),
        target_2_probability=signal.get("target_2_probability"),
        volume_confirmed=signal.get("volume_confirmed", True),
        upper_wick_near_high=signal.get("upper_wick_near_high", False),
        daily_pnl_pct=daily_pnl_pct,
        data_valid=risk.get("passed", False) or not (risk.get("blocks_buy") and risk.get("blocks_sell")),
    )
    sizing = calculate_position_size(ctx)

    warnings = list(risk.get("reasons", []))
    if risk.get("blocks_buy") and sizing["action"] == "BUY":
        sizing = {"action": "HOLD", "buy_cash_amount": 0.0, "sell_quantity_ratio": 0.0,
                  "reasons": ["리스크 가드에 의해 매수 차단됨"] + sizing.get("reasons", []), "warnings": []}
    if risk.get("blocks_sell") and sizing["action"] == "SELL":
        sizing = {"action": "HOLD", "buy_cash_amount": 0.0, "sell_quantity_ratio": 0.0,
                  "reasons": ["리스크 가드에 의해 매도 차단됨"] + sizing.get("reasons", []), "warnings": []}

    proposal = {
        "blocked": False,
        "block_reason": None,
        "missing_data": [],
        "mode": mode,
        "current_price": current_price,
        "recent_high": signal.get("recent_high"),
        "recent_low": signal.get("recent_low"),
        "drawdown_rate": signal.get("drawdown_rate"),
        "profit_rate": (
            round((current_price / hynix_position.avg_price - 1.0) * 100, 2)
            if hynix_position and hynix_position.avg_price else None
        ),
        "short_term_score": signal.get("short_term_score"),
        "direction": signal.get("direction"),
        "action": sizing["action"],
        "buy_cash_amount": sizing["buy_cash_amount"],
        "sell_quantity_ratio": sizing["sell_quantity_ratio"],
        "support_levels": signal.get("support_levels"),
        "target_levels": signal.get("target_levels"),
        "target_probabilities": signal.get("target_probabilities"),
        "judgement": signal.get("judgement"),
        "reasons_top5": signal.get("reasons_top5"),
        "sizing_reasons": sizing.get("reasons", []),
        "risk_warnings": warnings,
        "sizing_warnings": sizing.get("warnings", []),
        "news_warning": signal.get("news_warning"),
        "cash": cash,
        "total_equity": total_equity,
        "current_position_value": current_position_value,
        "position_quantity": hynix_position.quantity if hynix_position else 0,
        "cash_ratio": round(cash / total_equity * 100, 2) if total_equity else None,
        "symbol_ratio": round(current_position_value / total_equity * 100, 2) if total_equity else None,
        "disclaimer": signal.get("disclaimer"),
        "signal": signal,
        "risk": risk,
        "computed_at": kst_now().isoformat(),
    }
    _log_decision(proposal, mode=mode)
    return proposal


def _blocked_proposal(reason: str, missing_data: Optional[list] = None) -> dict:
    return {
        "blocked": True,
        "block_reason": reason,
        "missing_data": missing_data or [],
        "action": "HOLD",
        "buy_cash_amount": 0.0,
        "sell_quantity_ratio": 0.0,
        "disclaimer": "확률 기반 참고자료이며 투자판단은 사용자 책임입니다.",
        "computed_at": kst_now().isoformat(),
    }


# ── 실행 ─────────────────────────────────────────────────────────────────────

def execute_proposal(
    proposal: dict,
    mode: str,
    confirm_text: str = "",
    runtime_real_mode: bool = False,
    runtime_enable_real_buy: bool = False,
    runtime_enable_real_sell: bool = False,
    full_auto: bool = False,
) -> dict:
    """승인된 제안을 실제 주문으로 실행한다.

    2026-07-15부터 비활성화됨 — 이 경로는 SK하이닉스(000660)를 직접 매수·매도
    했다. Enhanced 자동매매는 이제 상승 신호에 KODEX SK하이닉스단일종목레버리지
    (0193T0), 하락 신호에 SOL SK하이닉스선물단일종목인버스2X(0197X0)를 매매하며
    (app.trading.hynix_switch_position_manager), 000660 직접 주문은 완전히
    금지된다. 이 레거시 제안 실행 경로는 대체되지 않고 그대로 차단만 한다.
    """
    return {
        "success": False,
        "message": (
            "이 실행 경로(레거시 제안 실행)는 비활성화되었습니다 — SK하이닉스(000660) 직접 "
            "주문은 완전히 금지됩니다. Enhanced 자동매매(0193T0/0197X0)를 사용하세요."
        ),
        "error_type": "signal_symbol_direct_order_disabled",
    }


def _execute_proposal_disabled_legacy_body(
    proposal: dict,
    mode: str,
    confirm_text: str = "",
    runtime_real_mode: bool = False,
    runtime_enable_real_buy: bool = False,
    runtime_enable_real_sell: bool = False,
    full_auto: bool = False,
) -> dict:
    """요구사항8(2026-07-15) — 000660 직접 주문 코드는 삭제하지 않고 이 함수 안에
    비활성 상태로 보존한다(호출되지 않음, 사후 참고/회귀 방지용). 실제 실행 경로는
    위 execute_proposal()의 즉시 차단 응답이다."""
    from app.trading.broker_factory import create_broker
    from app.config import get_config

    if is_stopped():
        return {"success": False, "message": "자동매매가 정지 상태입니다.", "error_type": "stopped"}

    if proposal.get("blocked") or proposal.get("action") not in ("BUY", "SELL"):
        return {"success": False, "message": "실행 가능한 제안이 아닙니다 (HOLD 또는 차단됨).", "error_type": "not_actionable"}

    cfg = get_config()
    try:
        broker = create_broker(
            cfg, mode=mode, confirm_text=confirm_text,
            runtime_real_mode=runtime_real_mode,
            runtime_enable_real_buy=runtime_enable_real_buy,
            runtime_enable_real_sell=runtime_enable_real_sell,
        )
    except Exception as exc:
        result = {"success": False, "message": f"브로커 초기화 실패: {exc}", "error_type": "broker_init_failed"}
        _log_order(result, action=proposal.get("action"), symbol=HYNIX_SYMBOL, quantity=0, price=0, mode=mode, full_auto=full_auto)
        return result

    current_price = proposal.get("current_price")
    if not current_price or current_price <= 0:
        result = {"success": False, "message": "현재가 검증 실패", "error_type": "invalid_price"}
        _log_order(result, action=proposal.get("action"), symbol=HYNIX_SYMBOL, quantity=0, price=0, mode=mode, full_auto=full_auto)
        return result

    if proposal["action"] == "BUY":
        quantity = int(proposal.get("buy_cash_amount", 0) // current_price)
        if quantity < 1:
            result = {"success": False, "message": "제안 매수금액으로 1주도 매수할 수 없습니다.", "error_type": "quantity_too_small"}
            _log_order(result, action="BUY", symbol=HYNIX_SYMBOL, quantity=0, price=current_price, mode=mode, full_auto=full_auto)
            return result
        order = broker.buy(HYNIX_SYMBOL, HYNIX_NAME, quantity, current_price)
    else:
        try:
            positions = broker.get_positions()
        except Exception as exc:
            result = {"success": False, "message": f"계좌 조회 실패: {exc}", "error_type": "account_query_failed"}
            _log_order(result, action="SELL", symbol=HYNIX_SYMBOL, quantity=0, price=current_price, mode=mode, full_auto=full_auto)
            return result
        hynix_position = next((p for p in positions if p.symbol == HYNIX_SYMBOL), None)
        if hynix_position is None or hynix_position.quantity < 1:
            result = {"success": False, "message": "매도할 보유 수량이 없습니다.", "error_type": "no_position"}
            _log_order(result, action="SELL", symbol=HYNIX_SYMBOL, quantity=0, price=current_price, mode=mode, full_auto=full_auto)
            return result
        quantity = max(1, int(hynix_position.quantity * proposal.get("sell_quantity_ratio", 0)))
        quantity = min(quantity, hynix_position.quantity)
        order = broker.sell(HYNIX_SYMBOL, HYNIX_NAME, quantity, current_price)

    result = order.to_dict() if hasattr(order, "to_dict") else dict(order)
    _log_order(result, action=proposal["action"], symbol=HYNIX_SYMBOL, quantity=quantity, price=current_price, mode=mode, full_auto=full_auto)
    return result


# ── 완전자동 ─────────────────────────────────────────────────────────────────

def run_full_auto_cycle(mode: str = "mock") -> dict:
    """ENABLE_FULL_AUTO=true일 때만 사용자 클릭 없이 제안 생성 + 실행까지 수행."""
    from app.config import get_config

    cfg = get_config()
    if not cfg.full_auto_enabled():
        return {"skipped": True, "reason": "ENABLE_FULL_AUTO가 활성화되지 않았습니다."}

    proposal = generate_trade_proposal(mode=mode)
    if proposal.get("blocked") or proposal.get("action") not in ("BUY", "SELL"):
        return {"skipped": True, "reason": proposal.get("block_reason") or "실행할 제안 없음 (HOLD)", "proposal": proposal}

    if mode == "real":
        if not cfg.full_auto_real_confirm_ok():
            logger.warning("[HYNIX_AUTO] 완전자동 REAL 실행 게이트 미충족 — 실행하지 않음")
            return {"skipped": True, "reason": "완전자동 REAL 게이트(safety.enable_real_trading / FULL_AUTO_REAL_CONFIRM_TEXT) 미충족", "proposal": proposal}
        result = execute_proposal(
            proposal, mode="real", confirm_text=cfg.real_confirm_text(),
            runtime_real_mode=True, runtime_enable_real_buy=True, runtime_enable_real_sell=True,
            full_auto=True,
        )
    else:
        result = execute_proposal(proposal, mode="mock", full_auto=True)

    return {"skipped": False, "proposal": proposal, "order_result": result}


# ── 로깅 ─────────────────────────────────────────────────────────────────────

def _log_decision(proposal: dict, mode: str) -> None:
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        date_str = kst_now().strftime("%Y%m%d")
        path = _LOG_DIR / f"hynix_auto_trade_decisions_{date_str}.jsonl"
        record = dict(proposal)
        record["mode"] = mode
        record["logged_at"] = kst_now().isoformat()
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception as exc:
        logger.debug("hynix auto-trade decision log write failed: %s", exc)


def _log_order(result: dict, action: str, symbol: str, quantity: int, price: float, mode: str, full_auto: bool) -> None:
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        date_str = kst_now().strftime("%Y%m%d")
        path = _LOG_DIR / f"hynix_auto_trade_orders_{date_str}.csv"
        is_new = not path.exists()
        with path.open("a", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=_ORDER_LOG_COLUMNS)
            if is_new:
                writer.writeheader()
            writer.writerow({
                "timestamp": kst_now().strftime("%Y-%m-%d %H:%M:%S"),
                "mode": mode,
                "full_auto": full_auto,
                "action": action,
                "symbol": symbol,
                "name": HYNIX_NAME,
                "quantity": quantity,
                "price": price,
                "order_amount": (quantity or 0) * (price or 0),
                "success": result.get("success"),
                "order_id": result.get("order_id", ""),
                "message": result.get("message", ""),
                "error_type": result.get("error_type", ""),
            })
    except Exception as exc:
        logger.debug("hynix auto-trade order log write failed: %s", exc)


def _daily_pnl_pct(total_equity: float) -> float:
    """당일 시작 자산 대비 현재 총자산의 등락률(%). 상태 파일에 자정 기준 스냅샷을 유지한다."""
    try:
        _STATE_DIR.mkdir(parents=True, exist_ok=True)
        date_str = kst_now().strftime("%Y%m%d")
        path = _STATE_DIR / f"hynix_daily_equity_{date_str}.json"
        if path.exists():
            baseline = json.loads(path.read_text(encoding="utf-8")).get("baseline_equity")
        else:
            baseline = None
        if baseline is None or baseline <= 0:
            path.write_text(json.dumps({"baseline_equity": total_equity}), encoding="utf-8")
            return 0.0
        return (total_equity / baseline - 1.0) * 100
    except Exception as exc:
        logger.debug("daily pnl baseline read/write failed: %s", exc)
        return 0.0
