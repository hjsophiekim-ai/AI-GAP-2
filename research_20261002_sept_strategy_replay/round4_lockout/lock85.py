"""85영업일: A P3-R0 vs WHIPSAW LOCKOUT (L30/L45/ALT3). production worker 전체 재생, REAL 체결. READ-ONLY.
lockout 이 한 번도 발동하지 않은 날은 변형 코드가 A 와 같은 경로 -> A 재생 결과를 그대로 쓴다 (샘플 비발동일 재생으로 동일성 검증)."""
import importlib.util, json, os, sys, glob
import numpy as np
import pandas as pd
sys.stdout.reconfigure(encoding="utf-8")
S = sys.argv[1]
spec = importlib.util.spec_from_file_location("s3", S + "/wk/lab/sept3_lib.py")
L = importlib.util.module_from_spec(spec); spec.loader.exec_module(L)
O4, O5 = S + "/wk/out4", S + "/wk/out5"
DAYS = sorted(os.path.basename(p)[7:15] for p in glob.glob(O4 + "/REAL_A_*.json"))
SEPT = [d for d in DAYS if d.startswith("202609")]
RECENT = ["20260922", "20260923", "20260928", "20260929", "20260930", "20261001"]
LD = json.load(open(S + "/wk/lab/lockdays.json"))
V = ("L30", "L45", "ALT3")
NM = {"A": "A P3-R0", "L30": "B LOCK30", "L45": "C LOCK45", "ALT3": "D ALT3"}
J = lambda p: json.load(open(p, encoding="utf-8"))
sig_of = lambda o: ([(x["t"], x["side"], x["sym"], x["qty"], x["px"]) for x in o["orders"]],
                    [(e["exit_reason"], e["net_pnl"]) for e in o["ex"] if e["side"] == "SELL"][-sum(1 for x in o["orders"] if x["side"] == "SELL"):] if any(x["side"] == "SELL" for x in o["orders"]) else [])

# anchor (9월 A = 기존 REAL A, out4 는 round3 에서 이미 anchor 통과) + 비발동일 동일성
A = {d: J(f"{O4}/REAL_A_{d}.json") for d in DAYS}
sa = sum(t["krw"] for d in SEPT for t in L.trades(A[d]))
bad = [d for d in SEPT + ["20261001"] if sig_of(A[d]) != sig_of(J(f"{S}/wk/out3/REAL_A_{d}.json"))]
print(f"## anchor: 9월 A {sa:+,.0f}원 (기존 +1,416,214) · 거래단위 불일치 {bad or '없음'}")
if bad or round(sa) != 1_416_214:
    print("=> INVALID REPLAY"); sys.exit(0)
chk = []
for v in V:
    for d in DAYS:
        p = f"{O5}/REAL_{v}_{d}.json"
        if os.path.exists(p) and d not in LD.get(v, {}):
            o = J(p)
            chk.append((v, d, sig_of(o) == sig_of(A[d]) and not o["lock_events"]))
print(f"  비발동일 재생 동일성 검증: {sum(c[2] for c in chk)}/{len(chk)} 일치 {[(v, d) for v, d, ok in chk if not ok] or ''}")
missing = [(v, d) for v in V for d in LD.get(v, {}) if not os.path.exists(f"{O5}/REAL_{v}_{d}.json")]
if missing:
    print("  !! 발동일 재생 누락", missing); sys.exit(0)
# 재생상 발동일 == 사전 계산 발동일 ?
mism = [(v, d) for v in V for d in LD.get(v, {}) if not J(f"{O5}/REAL_{v}_{d}.json")["lock_events"]]
print(f"  사전 계산 발동일 중 재생에서 미발동: {mism or '없음'}")
PRED = {v: set(LD.get(v, {})) for v in V}

OBJ = {"A": A}
for v in V:
    OBJ[v] = {}
    for d in DAYS:
        p = f"{O5}/REAL_{v}_{d}.json"
        if not os.path.exists(p):
            print(f"  !! {v} {d} 재생 없음"); sys.exit(0)
        OBJ[v][d] = J(p)
    LD[v] = {d: 1 for d in DAYS if OBJ[v][d].get("lock_events")}
T = {k: {d: L.trades(OBJ[k][d]) for d in DAYS} for k in OBJ}
for v in V:
    extra = sorted(set(LD[v]) - PRED[v])
    same_nt = sum(1 for d in DAYS if d not in LD[v] and sig_of(OBJ[v][d]) == sig_of(A[d]))
    print(f"  {v}: 재생상 발동일 {len(LD[v])}일 (사전계산에 없던 발동일 {extra or '없음'}) · 비발동일 A 동일 {same_nt}/{len(DAYS) - len(LD[v])}")
flat = lambda k, ds: [t for d in ds for t in T[k][d]]


