"""P3 SHADOW-BASE ledger 초기 seed 생성 + 검증 (2026-09-27).

내일 장 시작부터 P3 를 쓰려면 detector 가 최근 완료 BASE 거래 10건을 이미
들고 있어야 한다. 실거래로 10건을 새로 쌓을 때까지 기다리면 WARMUP 이 며칠
이어진다 -- 그래서 **확정된 80영업일 BASE replay** 에서 마지막 30건을 옮겨
심는다.

절대 원칙 두 가지:

1. **미래정보 금지.** seed 에 들어가는 거래는 전부 ``--as-of`` (기본
   2026-09-22) 까지 **완료된** 것뿐이다. 그 이후 거래는 한 건도 넣지 않는다.
2. **검증 통과 전에는 쓰지 않는다.** seed 로 계산한 최근10 H50/TP1 rate 와
   regime 이 연구 번들의 shadow 판정과 일치해야만 파일을 쓴다. 불일치면
   아무 것도 쓰지 않고 실패로 끝낸다 -- 그 경우 운영은 WARMUP(=BASE) 으로
   시작하면 된다. 억지로 채우지 않는다.

사용법::

    python scripts/macd2_p3_seed_shadow_ledger.py \
        --bundle <lab80/w1.pkl> --expect-regime CHOP \
        --expect-h50 0.50 --expect-tp1 0.00 --out data/state/macd2_shadow_base_ledger.json

``--dry-run`` 이면 검증만 하고 파일을 쓰지 않는다.
"""
from __future__ import annotations

import argparse
import json
import os
import pickle
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.trading.macd2 import chop_regime, config  # noqa: E402

#: seed 행이 갖춰야 할 필드 -- 하나라도 없으면 그 행을 버린다.
REQUIRED = ("date", "entry_time", "exit_time", "direction",
            "h50_held", "tp1_hit", "entry_price", "exit_price", "net_pct")


def _load_base_trades(bundle: Path, run_key: str) -> list[dict[str, Any]]:
    with open(bundle, "rb") as fh:
        blob = pickle.load(fh)
    runs = blob.get("runs") if isinstance(blob, dict) else None
    if not isinstance(runs, dict) or run_key not in runs:
        raise SystemExit(f"번들에 '{run_key}' 실행결과가 없습니다: {bundle}")
    return list(runs[run_key])


def _to_shadow(raw: dict[str, Any]) -> chop_regime.ShadowTrade:
    """replay 거래 한 건 -> ShadowTrade. 필드명 매핑을 여기 한 곳에 모은다."""
    return chop_regime.ShadowTrade(
        shadow_trade_id=chop_regime.make_shadow_trade_id(
            raw["entry_time"], raw["direction"], raw.get("slot_number")),
        trading_date=str(raw["date"]),
        entry_time=str(raw["entry_time"]),
        exit_time=str(raw["exit_time"]),
        direction=str(raw["direction"]),
        slot=(int(raw["slot_number"]) if raw.get("slot_number") is not None else None),
        entry_price=float(raw["entry_price"]),
        exit_price=float(raw["exit_price"]),
        net_pct=float(raw["net_pct"]),
        # 연구 필드명 -> production 필드명. detector 가 보는 단 두 개다.
        h50_intervened=bool(raw["h50_held"]),
        tp1_hit=bool(raw["tp1_hit"]),
        entry_reason="SEED_REPLAY",
        exit_reason=str(raw.get("exit_reason") or ""),
        completed=True,
        shadow_only=True,
    )


def _from_bundle(rows, as_of):
    """replay 번들 행 -> ShadowTrade. **출처/날짜 검증이 여기 한 곳에 모인다.**"""
    kept: list[chop_regime.ShadowTrade] = []
    dropped = 0
    for raw in rows:
        if any(raw.get(k) is None for k in REQUIRED):
            dropped += 1
            continue
        if str(raw["date"]) > str(as_of):
            dropped += 1  # 미래 거래 -- 절대 넣지 않는다
            continue
        exit_date = str(raw["exit_time"])[:10].replace("-", "")
        if exit_date and exit_date > str(as_of):
            dropped += 1  # 종료가 기준일을 넘어간 거래도 제외
            continue
        kept.append(_to_shadow(raw))
    return kept, dropped


