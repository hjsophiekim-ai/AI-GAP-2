"""유예 판정 전수 (대상일 기준) — 워밍업일 이벤트를 제외하고 다시 센다.

재생은 대상일 앞 2영업일을 워밍업으로 함께 돌리므로, 원장의 slh 로그에는
워밍업일의 판정도 섞여 있다. 집계는 반드시 날짜로 걸러야 한다.
"""
import json
import os
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
OUTD = Path(os.environ.get("ARM3_OUT",
            r"C:/Users/Kim Hyunjoo/AppData/Local/Temp/claude/arm3_sp/out"))
TAG = os.environ.get("SLH_TAG", "CHK_SLH_ON")

days = sorted(f.stem.split("_")[-1] for f in OUTD.glob(f"{TAG}_2026*.json"))
nd = npass = nhard = 0
surv, conf = [], []
print(f"유예 판정 전수 ({TAG}, 대상일 기준 · 워밍업일 제외)\n")
for d in days:
    js = json.loads((OUTD / f"{TAG}_{d}.json").read_text(encoding="utf-8"))
    log = [x for x in (js.get("slh") or []) if x["t"][:10].replace("-", "") == d]
    sells = {o["t"][11:19] for o in js["orders"]
             if o["side"] == "SELL" and o["t"][:10].replace("-", "") == d}
    for x in log:
        if x["k"] == "DEFER":
            nd += 1
            until = x["until"][11:19]
            after = sorted(t for t in sells if t >= until)
            ex = after[0] if after else "(유예창 내 타규칙 청산)"
            kind = "CONFIRMED" if after and ex[:5] == until[:5] else "SURVIVE"
            (conf if kind == "CONFIRMED" else surv).append((d, x["t"][11:19]))
            print(f"  {d} {x['t'][11:19]} DEFER net={x['net']:>8} held={x['held']:>5}분 "
                  f"-> 확인봉종료 {until} -> 청산 {ex}  [{kind}]")
        elif x["k"] == "PASS":
            npass += 1
            print(f"  {d} {x['t'][11:19]} PASS  why={x['why']:<12} held={x.get('held')}분")
        else:
            nhard += 1
            print(f"  {d} {x['t'][11:19]} HARD  net={x['net']}")
print(f"\n유예 {nd}건 (손절 집행을 막음 {len(surv)} / 다음봉 재확인 청산 {len(conf)})"
      f" · 대상제외 PASS {npass}건 · 유예 중 hard stop {nhard}건")
print(f"유예가 손절을 막은 비율 = {len(surv)}/{nd} = {100 * len(surv) / max(nd, 1):.0f}%")
