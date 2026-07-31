#!/usr/bin/env python
"""Read-only MACD live first-flag verification checklist (ops / tomorrow morning).

Does NOT change strategy, thresholds, or trading state. Reads state / mutex /
ledger / git only.

## Tomorrow morning runbook (after Render Stop→Start)

1. On Render (live): MACD **Stop** → **Start** (full reload).
2. Confirm ``worker_code_sha`` in state is one of ``5b47073``, ``587e18d``,
   or the current local HEAD short (documented below if HEAD moved).
3. From a machine that can see ``data/state`` + ledger (local sync or Render shell):

   ```
   python scripts/verify_macd_live_flag_checklist.py
   python scripts/verify_macd_live_flag_checklist.py --poll --timeout-sec 7200
   ```

4. Walk items **1–9** printed by the script (same list as
   ``data/state/macd_tomorrow_live_checklist.md``).
5. First real flag dump path (when ``--poll`` finds one, or ``--dump-now``):

   ``data/state/macd_live_first_flag_YYYYMMDD.json``

## Checklist items (encoded)

1. Local / Origin / Render SHA match
2. worker_code_sha = 5b47073 or 587e18d (or document current HEAD if moved)
3. Previous worker thread residual = 0
4. last_tick updates ~every 5s
5. First real flag linkage: signal_id, decision_trace, order_requested_at,
   KIS order no, broker_executed_at, position_confirmed_at, ledger
6. If flag but no order → primary_block_reason shown
7. Same flag held → 0 duplicate orders
8. Opposite flag → full sell → qty 0 → opposite buy
9. 15:00 account holdings = 0

Prep only today — do **not** claim live verify done until tomorrow after Stop→Start.
"""
from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

STATE_DIR = ROOT / "data" / "state"
STATE_PATH = STATE_DIR / "macd_hynix_state.json"
MUTEX_PATH = STATE_DIR / "macd_hynix_mutex.json"
RUNTIME_INFO_PATH = STATE_DIR / "runtime_info.json"
LEDGER_PATH = ROOT / "data" / "logs" / "macd_hynix_execution_ledger.csv"

# SHAs accepted for tomorrow live verify at prep time (HEAD + prior E2E fix).
KNOWN_GOOD_WORKER_SHAS = ("5b47073", "587e18d")
FORCE_FLAT_HHMM = (15, 0)
TICK_TARGET_SEC = 5.0
TICK_SOFT_MAX_SEC = 8.0


def _git(*args: str) -> Optional[str]:
    try:
        out = subprocess.check_output(
            ["git", *args],
            cwd=str(ROOT),
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        )
        return (out or "").strip() or None
    except Exception:
        return None


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _parse_iso(ts: Any) -> Optional[datetime]:
    if ts is None:
        return None
    s = str(ts).strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


def _now() -> datetime:
    return datetime.now()


def _today_ymd(now: Optional[datetime] = None) -> str:
    return (now or _now()).strftime("%Y%m%d")


def _today_iso(now: Optional[datetime] = None) -> str:
    return (now or _now()).strftime("%Y-%m-%d")


def _sha_short(sha: Optional[str], n: int = 7) -> str:
    if not sha:
        return ""
    return str(sha).strip()[:n]


