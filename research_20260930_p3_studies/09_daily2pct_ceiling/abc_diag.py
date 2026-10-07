"""9월 '매일 최소 +2%' A/B/C 진단. READ-ONLY.
FILTER CEILING   = R0 거래 중 시간 비중복 수익 거래 최대 3건 합 (수량 1.0)
FULL-FLAG CEIL.  = R0 ∪ ALLFLAGS(한도3) ∪ ALLFLAGS_NOCAP(한도 해제) 후보 중 시간 비중복 수익 거래 최대 3건 합
A = FULL>=2 ∧ R0<2 / B = FILTER<2 ∧ FULL>=2 (A 의 부분집합) / C = FULL<2
"""
import itertools, pickle, sys
from collections import Counter
from pathlib import Path
import pandas as pd
sys.stdout.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "proj")); sys.path.insert(0, str(HERE / "proj" / "scripts"))
from app.trading.macd2 import config, time_window_filter as twf, time_window_3slot as tw3, teg_gate as TG
from app.trading.macd2.models import Direction
R0 = pickle.load(open(HERE / "out_H30_d83.pkl", "rb"))["trades"]
P1 = pickle.load(open(HERE / "out_ALLFLAGS_d83.pkl", "rb"))["trades"]
P2 = pickle.load(open(HERE / "out_ALLFLAGS_NOCAP_d83.pkl", "rb"))["trades"]
ctx = pickle.load(open(HERE / "_ctx83.pkl", "rb")); BARS = ctx["hynix_bars_3m"].reset_index(drop=True)
SEP = [d for d in ctx["dates"] if d.startswith("202609")]
TS = lambda x: pd.Timestamp(x)
hm = lambda x: TS(x).strftime("%H:%M")
key = lambda t: (t["date"], TS(t["entry_time"]).strftime("%H%M"), t["direction"])
p = lambda *a: print(*a, flush=True)


def best3(ts):
    c = [t for t in ts if float(t["net_pct"]) > 0]
    best, pick = 0.0, ()
    for k in (1, 2, 3):
        for comb in itertools.combinations(c, k):
            s = sorted(comb, key=lambda t: TS(t["entry_time"]))
            if all(TS(s[i + 1]["entry_time"]) >= TS(s[i]["exit_time"]) for i in range(len(s) - 1)):
                v = sum(float(t["net_pct"]) for t in s)
                if v > best: best, pick = v, tuple(s)
    return best, pick


def why_not(t):
    """R0 가 이 후보 거래를 왜 안 했나 (그 시점 R0 상태 기준)."""
    d = t["date"]; at = TS(t["entry_time"]); dirn = Direction(t["direction"])
    r0d = sorted((x for x in R0 if x["date"] == d), key=lambda x: TS(x["entry_time"]))
    held = [x for x in r0d if TS(x["entry_time"]) < at < TS(x["exit_time"])]
    if held and held[0]["direction"] == t["direction"]:
        return "R0 같은방향 보유중"
    before = [x for x in r0d if TS(x["entry_time"]) < at]
    idx = int(t["decision_idx"]); sl = BARS.iloc[: idx + 1]; fdt = TS(BARS["datetime"].iloc[idx - 1]).to_pydatetime()
    rec = at.to_pydatetime(); pos = Direction(held[0]["direction"]) if held else None
    b = twf.evaluate_time_window_entry(sl, dirn, fdt, rec, position_direction=pos,
                                       morning_entry_count=0, afternoon_entry_count=0, daily_entry_count=0)
    bypass = (b.block_reason == config.TW_REJECT_TIME_WINDOW and (b.metrics or {}).get("window") in (twf.WINDOW_AFTERNOON_1, twf.WINDOW_AFTERNOON_2)
              and rec.astimezone(config.KST).time() < config.TW_AFTERNOON_ENTRY_HARD_CUTOFF)
    if not (b.approved or bypass):
        return f"TW:{b.block_reason}"
    v, vr = twf.evaluate_tw2_extra_vetoes(sl, dirn, fdt, rec)
    if v:
        return f"VETO:{vr}"
    mc = sum(1 for x in before if x.get("session") == tw3.SESSION_MORNING)
    ac = len(before) - mc
    lad = next((x["direction"] for x in reversed(before) if x.get("session") == tw3.SESSION_AFTERNOON), None)
    s = tw3.resolve_slot(now=rec, slots_used_today=len(before), morning_count=mc, afternoon_count=ac,
                         direction=dirn, is_flat=not held, last_afternoon_direction=lad)
    if not s.slot_allowed:
        return f"SLOT:{s.reject_reason}"
    if s.requires_quality_gate:
        q = tw3.evaluate_trend_quality(sl, dirn)
        if not q.approved:
            return "QUALITY"
    if s.requires_teg_gate:
        g = TG.evaluate_teg(sl, dirn, fdt, rec)
        if not g.approved:
            return "TEG:" + ",".join(c for c in TG.ALL_CONDITIONS if not g.conditions.get(c, False))
    return "통과(경로차이: R0 는 다른 포지션/전환 흐름)"


