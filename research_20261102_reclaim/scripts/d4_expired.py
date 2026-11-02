"""E 돌파대기 '만료(미진입)' 신호의 손익 — E 생략 재생(REGE_F)에서 같은 신호를 찾아 계산.

A(L85_D) 의 brk 로그: E_PENDING_ARMED(t=T+3) → E_BREAKOUT_ENTRY 또는 E_PENDING_EXPIRED.
만료 신호마다 F 재생에서 진입시각이 그 T+3 와 같은 분(±1분)인 포지션을 찾는다.
regime 은 F 재생의 entries[].regime (진입 시 각인).

  python d4_expired.py --a-out <A> --f-out <F> [--f-tag REGE_F]
"""
import argparse
import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import d1_diag as D  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")


def eps_with_pnl(js, day):
    out = []
    for ep in D.episodes(js, day):
        b = ep["buys"][0]
        e = float(b["px"])
        pnl = sum(s["qty"] * (s["px"] - e) - D.FEE * s["qty"] * (s["px"] + e) for s in ep["sells"])
        out.append(dict(t=pd.Timestamp(b["t"][:19]), sym=ep["sym"], qty=b["qty"], px=e, pnl=pnl,
                        exit=ep["sells"][-1]["t"][11:19] if ep["sells"] else "",
                        reason=";".join(sorted({a.split(":")[0] for s in ep["sells"] for a in s.get("acts", [])})),
                        regime=ep["ent"].get("regime"), sid=ep["ent"].get("sid", "")))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--a-out", required=True)
    ap.add_argument("--f-out", required=True)
    ap.add_argument("--a-tag", default="L85_D")
    ap.add_argument("--f-tag", default="REGE_F")
    a = ap.parse_args()
    rows, missing = [], []
    for f in sorted(Path(a.a_out).glob(f"{a.a_tag}_2026*.json")):
        day = f.stem.split("_")[-1]
        if day < D.REAL_FROM:
            continue
        js = json.loads(f.read_text(encoding="utf-8"))
        brk = [b for b in js["brk"] if b["t"][:10].replace("-", "") == day]
        arms = [pd.Timestamp(b["t"][:19]) for b in brk if str(b["ev"]).startswith("E_PENDING_ARMED")]
        dirs = {pd.Timestamp(b["t"][:19]): str(b["ev"]).split(":")[1] for b in brk if str(b["ev"]).startswith("E_PENDING_ARMED")}
        exp = [pd.Timestamp(b["armed_at"][:19]) if b.get("armed_at") else None for b in brk if str(b["ev"]).startswith("E_PENDING_EXPIRED")]
        entered = [pd.Timestamp(b["armed_at"][:19]) for b in brk if str(b["ev"]).startswith("E_BREAKOUT_ENTRY")]
        # 만료 신호 = 진입으로 이어지지 않은 ARMED (순서대로 매칭)
        expired = [t for t in arms if not any(abs((x - t).total_seconds()) < 1 for x in entered)]
        expired_by_log = len(exp)
        if not expired:
            continue
        ff = Path(a.f_out) / f"{a.f_tag}_{day}.json"
        if not ff.exists():
            for t in expired:
                missing.append((day, t, "F 재생 없음"))
            continue
        feps = eps_with_pnl(json.loads(ff.read_text(encoding="utf-8")), day)
        for t in expired:
            m = [e for e in feps if abs((e["t"] - t).total_seconds()) <= 90]
            if not m:
                missing.append((day, t.strftime("%H:%M"), dirs.get(t)))
                continue
            e = m[0]
            rows.append(dict(day=day, t3=t.strftime("%H:%M"), dir=dirs.get(t), sym=e["sym"], regime=e["regime"],
                             entry=e["t"].strftime("%H:%M:%S"), px=e["px"], qty=e["qty"], exit=e["exit"],
                             reason=e["reason"], pnl=e["pnl"], n_log_expired=expired_by_log))
    d = pd.DataFrame(rows)
    print(f"만료(미진입) 신호 → E 생략 재생에서 찾은 진입 {len(d)}건 / 못 찾음 {len(missing)}건")
    if len(d):
        print(f"\n  {'날짜':8s} {'T+3':5s} {'방향':9s} {'regime':6s} {'진입':8s} {'가격':>7s} {'수량':>5s} {'청산':8s} {'사유':28s} {'손익':>11s}")
        for r in d.sort_values(["day", "t3"]).itertuples():
            print(f"  {r.day} {r.t3} {str(r.dir):9s} {str(r.regime):6s} {r.entry} {r.px:7,.0f} {r.qty:5d} {r.exit} {r.reason[:28]:28s} {r.pnl:+11,.0f}")
        print("\n[합계]")
        for reg, g in d.groupby(d.regime.astype(str)):
            print(f"  {reg:6s} {len(g):2d}건  승 {int((g.pnl>0).sum())} / 패 {int((g.pnl<=0).sum())}  손익 {g.pnl.sum():+12,.0f}")
        print(f"  전체   {len(d):2d}건  승 {int((d.pnl>0).sum())} / 패 {int((d.pnl<=0).sum())}  손익 {d.pnl.sum():+12,.0f}"
              f"   (+면 E 가 놓친 이익, −면 E 가 피한 손실)")
        for m, g in d.groupby(d.day.str[:6]):
            print(f"    {m}  {len(g)}건  {g.pnl.sum():+12,.0f}")
    if missing:
        print("\n[F 재생에서 진입이 없던 만료 신호] (E 생략 시 앞선 진입으로 포지션/슬롯이 달라져 안 산 경우 등)")
        for x in missing:
            print("  ", x)
    out = Path(__file__).resolve().parents[1] / "out" / "e_expired.csv"
    d.to_csv(out, index=False, encoding="utf-8-sig")


if __name__ == "__main__":
    main()
