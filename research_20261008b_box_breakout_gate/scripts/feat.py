"""BOX vs BREAKOUT 판별 연구 - 피처 추출. READ-ONLY (production/config 무수정).

표본 = 9월+10월 production T+3 재확인 지점(signal_id 에 :TW2_3SLOT_CONFIRM 포함).
결정시각 decision = 재확인봉 완성시각 + 3분. 피처는 decision 직전까지의 완성 1분봉만 사용(선행정보 없음).
EMA/MACD 는 전일 정규장까지 이어 붙여 워밍업, 박스/경로 피처는 당일 09:00 이후만 사용.
"""
import glob, json, os, sys
import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
W = os.path.dirname(os.path.abspath(__file__))
ROOT = "G:/다른 컴퓨터/내 노트북 (2)/Desktop/AI-GAP 2"
D1 = ROOT + "/research_20261004_chop_staged/wk/data"       # ~1002
D2 = ROOT + "/data/cache"                                  # 1006,1007
D3 = W + "/data"                                           # 1008 (naver)
JD = [ROOT + "/research_20261008_peaklock/out", W + "/out"]

_cache = {}


def _path(day):
    for p in (D1 + "/replay_%s_hynix_1m.csv" % day,
              D2 + "/replay_%s_hynix_1m.csv" % day,
              D3 + "/naver_%s_hynix_1m.csv" % day):
        if os.path.exists(p):
            return p
    return None


def _day(day):
    if day in _cache:
        return _cache[day]
    p = _path(day)
    if p is None:
        _cache[day] = (None, None)
        return _cache[day]
    df = pd.read_csv(p)
    df["datetime"] = pd.to_datetime(df["datetime"])
    hm = df.datetime.dt.strftime("%H%M")
    df = df[(hm >= "0900") & (hm <= "1530")]
    df = df.drop_duplicates("datetime").sort_values("datetime").reset_index(drop=True)
    _cache[day] = (df, os.path.basename(p))
    return _cache[day]


ALLDAYS = sorted({os.path.basename(p).split("_")[1] for p in
                  glob.glob(D1 + "/replay_*_hynix_1m.csv") +
                  glob.glob(D2 + "/replay_*_hynix_1m.csv")} |
                 {os.path.basename(p).split("_")[1] for p in glob.glob(D3 + "/naver_*_hynix_1m.csv")})


def prev_day(day):
    i = ALLDAYS.index(day)
    return ALLDAYS[i - 1] if i > 0 else None



def trades(o):
    """research_20261004_chop_staged/an8.py 의 trades() 를 그대로 옮긴 것 (거래 묶기 동일)."""
    sells = [x for x in o["orders"] if x["side"] == "SELL"]
    ex_s = [e for e in o["ex"] if e["side"] == "SELL"][-len(sells):] if sells else []
    assert len(ex_s) == len(sells), (o["D"], o["VAR"])
    exe = {pd.Timestamp(s["detected_at"]).strftime("%H:%M:%S"): s["signal_id"]
           for s in o["sig"] if s["order_result"] == "EXECUTED"}
    out, cur, si = [], None, 0
    for x in o["orders"]:
        t = pd.Timestamp(x["t"])
        if x["side"] == "BUY":
            if cur is not None and cur["sold"] < cur["q"]:
                tot = cur["q"] + x["qty"]
                cur["px_in"] = (cur["px_in"] * cur["q"] + x["px"] * x["qty"]) / tot
                cur["q"] = tot
                continue
            cur = dict(day=o["D"], entry=t, px_in=float(x["px"]), q=int(x["qty"]),
                       sid=exe.get(t.strftime("%H:%M:%S"), ""), sold=0, krw=0.0,
                       reasons=[], exit=None, sym=x["sym"])
            out.append(cur)
            continue
        e = ex_s[si]; si += 1
        if cur is None:
            continue
        cur["sold"] += x["qty"]
        cur["krw"] += float(e["net_pnl"] or 0)
        cur["reasons"].append(e["exit_reason"] or "?")
        cur["exit"] = t
        if cur["sold"] >= cur["q"]:
            cur["net"] = cur["krw"] / (cur["q"] * cur["px_in"]) * 100
            cur["hold_min"] = (cur["exit"] - cur["entry"]).total_seconds() / 60
            cur = None
    return [t for t in out if "net" in t]


def load_signals():
    sigs, ex, seen = [], {}, set()
    for d in JD:
        for p in sorted(glob.glob(d + "/REAL_UPFASTRS_*.json")):
            day = os.path.basename(p)[-13:-5]
            if day < "20260901":
                continue
            j = json.load(open(p, encoding="utf-8"))
            for s in j["sig"]:
                sid = s["signal_id"]
                if ":TW2_3SLOT_CONFIRM" not in sid or sid.endswith(":BRK"):
                    continue
                if sid in seen or not sid.startswith(day):
                    continue
                seen.add(sid)
                sigs.append(dict(day=day, sid=sid, dir=s["direction"],
                                 confirm_bar=s["completed_bar_at"],
                                 result=s["order_result"], reason=s.get("block_reason", "")))
            for t in trades(j):       # chop_staged/an8.py 와 동일한 거래 묶기 (그 날 orders 만)
                ex.setdefault(t["sid"].replace(":BRK", ""),
                              {"net": t["krw"], "exit": "+".join(dict.fromkeys(t["reasons"])),
                               "day": t["day"], "netpct": t["net"], "hold": t["hold_min"]})
    return sigs, ex


