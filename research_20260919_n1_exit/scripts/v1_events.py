"""V1 — 회귀확인 + C1 발동 7건 이벤트 전량 + 진입집합 parity + runner 영향.

READ-ONLY. production 무수정.
"""
import pickle, sys
sys.stdout.reconfigure(encoding="utf-8")
import pandas as pd
import axlib as A
A.H._MEMO_PATH = A.HERE / "_memo_B.pkl"
from common import summarize, pnl
import axval as V

c = A.ctx(78); D = c.dates
cfg, po, q3, h50 = A.SPEC["N1"]
C1 = {"decide": 99.0, "strong": {}, "weak": {},
      "pp": {"arm": 5.0, "give": 1.5, "cond": "gap_neg"}}

print("=== 0. 회귀확인 (훅 추가 후) ===")
base = A.run("N1", c, D); A.save()
mb = summarize(base, D)
print(f"  N1  n={mb['trades']} 복리={mb['compound_pct']:.4f}  기대 158 / 401.0853  "
      f"{'OK' if mb['trades']==158 and abs(mb['compound_pct']-401.0853)<0.005 else '<<<불일치'}")
c1 = A.run("N1", c, D, cfg=cfg, po=po, q3=q3, h50=h50, ax=C1); A.save()
m1 = summarize(c1, D)
print(f"  C1  n={m1['trades']} 복리={m1['compound_pct']:.4f}  기대 158 / 438.63  "
      f"{'OK' if m1['trades']==158 and abs(m1['compound_pct']-438.63)<0.02 else '<<<불일치'}")

key = lambda t: (t["date"], t["entry_time"])
B = {key(t): t for t in base}; K = {key(t): t for t in c1}

# ── 12. 진입집합 parity ───────────────────────────────────────────────────
print("\n=== 12. 진입집합 parity (N1 vs C1) ===")
PF = ["date", "entry_time", "direction", "entry_symbol", "entry_price", "w1a",
      "slot_number", "session", "entry_bar_idx", "entry_chop", "chop_score", "tq"]
only_b, only_a = sorted(set(B) - set(K)), sorted(set(K) - set(B))
mism = []
for kk in sorted(set(B) & set(K)):
    for f in PF:
        va, vb = K[kk].get(f), B[kk].get(f)
        if (va != vb) and not (pd.isna(va) and pd.isna(vb)):
            mism.append((kk, f, vb, va))
print(f"  N1 전용 {len(only_b)}건 / C1 전용 {len(only_a)}건 / 공통 {len(set(B)&set(K))}건")
print(f"  공통거래 진입필드 불일치: {len(mism)}건  {'→ parity OK (diff 0)' if not mism else mism[:5]}")

# ── 1. 발동 이벤트 전량 ───────────────────────────────────────────────────
fired = [t for t in c1 if t.get("pp_fired")]
print(f"\n=== 1. C1 발동 {len(fired)}건 — 이벤트 전량 ===")
QQ = c.quotes


def fwd(sym, t0, mins):
    """청산 직후 mins 분간 forward MFE/MAE (청산가 기준 %)."""
    q = QQ[sym]
    t0 = pd.Timestamp(t0)
    px0 = q.at(t0)
    if px0 is None:
        return None, None
    hi = lo = 0.0
    for m in range(1, mins + 1):
        px = q.exact.get(t0 + pd.Timedelta(minutes=m))
        if px is None:
            continue
        r = (px / px0 - 1.0) * 100.0
        hi = max(hi, r); lo = min(lo, r)
    return hi, lo


