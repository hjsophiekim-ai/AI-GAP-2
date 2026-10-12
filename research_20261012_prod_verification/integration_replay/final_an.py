"""production replay 집계 (2026-10-12) — integration/macd2-slowtp2-chop-on-main.

P3 / SLOW(P3+SLOW-TP2) / CHOP(P3+SLOW-TP2+2SLOT+MC+STRUCTSTOP), 89일 REAL 체결.
"""
import json, os, sys
from collections import Counter, defaultdict
import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
RP = os.path.dirname(os.path.abspath(__file__))
WT = os.path.join(os.path.dirname(RP), "wt_int")
sys.path.insert(0, os.path.join(WT, "research_20261008b_box_breakout_gate", "scripts"))
from feat import trades  # noqa: E402

ARCH = os.path.join(WT, "research_20261002_sept_strategy_replay", "round3_85d", "replay_json")
YDAY = os.path.join(WT, "research_20261012_prod_verification", "out")
pd.set_option("display.width", 250)
pd.set_option("display.max_rows", 500)

P85 = [l.strip() for l in open(os.path.join(WT, "research_20261012_prod_verification", "p3_days.txt"))]
ALLD = sorted({l.split()[1] for l in open(os.path.join(RP, "jobs.txt"))})
D23 = [d for d in ALLD if "20260903" <= d <= "20261008"]


def load(tag, d):
    p = os.path.join(RP, "out", f"REAL_{tag}_{d}.json")
    return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else None


def sec(t):
    print("\n" + "=" * 118 + "\n" + t + "\n" + "=" * 118)


def met(days, tr):
    daily = np.array([sum(t["krw"] for t in tr if t["day"] == d) for d in days], float)
    eq = np.cumprod(1 + daily / 1e7)
    k = np.array([t["krw"] for t in tr], float) if tr else np.zeros(1)
    return dict(총손익=daily.sum(), PF=k[k > 0].sum() / max(-k[k < 0].sum(), 1.0),
                MDD=(eq / np.maximum.accumulate(np.r_[1.0, eq])[1:] - 1).min() * 100,
                최대1일손실=daily.min(), 손실일=int((daily < 0).sum()),
                승률=(k > 0).mean() * 100 if len(tr) else 0.0, 거래=len(tr))


FM = {"총손익": "{:+,.0f}".format, "Δ": "{:+,.0f}".format, "최대1일손실": "{:+,.0f}".format,
      "PF": "{:.2f}".format, "MDD": "{:.2f}".format, "승률": "{:.1f}".format}

# ── 로드 ──────────────────────────────────────────────────────────────────
O = {tag: {d: load(tag, d) for d in ALLD} for tag in ("P3", "SLOW", "CHOP")}
sec("0. 재생 보유현황")
for tag in O:
    miss = [d for d in ALLD if O[tag][d] is None]
    print(f"{tag:5s} {len(ALLD) - len(miss)}/{len(ALLD)}  누락: {miss or '없음'}")
    bad = [d for d in ALLD if O[tag][d] is not None and
           (O[tag][d].get("prodmode") != ("CHOP" if tag == "CHOP" else "P3")
            or O[tag][d].get("slow_cfg") != (tag != "P3")
            or O[tag][d].get("mode_state") != ("CHOP" if tag == "CHOP" else "P3"))]
    print(f"      모드/SLOW 설정 불일치: {bad or '없음'}")

TR = {tag: {d: (trades(O[tag][d]) if O[tag][d] else []) for d in ALLD} for tag in O}


def T(tag, days):
    return sum((TR[tag][d] for d in days), [])


def day_pnl(tag, d):
    return sum(t["krw"] for t in TR[tag][d])


# ── ① P3 OFF parity ───────────────────────────────────────────────────────
sec("① P3 OFF parity — 통합브랜치 P3(SLOW OFF) vs 기존 production P3 아카이브")


def orders_key(o):
    return [(x["t"], x["side"], x["sym"], x["qty"], x["px"]) for x in o["orders"]]


def ex_key(o):
    return [(e["side"], e["symbol"], e["executed_qty"], e["executed_price"], e["exit_reason"], e["net_pnl"])
            for e in o["ex"]]


par = []
for d in ALLD:
    new = O["P3"][d]
    ref, src = None, None
    for p, s in ((os.path.join(ARCH, f"REAL_A_{d}.json"), "round3_85d"),
                 (os.path.join(YDAY, f"REAL_PBASE_{d}.json"), "yday_PBASE"),
                 (os.path.join(YDAY, f"REAL_APROD_{d}.json"), "yday_APROD")):
        if os.path.exists(p):
            ref, src = json.load(open(p, encoding="utf-8")), s
            break
    if new is None or ref is None:
        par.append(dict(day=d, src=src, 주문일치=None, 체결원장일치=None, Δ=None))
        continue
    on, orf = orders_key(new), orders_key(ref)
    par.append(dict(day=d, src=src, 주문수=len(on), 주문일치=on == orf,
                    청산사유일치=[e[4] for e in ex_key(new)] == [e[4] for e in ex_key(ref)],
                    Δ=day_pnl("P3", d) - sum(t["krw"] for t in trades(ref))))