def _load_ledger(limit: int = 5000) -> list[dict[str, Any]]:
    if not LEDGER_PATH.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        with LEDGER_PATH.open("r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                rows.append(dict(row))
    except Exception:
        return []
    if limit and len(rows) > limit:
        return rows[-limit:]
    return rows


def _row_date(row: dict[str, Any]) -> Optional[str]:
    for key in ("timestamp", "order_requested_at", "signal_detected_at", "broker_executed_at"):
        dt = _parse_iso(row.get(key))
        if dt:
            return dt.strftime("%Y-%m-%d")
    return None


def _today_ledger(rows: list[dict[str, Any]], day: str) -> list[dict[str, Any]]:
    return [r for r in rows if _row_date(r) == day]


def _collect_git() -> dict[str, Any]:
    local_full = _git("rev-parse", "HEAD")
    local_short = _git("rev-parse", "--short", "HEAD") or _sha_short(local_full)
    origin_full = _git("rev-parse", "origin/main")
    origin_short = _sha_short(origin_full)
    runtime = _load_json(RUNTIME_INFO_PATH)
    render_sha = (
        runtime.get("render_sha")
        or None
    )
    # Prefer env-style values already persisted; else note unavailable locally.
    return {
        "local_full": local_full,
        "local_short": local_short,
        "origin_main_full": origin_full,
        "origin_main_short": origin_short,
        "render_sha": render_sha,
        "render_sha_short": _sha_short(render_sha) if render_sha else None,
        "runtime_info_path": str(RUNTIME_INFO_PATH),
        "runtime_sha_all_match": runtime.get("sha_all_match"),
        "known_good_worker_shas": list(KNOWN_GOOD_WORKER_SHAS),
        "note": (
            "Render SHA comes from data/state/runtime_info.json when present "
            "(RENDER_GIT_COMMIT on the live host). Locally it may equal HEAD "
            "or be stale — confirm after Stop→Start on Render."
        ),
    }


def _status(ok: Optional[bool], detail: str) -> dict[str, Any]:
    if ok is True:
        label = "PASS"
    elif ok is False:
        label = "FAIL"
    else:
        label = "PENDING"
    return {"status": label, "ok": ok, "detail": detail}


def _first_real_flag_bundle(
    state: dict[str, Any],
    ledger_today: list[dict[str, Any]],
    session_date: str,
) -> Optional[dict[str, Any]]:
    """Pick first actionable flag after session open for today."""
    trace = state.get("decision_trace") if isinstance(state.get("decision_trace"), dict) else {}
    last_signal_id = state.get("last_signal_id") or (trace or {}).get("signal_id")
    pending_id = state.get("pending_signal_id")
    latency = state.get("order_latency_last") or state.get("order_latency") or {}
    if isinstance(latency, dict) and latency.get("signal_id"):
        sid = str(latency.get("signal_id"))
    else:
        sid = str(last_signal_id or pending_id or "")

    # Prefer earliest successful BUY today with signal_id
    buys = [
        r for r in ledger_today
        if str(r.get("action") or "").upper() == "BUY"
        and str(r.get("signal_id") or "").strip()
        and str(r.get("success") or "").lower() in ("true", "1", "yes", "")
    ]
    buys_sorted = sorted(
        buys,
        key=lambda r: _parse_iso(r.get("order_requested_at") or r.get("timestamp")) or datetime.min,
    )
    if buys_sorted:
        first = buys_sorted[0]
        sid = str(first.get("signal_id") or sid)
        linked = [r for r in ledger_today if str(r.get("signal_id") or "") == sid]
        return {
            "source": "ledger_first_buy",
            "signal_id": sid,
            "decision_trace": trace or None,
            "order_requested_at": first.get("order_requested_at") or state.get("order_requested_at"),
            "kis_order_no": first.get("order_id") or state.get("last_order_id"),
            "kis_order_accepted_at": first.get("kis_order_accepted_at") or state.get("kis_order_accepted_at"),
            "broker_executed_at": first.get("broker_executed_at") or state.get("broker_executed_at"),
            "position_confirmed_at": first.get("position_confirmed_at") or state.get("position_confirmed_at"),
            "ledger_rows": linked,
            "order_latency_last": latency if isinstance(latency, dict) else None,
            "pipeline": state.get("pipeline"),
            "primary_block_reason": state.get("primary_block_reason"),
            "session_date": session_date,
        }

    # Flag armed / traced but maybe blocked
    flag = str(state.get("current_flag") or state.get("last_flag") or (trace or {}).get("flag") or "")
    has_trace = bool(trace) and (
        trace.get("signal_id")
        or trace.get("execute_attempted")
        or trace.get("broker_called")
        or flag in ("UP_RED", "DOWN_BLUE")
        or bool(pending_id)
        or bool(last_signal_id)
    )
    if has_trace or sid:
        return {
            "source": "state_decision_trace",
            "signal_id": sid or None,
            "decision_trace": trace or None,
            "order_requested_at": state.get("order_requested_at"),
            "kis_order_no": state.get("last_order_id"),
            "kis_order_accepted_at": state.get("kis_order_accepted_at"),
            "broker_executed_at": state.get("broker_executed_at"),
            "position_confirmed_at": state.get("position_confirmed_at"),
            "ledger_rows": [
                r for r in ledger_today
                if sid and str(r.get("signal_id") or "") == sid
            ],
            "order_latency_last": latency if isinstance(latency, dict) else None,
            "pipeline": state.get("pipeline"),
            "primary_block_reason": state.get("primary_block_reason"),
            "flag": flag,
            "pending_signal_id": pending_id,
            "session_date": session_date,
        }
    return None


def evaluate_checklist(
    *,
    state: dict[str, Any],
    mutex: dict[str, Any],
    git_info: dict[str, Any],
    ledger_rows: list[dict[str, Any]],
    prev_tick_at: Optional[str] = None,
) -> dict[str, Any]:
    now = _now()
    day = _today_iso(now)
    session_date = str(state.get("session_date") or day)
    worker = state.get("worker") if isinstance(state.get("worker"), dict) else {}
    ledger_today = _today_ledger(ledger_rows, day)
    bundle = _first_real_flag_bundle(state, ledger_today, session_date)

    local_s = git_info.get("local_short") or ""
    origin_s = git_info.get("origin_main_short") or ""
    render_s = git_info.get("render_sha_short") or ""
    worker_sha = _sha_short(state.get("worker_code_sha") or state.get("git_sha") or mutex.get("git_sha"))

    # 1) Local/Origin/Render SHA match
    sha_parts = [p for p in (local_s, origin_s, render_s) if p]
    if len(sha_parts) >= 2 and local_s and origin_s and local_s == origin_s:
        if render_s and render_s != local_s:
            item1 = _status(
                False,
                f"local={local_s} origin={origin_s} render={render_s} (Render mismatch)",
            )
        elif not render_s:
            item1 = _status(
                None,
                f"local={local_s} origin={origin_s} render=UNAVAILABLE "
                f"(confirm runtime_info.json on Render after Start)",
            )
        else:
            item1 = _status(True, f"local=origin=render={local_s}")
    elif local_s and origin_s and local_s != origin_s:
        item1 = _status(False, f"local={local_s} != origin={origin_s} render={render_s or '?'}")
    else:
        item1 = _status(None, f"incomplete SHA view local={local_s} origin={origin_s} render={render_s or '?'}")

    # 2) worker_code_sha in known set OR current HEAD
    accepted = set(KNOWN_GOOD_WORKER_SHAS)
    if local_s:
        accepted.add(local_s)
    if worker_sha and any(worker_sha.startswith(a) or a.startswith(worker_sha) for a in accepted):
        item2 = _status(
            True,
            f"worker_code_sha={worker_sha} (accepted; known={list(KNOWN_GOOD_WORKER_SHAS)} head={local_s})",
        )
    elif worker_sha:
        item2 = _status(
            False,
            f"worker_code_sha={worker_sha} not in {sorted(accepted)} — document new HEAD if intentional",
        )
    else:
        item2 = _status(None, "worker_code_sha missing (worker not started yet?)")

    # 3) Previous worker thread residual = 0
    alive = bool(worker.get("alive"))
    stale = bool(state.get("stale_worker"))
    mutex_on = bool(mutex.get("macd_auto_trade_on") or mutex.get("enabled"))
    auto_on = bool(state.get("auto_trade_on"))
    if stale:
        item3 = _status(False, f"stale_worker=True reason={state.get('stale_worker_reason')}")
    elif auto_on and alive and not stale:
        item3 = _status(
            True,
            "worker.alive=True stale_worker=False (file-level residual=0; "
            "confirm single thread_ident in UI/process after Stop→Start)",
        )
    elif auto_on and not alive:
        item3 = _status(False, "auto_trade_on but worker.alive=False (residual/stopped?)")
    else:
        item3 = _status(
            None,
            f"auto_trade_on={auto_on} alive={alive} mutex_on={mutex_on} — await Start",
        )

    # 4) last_tick ~ every 5s
    last_tick = worker.get("last_tick_at")
    intervals = [float(x) for x in (worker.get("tick_intervals") or []) if x is not None and x != ""]
    avg_iv = worker.get("avg_interval")
    try:
        avg_f = float(avg_iv) if avg_iv is not None else (sum(intervals[-10:]) / len(intervals[-10:]) if intervals else None)
    except Exception:
        avg_f = None
    tick_dt = _parse_iso(last_tick)
    age_sec = (now - tick_dt).total_seconds() if tick_dt else None
    moved = None
    if prev_tick_at is not None and last_tick:
        moved = str(prev_tick_at) != str(last_tick)
    if not last_tick:
        item4 = _status(None, "last_tick_at missing")
    elif age_sec is not None and age_sec > 30 and alive:
        item4 = _status(False, f"last_tick_at stale age={age_sec:.1f}s avg_interval={avg_f}")
    elif avg_f is not None and avg_f <= TICK_SOFT_MAX_SEC:
        detail = f"avg_interval={avg_f:.3f}s (~{TICK_TARGET_SEC}s) last_tick={last_tick}"
        if moved is True:
            detail += " (tick advanced while polling)"
        item4 = _status(True, detail)
    elif avg_f is not None:
        item4 = _status(False, f"avg_interval={avg_f:.3f}s > {TICK_SOFT_MAX_SEC}s last_tick={last_tick}")
    else:
        item4 = _status(None, f"last_tick={last_tick} age={age_sec} intervals empty — keep polling")

    # 5) First real flag full linkage
    if not bundle:
        item5 = _status(None, "no first real flag / decision_trace / ledger BUY yet today")
    else:
        sid = bundle.get("signal_id")
        need = {
            "signal_id": bool(sid),
            "decision_trace": bool(bundle.get("decision_trace")),
            "order_requested_at": bool(bundle.get("order_requested_at")),
            "kis_order_no": bool(bundle.get("kis_order_no")),
            "broker_executed_at": bool(bundle.get("broker_executed_at")),
            "position_confirmed_at": bool(bundle.get("position_confirmed_at")),
            "ledger": bool(bundle.get("ledger_rows")),
        }
        missing = [k for k, v in need.items() if not v]
        if not missing:
            item5 = _status(True, f"signal_id={sid} all linkage fields present")
        elif bundle.get("primary_block_reason") and not need["order_requested_at"]:
            item5 = _status(
                None,
                f"flag/trace present but blocked ({bundle.get('primary_block_reason')}); "
                f"missing={missing}",
            )
        else:
            item5 = _status(False if need["signal_id"] else None, f"signal_id={sid} missing={missing}")

    # 6) Flag but no order → primary_block_reason
    flag_now = str(state.get("current_flag") or state.get("last_flag") or "")
    new_sig = bool((state.get("last_signal_eval") or {}).get("new_signal")) or bool(state.get("pending_signal_id"))
    has_order = bool(state.get("order_requested_at") or (bundle and bundle.get("order_requested_at")))
    pbr = state.get("primary_block_reason") or state.get("order_block_reason")
    if (flag_now in ("UP_RED", "DOWN_BLUE") or new_sig or state.get("pending_signal_id")) and not has_order:
        if pbr:
            item6 = _status(True, f"flag without order; primary_block_reason={pbr}")
        else:
            item6 = _status(False, "flag/pending without order and primary_block_reason empty")
    elif has_order:
        item6 = _status(True, "order path active (N/A block-reason case) or prior order timestamps set")
    else:
        item6 = _status(None, "no flag-without-order case yet")

    # 7) Same flag held → 0 duplicate orders
    by_sid: dict[str, list[dict[str, Any]]] = {}
    for r in ledger_today:
        sid = str(r.get("signal_id") or "").strip()
        if not sid:
            continue
        if str(r.get("action") or "").upper() != "BUY":
            continue
        by_sid.setdefault(sid, []).append(r)
    dup = {sid: rows for sid, rows in by_sid.items() if len(rows) > 1}
    if not by_sid:
        item7 = _status(None, "no BUY rows today yet")
    elif dup:
        item7 = _status(False, f"duplicate BUY signal_ids={ {k: len(v) for k, v in dup.items()} }")
    else:
        item7 = _status(True, f"0 duplicate BUYs across {len(by_sid)} signal_id(s)")

    # 8) Opposite flag → full sell → qty 0 → opposite buy
    sells = [r for r in ledger_today if str(r.get("action") or "").upper() == "SELL"]
    buys = [r for r in ledger_today if str(r.get("action") or "").upper() == "BUY"]
    if len(buys) >= 2 and sells:
        # Heuristic: sell then buy with different symbols around same window
        item8 = _status(
            True,
            f"observed SELL={len(sells)} BUY={len(buys)} today (verify qty→0 then opposite buy in dump)",
        )
    elif sells and not buys:
        pos_qty = int((state.get("position") or {}).get("quantity") or 0)
        if pos_qty == 0:
            item8 = _status(None, "SELL seen, flat now — awaiting opposite BUY")
        else:
            item8 = _status(False, f"SELL path incomplete; position.qty={pos_qty}")
    else:
        item8 = _status(None, "no opposite-switch episode observed yet today")

    # 9) 15:00 holdings = 0
    pos = state.get("position") or {}
    qty = int(pos.get("quantity") or 0)
    liq_done = str(state.get("force_liquidate_done_date") or "")
    hm = (now.hour, now.minute)
    if hm >= FORCE_FLAT_HHMM:
        if qty == 0:
            item9 = _status(
                True,
                f"after 15:00 position.qty=0 force_liquidate_done_date={liq_done or 'n/a'}",
            )
        else:
            item9 = _status(False, f"after 15:00 still qty={qty} liq_done={liq_done}")
    else:
        item9 = _status(None, f"before 15:00 (now={now.strftime('%H:%M')}); qty={qty} — check at close")

    items = {
        "1_local_origin_render_sha_match": item1,
        "2_worker_code_sha_accepted": item2,
        "3_previous_worker_thread_residual_0": item3,
        "4_last_tick_about_5s": item4,
        "5_first_real_flag_linkage": item5,
        "6_flag_no_order_shows_primary_block_reason": item6,
        "7_same_flag_held_zero_duplicate_orders": item7,
        "8_opposite_flag_full_sell_then_buy": item8,
        "9_1500_holdings_flat": item9,
    }
    return {
        "evaluated_at": now.isoformat(),
        "session_date": session_date,
        "mode": state.get("mode"),
        "auto_trade_on": state.get("auto_trade_on"),
        "worker_code_sha": worker_sha,
        "mutex": {
            "owner": mutex.get("owner"),
            "enabled": mutex.get("enabled"),
            "macd_auto_trade_on": mutex.get("macd_auto_trade_on"),
            "mode": mutex.get("mode"),
            "git_sha": mutex.get("git_sha"),
            "updated_at": mutex.get("updated_at"),
        },
        "worker": {
            "alive": worker.get("alive"),
            "last_tick_at": last_tick,
            "avg_interval": avg_f,
            "tick_intervals_tail": intervals[-10:],
        },
        "git": git_info,
        "first_flag_bundle": bundle,
        "ledger_today_count": len(ledger_today),
        "items": items,
        "pass_count": sum(1 for v in items.values() if v.get("ok") is True),
        "fail_count": sum(1 for v in items.values() if v.get("ok") is False),
        "pending_count": sum(1 for v in items.values() if v.get("ok") is None),
    }


def print_report(report: dict[str, Any]) -> None:
    print("=" * 78)
    print("MACD LIVE FIRST-FLAG CHECKLIST (read-only)")
    print("=" * 78)
    print(f"evaluated_at: {report.get('evaluated_at')}")
    print(f"session_date: {report.get('session_date')}  mode={report.get('mode')}  auto_trade_on={report.get('auto_trade_on')}")
    print(f"worker_code_sha: {report.get('worker_code_sha')}")
    g = report.get("git") or {}
    print(
        f"git: local={g.get('local_short')} origin={g.get('origin_main_short')} "
        f"render={g.get('render_sha_short') or 'UNAVAILABLE'}"
    )
    w = report.get("worker") or {}
    print(f"worker.alive={w.get('alive')} last_tick={w.get('last_tick_at')} avg_interval={w.get('avg_interval')}")
    print(f"mutex: {report.get('mutex')}")
    print("-" * 78)
    for key, item in (report.get("items") or {}).items():
        print(f"[{item.get('status'):7}] {key}: {item.get('detail')}")
    print("-" * 78)
    print(
        f"PASS={report.get('pass_count')} FAIL={report.get('fail_count')} "
        f"PENDING={report.get('pending_count')} ledger_today={report.get('ledger_today_count')}"
    )
    bundle = report.get("first_flag_bundle")
    if bundle:
        print(f"first_flag source={bundle.get('source')} signal_id={bundle.get('signal_id')}")
    else:
        print("first_flag: (none yet)")
    print("=" * 78)


def build_dump(report: dict[str, Any], state: dict[str, Any], mutex: dict[str, Any]) -> dict[str, Any]:
    return {
        "captured_at": datetime.now().isoformat(),
        "prep_note": (
            "Live first-flag execution log dump. Strategy/thresholds unchanged. "
            "READY_FOR_MOCK still stands from prior mock E2E; this file is for "
            "tomorrow's live verify after Stop→Start."
        ),
        "known_good_worker_shas": list(KNOWN_GOOD_WORKER_SHAS),
        "checklist_report": report,
        "mutex": mutex,
        "state_excerpt": {
            "auto_trade_on": state.get("auto_trade_on"),
            "mode": state.get("mode"),
            "session_date": state.get("session_date"),
            "worker_code_sha": state.get("worker_code_sha"),
            "git_sha": state.get("git_sha"),
            "stale_worker": state.get("stale_worker"),
            "stale_worker_reason": state.get("stale_worker_reason"),
            "current_flag": state.get("current_flag"),
            "last_flag": state.get("last_flag"),
            "last_signal_id": state.get("last_signal_id"),
            "pending_signal_id": state.get("pending_signal_id"),
            "primary_block_reason": state.get("primary_block_reason"),
            "order_block_reason": state.get("order_block_reason"),
            "duplicate_block_reason": state.get("duplicate_block_reason"),
            "order_requested_at": state.get("order_requested_at"),
            "kis_order_accepted_at": state.get("kis_order_accepted_at"),
            "broker_executed_at": state.get("broker_executed_at"),
            "position_confirmed_at": state.get("position_confirmed_at"),
            "order_latency": state.get("order_latency"),
            "order_latency_last": state.get("order_latency_last"),
            "decision_trace": state.get("decision_trace"),
            "pipeline": state.get("pipeline"),
            "position": state.get("position"),
            "worker": state.get("worker"),
            "processed_signal_ids": state.get("processed_signal_ids"),
            "force_liquidate_done_date": state.get("force_liquidate_done_date"),
            "updated_at": state.get("updated_at"),
        },
        "first_flag_bundle": report.get("first_flag_bundle"),
        "full_state": state,
    }


def dump_path_for_today(now: Optional[datetime] = None) -> Path:
    return STATE_DIR / f"macd_live_first_flag_{_today_ymd(now)}.json"


def write_dump(payload: dict[str, Any], path: Path) -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only MACD live first-flag checklist")
    parser.add_argument("--poll", action="store_true", help="Poll until first flag/order or timeout")
    parser.add_argument("--timeout-sec", type=int, default=7200, help="Poll timeout (default 7200)")
    parser.add_argument("--interval-sec", type=float, default=5.0, help="Poll interval seconds")
    parser.add_argument("--dump-now", action="store_true", help="Write dump JSON even without first flag")
    parser.add_argument("--once", action="store_true", help="Single evaluation (default if not --poll)")
    args = parser.parse_args(argv)

    git_info = _collect_git()
    prev_tick: Optional[str] = None
    deadline = time.monotonic() + max(1, int(args.timeout_sec))
    report: dict[str, Any] = {}
    state: dict[str, Any] = {}
    mutex: dict[str, Any] = {}

    while True:
        state = _load_json(STATE_PATH)
        mutex = _load_json(MUTEX_PATH)
        ledger_rows = _load_ledger()
        report = evaluate_checklist(
            state=state,
            mutex=mutex,
            git_info=git_info,
            ledger_rows=ledger_rows,
            prev_tick_at=prev_tick,
        )
        prev_tick = (report.get("worker") or {}).get("last_tick_at")
        print_report(report)

        bundle = report.get("first_flag_bundle")
        should_dump = bool(args.dump_now)
        if bundle and (
            bundle.get("source") == "ledger_first_buy"
            or bundle.get("order_requested_at")
            or (bundle.get("decision_trace") and bundle.get("signal_id"))
        ):
            should_dump = True

        if should_dump and bundle:
            out = dump_path_for_today()
            write_dump(build_dump(report, state, mutex), out)
            print(f"WROTE first-flag dump: {out}")
            if args.poll and bundle.get("source") == "ledger_first_buy":
                break
            if args.poll and bundle.get("order_requested_at"):
                break

        if not args.poll or args.once:
            if args.dump_now and not bundle:
                out = dump_path_for_today()
                write_dump(build_dump(report, state, mutex), out)
                print(f"WROTE snapshot dump (no first flag yet): {out}")
            break

        if time.monotonic() >= deadline:
            print("POLL TIMEOUT — first real flag not observed within timeout")
            out = dump_path_for_today()
            write_dump(build_dump(report, state, mutex), out)
            print(f"WROTE timeout snapshot: {out}")
            return 2

        time.sleep(max(1.0, float(args.interval_sec)))

    # Exit 0 even with PENDING — this is an ops checklist, not a CI gate.
    # Exit 1 only when hard FAILs exist after evaluation (operator attention).
    if int(report.get("fail_count") or 0) > 0:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