rows = []
for t in sorted(fired, key=lambda x: x["date"]):
    kk = key(t); b = B[kk]
    hi5, lo5 = fwd(t["entry_symbol"], t["exit_time"], 5)
    hi10, lo10 = fwd(t["entry_symbol"], t["exit_time"], 10)
    hi20, lo20 = fwd(t["entry_symbol"], t["exit_time"], 20)
    hi30, lo30 = fwd(t["entry_symbol"], t["exit_time"], 30)
    rows.append(dict(
        date=t["date"], entry=str(t["entry_time"])[11:16], dirn=t["direction"],
        sym=t["entry_symbol"], entry_px=t["entry_price"],
        peak=b["peak_net_pct"], peak_at=str(b.get("peak_at"))[11:16],
        arm_at=str(t.get("pp_arm_at"))[11:16], flip_at=str(t.get("pp_flip_at"))[11:16],
        fire_at=str(t.get("pp_fire_at"))[11:16], pp_peak=t.get("pp_peak"),
        give=t.get("pp_give"),
        c1_exit=str(t["exit_time"])[11:16], c1_px=t["exit_price"], c1_net=t["net_pct"],
        n1_exit=str(b["exit_time"])[11:16], n1_px=b["exit_price"], n1_net=b["net_pct"],
        n1_reason=b["exit_reason"], d=pnl(t) - pnl(b),
        f5=hi5, f5l=lo5, f10=hi10, f10l=lo10, f20=hi20, f20l=lo20, f30=hi30, f30l=lo30))

for r in rows:
    print(f"\n  {r['date']} {r['entry']} {r['dirn']:10s} {r['sym']}  진입가 {r['entry_px']:.0f}")
    print(f"    peak(MFE) {r['peak']:.3f}% @ {r['peak_at']}   arm(>=5.0) @ {r['arm_at']}   "
          f"gap 반전 @ {r['flip_at']}   조건충족 @ {r['fire_at']}")
    print(f"    발동시 peak {r['pp_peak']:.3f} / 반납 {r['give']:.3f}%p")
    print(f"    C1 청산 {r['c1_exit']} @ {r['c1_px']:.0f} → net {r['c1_net']:+.3f}")
    print(f"    N1 청산 {r['n1_exit']} @ {r['n1_px']:.0f} → net {r['n1_net']:+.3f}  [{r['n1_reason']}]")
    print(f"    Δ {r['d']:+.3f}%p")
    print(f"    청산 직후 forward (MFE/MAE, 청산가 대비): "
          f"5분 {r['f5']:+.2f}/{r['f5l']:+.2f}  10분 {r['f10']:+.2f}/{r['f10l']:+.2f}  "
          f"20분 {r['f20']:+.2f}/{r['f20l']:+.2f}  30분 {r['f30']:+.2f}/{r['f30l']:+.2f}")

pd.DataFrame(rows).to_csv(A.HERE / "RESULTS" / "C1_events.csv", index=False, encoding="utf-8-sig")

# ── 11. runner 보호 ───────────────────────────────────────────────────────
print("\n=== 11. TP2 8% runner 9건 ===")
RUN8 = sorted(k for k, t in B.items() if t["peak_net_pct"] >= 8.0)
print(f"{'일자':10s} {'진입':6s} {'peak':>6s} {'N1 net':>8s} {'N1 사유':22s} {'C1 net':>8s} "
      f"{'C1 사유':16s} {'C1 발동':>7s} {'Δ':>7s}")
tot = 0.0
for kk in RUN8:
    b, a = B[kk], K[kk]
    d = a["net_pct"] - b["net_pct"]; tot += d
    print(f"{kk[0]:10s} {str(kk[1])[11:16]:6s} {b['peak_net_pct']:6.2f} {b['net_pct']:+8.3f} "
          f"{str(b['exit_reason'])[:22]:22s} {a['net_pct']:+8.3f} {str(a['exit_reason'])[:16]:16s} "
          f"{'Y' if a.get('pp_fired') else 'N':>7s} {d:+7.3f}")
print(f"  runner 9건 합계 Δ = {tot:+.4f}  "
      f"(TP2 도달 방해 {sum(1 for k in RUN8 if K[k].get('pp_fired'))}건)")
print(f"  arm(5.0) 무장까지 간 runner: {sum(1 for k in RUN8 if K[k].get('pp_armed'))}건")

pickle.dump({"base": base, "c1": c1, "dates": D}, open(A.HERE / "v1.pkl", "wb"))
print("\n저장: v1.pkl, RESULTS/C1_events.csv")
