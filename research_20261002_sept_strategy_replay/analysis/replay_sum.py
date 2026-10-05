"""production replay 결과 요약: 거래별 net% (실현가 기준, 수수료 왕복 0.03%) 와 원화 손익."""
import json, os, sys
sys.stdout.reconfigure(encoding="utf-8")
S = sys.argv[1]; SFXS = sys.argv[2:] or [""]
DAYS = ["20260922", "20260923", "20260928", "20260929", "20260930", "20261001"]
FEE = 0.03
for sfx in SFXS:
    tot, krw = 0.0, 0.0
    print(f"\n### replay{sfx or ' (P3 현행)'}")
    for d in DAYS:
        p = f"{S}/wk/out/wk_{d}{sfx}.json"
        if not os.path.exists(p):
            print(f"  {d} (없음)"); continue
        o = json.load(open(p, encoding="utf-8"))
        cur, line, ds, dk = None, [], 0.0, 0.0
        for r in o["orders"]:
            if r["side"] == "BUY":
                cur = dict(t=r["t"][11:16], sym=r["sym"], q=r["qty"], px=r["px"], sold=0, pr=0.0, legs=[])
            elif cur:
                cur["sold"] += r["qty"]; cur["pr"] += r["qty"] * r["px"]; cur["legs"].append(f"{r['t'][11:16]} {r['qty']}@{r['px']:.0f}")
            if cur and cur["sold"] >= cur["q"]:
                n = (cur["pr"] / cur["q"] / cur["px"] - 1) * 100 - FEE
                k = cur["pr"] - cur["q"] * cur["px"] - (cur["pr"] + cur["q"] * cur["px"]) * FEE / 200
                ds += n; dk += k
                line.append(f"{cur['t']} {'레버' if cur['sym'] == '0193T0' else '인버'} {cur['q']}@{cur['px']:.0f} -> {', '.join(cur['legs'])} {n:+.2f}% ({k:+,.0f}원)")
                cur = None
        if cur:
            line.append(f"{cur['t']} 보유중(미청산)")
        tot += ds; krw += dk
        print(f"  {d}  합 {ds:+.2f}%  {dk:+,.0f}원")
        for l in line:
            print("     " + l)
    print(f"  == 6일 합 {tot:+.2f}%  {krw:+,.0f}원")
