"""연구3 MORNING-WHIP SLOT SAVE — 오전 휩쏘일 판정기 (2026-10-16). READ-ONLY.

"오전에 최근 60분 방향전환이 많거나 휩쏘가 강한 날" 의 정의는 **새로 만들지 않는다**.
production 에 이미 들어 있는 CHOP-STACK MC-DELAY 의 ON 조건을 그대로 쓴다
(app/trading/macd2/chop_defense.morning_no_trade, 임계값은 config 에 사전고정):

  (a) 09:00~CHOP_MC_WINDOW_END(10:30) 사이 **원 플래그** 방향전환 >= CHOP_MC_REVERSALS(2)
  (b) 최근 CHOP_MC_LOOKBACK_MIN(60)분 완성 1분봉에서
      PE(= |끝-시작| / 총이동) < CHOP_MC_PE_MAX(0.20)  AND  EMA20 교차 >= CHOP_MC_XC_MIN(3)
      (완성봉 >= CHOP_MC_MIN_BARS(30))

ON 시각 = 09:00~10:30 중 (a)와 (b)가 처음 동시에 성립하는 분. 사전계산이다 —
규칙 입력(하이닉스 1분봉 + production 원 플래그 시각)은 A/B 변형에서 동일하므로
MC-DELAY 연구(2026-10-11)와 같은 근거로 오프라인 계산이 타당하다.

원 플래그는 재생 원장의 sig 행 중 ``:TW2_3SLOT_CONFIRM`` 이 **아닌** 행
(= _judge_tw2_3slot_flag 가 note_morning_flag 를 부르는 그 플래그)에서 가져온다.
"""
import glob
import json
import os
import sys

import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
SP = "C:/Users/KIMHYU~1/AppData/Local/Temp/claude/G----------------2--Desktop-AI-GAP-2/4745b385-5441-4c15-97e7-446b1c980929/scratchpad/bb"
MY = "C:/Users/KIMHYU~1/AppData/Local/Temp/claude/G----------------2--Desktop-AI-GAP-2/e202387f-ec1a-41fa-a078-e46b72ece959/scratchpad/wk"
BARS = SP + "/sp/data"
OUT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/out"
os.makedirs(OUT, exist_ok=True)

# config 사전고정값 (app/trading/macd2/config.py)
WINDOW_END = "1030"
REVERSALS = 2
LOOKBACK = 60
MIN_BARS = 30
PE_MAX = 0.20
XC_MIN = 3


def bars(day):
    x = pd.read_csv(f"{BARS}/replay_{day}_hynix_1m.csv")
    x["datetime"] = pd.to_datetime(x["datetime"].astype(str).str[:19])
    x = x.drop_duplicates("datetime", keep="last").sort_values("datetime")
    return x[x["datetime"].dt.strftime("%Y%m%d") == day].reset_index(drop=True)


def raw_flags(path, day):
    """원 플래그 = :TW2_3SLOT_CONFIRM 이 아닌 sig 행. 봉시각(completed_bar_at)으로 기록."""
    o = json.load(open(path, encoding="utf-8"))
    out = []
    for s in o.get("sig", []):
        sid = str(s.get("signal_id") or "")
        if ":" in sid or not sid.startswith(day):
            continue
        hhmm = str(s.get("completed_bar_at") or "")[:4]
        if not hhmm or hhmm < "0900" or hhmm > WINDOW_END:
            continue
        val = str(s.get("direction") or "")
        if out and out[-1] == (hhmm, val):
            continue
        out.append((hhmm, val))
    return out


def path_is_chop(seg):
    """chop_defense._path_is_chop 와 같은 계산."""
    if len(seg) < MIN_BARS:
        return False, np.nan, 0
    c = seg["close"].to_numpy(float)
    moved = float(np.abs(np.diff(c)).sum())
    if moved <= 0:
        return False, np.nan, 0
    pe = abs(float(c[-1] - c[0])) / moved
    ema = pd.Series(c).ewm(span=20, adjust=False).mean().to_numpy()
    sign = np.sign(c - ema)
    sign = sign[sign != 0]
    xc = int((np.diff(sign) != 0).sum()) if len(sign) > 1 else 0
    return bool(pe < PE_MAX and xc >= XC_MIN), pe, xc


def whip_on(day, flags):
    """09:00~10:30 중 (a)+(b)가 처음 성립하는 분. 없으면 None."""
    b = bars(day)
    if not len(b):
        return None
    t0 = pd.Timestamp(f"{day[:4]}-{day[4:6]}-{day[6:]} 09:00")
    for m in range(0, 91):                       # 09:00 ~ 10:30
        now = t0 + pd.Timedelta(minutes=m)
        hhmm = now.strftime("%H%M")
        fl = [f for f in flags if f[0] <= hhmm]
        rev = sum(1 for i in range(1, len(fl)) if fl[i][1] != fl[i - 1][1])
        if rev < REVERSALS:
            continue
        work = b[b["datetime"] + pd.Timedelta(minutes=1) <= now]
        work = work[work["datetime"].dt.strftime("%H%M") >= "0900"]
        ok, pe, xc = path_is_chop(work.iloc[-LOOKBACK:])
        if ok:
            return dict(day=day, on=hhmm, rev=rev, pe=round(pe, 3), xc=xc,
                        n_flags=len(fl))
    return None


def main():
    src = sorted(glob.glob(SP + "/out/REAL_E2SLOT_2026*.json"))
    rows, none_days = [], []
    for p in src:
        day = os.path.basename(p)[len("REAL_E2SLOT_"):-5]
        if not os.path.exists(f"{BARS}/replay_{day}_hynix_1m.csv"):
            continue
        r = whip_on(day, raw_flags(p, day))
        (rows.append(r) if r else none_days.append(day))
    df = pd.DataFrame(rows)
    df["half"] = np.where(df.day < "20260801", "5-7월", "8-10월")
    df.to_csv(OUT + "/s3_whipdays.csv", index=False, encoding="utf-8-sig")
    tot = len(rows) + len(none_days)
    print(f"판정 대상 {tot}일 (05-27~10-08, 원 플래그 = E2SLOT 원장)")
    print(f"오전 휩쏘일 {len(df)}일 ({len(df) / tot * 100:.0f}%) / 아닌 날 {len(none_days)}일")
    for h, g in df.groupby("half"):
        print(f"  {h}: {len(g)}일")
    print()
    print(df.sort_values("day").to_string(index=False))
    with open(MY + "/whipdays.txt", "w") as f:
        f.write("\n".join(f"{r.day} {r.on}" for r in df.sort_values('day').itertuples()))
    print(f"\n-> {MY}/whipdays.txt")


if __name__ == "__main__":
    main()
