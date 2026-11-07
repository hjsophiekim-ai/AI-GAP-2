"""괴리 × 레짐 검증용 모집단 — 그날 **모든** 플래그를 뽑는다 (MU CONFIRM 한정 아님).

레짐 라벨은 전부 T+3 **이전** 데이터로만 만든다(미래 데이터 금지):
  er           : 09:00~T+3 하이닉스 효율비 |순변화| / Σ|1분변화|  (1 에 가까울수록 추세)
  net_pct      : 09:00~T+3 하이닉스 방향부호 변화율 (+면 이미 그 방향으로 갔다 = 추세 추종)
  n_flag_before: 그날 T+3 이전에 이미 난 플래그 수 (많을수록 왕복)
  prev_er      : 직전 3거래일의 당일 종가기준 효율비 평균 (당일 데이터 안 씀 = 선행 라벨)
괴리 지표는 production mu_catchup.evaluate 의 mu_hy_gap / mu_session_pct 를 그대로 쓴다.
"""
import csv
import sys
from datetime import datetime, timedelta

import cx_lib as X  # noqa: F401
import pandas as pd
import wx_lib as W
from app.trading.macd2 import mu_assist, mu_catchup
from app.trading.macd2.models import Direction

sys.stdout.reconfigure(encoding="utf-8")

DAYS = ["2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23",
        "2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30",
        "2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06"]
LEDGER_DAYS = {"2026-11-05", "2026-11-06"}

COLS = ["day", "flag_at", "t3_at", "dir", "session", "mu_status",
        "mu_session_pct", "hy_session_pct", "mu_hy_gap",
        "er", "net_pct", "n_flag_before", "flag_seq", "since_opp_min", "prev_er",
        "entry_px", "r15", "r30", "mfe", "mae", "touch"]


def day_er(hy, start, end, sign=1.0):
    """[start, end) 에 끝난 1분봉 종가로 효율비와 방향부호 변화율."""
    seg = hy[(hy["datetime"] >= start) & (hy["datetime"] + pd.Timedelta(minutes=1) <= end)]
    c = pd.to_numeric(seg["close"], errors="coerce").dropna()
    if len(c) < 5:
        return None, None
    diff = c.diff().abs().sum()
    if diff <= 0:
        return None, None
    net = float(c.iloc[-1]) - float(c.iloc[0])
    return round(abs(net) / float(diff), 4), round(sign * net / float(c.iloc[0]) * 100, 3)


prev_ers = []
with open("out_regime.csv", "w", newline="", encoding="utf-8-sig") as fo:
    w = csv.DictWriter(fo, fieldnames=COLS)
    w.writeheader()
    for day in DAYS:
        led = day in LEDGER_DAYS
        hy = W.load_1m(day, "hynix", prefer_snapshot=led)
        mus = W.mu_series(day)
        if hy.empty or mus is None:
            print(day, "데이터 없음", file=sys.stderr)
            continue
        open_t = datetime.fromisoformat(f"{day}T09:00:00+09:00")
        close_t = datetime.fromisoformat(f"{day}T15:30:00+09:00")
        pe = round(sum(prev_ers[-3:]) / len(prev_ers[-3:]), 4) if prev_ers else ""
        flags = W.flags_of_day(hy, day)
        etfs = {}
        prev_dir = {}
        for seq, (dt, d) in enumerate(flags, 1):
            last_opp = prev_dir.get(Direction.DOWN_BLUE if d == Direction.UP_RED else Direction.UP_RED)
            prev_dir[d] = dt
            t3 = dt + timedelta(minutes=6)
            if t3 > close_t:
                continue
            g = W.hy_gaps(hy, dt, d)
            st, _, _ = W.mu_status(mus, d, dt, g)
            cu = mu_catchup.evaluate(d, dt, t3, hy, mu_series=mus,
                                     cttr={"cttr": None, "state": "UNAVAILABLE"})
            sign = 1.0 if d == Direction.UP_RED else -1.0
            er, net = day_er(hy, open_t, t3, sign)
            k = "long" if d == Direction.UP_RED else "inverse"
            if k not in etfs:
                etfs[k] = W.load_1m(day, k, prefer_snapshot=led)
            row = {"day": day, "flag_at": f"{dt:%H:%M}", "t3_at": f"{t3:%H:%M}", "dir": d.value,
                   "session": "AM" if t3.time() < datetime.strptime("11:00", "%H:%M").time() else "PM",
                   "mu_status": st,
                   "mu_session_pct": "" if cu["mu_session_pct"] is None else cu["mu_session_pct"],
                   "hy_session_pct": "" if cu["hy_session_pct"] is None else cu["hy_session_pct"],
                   "mu_hy_gap": "" if cu["mu_hy_gap"] is None else cu["mu_hy_gap"],
                   "er": "" if er is None else er, "net_pct": "" if net is None else net,
                   "n_flag_before": seq - 1, "flag_seq": seq,
                   "since_opp_min": "" if last_opp is None else int((dt - last_opp).total_seconds() // 60),
                   "prev_er": pe}
            f = W.forward(etfs[k], t3, horizon_min=(15, 30))
            row.update(f)
            if f:
                fut = etfs[k][etfs[k]["datetime"] >= t3]
                e = f["entry_px"]
                row["mfe"] = round((float(fut["high"].max()) / e - 1) * 100, 3)
                row["mae"] = round((float(fut["low"].min()) / e - 1) * 100, 3)
            w.writerow({c: row.get(c, "") for c in COLS})
        der, _ = day_er(hy, open_t, close_t)
        if der is not None:
            prev_ers.append(der)
        del hy, mus, etfs, flags
        fo.flush()
        print("done", day, file=sys.stderr)
