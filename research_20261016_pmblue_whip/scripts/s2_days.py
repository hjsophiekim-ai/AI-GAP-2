"""연구2 — 변형이 실제로 건드릴 수 있는 날만 고른다 (2026-10-16). READ-ONLY.

PM-BLUE(13:30~14:30 DOWN_BLUE) 후보가 오후 경로에서 지나는 VWAP 관문은 두 개뿐이다.
  (1) TW2 VWAP 역행 veto : dev_dir < -1.0 이면 차단        (dev_dir = 방향정렬 % , 음수 = 불리)
  (2) TEG COND_VWAP      : dev_dir < 0 이면 탈락
따라서
  * 비적용(OFF)  이 바꿀 수 있는 플래그 = dev_dir < 0 인 것
  * 완화(RELAX)  = TW2 -2.0 / TEG -1.0 -> 둘 다 통과하려면 dev_dir >= -1.0
                   즉 -1.0 <= dev_dir < 0 인 것만 새로 열린다
dev_dir 은 production 과 같은 계산으로 만든다 — 3분봉(확정봉까지)의 세션 VWAP 대 종가.
"""
import os
import sys

import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
ROOT = "G:/다른 컴퓨터/내 노트북 (2)/Desktop/AI-GAP 2"
BARS = "C:/Users/KIMHYU~1/AppData/Local/Temp/claude/G----------------2--Desktop-AI-GAP-2/4745b385-5441-4c15-97e7-446b1c980929/scratchpad/bb/sp/data"
MY = "C:/Users/KIMHYU~1/AppData/Local/Temp/claude/G----------------2--Desktop-AI-GAP-2/e202387f-ec1a-41fa-a078-e46b72ece959/scratchpad/wk"
OUT = ROOT + "/research_20261016_pmblue_whip/out"


def dev3m(day, t):
    """3분봉(09:00~확정봉) 세션 VWAP 대비 종가, DOWN_BLUE 기준 방향정렬 %."""
    p = f"{BARS}/replay_{day}_hynix_1m.csv"
    if not os.path.exists(p):
        return np.nan
    x = pd.read_csv(p)
    x["datetime"] = pd.to_datetime(x["datetime"].astype(str).str[:19])
    x = x.drop_duplicates("datetime", keep="last").set_index("datetime").sort_index()
    b3 = pd.DataFrame({
        "open": x["open"].resample("3min", label="left", closed="left").first(),
        "high": x["high"].resample("3min", label="left", closed="left").max(),
        "low": x["low"].resample("3min", label="left", closed="left").min(),
        "close": x["close"].resample("3min", label="left", closed="left").last(),
        "volume": x["volume"].resample("3min", label="left", closed="left").sum(),
    }).dropna()
    b3 = b3[(b3.index.time >= pd.Timestamp("09:00").time()) & (b3.index + pd.Timedelta(minutes=3) <= pd.Timestamp(t))]
    if not len(b3):
        return np.nan
    tp = (b3.high + b3.low + b3.close) / 3
    vw = (tp * b3.volume).cumsum() / b3.volume.cumsum().replace(0, np.nan)
    c, v = float(b3.close.iloc[-1]), float(vw.iloc[-1])
    if not (v > 0):
        return np.nan
    return -(c / v - 1.0) * 100.0            # DOWN_BLUE: sign = -1


def main():
    pm = pd.read_csv(OUT + "/s2_pmblue_flags.csv", dtype={"day": str}, parse_dates=["t"])
    pm = pm[pm.day <= "20261008"].copy()
    pm["dev3m"] = [dev3m(r.day, r.t) for r in pm.itertuples()]
    pm.to_csv(OUT + "/s2_pmblue_dev.csv", index=False, encoding="utf-8-sig")
    pm["blocked"] = pm.result != "EXECUTED"
    off = pm[pm.blocked & (pm.dev3m < 0)]
    rel = pm[pm.blocked & (pm.dev3m >= -1.0) & (pm.dev3m < 0)]
    print(f"PM-BLUE {len(pm)}건 / {pm.day.nunique()}일 (~10/08)")
    print(f"  dev3m < 0 (COND_VWAP 탈락) 이면서 미체결 : {len(off)}건 / {off.day.nunique()}일  <- PMOFF 대상")
    print(f"  그중 -1.0 <= dev3m < 0                  : {len(rel)}건 / {rel.day.nunique()}일  <- PMRELAX 대상")
    print("\n1분봉 vwap_pos 와 3분봉 dev3m 비교 (부호가 갈리는 것만)")
    d = pm[(np.sign(pm.vwap_pos) != np.sign(pm.dev3m))][["day", "t", "reason", "vwap_pos", "dev3m"]]
    print(d.round(2).to_string(index=False) if len(d) else "  없음")
    print("\nPMOFF 대상 플래그")
    print(off[["day", "t", "reason", "vwap_pos", "dev3m", "ret"]].round(2).to_string(index=False))

    pmap = {r.split()[1]: (r.split()[2], r.split()[3]) for r in open(MY + "/jobs_base.txt") if r.strip()}
    for tag, g in (("PMOFF", off), ("PMRELAX", rel)):
        days = sorted(g.day.unique())
        with open(f"{MY}/jobs_{tag}.txt", "w") as f:
            f.write("".join(f"{tag} {d} {pmap[d][0]} {pmap[d][1]}\n" for d in days if d in pmap))
        print(f"\n{tag}: {len(days)}일 -> {MY}/jobs_{tag}.txt")
        print("  " + " ".join(days))


if __name__ == "__main__":
    main()
