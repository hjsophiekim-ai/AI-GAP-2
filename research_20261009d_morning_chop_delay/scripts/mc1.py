"""MORNING-CHOP-DELAY 발동 스캔 (사전 고정 규칙, 탐색 없음). READ-ONLY.

ON  : (a) 09:00~10:30 사이 플래그 **방향전환 2회 이상**
      (b) 최근 60분(최소 30봉) 하이닉스 PE < 0.20  AND  EMA20 교차 >= 3회
      (a)와 (b)가 동시에 성립하는 **첫 순간** 부터 ON. (a)의 창은 10:30 까지.
      PE 0.20 / 교차 3 은 research_20261004_chop_staged 의 '왕복장' 판정 상수를
      그대로 재사용한 것이며 새로 고른 값이 아니다.
OFF : 10:30 이후, 한 방향 돌파확인이 성립하는 첫 순간.
      확인조건은 research_20261008c_trend_only_2slot 의 _trend_ok 와 동일 —
      30분 박스(마지막 3분 제외) 이탈 + 연속 2봉 유지 + 직전 5봉 극값 갱신 +
      박스중앙 미복귀 + 3분봉 MACD gap 부호 일치. 방향은 UP/DOWN 중 먼저 성립하는 쪽.
"""
import json, os, sys
import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
W = os.path.dirname(os.path.abspath(__file__))
PK = "G:/다른 컴퓨터/내 노트북 (2)/Desktop/AI-GAP 2/research_20261008_peaklock/out"
SPD = W + "/sp/data"
sys.path.insert(0, W)
import feat as FT  # noqa
pd.set_option("display.width", 240)

DAYS = [l.split()[1] for l in open(W + "/jobs_TREND2.txt", encoding="utf-8")]
PE_MAX, XC_MIN, REV_MIN = 0.20, 3, 2
ON_WINDOW_END, RELEASE_FROM = "1030", "1030"
BOXW, BOXSKIP, HHLL, NOUT = 30, 3, 5, 2


def A(d):
    for p in (f"{W}/out/REAL_A1008_{d}.json", f"{W}/out/REAL_UPFASTRS_{d}.json",
              f"{W}/base/REAL_UPFASTRS_{d}.json", f"{PK}/REAL_UPFASTRS_{d}.json"):
        if os.path.exists(p):
            return json.load(open(p, encoding="utf-8"))


def hy(d):
    df, _ = FT._day(d)
    return df


def ema(a, n):
    return pd.Series(a).ewm(span=n, adjust=False).mean().to_numpy()


def chop_path(c):
    """최근 창의 PE 와 EMA20 교차 횟수."""
    if len(c) < 30:
        return None
    dd = np.abs(np.diff(c)).sum()
    pe = abs(c[-1] - c[0]) / dd if dd else 0.0
    e = ema(c, 20)
    s = np.sign(c - e)
    s = s[s != 0]
    xc = int((np.diff(s) != 0).sum()) if len(s) > 1 else 0
    return pe, xc


def trend_ok(df, i, up):
    """i = 현재 완성봉 인덱스. 연구 c 의 _trend_ok 와 동일 조건."""
    if i < BOXW + 2:
        return False
    c = df.close.to_numpy(float)[:i + 1]
    h = df.high.to_numpy(float)[:i + 1]
    l = df.low.to_numpy(float)[:i + 1]
    i0 = max(0, len(c) - BOXW)
    bh, bl = h[i0:len(c) - BOXSKIP].max(), l[i0:len(c) - BOXSKIP].min()
    mid = (bh + bl) / 2.0
    bound = bh if up else bl
    last = c[-NOUT:]
    a = bool((last > bound).all()) if up else bool((last < bound).all())
    if not a:
        return False
    b = bool(h[-1] > h[-1 - HHLL:-1].max()) if up else bool(l[-1] < l[-1 - HHLL:-1].min())
    if not b:
        return False
    cc = bool(c[-1] >= mid) if up else bool(c[-1] <= mid)
    if not cc:
        return False
    g = FT.macd_gap_3m(pd.Series(c, index=df.datetime[:i + 1]))
    return bool(len(g) and ((g.iloc[-1] > 0) if up else (g.iloc[-1] < 0)))


rows = []
for d in DAYS:
    j = A(d)
    df = hy(d)
    if df is None or not len(j.get("sig", [])):
        rows.append(dict(day=d, on=None, note="NO_DATA"))
        continue
    # 플래그(원 신호만, T+3 확정행 제외) 시각/방향
    fl = []
    for s in j["sig"]:
        sid = s["signal_id"]
        if ":" in sid:
            continue
        hm = s["completed_bar_at"][:4]
        if hm < "0900" or hm > ON_WINDOW_END:
            continue
        fl.append((hm, s["direction"]))
    fl.sort()
    rev_at = None
    rev = 0
    for k in range(1, len(fl)):
        if fl[k][1] != fl[k - 1][1]:
            rev += 1
            if rev >= REV_MIN and rev_at is None:
                rev_at = fl[k][0]
    hm = df.datetime.dt.strftime("%H%M").to_numpy()
    c = df.close.to_numpy(float)
    on_at = None
    if rev_at is not None:
        for i in range(len(df)):
            if hm[i] < rev_at or hm[i] > ON_WINDOW_END:
                continue
            seg = c[max(0, i - 59):i + 1]
            r = chop_path(seg)
            if r and r[0] < PE_MAX and r[1] >= XC_MIN:
                on_at = hm[i]
                break
    off_at, off_dir = None, None
    if on_at is not None:
        for i in range(len(df)):
            if hm[i] < RELEASE_FROM:
                continue
            for up in (True, False):
                if trend_ok(df, i, up):
                    off_at, off_dir = hm[i], ("UP" if up else "DOWN")
                    break
            if off_at:
                break
    rows.append(dict(day=d, 플래그수=len(fl), 방향전환=rev, ON=on_at, OFF=off_at, OFF방향=off_dir))

R = pd.DataFrame(rows)
print(R.to_string(index=False))
on_days = [r["day"] for r in rows if r.get("ON")]
print("\nMORNING_CHOP 발동일 %d / %d일 : %s" % (len(on_days), len(DAYS), " ".join(on_days)))
open(W + "/mc_on_days.txt", "w").write("\n".join(on_days) + "\n")
R.to_csv(W + "/mc_scan.csv", index=False, encoding="utf-8-sig")