TODAY = [
    dict(day="20261008", sid="20261008_092400_UP_RED:TW2_3SLOT_CONFIRM", dir="UP_RED",
         confirm_bar="092700", result="WAITING", reason="HISTORY_GAP"),
    dict(day="20261008", sid="20261008_093600_DOWN_BLUE:TW2_3SLOT_CONFIRM", dir="DOWN_BLUE",
         confirm_bar="093900", result="EXECUTED", reason=""),
    dict(day="20261008", sid="20261008_101200_UP_RED:TW2_3SLOT_CONFIRM", dir="UP_RED",
         confirm_bar="101500", result="EXECUTED", reason=""),
    dict(day="20261008", sid="20261008_110600_DOWN_BLUE:TW2_3SLOT_CONFIRM", dir="DOWN_BLUE",
         confirm_bar="110900", result="FILTERED_OUT", reason="TW2_3SLOT_REJECT_DAILY_SLOT_CAP"),
    dict(day="20261008", sid="20261008_130900_UP_RED:TW2_3SLOT_CONFIRM", dir="UP_RED",
         confirm_bar="131200", result="FILTERED_OUT", reason="TW2_REJECT_VWAP_VETO"),
    dict(day="20261008", sid="20261008_143000_DOWN_BLUE:TW2_3SLOT_CONFIRM", dir="DOWN_BLUE",
         confirm_bar="143300", result="FILTERED_OUT", reason="TW2_3SLOT_REJECT_DAILY_SLOT_CAP"),
]
TODAY_NET = {"20261008_093600_DOWN_BLUE:TW2_3SLOT_CONFIRM": -113928.0,
             "20261008_101200_UP_RED:TW2_3SLOT_CONFIRM": -90802.0}


def ema(a, n):
    return pd.Series(a).ewm(span=n, adjust=False).mean().to_numpy()


def macd_gap_3m(s):
    g = s.resample("3min").last().dropna()
    a = g.to_numpy()
    m = ema(a, 12) - ema(a, 26)
    return pd.Series(m - ema(m, 9), index=g.index)


BOXW = (20, 30, 40)
MIN_HIST = 25          # 당일 09:00 이후 완성봉 최소 개수 (20분 박스 + 여유)


def features(intra, warm, dec, direction):
    """intra = 당일 09:00~dec-1min, warm = 전일 정규장 + intra (지표 워밍업용)."""
    s = 1.0 if direction == "UP_RED" else -1.0
    if len(intra) < MIN_HIST:
        return None
    c = intra.close.to_numpy(); hi = intra.high.to_numpy(); lo = intra.low.to_numpy()
    px = c[-1]
    out = {"px": px, "n_hist": len(intra)}
    for w in BOXW:
        i0 = max(0, len(c) - w)
        seg = slice(i0, len(c) - 3)              # 박스 = 직전 w분 중 마지막 3분 제외
        bh, bl = hi[seg].max(), lo[seg].min()
        bm = (bh + bl) / 2.0
        bound = bh if s > 0 else bl
        out["bw%d" % w] = len(c) - 3 - i0        # 실제 박스 봉수
        out["box_rng%d" % w] = (bh - bl) / bm * 100
        out["brk%d" % w] = s * (px - bound) / bm * 100
        k = 0
        for i in range(len(c) - 1, -1, -1):
            if (c[i] > bound) if s > 0 else (c[i] < bound):
                k += 1
            else:
                break
        out["nout%d" % w] = k
        c0 = c[i0]
        dd = np.abs(np.diff(c[i0:]))
        out["net%d" % w] = s * (px - c0) / bm * 100
        out["peff%d" % w] = (s * (px - c0) / dd.sum()) if dd.sum() > 0 else 0.0
        out["nover%d" % w] = (s * (px - c0) / (bh - bl)) if bh > bl else 0.0
        out["boxhi%d" % w] = bh
        out["boxlo%d" % w] = bl
    nh = sum(1 for i in range(len(c) - 5, len(c)) if hi[i] >= hi[max(0, i - 5):i + 1].max())
    nl = sum(1 for i in range(len(c) - 5, len(c)) if lo[i] <= lo[max(0, i - 5):i + 1].min())
    out["hhll"] = s * (nh - nl)
    wc = warm.close.to_numpy()
    for n in (20, 50):
        e = ema(wc, n)
        out["ema%d_sl" % n] = s * (e[-1] - e[-6]) / e[-6] * 100
    out["ema_sep"] = s * (ema(wc, 20)[-1] - ema(wc, 50)[-1]) / px * 100
    g = macd_gap_3m(warm.set_index("datetime")["close"])
    if len(g) >= 3:
        out["gap"] = s * g.iloc[-1] / px * 1e4
        out["gap_d"] = s * (g.iloc[-1] - g.iloc[-2]) / px * 1e4
        out["gap_dd"] = s * (g.iloc[-1] - 2 * g.iloc[-2] + g.iloc[-3]) / px * 1e4
    else:
        out["gap"] = out["gap_d"] = out["gap_dd"] = np.nan
    return out


