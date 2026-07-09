#!/usr/bin/env python
"""
diagnose_hynix_auto_state.py — 하이닉스 자동매매 상태 진단 스크립트.

아래를 서로 비교해 불일치를 리포트한다:
  1) mock 상태파일(hynix_auto_state_mock.json) vs mock 브로커(DryRunBroker) 실제 포지션
  2) real 상태파일(hynix_auto_state_real.json) vs real 브로커(KIS) 실제 포지션
  3) 오늘자 거래 로그(data/logs/hynix_auto_trade_log_{date}.csv) — 최근 BUY/SELL 로그
  4) UI가 실제로 표시할 포지션 소스(get_hynix_auto_position 기준)
  5) 손절/체결 진단 — mode별 손절모드/손절가/현재가/최근 real 주문번호/체결확인여부/
     자동손절 실행 가능 여부(check_auto_stop_loss_safety 6개 조건)

실행: python scripts/diagnose_hynix_auto_state.py
불일치가 있으면 종료코드 1을 반환한다(CI/스케줄러에서 감지용으로 사용 가능).
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from app.services.hynix_switch_state import load_state  # noqa: E402
from app.trading.hynix_position_common import (  # noqa: E402
    get_hynix_auto_position, POSITION_CONFLICT, POSITION_NONE, HYNIX_SYMBOL, INVERSE_SYMBOL,
)


def _section(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def _symbol_to_type(symbol) -> str:
    if symbol == HYNIX_SYMBOL:
        return "HYNIX"
    if symbol == INVERSE_SYMBOL:
        return "INVERSE"
    return "NONE"


def describe_state(mode: str) -> dict:
    state = load_state(mode=mode)
    pos = state.get("position") or {}
    return {
        "mode": mode,
        "symbol": pos.get("symbol"),
        "position_type": _symbol_to_type(pos.get("symbol")),
        "quantity": pos.get("quantity"),
        "entry_price": pos.get("entry_price"),
        "daily_trade_count": state.get("daily_trade_count"),
        "auto_trade_on": state.get("auto_trade_on"),
        "position_conflict": state.get("position_conflict"),
        "residual_position_error": state.get("residual_position_error"),
        "cash": state.get("cash"),
        "last_trade_time": state.get("last_trade_time"),
        "last_action": state.get("last_action"),
        "last_order_id": state.get("last_order_id"),
        "stop_loss_mode": state.get("stop_loss_mode"),
        "pending_manual_stop_loss_alert": state.get("pending_manual_stop_loss_alert"),
        "dynamic_exit_last_decision": state.get("dynamic_exit_last_decision"),
    }


def describe_broker(mode: str) -> dict:
    try:
        from app.config import get_config

        cfg = get_config()
        if mode == "mock":
            from app.trading.dry_run_broker import DryRunBroker

            mock_state = load_state(mode="mock")
            broker = DryRunBroker(initial_balance=mock_state.get("mock_budget_krw", 10_000_000.0))
        else:
            from app.trading.broker_factory import create_broker

            broker = create_broker(cfg, mode="real")

        positions = broker.get_positions()
        detected = get_hynix_auto_position(positions)
        return {
            "mode": mode,
            "ok": True,
            "broker": broker,
            "positions_raw": [(p.symbol, p.name, p.quantity, p.avg_price) for p in positions],
            "detected_position": detected["current_position"],
            "cash": broker.get_buyable_cash(),
            "conflict_error": detected.get("error"),
        }
    except Exception as exc:
        return {"mode": mode, "ok": False, "error": f"브로커 조회 실패: {exc}"}


def latest_trade_log_rows(n: int = 10):
    try:
        import pandas as pd

        path = PROJECT_ROOT / "data" / "logs" / f"hynix_auto_trade_log_{datetime.now().strftime('%Y%m%d')}.csv"
        if not path.exists():
            return None
        return pd.read_csv(path).tail(n)
    except Exception as exc:
        print(f"(거래로그 읽기 실패: {exc})")
        return None


def latest_action_rows(action_prefix: str, n: int = 3):
    """오늘자 거래로그 중 action이 지정 접두어로 시작하는 최근 n건(예: 'BUY', 'SELL')."""
    rows = latest_trade_log_rows(n=1000)
    if rows is None or rows.empty or "action" not in rows.columns:
        return None
    matched = rows[rows["action"].astype(str).str.upper().str.startswith(action_prefix.upper())]
    return matched.tail(n) if not matched.empty else None


def latest_stop_loss_rows(mode: str, n: int = 3):
    """data/logs/stop_loss_log.csv 중 해당 mode의 최근 n건."""
    try:
        import pandas as pd

        path = PROJECT_ROOT / "data" / "logs" / "stop_loss_log.csv"
        if not path.exists():
            return None
        rows = pd.read_csv(path)
        if "mode" not in rows.columns:
            return None
        matched = rows[rows["mode"].astype(str) == mode]
        return matched.tail(n) if not matched.empty else None
    except Exception as exc:
        print(f"(stop_loss_log 읽기 실패: {exc})")
        return None


def compare_state_vs_broker(mode: str, state_info: dict, broker_info: dict, mismatches: list[str]) -> None:
    if not broker_info.get("ok"):
        print(f"⚠️  [{mode}] 브로커 조회 불가 — 비교 생략: {broker_info.get('error')}")
        return

    state_type = state_info["position_type"]
    broker_type = broker_info["detected_position"]

    if broker_type == POSITION_CONFLICT:
        mismatches.append(f"[{mode}] 브로커에 000660/0197X0 동시 보유 감지: {broker_info.get('conflict_error')}")
        return

    if state_type != broker_type:
        mismatches.append(
            f"[{mode}] state 포지션({state_type}, symbol={state_info['symbol']}) != "
            f"브로커 실제 포지션({broker_type}, raw={broker_info.get('positions_raw')})"
        )
    else:
        print(f"✅ [{mode}] state와 브로커 포지션 일치: {state_type}")


def describe_stop_loss_diagnosis(mode: str, state_info: dict, broker_info: dict) -> dict:
    """손절모드/손절가/현재가/최근 real 주문번호/체결확인여부/자동손절 실행 가능 여부."""
    from app.trading.hynix_stop_loss_control import check_auto_stop_loss_safety

    symbol = state_info.get("symbol")
    decision = state_info.get("dynamic_exit_last_decision") or {}
    entry_price = state_info.get("entry_price")
    sl_pct = decision.get("sl_pct")
    stop_loss_price = None
    if entry_price and sl_pct is not None:
        stop_loss_price = entry_price * (1 - sl_pct / 100)

    current_price = None
    if symbol:
        try:
            from app.trading.dynamic_exit_watcher import _fetch_current_price

            current_price = _fetch_current_price(symbol, mode)
        except Exception as exc:
            print(f"(현재가 조회 실패: {exc})")

    sl_rows = latest_stop_loss_rows(mode, n=1)
    order_confirmed = None
    if sl_rows is not None and not sl_rows.empty and "order_confirmed" in sl_rows.columns:
        order_confirmed = bool(sl_rows.iloc[-1]["order_confirmed"])

    auto_stop_loss_executable = None
    failed_checks: list[str] = []
    if broker_info.get("ok") and symbol:
        try:
            from app.trading.hynix_position_common import HynixPositionManager

            pm = HynixPositionManager(broker_info["broker"], mode=mode)
            pm.sync(force=True)
            safety = check_auto_stop_loss_safety(
                load_state(mode=mode), mode, pm, symbol, datetime.now(),
            )
            auto_stop_loss_executable = safety["ok"]
            failed_checks = safety["failed_checks"]
        except Exception as exc:
            failed_checks = [f"안전조건 평가 실패: {exc}"]

    return {
        "mode": mode,
        "stop_loss_mode": state_info.get("stop_loss_mode"),
        "stop_loss_price": stop_loss_price,
        "current_price": current_price,
        "last_order_id": state_info.get("last_order_id"),
        "order_confirmed": order_confirmed,
        "auto_stop_loss_executable": auto_stop_loss_executable,
        "failed_checks": failed_checks,
        "pending_manual_stop_loss_alert": state_info.get("pending_manual_stop_loss_alert"),
    }


def main() -> int:
    mismatches: list[str] = []

    _section("1) mock — state(hynix_auto_state_mock.json) vs 브로커(DryRunBroker)")
    mock_state = describe_state("mock")
    mock_broker = describe_broker("mock")
    print("state :", {k: v for k, v in mock_state.items() if k != "dynamic_exit_last_decision"})
    print("broker:", {k: v for k, v in mock_broker.items() if k != "broker"})
    compare_state_vs_broker("mock", mock_state, mock_broker, mismatches)

    _section("2) real — state(hynix_auto_state_real.json) vs 브로커(KIS 실계좌)")
    real_state = describe_state("real")
    real_broker = describe_broker("real")
    print("state :", {k: v for k, v in real_state.items() if k != "dynamic_exit_last_decision"})
    print("broker:", {k: v for k, v in real_broker.items() if k != "broker"})
    compare_state_vs_broker("real", real_state, real_broker, mismatches)

    _section("3) 오늘자 거래 로그 — 최근 BUY / SELL")
    rows = latest_trade_log_rows()
    if rows is not None and not rows.empty:
        print(rows.to_string(index=False))
        if "success" in rows.columns:
            failed = rows[rows["success"].astype(str).isin(["False", "false"])]
            if not failed.empty:
                print(f"\n⚠️  실패/스킵 주문 {len(failed)}건이 로그에 존재(정상 — 실패도 기록되어야 함)")
        else:
            mismatches.append("거래 로그에 success 컬럼이 없음(구버전 로그 포맷 — 실패 주문이 성공처럼 보일 위험)")
    else:
        print("오늘자 거래 로그 없음")

    latest_buy = latest_action_rows("BUY")
    print("\n최근 BUY 로그:")
    print(latest_buy.to_string(index=False) if latest_buy is not None else "  (없음)")

    latest_sell = latest_action_rows("SELL")
    print("\n최근 SELL 로그:")
    print(latest_sell.to_string(index=False) if latest_sell is not None else "  (없음)")

    if real_state.get("last_order_id") and real_broker.get("ok"):
        real_balance_qty = sum(q for (_, _, q, _) in real_broker.get("positions_raw", []))
        recent_real_buy = latest_action_rows("BUY", n=1)
        if recent_real_buy is not None and real_balance_qty <= 0:
            mismatches.append(
                f"real 주문 로그(order_id={real_state.get('last_order_id')})가 있으나 "
                f"실계좌 보유수량 증가가 확인되지 않음 — 체결 미확인 가능성"
            )

    _section("4) UI가 실제로 표시할 포지션 소스")
    for label, state_info in (("mock", mock_state), ("real", real_state)):
        print(f"[{label}] 보유 종목: {state_info['symbol'] or '없음'} "
              f"(type={state_info['position_type']}, 오늘 거래횟수={state_info['daily_trade_count']})")
        if state_info["position_conflict"]:
            mismatches.append(f"[{label}] state.position_conflict=True — UI에 동시보유 경고가 떠야 함")
        if state_info["residual_position_error"]:
            mismatches.append(f"[{label}] state.residual_position_error=True — 전일 미청산 포지션 의심")

    _section("5) 손절/체결 진단")
    for label, state_info, broker_info in (("mock", mock_state, mock_broker), ("real", real_state, real_broker)):
        diag = describe_stop_loss_diagnosis(label, state_info, broker_info)
        print(f"[{label}] 손절모드={diag['stop_loss_mode']} | "
              f"손절가={diag['stop_loss_price']} | 현재가={diag['current_price']} | "
              f"최근주문번호={diag['last_order_id']} | 체결확인여부={diag['order_confirmed']}")
        if label == "real":
            print(f"  자동손절 실행 가능 여부: {diag['auto_stop_loss_executable']}")
            if diag["failed_checks"]:
                print(f"  실패 조건: {diag['failed_checks']}")
        if diag["pending_manual_stop_loss_alert"]:
            print(f"  ⚠️ 대기중 수동손절 알림: {diag['pending_manual_stop_loss_alert']}")

    _section("진단 결과")
    if mismatches:
        print(f"❌ 불일치 {len(mismatches)}건 발견:")
        for m in mismatches:
            print(" -", m)
        return 1

    print("✅ 불일치 없음 — state / 브로커 / 거래로그 / UI 소스 / 손절 진단이 모두 일치합니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
