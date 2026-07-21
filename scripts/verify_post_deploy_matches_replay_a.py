"""Post-deploy ledger check: only trades after deploy time count toward MATCHES_REPLAY_A."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

import pandas as pd

from app.services.hynix_execution_ledger import LEDGER_COLUMNS, load_ledger
from app.utils.runtime_info import read_runtime_info
from app.utils.time_utils import KST

WEIGHTED_SOURCES = {"WEIGHTED_ORDER_CONTROLLER", "WEIGHTED_RANGE_ENTRY"}
FORBIDDEN_BUY_PREFIXES = ("ENHANCED_", "ACTIVE_", "EARLY_")
REQUIRED_BUY_FIELDS = (
    "signal_source",
    "actual_entry_engine",
    "entry_path",
    "weighted_evidence",
    "expected_net_edge",
    "reward_risk",
    "direction_episode_id",
    "decision_snapshot_id",
    "deployed_git_sha",
)


def _parse_ts(value) -> datetime | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    ts = pd.to_datetime(value, errors="coerce")
    if pd.isna(ts):
        return None
    if getattr(ts, "tzinfo", None) is None:
        return ts.to_pydatetime().replace(tzinfo=KST)
    return ts.to_pydatetime()


def judge_post_deploy(
    *,
    deploy_at: datetime,
    deploy_sha: str | None = None,
    ledger_df: pd.DataFrame | None = None,
) -> dict:
    df = ledger_df if ledger_df is not None else load_ledger()
    if df is None or df.empty:
        return {
            "verdict": "MATCHES_REPLAY_A",
            "reason": "no ledger rows after deploy (nothing to violate)",
            "deploy_at": deploy_at.isoformat(),
            "deploy_sha": deploy_sha,
            "post_deploy_buys": 0,
            "violations": [],
        }

    rows = []
    for _, row in df.iterrows():
        ts = _parse_ts(row.get("timestamp"))
        if ts is None or ts < deploy_at:
            continue
        rows.append(row)

    violations: list[dict] = []
    buys = [r for r in rows if str(r.get("action")).upper() == "BUY" and bool(r.get("success"))]
    for row in buys:
        source = str(row.get("signal_source") or "")
        if source not in WEIGHTED_SOURCES:
            violations.append({
                "trade_id": row.get("trade_id"),
                "issue": "NON_WEIGHTED_BUY",
                "signal_source": source,
            })
            continue
        if any(source.startswith(p) for p in FORBIDDEN_BUY_PREFIXES):
            violations.append({
                "trade_id": row.get("trade_id"),
                "issue": "FORBIDDEN_SOURCE_PREFIX",
                "signal_source": source,
            })
        engine = str(row.get("actual_entry_engine") or "")
        if engine and engine != "WEIGHTED_ORDER_CONTROLLER_LIVE":
            violations.append({
                "trade_id": row.get("trade_id"),
                "issue": "WRONG_ACTUAL_ENTRY_ENGINE",
                "actual_entry_engine": engine,
            })
        if deploy_sha and str(row.get("deployed_git_sha") or "") not in ("", deploy_sha):
            # Allow empty only if column missing on partial migration; prefer exact match.
            if str(row.get("deployed_git_sha") or ""):
                violations.append({
                    "trade_id": row.get("trade_id"),
                    "issue": "DEPLOYED_SHA_MISMATCH",
                    "deployed_git_sha": row.get("deployed_git_sha"),
                    "expected": deploy_sha,
                })
        for field in REQUIRED_BUY_FIELDS:
            val = row.get(field)
            if val is None or (isinstance(val, float) and pd.isna(val)) or str(val).strip() == "":
                violations.append({
                    "trade_id": row.get("trade_id"),
                    "issue": "MISSING_REQUIRED_FIELD",
                    "field": field,
                })

    verdict = "MATCHES_REPLAY_A" if not violations else "DOES_NOT_MATCH"
    return {
        "verdict": verdict,
        "deploy_at": deploy_at.isoformat(),
        "deploy_sha": deploy_sha,
        "post_deploy_rows": len(rows),
        "post_deploy_buys": len(buys),
        "weighted_buys": sum(1 for r in buys if str(r.get("signal_source") or "") in WEIGHTED_SOURCES),
        "violations": violations,
        "ledger_columns_present": [c for c in REQUIRED_BUY_FIELDS if c in (df.columns if df is not None else LEDGER_COLUMNS)],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deploy-at", help="ISO timestamp (KST) of Render deploy for this SHA")
    parser.add_argument("--deploy-sha", help="Expected git SHA (defaults to runtime_info.git_sha)")
    parser.add_argument("--out", default="data/state/post_deploy_replay_a_verdict.json")
    args = parser.parse_args()

    runtime = read_runtime_info() or {}
    deploy_sha = args.deploy_sha or runtime.get("git_sha")
    if args.deploy_at:
        deploy_at = _parse_ts(args.deploy_at)
    else:
        deploy_at = _parse_ts(runtime.get("deployed_at") or runtime.get("render_deployed_at"))
    if deploy_at is None:
        # Fail closed for accidental full-ledger scoring.
        print("DOES_NOT_MATCH")
        print("missing --deploy-at / runtime deploy timestamp; refusing to score pre-deploy history")
        return 2

    result = judge_post_deploy(deploy_at=deploy_at, deploy_sha=deploy_sha)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(result["verdict"])
    print(json.dumps({k: result[k] for k in ("post_deploy_buys", "weighted_buys", "violations")}, ensure_ascii=False, indent=2))
    return 0 if result["verdict"] == "MATCHES_REPLAY_A" else 1


if __name__ == "__main__":
    raise SystemExit(main())
