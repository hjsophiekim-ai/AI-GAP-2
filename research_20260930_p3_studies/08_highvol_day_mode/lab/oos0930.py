"""0930 OOS (집계 제외, 참고) — 오늘 09:06 레드/Q2 runner 와 09:48 블루 전환이 R0/R1/R2/R3 에서 어떻게 되나.

READ-ONLY. cache84 = cache83 + 0930 장중 부분본(~10:34). 하루 단위 독립(슬롯/W1a/포지션이 매일 초기화,
15:00 강제청산)이라 dates=[0929, 0930] 만 돌리고, 0929 결과를 83일 런과 대조해 독립성을 검증한다.
섀도우 regime = BASE_d83(0527~0929) + 오늘 BASE 거래.
"""
import os
import pickle
import sys

os.environ["B3_NDAYS"] = "84"
sys.argv = [sys.argv[0]]
import b3lib as L  # noqa: E402
import b3run  # noqa: E402  (VAR 만 사용; argv 비워서 실행 없음)
import pandas as pd  # noqa: E402

HERE = L.HERE
DAYS = ["20260929", "20260930"]
L.DATES = DAYS
hm = lambda x: pd.Timestamp(x).strftime("%H:%M:%S") if x else "-"
key = lambda t: (t["date"], str(t["entry_time"]), t["direction"])

base83 = pickle.load(open(HERE / "out_BASE_d83.pkl", "rb"))["trades"]
base2 = L.run(None)
b29 = sorted(key(t) + (round(t["net_pct"], 8),) for t in base2 if t["date"] == "20260929")
a29 = sorted(key(t) + (round(t["net_pct"], 8),) for t in base83 if t["date"] == "20260929")
print("[독립성] BASE 0929 2일런 == 83일런:", b29 == a29, len(b29), len(a29))
regime = L.make_regime(base83 + [t for t in base2 if t["date"] == "20260930"], strict=True)

print("\n[0930 BASE 거래 (섀도우)]")
for t in sorted((t for t in base2 if t["date"] == "20260930"), key=lambda t: t["entry_time"]):
    print(f"  {hm(t['entry_time'])} {t['direction']} @{t['entry_price']:,.0f} → {hm(t['exit_time'])} "
          f"@{t['exit_price']:,.0f} {t['exit_reason']} net {t['net_pct']:+.3f} MFE {t['peak_net_pct']:.2f}")

NAMES = {"R0": "H30", "R1": "RL1", "R2": "RG2", "R3": "RC3"}
out = {}
for tag, v in NAMES.items():
    cfg = dict(sl=1.0, hold=20.0, p3_min=6.0, p3_trig=1.0)
    cfg.update(b3run.VAR[v])
    cfg["regime"] = regime
    ts = L.run(cfg)
    out[tag] = ts
    ref = pickle.load(open(HERE / f"out_{v}_d83.pkl", "rb"))["trades"]
    same = (sorted(key(t) + (round(t["net_pct"], 8),) for t in ts if t["date"] == "20260929")
            == sorted(key(t) + (round(t["net_pct"], 8),) for t in ref if t["date"] == "20260929"))
    day = sorted((t for t in ts if t["date"] == "20260930"), key=lambda t: t["entry_time"])
    tot = sum(t["net_pct"] * t["w1a"] for t in day)
    print(f"\n## {tag} ({v}) — 0929 83일런 일치 {same} · 0930 {len(day)}거래 · 가중합 {tot:+.3f}")
    for t in day:
        mode = ("P3-RUNNER" if (t.get("b3_rescued") or t.get("b3_ext_prom")) else "Y3" if t.get("b3_y3") else
                "B3" if t.get("b3_on") else "BASE")
        legs = " / ".join(f"{str(l[0])[11:19]} {l[1]:,.0f}×{l[2]*100:.0f}% {l[3]}({l[4]:+.2f})" for l in t["legs"])
        print(f"  {hm(t['entry_time'])} {t['direction'][:4]} regime={t.get('entry_regime')} {mode} w1a={t['w1a']:.2f} "
              f"@{t['entry_price']:,.0f} → {hm(t['exit_time'])} {t['exit_reason']} net {t['net_pct']:+.3f} "
              f"MFE {t['peak_net_pct']:.2f} (+2% {hm(t.get('p2_at'))}, floor {hm(t.get('p2_floor_at'))})")
        print(f"     legs: {legs}")
        for o in t.get("opp", []):
            print(f"     opp: {o}")
pickle.dump(out, open(HERE / "out_oos0930.pkl", "wb"))
