"""READ_ONLY_KIS_SMOKE — MOCK 계좌 전용, 주문/체결/포지션 변경 API 절대 호출 금지.

허용: 000660/0193T0/0197X0 현재가 조회, 000660 1분봉 조회(전일 포함
bootstrap), 3분봉 생성, MACD/Signal/Histogram 계산, signed B 신호 계산.
금지: buy/sell, orderable cash 변경성 호출, 주문 테스트, 체결조회, 포지션
변경, 원장 기록. app.trading.macd2.order_executor / worker / service / ledger
는 이 스크립트에서 import조차 하지 않는다 — 실수로라도 주문·원장 API에
닿을 길이 없다.

계좌 보유수량(get_positions, 순수 조회 API)만 실행 전후로 비교해 0건
변경을 확인한다. macd2 원장 CSV 2개도 이번 smoke에서는 절대 기록하지
않으므로 실행 전후 존재 여부/내용이 그대로인지 함께 확인한다.
"""
from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.trading.macd2 import config  # noqa: E402
from app.trading.macd2.broker_adapter import create_macd2_broker  # noqa: E402
from app.trading.macd2.ledger import EXECUTION_LEDGER_PATH, SIGNAL_LEDGER_PATH  # noqa: E402
from app.trading.macd2.market_data import MarketDataService  # noqa: E402
from app.trading.macd2.signal_engine import calculate_macd, evaluate_signed_b, resample_completed_3m  # noqa: E402

KST = config.KST
SYMBOLS = (config.WATCH_SYMBOL, config.LONG_SYMBOL, config.INVERSE_SYMBOL)


def _ledger_fingerprint() -> dict:
    fp = {}
    for label, path in (("signal_ledger", SIGNAL_LEDGER_PATH), ("execution_ledger", EXECUTION_LEDGER_PATH)):
        fp[label] = {
            "exists": path.exists(),
            "size": path.stat().st_size if path.exists() else None,
            "mtime": path.stat().st_mtime if path.exists() else None,
        }
    return fp


def _positions_snapshot(broker) -> list[tuple[str, int, float]]:
    return sorted((p.symbol, int(p.quantity), float(p.avg_price)) for p in broker.get_positions())


def main() -> int:
    report: dict = {"mode": "mock", "errors": []}

    broker = create_macd2_broker("mock")

    t0 = time.monotonic()
    try:
        positions_before = _positions_snapshot(broker)
    except Exception as exc:
        report["errors"].append(f"positions_before: {exc!r}")
        positions_before = None
    report["positions_before_call_sec"] = round(time.monotonic() - t0, 3)
    ledger_fp_before = _ledger_fingerprint()

    mds = MarketDataService(mode="mock")  # real KIS fetchers — no fakes injected

    quotes: dict[str, float] = {}
    quote_errors: dict[str, str] = {}
    t0 = time.monotonic()
    try:
        snaps = mds.refresh_quotes(symbols=SYMBOLS)
        for symbol, snap in snaps.items():
            if snap.error:
                quote_errors[symbol] = snap.error
            else:
                quotes[symbol] = snap.price
    except Exception as exc:
        report["errors"].append(f"refresh_quotes: {exc!r}")
    report["quotes"] = quotes
    report["quote_errors"] = quote_errors
    report["quotes_call_sec"] = round(time.monotonic() - t0, 3)

    t0 = time.monotonic()
    boot = None
    try:
        boot = mds.bootstrap(now=datetime.now(KST))
    except Exception as exc:
        report["errors"].append(f"bootstrap: {exc!r}")
    report["bootstrap_call_sec"] = round(time.monotonic() - t0, 3)

    if boot is not None:
        report["bootstrap_ok"] = boot.ok
        report["bootstrap_reason"] = boot.reason
        report["received_1m_bars"] = boot.received_1m_bars
        report["prior_day_1m_bars"] = boot.prior_day_1m_bars
        report["today_1m_bars"] = boot.today_1m_bars
        report["completed_3m_count"] = boot.completed_3m_count

    warmup_ready = False
    macd_snap = None
    signed_b = None
    if boot is not None and boot.ok:
        df_1m = mds.get_history_df()
        if not df_1m.empty:
            report["data_range"] = {
                "from": df_1m["datetime"].iloc[0].isoformat(),
                "to": df_1m["datetime"].iloc[-1].isoformat(),
            }
        bars_3m = resample_completed_3m(df_1m, now=datetime.now(KST))
        macd_snap = calculate_macd(bars_3m)
        warmup_ready = macd_snap is not None
        if macd_snap is not None:
            signed_b = evaluate_signed_b(macd_snap, None)

    report["warmup_ready"] = warmup_ready
    if macd_snap is not None:
        report["macd"] = macd_snap.macd
        report["signal"] = macd_snap.signal
        report["hist"] = macd_snap.hist
        report["hist_last3"] = macd_snap.hist_last3
        report["bar_dt"] = macd_snap.bar_dt.isoformat()
    report["current_flag"] = signed_b.value if signed_b is not None else None

    t0 = time.monotonic()
    try:
        positions_after = _positions_snapshot(broker)
    except Exception as exc:
        report["errors"].append(f"positions_after: {exc!r}")
        positions_after = None
    report["positions_after_call_sec"] = round(time.monotonic() - t0, 3)
    ledger_fp_after = _ledger_fingerprint()

    report["positions_unchanged"] = (
        positions_before is not None and positions_after is not None and positions_before == positions_after
    )
    report["positions_before"] = positions_before
    report["positions_after"] = positions_after
    report["ledger_unchanged"] = ledger_fp_before == ledger_fp_after
    report["ledger_fingerprint_before"] = ledger_fp_before
    report["ledger_fingerprint_after"] = ledger_fp_after
    # Orders API is never called by this script at all — 0 new orders by
    # construction, not by querying an execution/fill endpoint (forbidden).
    report["orders_placed_this_run"] = 0

    import json

    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0 if not report["errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