par = pd.DataFrame(par)
print(par.groupby("src").agg(일수=("day", "size"), 주문완전일치=("주문일치", "sum"),
                             청산사유일치=("청산사유일치", "sum"), Δ합=("Δ", "sum")).to_string())
bad = par[(par.주문일치 != True)]
print("\n불일치일:", "없음" if bad.empty else "")
if not bad.empty:
    print(bad.to_string(index=False))
print("P3 85일 총손익(새 production replay): {:+,.0f}  (아카이브 앵커 +17,227,606)".format(
    met(P85, T("P3", P85))["총손익"]))

# ── 발동 집계 ──────────────────────────────────────────────────────────────
SLOT_CAP = "TW2_3SLOT_REJECT_DAILY_SLOT_CAP"
MC_R = "CHOP_MORNING_NO_TRADE"


def counts(tag, days):
    c = Counter()
    for d in days:
        o = O[tag][d]
        if not o:
            continue
        for s in o["sig"]:
            if s["block_reason"] == SLOT_CAP:
                c["SLOTCAP_sig"] += 1
            if s["block_reason"] == MC_R:
                c["MC_BLOCK"] += 1
        prod = o.get("prod") or []
        defer = {e["entry"] for e in prod if e["k"] == "B3STOP" and e["res"] == "DEFER"}
        c["STRUCTSTOP"] += len(defer)
        c["SLOW_TP2"] += len({e["entry"] for e in prod if e["k"] == "SLOWTP2"})
        c["MC_ON_days"] += int(any(e["k"] == "MC" for e in prod))
    return c


def table(days, title):
    rows = []
    base = met(days, T("P3", days))["총손익"]
    for tag, nm in (("P3", "P3 (현행 production)"), ("SLOW", "P3 + SLOW-TP2"),
                    ("CHOP", "CHOP (P3+SLOW+2SLOT+MC+STRUCT)")):
        m = met(days, T(tag, days))
        c = counts(tag, days)
        rows.append(dict(변형=nm, **m, Δ=m["총손익"] - base,
                         SLOT2=c["SLOTCAP_sig"] if tag == "CHOP" else "-", MC=c["MC_BLOCK"],
                         STRUCT=c["STRUCTSTOP"], SLOW=c["SLOW_TP2"]))
        if tag == "P3":
            p3slot = c["SLOTCAP_sig"]
    print(f"\n[{title}] {len(days)}일   (P3 의 하루3회 cap 거절 신호 {p3slot}건)")
    print(pd.DataFrame(rows).to_string(index=False, formatters=FM))


def conc(days, tag):
    d = pd.Series({x: day_pnl(tag, x) - day_pnl("P3", x) for x in days})
    tot = d.sum()
    srt = d.sort_values(ascending=False)
    out = [f"Δ{tag} 합계 {tot:+,.0f} (차이 발생 {int((d != 0).sum())}일)"]
    for n in (1, 2, 3):
        adj = tot - srt.head(n).sum()
        out.append(f"상위{n}일 제외 {adj:+,.0f} {'우위' if adj > 0 else '열위'}")
    out.append("상위3: " + ", ".join(f"{k} {v:+,.0f}" for k, v in srt.head(3).items()))
    out.append("하위3: " + ", ".join(f"{k} {v:+,.0f}" for k, v in srt.tail(3).items()))
    print("  " + " | ".join(out))


# ── ② SLOW-TP2 ────────────────────────────────────────────────────────────
sec("② P3 + SLOW-TP2 — 85영업일 (5/27~10/01) + 9~10월 23일")
table(P85, "85영업일")
table(D23, "9~10월 23영업일")
conc(P85, "SLOW")
TR_ = [d for d in P85 if d < "20260801"]
TE_ = [d for d in P85 if d >= "20260801"]
for nm, ds in (("train 5~7월", TR_), ("test 8~10월", TE_)):
    print(f"  {nm} ({len(ds)}일) Δ SLOW {met(ds, T('SLOW', ds))['총손익'] - met(ds, T('P3', ds))['총손익']:+,.0f}")

print("\n[SLOW-TP2 발동 거래별 비교]  (천장 4.0->8.0 상향된 포지션)")
rows = []
for d in ALLD:
    o = O["SLOW"][d]
    if not o:
        continue
    ents = sorted({e["entry"] for e in (o.get("prod") or []) if e["k"] == "SLOWTP2"})
    for ent in ents:
        hh = pd.Timestamp(ent).strftime("%H:%M")
        ts = [t for t in TR["SLOW"][d] if t["entry"].strftime("%H:%M") == hh]
        tp = [t for t in TR["P3"][d] if t["entry"].strftime("%H:%M") == hh]
        rows.append(dict(day=d, 진입=hh,
                         P3=tp[0]["krw"] if tp else np.nan, P3청산="/".join(tp[0]["reasons"]) if tp else "-",
                         SLOW=ts[0]["krw"] if ts else np.nan, SLOW청산="/".join(ts[0]["reasons"]) if ts else "-",
                         일Δ=day_pnl("SLOW", d) - day_pnl("P3", d)))
