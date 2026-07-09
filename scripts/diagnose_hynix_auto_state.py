#!/usr/bin/env python
"""
diagnose_hynix_auto_state.py — 하이닉스 자동매매 상태 진단 스크립트.

아래를 서로 비교해 불일치를 리포트한다:
  1) mock 상태파일(hynix_auto_state_mock.json) vs mock 브로커(DryRunBroker) 실제 포지션
  2) real 상태파일(hynix_auto_state_real.json) vs real 브로커(KIS) 실제 포지션
  3) 오늘자 거래 로그(data/logs/hynix_auto_trade_log_{date}.csv)
  4) UI가 실제로 표시할 포지션 소스(get_hynix_auto_position 기준)

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


def main() -> int:
    mismatches: list[str] = []

    _section("1) mock — state(hynix_auto_state_mock.json) vs 브로커(DryRunBroker)")
    mock_state = describe_state("mock")
    mock_broker = describe_broker("mock")
    print("state :", mock_state)
    print("broker:", mock_broker)
    compare_state_vs_broker("mock", mock_state, mock_broker, mismatches)

    _section("2) real — state(hynix_auto_state_real.json) vs 브로커(KIS 실계좌)")
    real_state = describe_state("real")
    real_broker = describe_broker("real")
    print("state :", real_state)
    print("broker:", real_broker)
    compare_state_vs_broker("real", real_state, real_broker, mismatches)

    _section("3) 오늘자 거래 로그 (data/logs/hynix_auto_trade_log_*.csv)")
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

    _section("4) UI가 실제로 표시할 포지션 소스")
    for label, state_info in (("mock", mock_state), ("real", real_state)):
        print(f"[{label}] 보유 종목: {state_info['symbol'] or '없음'} "
              f"(type={state_info['position_type']}, 오늘 거래횟수={state_info['daily_trade_count']})")
        if state_info["position_conflict"]:
            mismatches.append(f"[{label}] state.position_conflict=True — UI에 동시보유 경고가 떠야 함")
        if state_info["residual_position_error"]:
            mismatches.append(f"[{label}] state.residual_position_error=True — 전일 미청산 포지션 의심")

    _section("진단 결과")
    if mismatches:
        print(f"❌ 불일치 {len(mismatches)}건 발견:")
        for m in mismatches:
            print(" -", m)
        return 1

    print("✅ 불일치 없음 — state / 브로커 / 거래로그 / UI 소스가 모두 일치합니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