def forward(df, dec, direction, f):
    s = 1.0 if direction == "UP_RED" else -1.0
    fw = df[(df.datetime >= dec) & (df.datetime <= dec + pd.Timedelta(minutes=30))]
    if len(fw) < 5:
        return None
    px = f["px"]
    hi = fw.high.to_numpy(); lo = fw.low.to_numpy(); c = fw.close.to_numpy()
    r = {"nfwd": len(fw),
         "mfe30": (hi.max() - px) / px * 100 if s > 0 else (px - lo.min()) / px * 100,
         "mae30": (px - lo.min()) / px * 100 if s > 0 else (hi.max() - px) / px * 100,
         "fwdnet30": s * (c[-1] - px) / px * 100}
    for H, tag in ((60, "60"), (10 ** 4, "eod")):
        w = df[(df.datetime >= dec) & (df.datetime <= dec + pd.Timedelta(minutes=H))]
        w = w[w.datetime.dt.strftime("%H%M") <= "1520"]
        if len(w) >= 5:
            h2 = w.high.to_numpy(); l2 = w.low.to_numpy(); c2 = w.close.to_numpy()
            r["mfe" + tag] = (h2.max() - px) / px * 100 if s > 0 else (px - l2.min()) / px * 100
            r["mae" + tag] = (px - l2.min()) / px * 100 if s > 0 else (h2.max() - px) / px * 100
            r["fwdnet" + tag] = s * (c2[-1] - px) / px * 100
        else:
            r["mfe" + tag] = r["mae" + tag] = r["fwdnet" + tag] = np.nan
    fav = (hi - px) / px * 100 if s > 0 else (px - lo) / px * 100
    adv = (px - lo) / px * 100 if s > 0 else (hi - px) / px * 100
    up = np.where(fav >= 0.5)[0]
    dn = np.where(adv >= 0.5)[0]
    r["first05"] = 1 if (len(up) and (not len(dn) or up[0] < dn[0])) else (-1 if len(dn) else 0)
    bh, bl = f["boxhi30"], f["boxlo30"]
    inside = (c <= bh) & (c >= bl)
    r["reentry_min"] = int(np.argmax(inside)) if inside.any() else 99
    for k in (1, 2, 3):
        sub = fw[fw.datetime <= dec + pd.Timedelta(minutes=k)]
        r["reent%d" % k] = int(bool(len(sub)) and bool(((sub.close <= bh) & (sub.close >= bl)).any()))
        r["ext%d" % k] = (s * (sub.close.iloc[-1] - px) / px * 100) if len(sub) else np.nan
    return r


def main():
    sigs, ex = load_signals()
    sigs = [x for x in sigs if x["day"] >= "20260901"] + TODAY
    rows, skipped = [], []
    for g in sigs:
        df, src = _day(g["day"])
        if df is None:
            skipped.append((g["sid"], "NO_BARS")); continue
        cb = g["confirm_bar"]
        dec = pd.Timestamp("%s-%s-%s %s:%s:00" % (g["day"][:4], g["day"][4:6], g["day"][6:], cb[:2], cb[2:4])) \
            + pd.Timedelta(minutes=3)
        intra = df[df.datetime <= dec - pd.Timedelta(minutes=1)]
        pv = prev_day(g["day"])
        pdf = _day(pv)[0] if pv else None
        warm = pd.concat([pdf, intra]) if pdf is not None else intra
        f = features(intra, warm, dec, g["dir"])
        if f is None:
            skipped.append((g["sid"], "SHORT_HIST n=%d" % len(intra))); continue
        fw = forward(df, dec, g["dir"], f)
        if fw is None:
            skipped.append((g["sid"], "SHORT_FWD")); continue
        er = ex.get(g["sid"]) or ex.get(g["sid"] + ":BRK") or {}
        net = TODAY_NET.get(g["sid"], er.get("net", np.nan))
        rows.append(dict(g, dec=dec, src=src, warm=bool(pdf is not None), **f, **fw,
                         net_krw=net, exitr=er.get("exit", "")))
    d = pd.DataFrame(rows)
    d.to_csv(W + "/feat.csv", index=False, encoding="utf-8-sig")
    print("rows %d  days %d" % (len(d), d.day.nunique()))
    print(d.groupby("day").size().to_string())
    print("skipped %d" % len(skipped))
    for x in skipped:
        print("  ", x)
    return d


if __name__ == "__main__":
    main()