rows = []
for d in SEP:
    r0d = [t for t in R0 if t["date"] == d]
    r0v = sum(float(t["net_pct"]) * float(t["w1a"]) for t in r0d)
    fv, fp = best3(r0d)
    pool = {}
    for t in r0d + [t for t in P1 if t["date"] == d] + [t for t in P2 if t["date"] == d]:
        k = key(t)
        if k not in pool or float(t["net_pct"]) > float(pool[k]["net_pct"]):
            pool[k] = t
    uv, up = best3(list(pool.values()))
    cls = "A+B" if (fv < 2 <= uv and r0v < 2) else ("A" if (uv >= 2 and r0v < 2) else ("C" if uv < 2 else "-"))
    rows.append((d, r0v, fv, uv, cls, up, r0d, len(pool)))
p("# 9월 '매일 최소 +2%' 가능성 — A/B/C 진단 (2026-09-30, READ-ONLY)\n")
p("FILTER = R0 거래 중 수익 거래 최적 선택 / FULL-FLAG = 게이트 해제 전 플래그 후보(한도3·한도해제 두 풀 ∪ R0) 중 시간 비중복 수익 거래 최대 3건 · 모두 수량 1.0, 기존 청산 규칙\n")
p("| 날짜 | R0 | FILTER | FULL-FLAG | 후보 수 | 분류 | +2% 가능 |")
p("|---|---|---|---|---|---|---|")
for d, r0v, fv, uv, cls, up, r0d, n in rows:
    p(f"| {d[4:]} | {r0v:+.2f} | {fv:+.2f} | {uv:+.2f} | {n} | {cls} | {'O' if uv >= 2 else 'X'} |")
A = [r for r in rows if r[4] in ("A", "A+B")]; B = [r for r in rows if r[4] == "A+B"]; C = [r for r in rows if r[4] == "C"]
p(f"\n- 9월 영업일 {len(rows)} · R0 ≥2% {sum(r[1] >= 2 for r in rows)} · FILTER ≥2% {sum(r[2] >= 2 for r in rows)} · FULL-FLAG ≥2% {sum(r[3] >= 2 for r in rows)}")
p(f"- A형(기회 있었는데 못 먹음) {len(A)}일 {[r[0][4:] for r in A]}")
p(f"  - 그중 B형(필터론 불가, 게이트 해제 시 가능) {len(B)}일 {[r[0][4:] for r in B]}")
p(f"  - 그중 필터만으로 가능(FILTER ≥2 ∧ R0 <2) {len(A) - len(B)}일 {[r[0][4:] for r in A if r[4] == 'A']}")
p(f"- C형(현재 구조로 불가) {len(C)}일 {[r[0][4:] for r in C]}\n")
p("## A형 날짜별 최적 선택 거래와 R0 가 놓친 이유")
reasons = Counter(); breasons = Counter()
for d, r0v, fv, uv, cls, up, r0d, n in A:
    r0k = {key(t) for t in r0d}
    p(f"### {d[4:]} ({cls}) R0 {r0v:+.2f} → FULL {uv:+.2f}")
    p("R0: " + " / ".join(f"{hm(t['entry_time'])} {t['direction'][:4]} {t['exit_reason'][:18]} {float(t['net_pct']):+.2f}" for t in sorted(r0d, key=lambda t: TS(t['entry_time']))))
    for t in up:
        if key(t) in r0k:
            why = "R0 도 진입"
        else:
            why = why_not(t); reasons[why.split(":")[0] if ":" in why else why] += 1
            if cls == "A+B": breasons[why] += 1
        p(f"- 선택 {hm(t['entry_time'])} {t['direction'][:4]} → {hm(t['exit_time'])} {t['exit_reason'][:22]} {float(t['net_pct']):+.2f} (MFE {float(t['peak_net_pct']):+.2f}) · {why}")
p("\n## 요약: R0 가 놓친 기회 거래의 원인 (A형 전체)")
for k, v in reasons.most_common(): p(f"- {k}: {v}건")
p("\n## B형에서 기회를 막은 게이트 (세부)")
for k, v in breasons.most_common(): p(f"- {k}: {v}건")
p("\n## C형 날짜의 후보 거래 (최대 net)")
for d, r0v, fv, uv, cls, up, r0d, n in C:
    p(f"- {d[4:]} R0 {r0v:+.2f} · FULL {uv:+.2f} · 후보 {n}건")
