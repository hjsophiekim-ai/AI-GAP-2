"""진입시점 feature 테이블 (2026-10-17). READ-ONLY.

BASE 재생 89일의 체결 162건에 대해 **결정시각(detected_at) 기준, 그 시점까지의 정보만으로**
feature 를 만든다. 지표 계산은 전부 production 함수를 그대로 호출한다
(`signal_engine.resample_completed_3m`, `major_flag_filter._session_vwap/_ema/_atr`,
`time_window_filter._gap_series`) — 재구현하지 않는다.

3분봉 입력은 하네스와 같게 만든다: 전전일·전일·당일 하이닉스 1분봉을 이어 붙이고
결정시각까지의 **완성** 3분봉만 쓴다(EMA/MACD 워밍업이 하네스와 같아진다).

방향정렬: s = +1 (UP_RED) / −1 (DOWN_BLUE). 모든 feature 는 "클수록 진입방향에 유리".
"""
import glob
import json
import os
import sys

import numpy as np
import pandas as pd

WT = "C:/Users/KIMHYU~1/AppData/Local/Temp/claude/G----------------2--Desktop-AI-GAP-2/4745b385-5441-4c15-97e7-446b1c980929/scratchpad/wt"
sys.path.insert(0, WT)
sys.stdout.reconfigure(encoding="utf-8")

from app.trading.macd2 import config, signal_engine                      # noqa: E402
from app.trading.macd2 import major_flag_filter as mff                   # noqa: E402
from app.trading.macd2 import time_window_filter as twf                  # noqa: E402

BARS = "C:/Users/KIMHYU~1/AppData/Local/Temp/claude/G----------------2--Desktop-AI-GAP-2/4745b385-5441-4c15-97e7-446b1c980929/scratchpad/bb/sp/data"
LED = "C:/Users/KIMHYU~1/AppData/Local/Temp/claude/G----------------2--Desktop-AI-GAP-2/e202387f-ec1a-41fa-a078-e46b72ece959/scratchpad/wk/out"
DEC = "G:/다른 컴퓨터/내 노트북 (2)/Desktop/AI-GAP 2/research_20261017_loss_decomp/out/d1_trades_89d.csv"
OUT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/out"
os.makedirs(OUT, exist_ok=True)

_HY = {}
_ALLDAYS = sorted({os.path.basename(p).split("_")[1]
                   for p in glob.glob(BARS + "/replay_*_hynix_1m.csv")})


def hy1m(day):
    if day not in _HY:
        p = f"{BARS}/replay_{day}_hynix_1m.csv"
        if not os.path.exists(p):
            _HY[day] = None
        else:
            x = pd.read_csv(p)
            x["datetime"] = pd.to_datetime(x["datetime"].astype(str).str[:19]).dt.tz_localize(config.KST)
            _HY[day] = x.drop_duplicates("datetime", keep="last").sort_values("datetime").reset_index(drop=True)
    return _HY[day]


def frame_1m(day):
    """하네스와 같은 입력: 전전일+전일+당일."""
    i = _ALLDAYS.index(day)
    parts = [hy1m(d) for d in _ALLDAYS[max(0, i - 2):i + 1]]
    parts = [p for p in parts if p is not None]
    return pd.concat(parts, ignore_index=True) if parts else None


