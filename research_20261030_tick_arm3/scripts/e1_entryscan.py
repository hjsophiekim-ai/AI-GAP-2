"""종합 진단 — CHOP+MU+XS 전 거래의 **진입 시점 production 지표** 전수 (재생 불필요).

목적: 수익과 안전성을 **함께** 올릴 지렛대를 찾는다. 청산 쪽은 7차례 독립 시도가 전부
≤0 이었으므로(REPORT 3~6장), 남은 후보는 '진입 품질에 따른 차등'이다. 그게 가능하려면
진입 시점에 이미 구할 수 있는 production 지표가 승자/패자를 가려야 한다.

    python .../e1_entryscan.py --tag CHOPX_A

출력: 거래별 진입지표 + 결과, 그리고 지표별 승/패 분리도.
"""
import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from a2_metrics import episodes  # noqa: E402
import a3_peaks as P  # noqa: E402
from c1_vwapcand import _mean_cttr_full, hy_frame, vwap_dev  # noqa: E402
from app.trading.macd2 import config, exec_strength as XS, mu_assist as MU  # noqa: E402
from app.trading.macd2 import signal_engine as SE, worker as W  # noqa: E402
from app.trading.macd2.models import Direction  # noqa: E402

SP = Path(os.environ.get("REPLAY_SP", r"C:/Users/KIMHYU~1/AppData/Local/Temp/claude/b3t_sp"))
RT = Path(os.environ.get("ARM3_RT", r"C:/Users/KIMHYU~1/AppData/Local/Temp/claude/arm3_sp/rt"))
KST = config.KST
DAYS = ["20261016", "20261019", "20261020", "20261021", "20261022", "20261023",
        "20261026", "20261027", "20261028", "20261029", "20261030"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="CHOPX_A")
    ap.add_argument("--days", nargs="+", default=DAYS)
    a = ap.parse_args()
    comp = sorted({f.stem.split("_")[1] for f in (SP / "data").glob("replay_*_hynix_1m.csv")})

    rows = []
    for d in a.days:
        p = P.OUT / f"{a.tag}_{d}.json"
        if not p.exists():
            continue
        j = json.loads(p.read_text(encoding="utf-8"))
        ent = {e["t"][11:19]: e for e in j.get("entries", [])}
        # 승인된 T+3 판정 (진입 직전) — sid 로 잇는다
        dec = {}
        for g in j.get("gates", []):
            if g["ev"] == "DECISION" and g.get("approved"):
                dec[str(g.get("sid") or "")] = g
        i = comp.index(d)
        hy = hy_frame([comp[i - 2], comp[i - 1], d])
        day = f"{d[:4]}-{d[4:6]}-{d[6:]}"
        for e in episodes(p, d):
            t_in = datetime.fromisoformat(e["entry_t"])
            snap = ent.get(e["entry_t"][11:19], {})
            sid = str(snap.get("sid") or "")
            gd = dec.get(sid) or dec.get(sid.split(":E_")[0]) or {}
            m = gd.get("metrics") or {}
            dirn = Direction.UP_RED if e["sym"] == config.LONG_SYMBOL else Direction.DOWN_BLUE
            df1 = hy[hy["datetime"] + pd.Timedelta(minutes=1) <= pd.Timestamp(t_in)].reset_index(drop=True)
            bars = SE.resample_completed_3m(df1, now=t_in)
            dev = vwap_dev(bars, dirn)
            cttr = _mean_cttr_full(t_in)
            tk = P.ticks(e["sym"], day)
            pk = float("nan")
            if tk is not None and e["exit_t"]:
                w = tk[(tk.index >= pd.Timestamp(e["entry_t"]).tz_localize(None))
                       & (tk.index <= pd.Timestamp(e["exit_t"]).tz_localize(None))]
                if len(w):
                    pk = W._net_return_pct(e["sym"], e["entry_px"], float(w["price"].max()), e["qty"])
            gap_f = m.get("gap_flag")
            gap_n = m.get("gap_now")
            rows.append(dict(
                day=d, t=e["entry_t"][11:19], sym=e["sym"], qty=e["qty"], px=e["entry_px"],
                notional=e["entry_px"] * e["qty"],
                pnl=e["pnl"], ret=e["ret_pct"], peak=pk,
                trend_ok=snap.get("trend_ok"), snap_reason=snap.get("snap_reason"),
                regime=snap.get("regime"), ebrk=":E_BRK" in sid,
                qual=m.get("quality_score", m.get("quality_passed")),
                slot=m.get("slot_number"), sess=m.get("session"),
                win=m.get("window"), dev=dev, cttr=cttr,
                cttr_ok=(XS.state_for(dirn, cttr) == XS.MATCH) if cttr is not None else None,
                gap_f=gap_f, gap_n=gap_n,
                gap_exp=(None if (gap_f is None or gap_n is None) else bool(gap_n > gap_f)),
                exits=";".join(e["exits"]),
            ))

    print(f"\n{'='*165}\n[{a.tag}] 전 거래 진입지표 × 결과\n{'='*165}")
    hdr = (f"{'날짜':8s}{'진입':9s}{'종목':8s}{'수량':>6s}{'익스포저':>11s}{'손익':>11s}{'수익%':>7s}{'틱최고':>7s}"
           f"{'추세':6s}{'스냅':11s}{'regime':8s}{'E':3s}{'품질':>4s}{'슬롯':>4s}{'세션':10s}"
           f"{'VWAP%':>8s}{'cttr':>7s}{'일치':5s}{'gap확대':7s}  청산")
    print(hdr)
    for r in sorted(rows, key=lambda x: -x["pnl"]):
        print(f"{r['day'][4:6]+'/'+r['day'][6:]:8s}{r['t']:9s}{r['sym']:8s}{r['qty']:>6,}"
              f"{r['notional']:>11,.0f}{r['pnl']:>+11,.0f}{r['ret']:>7.2f}{r['peak']:>7.2f}"
              f"{str(r['trend_ok'])[:5]:6s}{str(r['snap_reason'])[:11]:11s}{str(r['regime'])[:8]:8s}"
              f"{('E' if r['ebrk'] else '.'):3s}{str(r['qual'] if r['qual'] is not None else '-'):>4s}"
              f"{str(r['slot'] if r['slot'] is not None else '-'):>4s}{str(r['sess'] or '-')[:10]:10s}"
              f"{(r['dev'] if r['dev'] is not None else float('nan')):>8.3f}"
              f"{(r['cttr'] if r['cttr'] is not None else float('nan')):>7.1f}"
              f"{('O' if r['cttr_ok'] else '.'):5s}{('O' if r['gap_exp'] else '.'):7s}  {r['exits'][:40]}")

    wins = [r for r in rows if r["pnl"] > 0]
    loss = [r for r in rows if r["pnl"] <= 0]
    print(f"\n{'='*165}\n[지표별 승/패 분리도]  승 {len(wins)} / 패 {len(loss)}\n{'='*165}")

    def split(name, fn):
        wv = [fn(r) for r in wins if fn(r) is not None]
        lv = [fn(r) for r in loss if fn(r) is not None]
        if not wv or not lv:
            print(f"  {name:22s} (값 부족)")
            return
        if isinstance(wv[0], bool):
            print(f"  {name:22s} 승 True {sum(wv)}/{len(wv)}  ·  패 True {sum(lv)}/{len(lv)}")
        else:
            import statistics as st
            print(f"  {name:22s} 승 중앙 {st.median(wv):+8.3f} [{min(wv):+.2f},{max(wv):+.2f}]  ·  "
                  f"패 중앙 {st.median(lv):+8.3f} [{min(lv):+.2f},{max(lv):+.2f}]")
    split("trend_ok", lambda r: r["trend_ok"])
    split("E 돌파대기 진입", lambda r: r["ebrk"])
    split("체결강도 방향일치", lambda r: r["cttr_ok"])
    split("gap 확대", lambda r: r["gap_exp"])
    split("VWAP 편차%", lambda r: r["dev"])
    split("cttr", lambda r: r["cttr"])
    split("익스포저(원)", lambda r: r["notional"])
    split("품질점수", lambda r: float(r["qual"]) if str(r["qual"]).replace(".", "").isdigit() else None)

    print(f"\n[손실 거래 전수]")
    for r in sorted(loss, key=lambda x: x["pnl"]):
        print(f"  {r['day'][4:6]}/{r['day'][6:]} {r['t']} {r['sym']} {r['pnl']:+,.0f} ({r['ret']:+.2f}%) "
              f"틱최고 {r['peak']:+.2f}% · trend_ok={r['trend_ok']} {r['snap_reason']} "
              f"regime={r['regime']} E={'O' if r['ebrk'] else '.'} 품질={r['qual']} "
              f"VWAP={r['dev']:+.3f} cttr={r['cttr']:.1f} · {r['exits'][:40]}")
    tot = sum(r["pnl"] for r in rows)
    print(f"\n총 {len(rows)}거래 {tot:+,.0f}원 · 손실합 {sum(r['pnl'] for r in loss):+,.0f} "
          f"· 최대손실 {min(r['pnl'] for r in rows):+,.0f}")


if __name__ == "__main__":
    main()
