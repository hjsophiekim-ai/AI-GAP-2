# -*- coding: utf-8 -*-
"""§4 가설 재검증, §7 Top-N 의존성, §8 거래 의존성. READ-ONLY."""
import sys, pickle, statistics as st
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"C:\Users\FURSYS\Desktop\AI-GAP 2")
import k1_core as K

D = pickle.load(open("_ctx_B.pkl", "rb"))["dates"]
W78, W70, W30 = D, D[-70:], D[-30:]
TS = K.load()
M, AF = K.MORNING, K.AFTERNOON
P2 = K.make_extra(1.05, 0.25, 1.00)
A = K.size_chain(TS, None)
B = K.size_chain(TS, P2)


def med(x):
    return st.median(x) if x else float("nan")


print("=" * 104)
print("§4  P2 핵심가설 재검증 — '오전 세 번째 진입은 평균적으로 약하다'")
print("=" * 104)
print(f"{'버킷':22s} {'건수':>4s} {'평균net%':>9s} {'중앙net%':>9s} {'승률':>6s} "
      f"{'A 기여KRW':>13s} {'P2 기여KRW':>13s} {'차이':>11s}")
print("-" * 104)
akey = {(t["date"], t["entry_time"]): t for t in A}
buckets = [
    ("slot1 (전체)", lambda t: t["slot"] == 1),
    ("slot2 (전체)", lambda t: t["slot"] == 2),
    ("slot3 오전", lambda t: t["slot"] == 3 and t["session"] == M),
    ("slot3 오후", lambda t: t["slot"] == 3 and t["session"] == AF),
    ("slot1 오전", lambda t: t["slot"] == 1 and t["session"] == M),
    ("slot2 오전", lambda t: t["slot"] == 2 and t["session"] == M),
]
for tag, f in buckets:
    sel = [t for t in B if f(t)]
    if not sel:
        print(f"{tag:22s}  (없음)"); continue
    nets = [t["net_pct"] for t in sel]
    ak = sum(akey[(t["date"], t["entry_time"])]["pnl_krw"] for t in sel)
    bk = sum(t["pnl_krw"] for t in sel)
    print(f"{tag:22s} {len(sel):4d} {sum(nets)/len(nets):9.3f} {med(nets):9.3f} "
          f"{sum(1 for x in nets if x > 0)/len(nets)*100:5.1f}% {ak:13,.0f} {bk:13,.0f} {bk-ak:+11,.0f}")

mo3 = [t for t in B if t["slot"] == 3 and t["session"] == M]
print(f"\n  오전 slot3 전체 건수 = {len(mo3)} (78일)")
print(f"  날짜: {', '.join(sorted(t['date'] for t in mo3))}")
print(f"  각 건 net%: {', '.join(f'{t['net_pct']:+.2f}' for t in sorted(mo3, key=lambda x: x['date']))}")

print("\n  cap 적용 전/후 동일 현상 유지 여부 (전략상 계산금액 기준 vs 실제 주문금액 기준)")
print("-" * 104)
for tag, f in buckets[:4]:
    sel = [t for t in B if f(t)]
    if not sel:
        continue
    pre = sum(t["strategy_krw"] * t["net_pct"] / 100.0 for t in sel)
    post = sum(t["pnl_krw"] for t in sel)
    print(f"  {tag:22s} cap전 {pre:13,.0f}  cap후 {post:13,.0f}  차이 {post-pre:+11,.0f}")

print()
print("=" * 104)
print("§7  Top-N 의존성 (KRW)")
print("=" * 104)
RUNS = {"A 현행": A, "D P0": K.size_chain(TS, K.make_extra(1.00, 0.25, 1.00)),
        "E P1": K.size_chain(TS, K.make_extra(1.02, 0.25, 1.00)), "C P2": B,
        "F P3": K.size_chain(TS, K.make_extra(1.10, 0.25, 1.00))}
print(f"{'전략':10s} {'78d 전체':>13s} {'-Top1':>13s} {'-Top3':>13s} {'-Top5':>13s} {'-Top10':>13s} {'복리%':>9s}")
print("-" * 104)
for n, r in RUNS.items():
    print(f"{n:10s} {K.krw_pnl(r, W78):13,.0f} " +
          " ".join(f"{K.excl_top_krw(r, W78, k):13,.0f}" for k in (1, 3, 5, 10)) +
          f" {K.compound_pct(r, W78):9.2f}")
print()
print(f"{'':10s} {'vs A: 전체':>13s} {'-Top1':>13s} {'-Top3':>13s} {'-Top5':>13s} {'-Top10':>13s}")
print("-" * 104)
for n, r in RUNS.items():
    if n == "A 현행":
        continue
    print(f"{n:10s} {K.krw_pnl(r, W78)-K.krw_pnl(A, W78):+13,.0f} " +
          " ".join(f"{K.excl_top_krw(r, W78, k)-K.excl_top_krw(A, W78, k):+13,.0f}"
                   for k in (1, 3, 5, 10)))

print()
print("=" * 104)
print("§8  P2 로 실제 주문금액이 달라진 거래 전량")
print("=" * 104)
diff = []
for a, b in zip(A, B):
    if abs(a["actual_krw"] - b["actual_krw"]) > 1e-6 or abs(a["pnl_krw"] - b["pnl_krw"]) > 1e-6:
        diff.append((a, b))
print(f"  변경된 거래 {len(diff)} / 전체 {len(A)}")
print(f"\n{'날짜':9s} {'시각':6s} {'sl':>2s} {'세션':4s} {'A 주문':>12s} {'P2 주문':>12s} "
      f"{'차이':>11s} {'net%':>7s} {'A 손익':>11s} {'P2 손익':>11s} {'손익차':>10s}")
print("-" * 104)
for a, b in diff:
    print(f"{a['date']:9s} {a['entry_time'][11:16]:6s} {a['slot']:2d} "
          f"{('오전' if a['session']==M else '오후'):4s} {a['actual_krw']:12,.0f} "
          f"{b['actual_krw']:12,.0f} {b['actual_krw']-a['actual_krw']:+11,.0f} "
          f"{a['net_pct']:7.2f} {a['pnl_krw']:11,.0f} {b['pnl_krw']:11,.0f} "
          f"{b['pnl_krw']-a['pnl_krw']:+10,.0f}")

deltas = [b["pnl_krw"] - a["pnl_krw"] for a, b in diff]
up = [d for d in deltas if d > 0]; dn = [d for d in deltas if d < 0]
tot = sum(deltas)
print("-" * 104)
print(f"  개선 {len(up)}건 합 {sum(up):+,.0f} / 악화 {len(dn)}건 합 {sum(dn):+,.0f} / 순 {tot:+,.0f}")
srt = sorted(deltas, reverse=True)
print(f"  최대 단일거래 기여율 = {srt[0]/tot*100:.1f}%   상위3 기여율 = {sum(srt[:3])/tot*100:.1f}%")
if sum(srt[:3]) / tot > 0.5:
    print("  *** 경고: 상위3 거래가 전체 uplift 의 50% 이상 ***")
else:
    print("  -> 상위3 의존 50% 미만: 소수의존 경고 없음")
print(f"  상위1 제외 후 순 uplift = {tot-srt[0]:+,.0f}")
print(f"  상위3 제외 후 순 uplift = {tot-sum(srt[:3]):+,.0f}")
