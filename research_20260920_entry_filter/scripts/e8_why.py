"""E8 — 왜 solo 에서 좋던 플래그가 결합실행에서는 손해인가 (변위효과 확인)."""
import sys, pickle
sys.stdout.reconfigure(encoding="utf-8")
import pandas as pd
import axlib as A
import axval as V
from common import summarize

E6 = pickle.load(open(A.HERE / "e6.pkl", "rb"))
OUT, D = E6["out"], E6["dates"]
SOLO = {(r["date"], r["at"]): r for r in pickle.load(open(A.HERE / "e3.pkl", "rb")).values()}
BASE = OUT["기준 C1 (veto -1.0)"]
key = lambda t: (t["date"], t["entry_time"])
BK = {key(t): t for t in BASE}

for tag in ("veto -1.25", "veto -1.5", "veto -3"):
    ts = OUT[tag]; ka = {key(t): t for t in ts}
    r = V.report(tag, ts, BASE, D)
    new = sorted(set(ka) - set(BK)); gone = sorted(set(BK) - set(ka))
    print("\n" + "=" * 140)
    print(f"{tag}: 신규진입 {len(new)} / 소멸 {len(gone)} / 공통거래 중 변경 {r['chg']}건 "
          f"(단순합 {r['sum_d']:+.2f}%p)")
    print("=" * 140)
    s_new = s_solo = 0.0
    print(f"{'신규진입':9s} {'시각':6s} {'방향':10s} {'실제net':>8s} {'solo예상':>9s} {'차이':>8s} {'청산사유'}")
    for k in new:
        t = ka[k]; so = SOLO.get(k)
        sn = float(so["solo_net"]) if so and so.get("solo_net") is not None else float("nan")
        s_new += float(t["net_pct"]); s_solo += (sn if sn == sn else 0.0)
        print(f"{k[0]:9s} {str(k[1])[11:16]:6s} {t['direction']:10s} {t['net_pct']:+8.3f} "
              f"{sn:+9.3f} {t['net_pct']-sn:+8.3f} {t['exit_reason']}")
    print(f"  신규진입 실제합 {s_new:+.2f}%p / solo 예상합 {s_solo:+.2f}%p")
    if gone:
        print(f"\n{'소멸거래':9s} {'시각':6s} {'방향':10s} {'원래net':>8s} {'청산사유'}")
        for k in gone:
            t = BK[k]
            print(f"{k[0]:9s} {str(k[1])[11:16]:6s} {t['direction']:10s} {t['net_pct']:+8.3f} {t['exit_reason']}")
        print(f"  소멸거래 합 {sum(float(BK[k]['net_pct']) for k in gone):+.2f}%p")
    if r["chg"]:
        print(f"\n  기존거래 중 전개가 바뀐 것 {r['chg']}건:")
        for kk, x in r["diffs"][:15]:
            a, b = ka[kk], BK[kk]
            print(f"    {kk[0]} {str(kk[1])[11:16]} {a['net_pct']:+7.3f} vs {b['net_pct']:+7.3f} "
                  f"Δ{x:+7.3f}  {b['exit_reason']} -> {a['exit_reason']}")
    # 그날 슬롯 소비 변화
    dn = {}
    for k in new:
        dn.setdefault(k[0], []).append(k)
    print(f"\n  신규진입이 발생한 날 {len(dn)}일 — 그날 전체 손익 변화:")
    from common import compound
    for d in sorted(dn):
        a = sum(float(t["net_pct"]) * float(t["w1a"]) for t in ts if t["date"] == d)
        b = sum(float(t["net_pct"]) * float(t["w1a"]) for t in BK.values() if t["date"] == d)
        na = sum(1 for t in ts if t["date"] == d); nb = sum(1 for t in BK.values() if t["date"] == d)
        print(f"    {d}  거래 {nb}->{na}  가중합 {b:+7.2f} -> {a:+7.2f}  ({a-b:+7.2f})")
