"""추세약화 확인형 보호(W1/W2) — 발동 거래 전수 + 약화신호별 유효성.

    python .../a6_weak.py --strat CHOPX --variants A W1 W2
"""
import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8")
from a2_metrics import episodes, key  # noqa: E402

OUT = Path(os.environ.get("ARM3_OUT", r"C:/Users/KIMHYU~1/AppData/Local/Temp/claude/arm3_sp/out"))
DAYS = ["20261016", "20261019", "20261020", "20261021", "20261022", "20261023",
        "20261026", "20261027", "20261028", "20261029", "20261030"]
SIGNAME = {
    "s1_gap_shrink": "① MACD gap 축소",
    "s2_mu_weak": "② MU 약화",
    "s3_cttr_weak": "③ 체결강도 약화",
    "s4_giveback": "④ 고점 0.5%p 반납",
    "s5_vwap_lost": "⑤ VWAP 우위 상실",
    "s6_opp_pending": "⑥ 반대플래그 미확정",
}


def arm_events(tag, day):
    p = OUT / f"{tag}_{day}.json"
    if not p.exists():
        return []
    d = json.loads(p.read_text(encoding="utf-8"))
    return [e for e in d.get("arm3", []) if e["t"][:10].replace("-", "") == day]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strat", default="CHOPX")
    ap.add_argument("--variants", nargs="+", default=["A", "W1", "W2"])
    ap.add_argument("--days", nargs="+", default=DAYS)
    a = ap.parse_args()
    base = a.variants[0]

    eps = {}
    for v in a.variants:
        eps[v] = {}
        for d in a.days:
            p = OUT / f"{a.strat}_{v}_{d}.json"
            if p.exists():
                for e in episodes(p, d):
                    eps[v][key(e)] = e

    print(f"\n{'='*124}\n[{a.strat}] 보호가 실제 발동/후보가 된 거래 전수\n{'='*124}")
    hdr = f"{'날짜':8s}{'진입':9s}{'종목':9s}"
    for v in a.variants:
        hdr += f"{v+'손익':>13s}"
    print(hdr + "   변형별 ARM/활성/청산")
    rows = []
    fire_sigs, cand_sigs = Counter(), Counter()
    for d in a.days:
        evs = {v: arm_events(f"{a.strat}_{v}", d) for v in a.variants}
        if not any(evs.values()):
            continue
        ks = sorted({k for v in a.variants for k in eps[v] if k[0] == d})
        for k in ks:
            e0 = eps[base].get(k)
            if e0 is None:
                continue
            hit = False
            cells, notes = "", []
            for v in a.variants:
                e = eps[v].get(k)
                cells += (f"{e['pnl']:>+13,.0f}" if e else f"{'-':>13s}")
                ep_evs = [x for x in evs[v] if x["sym"] == k[2]]
                cand = next((x for x in ep_evs if x["k"] == "CAND"), None)
                act = next((x for x in ep_evs if x["k"] == "ACTIVE"), None)
                br = next((x for x in ep_evs if x["k"] == "BREACH"), None)
                arm0 = next((x for x in ep_evs if x["k"] == "ARM"), None)
                if cand or act or br:
                    hit = True
                    s = f"{v}:"
                    if cand:
                        s += f" 후보{cand['t'][11:19]}"
                    if act:
                        s += f" 활성{act['t'][11:19]}[{','.join(SIGNAME.get(x, x)[0] for x in act['sigs'])}]"
                        fire_sigs.update(act["sigs"])
                        for kk, vv in (act.get("all") or {}).items():
                            if vv:
                                cand_sigs[kk] += 1
                    elif arm0 and not act:
                        s += " 즉시활성"
                    if br:
                        s += f" 이탈{br['t'][11:19]}(선{br['line']})"
                    elif cand:
                        s += " 미이탈"
                    notes.append(s)
            if hit:
                print(f"{d[4:6]}/{d[6:]:6s}{k[1][11:19]:9s}{k[2]:9s}{cells}   " + " | ".join(notes))
                rows.append((d, k))
    print(f"\n{'='*124}\n[약화신호별 발동 횟수] (보호를 켠 시점에 참이었던 신호)\n{'='*124}")
    if fire_sigs:
        for s, n in fire_sigs.most_common():
            print(f"  {SIGNAME.get(s, s):22s} {n}회")
    else:
        print("  (활성화된 거래 없음)")


if __name__ == "__main__":
    main()
