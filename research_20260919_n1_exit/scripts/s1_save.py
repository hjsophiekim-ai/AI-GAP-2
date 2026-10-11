"""S1 — 지금까지의 모든 후보를 한 표로 합쳐 저장한다."""
import pickle, sys, csv
sys.stdout.reconfigure(encoding="utf-8")
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import axval as V
from common import compound, excl_topn, pnl
import hengine as H

FILES = ["ax78.pkl", "ax78_d.pkl", "ax78_grid2.pkl", "ax78_e35.pkl", "pp78.pkl", "pp78b.pkl"]
SRC = {"ax78.pkl": "A/B/C 계열(sticky 판정·peak 래칫)",
       "ax78_d.pkl": "D 계열(구조붕괴 청산)+off_tp2 기준선",
       "ax78_grid2.pkl": "D 전수격자 + E 계열(레벨 확정)",
       "ax78_e35.pkl": "off_tp2 곡선 + E3.5 위약",
       "pp78.pkl": "peak protection 20후보",
       "pp78b.pkl": "peak protection regime_off 후속"}
TR, ORIG, dates = {}, {}, None
for f in FILES:
    p = HERE / f
    if not p.exists():
        continue
    d = pickle.load(open(p, "rb")); dates = d["dates"]
    for k, v in d["trades"].items():
        if k not in TR:
            TR[k] = v; ORIG[k] = SRC[f]
base = TR["N1"]; W30 = dates[-30:]
print(f"후보 {len(TR)}개, 창 {len(dates)}일 {dates[0]}~{dates[-1]}")

FIELD = ["name", "source", "n", "c78", "d78", "c30", "d30", "pf78", "dpf78", "pf30",
         "mdd78", "dmdd78", "mdd30", "t1_78", "t3_78", "t10_78", "dt10_78", "dt10_30",
         "half1_d", "half2_d", "k5_d", "wf6_d", "wf6_win", "wf6_lose",
         "chg", "up", "dn", "sum_d", "sum_d_ex1", "sum_d_ex3",
         "only_a", "only_b", "runner8_dmg", "win_rate", "day_win_rate"]
key = lambda t: (t["date"], t["entry_time"]); B = {key(t): t for t in base}
RUN8 = sorted(k for k, t in B.items() if t["peak_net_pct"] >= 8.0)
rows = []
for k, ts in TR.items():
    r = V.report(k, ts, base, dates)
    m = H.metrics(ts, dates); m30 = H.metrics([t for t in ts if t["date"] in set(W30)], W30)
    ka = {key(t): t for t in ts}
    dmg = sum(ka[x]["net_pct"] - B[x]["net_pct"] for x in RUN8 if x in ka)
    rows.append({
        "name": k, "source": ORIG[k], "n": m["trades"],
        "c78": round(r["78d_a"], 4), "d78": round(r["78d_d"], 4),
        "c30": round(r["30d_a"], 4), "d30": round(r["30d_d"], 4),
        "pf78": r["78d_pf_a"], "dpf78": round((r["78d_pf_a"] or 0)-(r["78d_pf_b"] or 0), 4),
        "pf30": r["30d_pf_a"],
        "mdd78": r["78d_mdd_a"], "dmdd78": round(r["78d_mdd_a"]-r["78d_mdd_b"], 4),
        "mdd30": r["30d_mdd_a"],
        "t1_78": round(excl_topn(ts, dates, 1), 4), "t3_78": round(excl_topn(ts, dates, 3), 4),
        "t10_78": round(excl_topn(ts, dates, 10), 4),
        "dt10_78": round(r["top10_d"], 4), "dt10_30": round(r["top10_d30"], 4),
        "half1_d": round(r["half"][0], 4), "half2_d": round(r["half"][1], 4),
        "k5_d": " ".join(f"{x:+.2f}" for x in r["k5"]),
        "wf6_d": " ".join(f"{x:+.2f}" for x in r["wf6"]),
        "wf6_win": r["wf6_win"], "wf6_lose": r["wf6_lose"],
        "chg": r["chg"], "up": r["up"], "dn": r["dn"],
        "sum_d": round(r["sum_d"], 4), "sum_d_ex1": round(r["sum_d_ex1"], 4),
        "sum_d_ex3": round(r["sum_d_ex3"], 4),
        "only_a": r["only_a"], "only_b": r["only_b"], "runner8_dmg": round(dmg, 4),
        "win_rate": m["win_rate_pct"], "day_win_rate": m["day_win_rate_pct"]})
rows.sort(key=lambda x: -(x["d78"] + 3 * x["d30"]))
out = HERE / "RESULTS" / "all_candidates_78d.csv"
with open(out, "w", newline="", encoding="utf-8-sig") as fh:
    w = csv.DictWriter(fh, FIELD); w.writeheader(); w.writerows(rows)
print("저장:", out.name, len(rows), "행")

# 거래 원본 CSV — 기준 + 주요 후보
TRF = ["date", "session", "slot_number", "direction", "entry_time", "entry_price",
       "exit_time", "exit_price", "exit_reason", "net_pct", "w1a", "peak_net_pct",
       "mae_net_pct", "tp1_hit", "entry_chop", "chop_score", "tq", "h50_held",
       "trend_at_entry", "hold_minutes", "pp_armed", "pp_fired", "ax_mode"]
for k in ("N1", "N1_Safe", "H50", "X2lite_W1", "E_weaklock3.5", "D_C3_a3.5_WEAK",
          "PDb+gap_neg", "PD_a3.5_g1.0+regime_off"):
    if k not in TR:
        continue
    fn = HERE / "RESULTS" / f"trades_{k.replace('+','_').replace('.','')}.csv"
    with open(fn, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh); w.writerow(TRF + ["pnl_w1a"])
        for t in sorted(TR[k], key=lambda x: (x["date"], x["entry_time"] or "")):
            w.writerow([t.get(f) for f in TRF] + [round(pnl(t), 4)])
print("거래 CSV 저장 완료")
pickle.dump({"trades": TR, "dates": dates, "source": ORIG},
            open(HERE / "RESULTS" / "ALL_TRADES.pkl", "wb"))
