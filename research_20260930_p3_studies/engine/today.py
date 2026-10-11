"""today — 2026-09-29 를 '정상 P3(Q2+H30)' 로 재생한 결과 (READ-ONLY 연구엔진).

- R0 = 랩 변형 H30 (production P3), BASE = N1+C1+SMART+AR1 (P3 없음; 섀도우 regime 입력)
- 83일 창(0527~0929). 0929 분봉은 수신 시각까지(장중)만 있다.
"""
import pickle
import sys

import pandas as pd

sys.stdout.reconfigure(encoding="utf-8"); sys.path.insert(0, "proj"); sys.path.insert(0, "proj/scripts")
DAY = "20260929"
L = lambda f: pickle.load(open(f, "rb"))["trades"]
R0, BASE = L("out_H30_d83.pkl"), L("out_BASE_d83.pkl")
P = lambda *a: print(*a, flush=True)
hm = lambda x: pd.Timestamp(x).strftime("%H:%M:%S") if x else "-"
sig = lambda ts: sorted((t["date"], str(t["entry_time"]), t["direction"], str(t["exit_time"]),
                         round(float(t["net_pct"]), 8)) for t in ts)

prev = L("prev/out_H30_d82_y3study.pkl")
same = sig([t for t in R0 if t["date"] != DAY]) == sig(prev)
P(f"일관성: 83일 R0 에서 0929 를 뺀 거래 = 82일 R0 ? {'IDENTICAL' if same else 'DIFF'} "
  f"({len([t for t in R0 if t['date'] != DAY])} vs {len(prev)})")

last_bar = pd.read_csv("cache83/replay_20260929_hynix_1m.csv")["datetime"].iloc[-1]
P(f"0929 분봉 마지막: {last_bar}")


def dump(tag, ts):
    today = sorted([t for t in ts if t["date"] == DAY], key=lambda t: t["entry_time"])
    P(f"\n## {tag}: 0929 거래 {len(today)}건")
    P("| # | 진입 | 방향 | 종목 | 진입가 | 진입 regime | 진입봉 CHOP | W1a | B3 +1.0 도달 | Q2 rescue | Y3 | H30 | 부분청산 | 청산 | 청산가 | 사유 | MFE | net | H50 개입 |")
    P("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for i, t in enumerate(today, 1):
        legs = [l for l in t.get("legs", []) if "PARTIAL" in str(l[3])]
        leg_s = "; ".join(f"{str(l[0])[11:19]} {l[3]} {float(l[2])*100:.0f}% @{float(l[1]):,.0f} ({float(l[4]):+.3f})" for l in legs) or "-"
        P(f"| {i} | {hm(t['entry_time'])} | {t['direction']} | {t.get('entry_symbol', '-')} | {float(t.get('entry_price') or 0):,.0f} | "
          f"{t.get('entry_regime') or '-'} | {'Y' if t.get('entry_chop') else 'N'} | x{float(t.get('w1a') or 1):.2f} | {hm(t.get('b3_first_trig_at'))} | "
          f"{hm(t.get('b3_rescue_at')) if t.get('b3_rescued') else '-'} | {hm(t.get('b3_y3_at')) if t.get('b3_y3') else '-'} | "
          f"{hm(t.get('b3_ext_at')) if t.get('b3_ext') else '-'} | {leg_s} | {hm(t['exit_time'])} | "
          f"{float(t.get('exit_price') or 0):,.0f} | {t['exit_reason']} | {t['peak_net_pct']:+.3f} | {t['net_pct']:+.3f} | "
          f"{'Y' if t.get('h50_held') else 'N'} |")
    tot = 1.0
    for t in today:
        tot *= 1 + t["net_pct"] * t["w1a"] / 100
    P(f"- 0929 가중 합성: {(tot - 1) * 100:+.3f}%")


dump("R0 = 정상 P3 (Q2+H30)", R0)
dump("참고: BASE (P3 없음 = N1+C1)", BASE)
