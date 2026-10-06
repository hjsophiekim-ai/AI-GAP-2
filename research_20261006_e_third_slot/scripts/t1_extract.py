"""E 3번째 거래 표본 + 진입시점 feature. READ-ONLY.

feature 는 전부 '3번째 진입 판정 시점에 이미 알 수 있던 값' 만 쓴다(미래정보 없음).
기존 로직에서 나온 값 + 그날 앞선 거래 흐름 + 1분봉으로 계산한 시장상태.
"""
import glob, json, os, sys
import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
O = os.path.dirname(os.path.abspath(__file__))
SPD = os.path.dirname(O)
ST = SPD + "/wt_reftp/research_20261004_chop_staged"
src = open(ST + "/an8.py", encoding="utf-8").read().split("DAYS = sorted(")[0]
ns = {"__file__": ST + "/an8.py"}
exec(compile(src, "an8lib", "exec"), ns)
trades = ns["trades"]
BUDGET = 10_000_000.0

files = sorted(glob.glob(SPD + "/eprod/out_merged/REAL_EPROD_*.json"))
files.append(SPD + "/today1006/out/REAL_EPROD_20261006.json")


def hy_frame(d):
    for base in (ST + "/wk/data", SPD + "/today1006/wk/data"):
        p = f"{base}/replay_{d}_hynix_1m.csv"
        if os.path.exists(p):
            x = pd.read_csv(p)
            x["datetime"] = pd.to_datetime(x["datetime"].astype(str).str[:19])
            return x
    return None


rows, daytab = [], []
for f in files:
    d = os.path.basename(f)[-13:-5]
    o = json.load(open(f, encoding="utf-8"))
    tt = sorted(trades(o), key=lambda t: t["entry"])
    ent = {e["t"][11:19]: e for e in o.get("entries", [])}
    daytab.append(dict(day=d, n=len(tt), pnl=sum(t["krw"] for t in tt)))
    if len(tt) < 3:
        continue
    t1, t2, t3 = tt[0], tt[1], tt[2]
    e3 = ent.get(t3["entry"].strftime("%H:%M:%S"), {})
    entry = t3["entry"].tz_localize(None) if t3["entry"].tzinfo is None else t3["entry"].tz_convert("Asia/Seoul").tz_localize(None)
    prev_exit = max(t1["exit"], t2["exit"])
    prev_exit = prev_exit.tz_convert("Asia/Seoul").tz_localize(None) if prev_exit.tzinfo else prev_exit
    # 1분봉 시장상태 (진입 시각 이전에 완성된 봉만)
    hy = hy_frame(d)
    mv_open = vwap_dist = rng30 = xc30 = np.nan
    if hy is not None:
        done = hy[(hy["datetime"] + pd.Timedelta(minutes=1) <= entry) & (hy["datetime"].dt.date == entry.date())]
        sess = done[done["datetime"].dt.time >= pd.Timestamp("09:00").time()]
        if len(sess) >= 30:
            c = sess["close"].astype(float).to_numpy()
            mv_open = (c[-1] / float(sess["open"].iloc[0]) - 1) * 100
            v = sess["volume"].astype(float).to_numpy()
            vwap = (c * v).sum() / max(v.sum(), 1)
            vwap_dist = (c[-1] / vwap - 1) * 100
            w = sess.tail(30)
            rng30 = (float(w["high"].max()) - float(w["low"].min())) / c[-1] * 100
            e20 = pd.Series(c).ewm(span=20, adjust=False).mean().to_numpy()
            s = np.sign(c - e20)[-30:]
            s = s[s != 0]
            xc30 = int((np.diff(s) != 0).sum()) if len(s) > 1 else 0
    up = t3["dir"].startswith("UP")
    notional = t3["q"] * t3["px_in"]
    rows.append(dict(
        day=d, t=t3["entry"].strftime("%H:%M"), up=up,
        krw=t3["krw"], net=t3["net"], reasons="+".join(dict.fromkeys(t3["reasons"])),
        # 기존 로직 값
        wait_fill=str(t3["sid"]).endswith(":E_BRK"),
        regime=e3.get("regime"), trend_ok=e3.get("trend_ok"),
        toxic=notional < 0.4 * BUDGET, rs_boost=notional > 1.2 * BUDGET,
        afternoon=t3["entry"].hour >= 12,
        # 그날 흐름
        same_dir_as_2=(t2["dir"] == t3["dir"]), same_dir_as_1=(t1["dir"] == t3["dir"]),
        pnl_before=t1["krw"] + t2["krw"], t2_win=t2["krw"] > 0, t1_win=t1["krw"] > 0,
        t2_stop=any("SL" in r or "STOP" in r for r in t2["reasons"]),
        min_since_exit=(entry - prev_exit).total_seconds() / 60,
        # 시장상태 (방향 정렬: 진입 방향으로 유리하면 +)
        mv_open_dir=(mv_open if up else -mv_open), vwap_dir=(vwap_dist if up else -vwap_dist),
        rng30=rng30, xc30=xc30,
    ))
df = pd.DataFrame(rows)
df.to_csv(O + "/third_trades.csv", index=False, encoding="utf-8-sig")
dt = pd.DataFrame(daytab)
print(f"대상 {len(dt)}일 · 3번째 거래 {len(df)}건 · 승 {(df.krw > 0).sum()} / 패 {(df.krw < 0).sum()} · "
      f"합계 {df.krw.sum():+,.0f} · 평균 {df.krw.mean():+,.0f} · 중앙 {df.krw.median():+,.0f}")
pd.set_option("display.width", 250)
print(df[["day", "t", "up", "krw", "net", "reasons", "wait_fill", "regime", "trend_ok", "toxic", "afternoon",
          "same_dir_as_2", "pnl_before", "t2_win", "min_since_exit", "mv_open_dir", "vwap_dir", "rng30", "xc30"]]
      .to_string(index=False, float_format=lambda v: f"{v:,.2f}"))
