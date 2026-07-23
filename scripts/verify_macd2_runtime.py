"""scripts/verify_macd2_runtime.py — read-only MACD2 runtime diagnostic.

Reads the CURRENT data/state/macd2_runtime.json and MACD2 ledger files (if
they exist) and reports status. Never calls KIS, never starts a Worker,
never places an order — a pure read-only snapshot, safe to run anytime.

docs §20's READ_ONLY_KIS_SMOKE (a real 3-quote fetch + real bootstrap) and
MOCK_ORDER_SMOKE (a real mock-account order round trip) are SEPARATE steps
requiring explicit user approval — this script does not perform either.

Usage:
    python scripts/verify_macd2_runtime.py
"""
from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from app.trading.macd2 import ledger, state_store  # noqa: E402


def main() -> None:
    print("=== MACD2 runtime state (read-only) ===")
    print(f"state path: {state_store.STATE_PATH}")
    if not state_store.STATE_PATH.exists():
        print("no runtime state file yet (MACD2 has never been started on this machine)")
    else:
        state = state_store.load_state()
        print(
            f"ui_mode={state.ui_mode.value} auto_trade_on={state.auto_trade_on} "
            f"mode={state.mode} budget={state.budget} warmup_ready={state.warmup_ready} "
            f"position={state.position} order_block_reason={state.order_block_reason} "
            f"updated_at={state.updated_at}"
        )

    print()
    print("=== MACD2 signal ledger (read-only) ===")
    print(f"path: {ledger.SIGNAL_LEDGER_PATH}")
    sig_rows = ledger.load_signal_ledger()
    print(f"{len(sig_rows)} rows" + (f" - most recent: {sig_rows[-1]}" if sig_rows else ""))

    print()
    print("=== MACD2 execution ledger (read-only) ===")
    print(f"path: {ledger.EXECUTION_LEDGER_PATH}")
    exec_rows = ledger.load_execution_ledger()
    print(f"{len(exec_rows)} rows" + (f" - most recent: {exec_rows[-1]}" if exec_rows else ""))

    print()
    print("NOTE: this script performs no KIS API calls, starts no Worker, and places no orders.")
    print("READ_ONLY_KIS_SMOKE and MOCK_ORDER_SMOKE (docs section 20) require separate, explicit approval.")


if __name__ == "__main__":
    main()