def main() -> int:
    ap = argparse.ArgumentParser()
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--bundle", type=Path,
                     help="확정 80일 BASE replay 번들(pickle, runs/A_BASE)")
    src.add_argument("--from-json", type=Path,
                     help="이미 검증된 seed JSON (저장소 fixture). 번들 없이 "
                          "배포 대상에서 그대로 설치할 때 쓴다.")
    ap.add_argument("--run-key", default="A_BASE")
    ap.add_argument("--as-of", default="20260922",
                    help="이 날짜까지 완료된 거래만 seed 로 쓴다(YYYYMMDD)")
    ap.add_argument("--count", type=int, default=int(config.P3_SHADOW_LEDGER_KEEP))
    ap.add_argument("--expect-regime", required=True, choices=["CHOP", "TREND"])
    ap.add_argument("--expect-h50", required=True, type=float)
    ap.add_argument("--expect-tp1", required=True, type=float)
    ap.add_argument("--tol", type=float, default=1e-9)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.from_json is not None:
        blob = json.loads(args.from_json.read_text(encoding="utf-8"))
        rows = list(blob.get("trades") or ())
        print(f"fixture {args.from_json} · {len(rows)}행 "
              f"(source={blob.get('source')})")
        # fixture 는 이미 production 스키마다 -- 필드명 매핑이 필요 없다.
        kept = [t for t in (chop_regime.ShadowTrade.from_dict(r) for r in rows)
                if t is not None and str(t.trading_date) <= str(args.as_of)]
        dropped = len(rows) - len(kept)
    else:
        rows = _load_base_trades(args.bundle, args.run_key)
        print(f"번들 {args.bundle} · {args.run_key} {len(rows)}거래")
        kept, dropped = _from_bundle(rows, args.as_of)

    kept.sort(key=lambda t: t.exit_time)
    seed = kept[-int(args.count):]
    print(f"검증 통과 {len(kept)}거래 (제외 {dropped}) · seed {len(seed)}건")
    if len(seed) < int(config.P3_DETECTOR_WINDOW):
        print(f"FAIL: seed 가 detector window({config.P3_DETECTOR_WINDOW}) 보다 적습니다.")
        return 2
    print(f"seed 구간 {seed[0].exit_time} ~ {seed[-1].exit_time}")

    # ── 2. detector 재계산 ───────────────────────────────────────────────
    decision = chop_regime.evaluate(seed)
    print(f"\n최근{decision.window} H50_rate={decision.h50_rate:.4f} "
          f"TP1_rate={decision.tp1_rate:.4f} → {decision.regime}")

    ok = (decision.regime == args.expect_regime
          and abs(float(decision.h50_rate) - args.expect_h50) <= args.tol + 1e-6
          and abs(float(decision.tp1_rate) - args.expect_tp1) <= args.tol + 1e-6)
    print(f"기대값  H50={args.expect_h50:.4f} TP1={args.expect_tp1:.4f} "
          f"regime={args.expect_regime} → {'MATCH' if ok else 'MISMATCH'}")
    if not ok:
        print("\nFAIL: 연구 번들과 불일치합니다. seed 를 쓰지 않습니다 "
              "-- 운영은 WARMUP(=BASE) 으로 시작하십시오.")
        return 3

    # ── 3. 기록 ──────────────────────────────────────────────────────────
    if args.dry_run:
        print("\n(dry-run) 검증만 하고 파일을 쓰지 않았습니다.")
        return 0
    out = args.out or chop_regime.LEDGER_PATH
    payload = {
        "schema_version": chop_regime.SCHEMA_VERSION,
        "updated_at": datetime.now(config.KST).isoformat(),
        "source": (f"SEED:{args.bundle.name}:{args.run_key}:asof{args.as_of}"
                   if args.bundle is not None
                   else f"SEED:{args.from_json.name}:asof{args.as_of}"),
        "trades": [t.to_dict() for t in seed],
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, out)
    print(f"\nOK: seed {len(seed)}건 기록 → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