def feats(day, dec_ts, s, flag_bar_ts, raw_flags):
    one = frame_1m(day)
    if one is None:
        return {}
    b3 = signal_engine.resample_completed_3m(one, now=dec_ts)
    w = mff._prepare_bars(b3)
    if w is None or len(w) < 40:
        return {}
    close = w["close"].astype(float)
    ema10 = mff._ema(close, 10)
    ema20 = mff._ema(close, 20)
    atr = mff._atr(w["high"].astype(float), w["low"].astype(float), close, 14)
    vw = mff._session_vwap(w)
    gs = twf._gap_series(w)
    gap = gs["gap"].astype(float) if gs is not None else pd.Series([np.nan] * len(w))
    c, a = float(close.iloc[-1]), float(atr.iloc[-1])
    a = a if a and a == a and a > 0 else np.nan
    e10, e20 = float(ema10.iloc[-1]), float(ema20.iloc[-1])
    v = float(vw.iloc[-1]) if len(vw) and vw.iloc[-1] == vw.iloc[-1] else np.nan
    # T+3 확인 강도 : 원 플래그 봉의 gap 대비 확정봉 gap (TEG 조건1 이 보는 바로 그 값)
    g_now = s * float(gap.iloc[-1])
    g_flag = np.nan
    if flag_bar_ts is not None:
        m = w.index[w["datetime"] == flag_bar_ts]
        if len(m):
            g_flag = s * float(gap.iloc[int(m[-1])])
    # 1분봉 기반
    sess = one[(one.datetime <= dec_ts) & (one.datetime.dt.strftime("%Y%m%d") == day)]
    sess = sess[sess.datetime.dt.strftime("%H%M") >= "0900"]
    def pe(n):
        seg = sess.tail(n)
        if len(seg) < max(10, n // 2):
            return np.nan
        cc = seg.close.to_numpy(float)
        mv = float(np.abs(np.diff(cc)).sum())
        return abs(float(cc[-1] - cc[0])) / mv if mv > 0 else np.nan
    def xc(n):
        seg = sess.tail(n)
        if len(seg) < 20:
            return np.nan
        cc = seg.close.to_numpy(float)
        e = pd.Series(cc).ewm(span=20, adjust=False).mean().to_numpy()
        sg = np.sign(cc - e)
        sg = sg[sg != 0]
        return int((np.diff(sg) != 0).sum()) if len(sg) > 1 else 0
    vol_rel = np.nan
    if len(sess) >= 60:
        v30 = float(sess.volume.tail(30).sum())
        vm = float(sess.volume.sum()) / max(len(sess) / 30.0, 1e-9)
        vol_rel = v30 / vm if vm > 0 else np.nan
    rv = {}
    for win in (15, 30, 60):
        fl = [f for f in raw_flags if 0 <= (dec_ts - f[0]).total_seconds() / 60 <= win]
        rv[f"rev{win}"] = sum(1 for i in range(1, len(fl)) if fl[i][1] != fl[i - 1][1])
        rv[f"nflag{win}"] = len(fl)
    return dict(
        ema_stack=int((s * (c - e10) > 0) and (s * (e10 - e20) > 0)),
        px_ema10=s * (c / e10 - 1) * 100, px_ema20=s * (c / e20 - 1) * 100,
        ema_sep=s * (e10 - e20) / c * 100,
        ema10_slope=s * (e10 - float(ema10.iloc[-3])) / c * 100,
        ema20_slope=s * (e20 - float(ema20.iloc[-3])) / c * 100,
        hist=s * float(gap.iloc[-1]) / a if a == a else np.nan,
        hist_slope=s * (float(gap.iloc[-1]) - float(gap.iloc[-3])) / a if a == a else np.nan,
        t3_gain=(g_now - g_flag) / a if (a == a and g_flag == g_flag) else np.nan,
        t3_ratio=(g_now / abs(g_flag)) if (g_flag == g_flag and abs(g_flag) > 1e-9) else np.nan,
        vwap_pos=s * (c / v - 1) * 100 if v == v else np.nan,
        atr_pct=a / c * 100 if a == a else np.nan,
        pe30=pe(30), pe60=pe(60), xc60=xc(60), vol_rel=vol_rel, **rv)


def raw_flags_of(day):
    p = f"{LED}/BASE_{day}.json"
    if not os.path.exists(p):
        return []
    o = json.load(open(p, encoding="utf-8"))
    out = []
    for s in o.get("sig", []):
        sid = str(s.get("signal_id") or "")
        if ":" in sid or not sid.startswith(day):
            continue
        t = pd.Timestamp(str(s["detected_at"])[:19]).tz_localize(config.KST)
        d = str(s.get("direction") or "")
        if out and out[-1][1] == d:
            continue
        out.append((t, d))
    return out


def main():
    d = pd.read_csv(DEC, dtype={"day": str})
    rows = []
    for r in d.itertuples():
        day = r.day
        s = 1 if r.dir == "UP" else -1
        dec_ts = pd.Timestamp(f"{day[:4]}-{day[4:6]}-{day[6:]} {r.dec or r.entry}:00").tz_localize(config.KST)
        fb = None
        sid = str(r.sid)
        if sid and "_" in sid and len(sid.split("_")) >= 2 and sid.split("_")[1].isdigit():
            h = sid.split("_")[1]
            fb = pd.Timestamp(f"{day[:4]}-{day[4:6]}-{day[6:]} {h[:2]}:{h[2:4]}:00").tz_localize(config.KST)
        f = feats(day, dec_ts, s, fb, raw_flags_of(day))
        rows.append(dict(day=day, dec=r.dec, entry=r.entry, dir=r.dir, krw=r.krw, net=r.net,
                         mfe_b=r.mfe_b, mfe=r.mfe, mae=r.mae, hold=r.hold, reason=r.reason,
                         is_brk=r.is_brk, **f))
    t = pd.DataFrame(rows)
    t["win"] = (t.krw > 0).astype(int)
    t["immrev"] = (t.mfe_b < 0.3).astype(int)
    t["chop"] = t.reason.astype(str).str.contains("B3_").astype(int)
    t.to_csv(OUT + "/f1_features.csv", index=False, encoding="utf-8-sig")
    print(f"거래 {len(t)}건 / feature 결측 {t.ema_stack.isna().sum()}건")
    print(t.groupby(t.day >= "20260801").agg(n=("krw", "size"), win=("win", "mean"),
                                             krw=("krw", "sum")).to_string())


if __name__ == "__main__":
    main()
