"""V3/V4/V5 — gap 조건 강건성 / 인식지연 / 슬리피지 스트레스."""
import pickle, sys
sys.stdout.reconfigure(encoding="utf-8")
import axlib as A
A.H._MEMO_PATH = A.HERE / "_memo_V3.pkl"
A._Q3_PATH = A.HERE / "_q3base_V3.pkl"
from common import summarize
import axval as V

c = A.ctx(78); D = c.dates
cfg, po, q3, h50 = A.SPEC["N1"]
base = A.run("N1", c, D); A.save()
key = lambda t: (t["date"], t["entry_time"]); B = {key(t): t for t in base}
RUN8 = sorted(k for k, t in B.items() if t["peak_net_pct"] >= 8.0)
OUT = {"N1": base}
HDR = (f"{'변형':26s} {'n':>4s} {'78d':>8s} {'Δ78':>8s} {'30d':>7s} {'Δ30':>7s} {'PF78':>6s} "
       f"{'PF30':>6s} {'MDD':>6s} {'ΔT10':>7s} {'앞39':>7s} {'뒤39':>7s} {'WF':>5s} "
       f"{'변경':>4s} {'↑':>3s} {'↓':>3s} {'run8':>6s}")


def go(k, **kw):
    ts = A.run("N1", c, D, cfg=cfg, po=po, q3=q3, h50=h50, **kw)
    OUT[k] = ts; A.save()
    r = V.report(k, ts, base, D); m = summarize(ts, D)
    ka = {key(t): t for t in ts}
    dmg = sum(ka[x]["net_pct"] - B[x]["net_pct"] for x in RUN8 if x in ka)
    print(f"{k:26s} {m['trades']:4d} {m['compound_pct']:8.2f} {r['78d_d']:+8.2f} "
          f"{r['30d_a']:7.2f} {r['30d_d']:+7.2f} {m['pf']:6.3f} {r['30d_pf_a']:6.3f} "
          f"{m['mdd_pct']:6.2f} {r['top10_d']:+7.2f} {r['half'][0]:+7.2f} {r['half'][1]:+7.2f} "
          f"{r['wf6_win']}/{r['wf6_lose']:<3d} {r['chg']:4d} {r['up']:3d} {r['dn']:3d} {dmg:+6.2f}",
          flush=True)


PP = lambda **kw: {"decide": 99.0, "strong": {}, "weak": {},
                   "pp": dict({"arm": 5.0, "give": 1.5}, **kw)}

print("=== 3. gap 조건 강건성 (arm 5.0 / give 1.5 고정) ===", flush=True)
print(HDR, flush=True)
go("A. gap<=0 반대전환 [C1]", ax=PP(cond="gap_neg"))
go("B. gap shrink 만", ax=PP(cond="gap_shrink"))
go("C. MACD crossover 만", ax=PP(cond="gap_cross"))
go("D. gap 조건 없음", ax=PP())

print("\n=== 4. 인식지연 스트레스 (조건 충족 후 N봉 뒤 체결. 그 사이 기존 래더 우선) ===", flush=True)
print(HDR, flush=True)
for nb, lab in ((0, "정상"), (1, "+3분"), (2, "+6분"), (3, "+9분")):
    go(f"지연 {lab}", ax=PP(cond="gap_neg"), pp_delay_bars=nb)

print("\n=== 5. 슬리피지 스트레스 (C1 청산 체결가에만 추가비용) ===", flush=True)
print(HDR, flush=True)
for sl in (0.0, 0.10, 0.20, 0.30, 0.50):
    go(f"슬리피지 +{sl:.2f}%p", ax=PP(cond="gap_neg"), pp_slip_pct=sl)

print("\n=== 4+5 동시 (지연 +3분 & 슬리피지) ===", flush=True)
print(HDR, flush=True)
for sl in (0.10, 0.20, 0.30):
    go(f"+3분 & 슬립 {sl:.2f}", ax=PP(cond="gap_neg"), pp_delay_bars=1, pp_slip_pct=sl)

pickle.dump({"trades": OUT, "dates": D}, open(A.HERE / "v345.pkl", "wb"))
print("\n저장: v345.pkl")
