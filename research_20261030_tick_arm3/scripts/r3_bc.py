"""A 재생이 끝난 뒤, **보호선이 ARM 되는 날만** B/C 를 재생한다.

보호선은 틱 최고 순수익이 +3.0% 에 닿아야 ARM 된다. 닿는 거래가 하나도 없는 날은
B/C 가 A 와 논리적으로 동일하므로 재생하지 않는다(결론 무손실, 계산량만 줄인다).
후보일 판정은 `a3_peaks.py` 와 같은 코드(1초 스냅샷 직접 측정)다.

    python .../r3_bc.py --strat CHOPX
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8")

DAYS = ["20261016", "20261019", "20261020", "20261021", "20261022", "20261023",
        "20261026", "20261027", "20261028", "20261029", "20261030"]
STRATS = {
    "N1":    ["--env", "WK_MODE=N1", "--env", "WK_MUA=0", "--env", "WK_XS=0"],
    "CHOPX": ["--env", "WK_MODE=CHOP", "--env", "WK_MUA=1", "--env", "WK_XS=1"],
}


def candidates(tag, days, arm=3.0):
    import a3_peaks as P
    from a2_metrics import episodes
    import pandas as pd
    from app.trading.macd2 import worker as _w
    hit = []
    for d in days:
        p = P.OUT / f"{tag}_{d}.json"
        if not p.exists():
            print(f"  !! {d} A 산출 없음 — 후보에 포함(보수적)")
            hit.append(d)
            continue
        day = f"{d[:4]}-{d[4:6]}-{d[6:]}"
        best = None
        for e in episodes(p, d):
            if not e["entry_t"] or not e["exit_t"]:
                continue
            df = P.ticks(e["sym"], day)
            if df is None:
                continue
            w = df[(df.index >= pd.Timestamp(e["entry_t"]).tz_localize(None))
                   & (df.index <= pd.Timestamp(e["exit_t"]).tz_localize(None))]
            if not len(w):
                continue
            pk = _w._net_return_pct(e["sym"], e["entry_px"], float(w["price"].max()), e["qty"])
            best = pk if best is None else max(best, pk)
        print(f"  {d}  틱최고 {best if best is not None else float('nan'):+.2f}%"
              f"{'  ◀ B/C 재생' if (best is not None and best >= arm) else '   (B/C ≡ A)'}")
        if best is not None and best >= arm:
            hit.append(d)
    return hit


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strat", default="CHOPX")
    ap.add_argument("-j", type=int, default=3)
    a = ap.parse_args()
    print(f"[{a.strat}] A 거래의 틱 최고 순수익 스캔")
    hit = candidates(f"{a.strat}_A", DAYS)
    print(f"\n-> B/C 재생 대상 {len(hit)}일: {' '.join(hit)}")
    print(f"-> 나머지 {len(DAYS) - len(hit)}일은 B/C ≡ A (재생 생략)\n")
    for v, arm in (("B", "SOFT"), ("C", "MEDIUM")):
        if not hit:
            break
        cmd = [sys.executable, str(ROOT / "research_20261030_tick_arm3" / "scripts" / "r1_run.py"),
               "--harness", "ARM3", "--var", "ARM3", "--tag", f"{a.strat}_{v}",
               *STRATS[a.strat], "--env", f"WK_ARM={arm}", "--env", "WK_TICKPX=1",
               "-j", str(a.j), *hit]
        print(f"######## {a.strat}_{v} ({len(hit)}일) ########", flush=True)
        subprocess.run(cmd, cwd=str(ROOT))
    Path(ROOT / "research_20261030_tick_arm3" / "out" / f"bc_days_{a.strat}.txt").write_text(
        " ".join(hit), encoding="utf-8")
    print(f"\n생략한 날은 집계에서 A 산출을 그대로 쓴다 (scripts/a4_fill.py)")


if __name__ == "__main__":
    main()
