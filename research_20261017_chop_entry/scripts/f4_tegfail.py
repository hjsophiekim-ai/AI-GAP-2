"""오후 TEG 거절 플래그의 탈락조건 분해 (2026-10-17). READ-ONLY.

목적: EMAV(=price_ema_stack_aligned 완화/비적용) 가 **실제로 열 수 있는** 플래그만 찾아
재생할 날짜를 줄인다. production `teg_gate.evaluate_teg` 를 그대로 호출한다.

bars_3m 은 하네스와 같게 만든다(전전일+전일+당일 1분봉 -> resample_completed_3m).
검증: 재구성한 TEG 의 approved 여부가 원장의 '통과/TEG거절' 과 얼마나 일치하는지 같이 찍는다.
"""
import glob
import json
import os
import sys

import pandas as pd

WT = "C:/Users/KIMHYU~1/AppData/Local/Temp/claude/G----------------2--Desktop-AI-GAP-2/4745b385-5441-4c15-97e7-446b1c980929/scratchpad/wt"
sys.path.insert(0, WT)
sys.stdout.reconfigure(encoding="utf-8")

from app.trading.macd2 import config, signal_engine, teg_gate           # noqa: E402
from app.trading.macd2.models import Direction                          # noqa: E402

BARS = "C:/Users/KIMHYU~1/AppData/Local/Temp/claude/G----------------2--Desktop-AI-GAP-2/4745b385-5441-4c15-97e7-446b1c980929/scratchpad/bb/sp/data"
LED = "C:/Users/KIMHYU~1/AppData/Local/Temp/claude/G----------------2--Desktop-AI-GAP-2/e202387f-ec1a-41fa-a078-e46b72ece959/scratchpad/wk/out"
OUT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/out"
MY = "C:/Users/KIMHYU~1/AppData/Local/Temp/claude/G----------------2--Desktop-AI-GAP-2/e202387f-ec1a-41fa-a078-e46b72ece959/scratchpad/wk"

_HY = {}
_ALL = sorted({os.path.basename(p).split("_")[1] for p in glob.glob(BARS + "/replay_*_hynix_1m.csv")})


def hy(day):
    if day not in _HY:
        p = f"{BARS}/replay_{day}_hynix_1m.csv"
        if not os.path.exists(p):
            _HY[day] = None
        else:
            x = pd.read_csv(p)
            x["datetime"] = pd.to_datetime(x["datetime"].astype(str).str[:19]).dt.tz_localize(config.KST)
            _HY[day] = x.drop_duplicates("datetime", keep="last").sort_values("datetime").reset_index(drop=True)
    return _HY[day]


def frame(day):
    i = _ALL.index(day)
    parts = [hy(d) for d in _ALL[max(0, i - 2):i + 1]]
    parts = [p for p in parts if p is not None]
    return pd.concat(parts, ignore_index=True) if parts else None


def main():
    rows = []
    for p in sorted(glob.glob(LED + "/BASE_*.json")):
        day = os.path.basename(p)[5:-5]
        o = json.load(open(p, encoding="utf-8"))
        one = frame(day)
        if one is None:
            continue
        for s in o["sig"]:
            sid = str(s["signal_id"])
            if not sid.endswith(":TW2_3SLOT_CONFIRM") or not sid.startswith(day):
                continue
            hm = s["detected_at"][11:16]
            if hm < "11:00":
                continue
            dec = pd.Timestamp(s["detected_at"][:19]).tz_localize(config.KST)
            b3 = signal_engine.resample_completed_3m(one, now=dec)
            if b3 is None or len(b3) < 40:
                continue
            fb = pd.Timestamp(b3["datetime"].iloc[-2])
            d = Direction.UP_RED if s["direction"] == "UP_RED" else Direction.DOWN_BLUE
            t = pd.Timestamp(b3["datetime"].iloc[-1]) + pd.Timedelta(minutes=3)
            dc = teg_gate.evaluate_teg(b3, d, fb.to_pydatetime(), t.to_pydatetime())
            conds = dc.conditions or {}
            fails = [k for k, v in conds.items() if not v]
            others = [k for k in fails if k != teg_gate.COND_EMA_STACK]
            rows.append(dict(day=day, hm=hm, dir=s["direction"], res=s["order_result"],
                             why=s["block_reason"], approved=int(dc.approved),
                             ema_fail=int(teg_gate.COND_EMA_STACK in fails),
                             n_other_fail=len(others), others=",".join(others)))
    x = pd.DataFrame(rows)
    x.to_csv(OUT + "/f4_tegfail.csv", index=False, encoding="utf-8-sig")
    teg = x[x.why == "TW2_3SLOT_REJECT_TEG"]
    print(f"오후 확정플래그 {len(x)}건 / TEG 거절 {len(teg)}건")
    print(f"\n재구성 검증: 원장 '통과' {int((x.why.fillna('') == '').sum())}건 중 "
          f"재구성 approved {int(x[x.why.fillna('') == ''].approved.sum())}건 / "
          f"원장 'TEG거절' {len(teg)}건 중 재구성 approved {int(teg.approved.sum())}건(0 이어야 정상)")
    print(f"\nTEG 거절 {len(teg)}건의 탈락조건:")
    print(f"  EMA 정렬에서 탈락          {int(teg.ema_fail.sum())}건")
    only = teg[(teg.ema_fail == 1) & (teg.n_other_fail == 0)]
    print(f"  **EMA 정렬 하나만** 탈락    {len(only)}건 / {only.day.nunique()}일  <- EMAV 가 열 수 있는 전부")
    print(f"  EMA + 다른조건 같이 탈락   {int(((teg.ema_fail == 1) & (teg.n_other_fail > 0)).sum())}건")
    print(f"  EMA 와 무관하게 탈락       {int((teg.ema_fail == 0).sum())}건")
    print("\n  다른 조건 동반 탈락 내역:")
    print(teg[teg.n_other_fail > 0].others.value_counts().head(12).to_string())
    if len(only):
        print(f"\n  EMA 단독탈락 플래그 ({len(only)}건):")
        print(only[["day", "hm", "dir"]].to_string(index=False))
        pmap = {r.split()[1]: (r.split()[2], r.split()[3]) for r in open(MY + "/jobs_base.txt") if r.strip()}
        for tag in ("CEMAOFF", "CEMASOFT"):
            ds = sorted(only.day.unique())
            with open(f"{MY}/jobs_{tag}.txt", "w") as f:
                f.write("".join(f"{tag} {d} {pmap[d][0]} {pmap[d][1]}\n" for d in ds if d in pmap))
            print(f"  -> {MY}/jobs_{tag}.txt  ({len(ds)}일)")


if __name__ == "__main__":
    main()
