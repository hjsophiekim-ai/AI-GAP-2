"""손절선 스윕 — 재생이 실제로 비교하는 값(완성 3분봉 '봉 종가')을 오프라인 재구성.

production 함수를 그대로 부른다:
  worker._net_return_pct        (TradeCostEngine 수수료 포함)
  signal_engine.forming_bar_window  (3분 격자)
하네스 가격 규칙(WK_FILL=REAL, TICKPX=0)도 그대로 복제한다:
  p(ts) = open(분) + (close(분)-open(분)) * (초/60),  ETF 5원 호가 반올림
  루프 격자 20초  ->  '봉 종가' = 그 봉 마지막 tick(봉끝-20초)의 가격
  진입봉은 제외(_advance_stop_loss_bar)

출력: 손절거래별 '봉 종가 net%' 수열 + 임계값별 최초 발동 봉.
재생으로 검증한 사실과 대조하는 용도다(이 스크립트는 예측, 재생이 정답).
"""
import json
import os
import sys
from datetime import timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, os.environ["WT"])
sys.stdout.reconfigure(encoding="utf-8")

from app.trading.macd2 import config, worker                      # noqa: E402
from app.trading.macd2.signal_engine import forming_bar_window    # noqa: E402

A_OUT = Path(r"C:/Users/Kim Hyunjoo/AppData/Local/Temp/claude/arm3_sp/out")
SP = Path(r"C:/Users/KIMHYU~1/AppData/Local/Temp/claude/b3t_sp/data")
KST = config.KST
ETF_TICK = 5.0
STEP = 20
THRESHOLDS = [-1.3, -1.5, -1.6, -1.7, -1.8, -2.0]


def bars(day, sym):
    tag = "long" if sym == config.LONG_SYMBOL else "inverse"
    d = pd.read_csv(SP / f"replay_{day}_{tag}_1m.csv")
    d["dt"] = pd.to_datetime(d["datetime"].astype(str).str[:19]).dt.tz_localize(KST)
    return d.set_index("dt").sort_index()


def px(ix, ts):
    """하네스 _px_raw(FILL=REAL, TICKPX=0) 와 같은 규칙."""
    m = ts.floor("min")
    if m not in ix.index:
        done = ix[ix.index + pd.Timedelta(minutes=1) <= ts]
        return float(done["close"].iloc[-1]) if len(done) else None
    r = ix.loc[m]
    if isinstance(r, pd.DataFrame):
        r = r.iloc[-1]
    frac = (ts - m).total_seconds() / 60.0
    p = float(r["open"]) + (float(r["close"]) - float(r["open"])) * frac
    return round(p / ETF_TICK) * ETF_TICK


def stop_trades():
    out = []
    for f in sorted(A_OUT.glob("L85_A_2026*.json")):
        day = f.stem.split("_")[-1]
        if day < "20260803":
            continue
        js = json.loads(f.read_text(encoding="utf-8"))
        cur = None
        for o in js["orders"]:
            if o["t"][:10].replace("-", "") != day:
                continue
            if o["side"] == "BUY":
                cur = o
            elif cur is not None and any("TIME_WINDOW_STOP_LOSS" in a for a in (o.get("acts") or [])):
                out.append(dict(day=day, sym=cur["sym"], t=pd.Timestamp(cur["t"][:19]),
                                px=float(cur["px"]), qty=int(cur["qty"]),
                                exit_t=o["t"][11:19], exit_px=float(o["px"])))
                cur = None
    return out


print(f"{'날짜':9s}{'진입':9s}{'종목':8s}{'진입가':>7s}  봉별 net% (봉끝시각=값)")
first = {th: [] for th in THRESHOLDS}
for tr in stop_trades():
    ix = bars(tr["day"], tr["sym"])
    t0 = tr["t"].tz_localize(KST) if tr["t"].tzinfo is None else tr["t"]
    entry_bar = forming_bar_window(t0.to_pydatetime())[0]
    cur_bar, cur_close, seq, hit = entry_bar, None, [], {}
    t = t0
    end = t0 + timedelta(minutes=90)
    while t <= end:
        p = px(ix, t)
        if p is None:
            t += timedelta(seconds=STEP); continue
        bar_start = forming_bar_window(t.to_pydatetime())[0]
        if pd.Timestamp(bar_start) != pd.Timestamp(cur_bar):
            if pd.Timestamp(cur_bar) != pd.Timestamp(entry_bar) and cur_close is not None:
                net = worker._net_return_pct(tr["sym"], tr["px"], cur_close, tr["qty"])
                seq.append((pd.Timestamp(bar_start).strftime("%H:%M"), cur_close, net))
                for th in THRESHOLDS:
                    if th not in hit and net <= th:
                        hit[th] = (pd.Timestamp(bar_start).strftime("%H:%M"), net)
            cur_bar, cur_close = bar_start, p
        else:
            cur_close = p
        if -1.3 in [k for k in hit] and len(hit) == len(THRESHOLDS):
            break
        t += timedelta(seconds=STEP)
    s = "  ".join(f"{a}:{c:,.0f}({n:+.2f})" for a, c, n in seq[:8])
    print(f"{tr['day']:9s}{t0.strftime('%H:%M:%S'):9s}{tr['sym']:8s}{tr['px']:7,.0f}  {s}")
    for th in THRESHOLDS:
        first[th].append((tr["day"], tr["t"].strftime("%H:%M:%S"), hit.get(th)))

print("\n[임계값별 최초 발동 봉] — 현행(-1.3) 과 같으면 그 손절은 바뀌지 않는다")
base = {(d, t): h for d, t, h in first[-1.3]}
print(f"{'임계':8s}{'거래수':>7s}{'현행과 동일':>12s}{'늦게 발동':>10s}{'미발동':>8s}")
for th in THRESHOLDS:
    same = late = never = 0
    for d, t, h in first[th]:
        b = base[(d, t)]
        if h is None:
            never += 1
        elif b is not None and h[0] == b[0]:
            same += 1
        else:
            late += 1
    print(f"{th:8.2f}{len(first[th]):7d}{same:12d}{late:10d}{never:8d}")

print("\n[현행과 달라지는 거래]")
for th in THRESHOLDS:
    diffs = [(d, t, base[(d, t)], h) for d, t, h in first[th]
             if (h is None) != (base[(d, t)] is None) or (h and base[(d, t)] and h[0] != base[(d, t)][0])]
    if not diffs:
        print(f"  {th:+.2f}%: 없음 (전 거래 현행과 같은 봉에서 손절)")
        continue
    print(f"  {th:+.2f}%: {len(diffs)}건")
    for d, t, b, h in diffs:
        print(f"      {d} {t}  현행 {b[0]}({b[1]:+.2f}) -> {('미발동' if h is None else f'{h[0]}({h[1]:+.2f})')}")