x = pd.DataFrame(rows)
if len(x):
    x["Δ"] = x.SLOW - x.P3
    print(x.to_string(index=False, float_format="{:+,.0f}".format))
oth = [d for d in ALLD if O["SLOW"][d] and abs(day_pnl("SLOW", d) - day_pnl("P3", d)) > 0.5
       and d not in set(x.day if len(x) else [])]
print("발동 기록 없이 손익이 달라진 날:", oth or "없음")

# ── ③ CHOP ────────────────────────────────────────────────────────────────
sec("③ CHOP — production CHOP 모드 전일 replay")
table(D23, "9~10월 23영업일 (09/03~10/08)")
conc(D23, "CHOP")
table(P85, "85영업일 (05/27~10/01)")
conc(P85, "CHOP")
for nm, ds in (("train 5~7월", TR_), ("test 8~10월(85일 내)", TE_),
               ("test 8~10월(+10/02~10/08)", [d for d in ALLD if d >= "20260801"])):
    a, s, c = (met(ds, T(k, ds)) for k in ("P3", "SLOW", "CHOP"))
    print(f"  {nm:24s} {len(ds):2d}일  P3 {a['총손익']:+,.0f} | SLOW {s['총손익']:+,.0f} | CHOP {c['총손익']:+,.0f}"
          f"  ΔCHOP-P3 {c['총손익'] - a['총손익']:+,.0f}  ΔCHOP-SLOW {c['총손익'] - s['총손익']:+,.0f}"
          f"  MDD {a['MDD']:.2f}/{c['MDD']:.2f}")
mon = defaultdict(list)
for d in ALLD:
    mon[d[:6]].append(d)
print("\n  월별 Δ(CHOP-P3) / Δ(CHOP-SLOW):", ", ".join(
    f"{m} {sum(day_pnl('CHOP', d) - day_pnl('P3', d) for d in ds):+,.0f}/{sum(day_pnl('CHOP', d) - day_pnl('SLOW', d) for d in ds):+,.0f}"
    for m, ds in sorted(mon.items())))

print("\n[일별 — 차이 나는 날, 9~10월 23일]")
rows = [dict(day=d, P3=day_pnl("P3", d), SLOW=day_pnl("SLOW", d), CHOP=day_pnl("CHOP", d)) for d in D23]
x = pd.DataFrame(rows)
x["Δchop"] = x.CHOP - x.P3
print(x[(x.P3 != x.CHOP) | (x.P3 != x.SLOW)].to_string(index=False, float_format="{:+,.0f}".format))

sec("10/08 별도")
for tag in ("P3", "SLOW", "CHOP"):
    print(f"  {tag:5s} {day_pnl(tag, '20261008'):+,.0f}  ",
          [(t["entry"].strftime("%H:%M"), round(t["krw"]), t["reasons"]) for t in TR[tag]["20261008"]])
o = O["CHOP"]["20261008"]
if o:
    print("  CHOP 차단신호:", [(s["detected_at"][11:16], s["block_reason"]) for s in o["sig"]
                           if s["block_reason"] in (SLOT_CAP, MC_R)])
    print("  STRUCT 유예 포지션:", sorted({e["entry"][11:16] for e in o["prod"] if e["k"] == "B3STOP" and e["res"] == "DEFER"}))


def detail(d):
    print(f"\n--- {d} ---")
    for tag in ("P3", "SLOW", "CHOP"):
        print(f"  {tag:5s} {day_pnl(tag, d):+,.0f}  ",
              [(t["entry"].strftime("%H:%M"), round(t["krw"]), t["reasons"]) for t in TR[tag][d]])
    o = O["CHOP"][d]
    print("  CHOP 신호:", [(s["detected_at"][11:16], s["order_result"], s["block_reason"]) for s in o["sig"]
                       if s["order_result"] != "FILTERED_OUT" or s["block_reason"] in (SLOT_CAP, MC_R)])
    print("  CHOP prod:", Counter((e["k"], e.get("res")) for e in o["prod"]))


sec("9/22 단일 원인")
detail("20260922")

sec("CHOP 거래별 원인 분해 (Δ 상위/하위 일)")
dd = pd.Series({d: day_pnl("CHOP", d) - day_pnl("SLOW", d) for d in ALLD}).sort_values()
for d in list(dd.index[:4]) + list(dd.index[-4:]):
    detail(d)