def table(ds, title):
    print(f"\n## {title} ({len(ds)}일)")
    print("| 전략 | 총손익 | 복리% | PF | MDD% | 승률% | 거래수 | 손실일 | 최대1일손실 | +2%일 |\n|---|---|---|---|---|---|---|---|---|---|")
    M = {}
    for k in ("A",) + V:
        m = M[k] = L.metrics(ds, flat(k, ds))
        print(f"| {NM[k]} | {m['krw']:+,.0f} | {m['comp']:+.2f} | {m['pf']:.2f} | {m['mdd']:.2f} | {m['win']:.1f} | {m['n']} | {m['lossd']} | {m['dmin']:+,.0f} | {m['d2']} |")
    for k in V:
        a, b = M["A"], M[k]
        print(f"  {NM[k]} − A: 총손익 {b['krw'] - a['krw']:+,.0f} · PF {b['pf'] - a['pf']:+.2f} · MDD {b['mdd'] - a['mdd']:+.2f}%p · 승률 {b['win'] - a['win']:+.1f}%p · 거래수 {b['n'] - a['n']:+d} · 손실일 {b['lossd'] - a['lossd']:+d}")
    return M


M = table(DAYS, "85영업일 전체")
print("\n## lockout 영향 (85일)")
for v in V:
    ev = sum(len(OBJ[v][d].get("lock_events", [])) for d in DAYS if d in LD.get(v, {}))
    blk = sum(len(OBJ[v][d].get("lock_blocked", [])) for d in DAYS if d in LD.get(v, {}))
    avoid, miss = [], []
    for d in LD.get(v, {}):
        wins = [(pd.Timestamp(e["t"]), pd.Timestamp(e["until"])) for e in OBJ[v][d]["lock_events"]]
        vk = {L.key(t) for t in T[v][d]}
        for t in T["A"][d]:
            if L.key(t) not in vk and any(a <= t["entry"] < b for a, b in wins):
                (avoid if t["krw"] < 0 else miss).append(t)
    dd = np.array([sum(t["krw"] for t in T[v][d]) - sum(t["krw"] for t in T["A"][d]) for d in DAYS])
    print(f"  {NM[v]}: 발동 {ev}회 ({len(LD.get(v, {}))}일) · 차단된 승인신호 {blk}건")
    print(f"     차단으로 피한 A 손실거래 {len(avoid)}건 {sum(t['krw'] for t in avoid):+,.0f}원 · 차단으로 놓친 A 수익거래 {len(miss)}건 {sum(t['krw'] for t in miss):+,.0f}원")
    print(f"     일별 B−A: 우세 {(dd > 0).sum()} / 열세 {(dd < 0).sum()} / 동일 {(dd == 0).sum()}일 · 상위2일 {np.sort(dd)[-2:].sum():+,.0f} · 상위2일 제외 {dd.sum() - np.sort(dd)[-2:].sum():+,.0f}")
    nr = [i for i, d in enumerate(DAYS) if d not in RECENT]
    print(f"     최근 6일 제외 B−A {dd[nr].sum():+,.0f}원 · 전반(~07/31) {dd[[i for i, d in enumerate(DAYS) if d < '20260801']].sum():+,.0f} · 후반(08/01~) {dd[[i for i, d in enumerate(DAYS) if d >= '20260801']].sum():+,.0f}")
    rng = np.random.default_rng(0)
    print(f"     일별 부트스트랩 P(B−A>0) {(np.array([rng.choice(dd, len(dd)).sum() for _ in range(5000)]) > 0).mean() * 100:.1f}%")
table([d for d in DAYS if d not in RECENT], "최근 6일 제외")

print("\n## (참고) 10/01, 10/02")
for d in ("20261001", "20261002"):
    for k in ("A",) + V:
        p = (f"{O4}/REAL_A_{d}.json" if k == "A" else f"{O5}/REAL_{k}_{d}.json")
        if k == "A" and d == "20261002":
            p = f"{O5}/REAL_A_{d}.json"
        if not os.path.exists(p):
            if k != "A" and d in DAYS and d not in LD.get(k, {}):
                p = f"{O4}/REAL_A_{d}.json"
            else:
                print(f"  {d} {NM[k]}: 재생 없음"); continue
        o = J(p); tr = L.trades(o)
        print(f"  {d} {NM[k]}: {sum(t['krw'] for t in tr):+,.0f}원 · " + ", ".join(f"{t['entry']:%H:%M} {t['dir']} -> {t['exit']:%H:%M} {L.rs(t)} {t['krw']:+,.0f}" for t in tr)
              + (f" · lockout {[(e['t'][11:16], e['until'][11:16]) for e in o.get('lock_events', [])]}" if o.get("lock_events") else ""))
