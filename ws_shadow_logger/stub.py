"""장외 테스트용 합성 프레임 (실제 KIS 형식·필드 수 그대로, 값만 가짜)."""
from __future__ import annotations

import json
import random
from datetime import datetime, timedelta

from .schema import COLUMNS


def _frame(tr, recs):
    cols = COLUMNS[tr]
    body = "^".join("^".join(str(r.get(c, "0")) for c in cols) for r in recs)
    return f"0|{tr}|{len(recs):03d}|{body}"


def trade(tr, sym, t, px, vol, side, cttr, buy_cum, sell_cum, acml):
    side_col = "CCLD_DVSN" if tr == "H0STCNT0" else "CNTG_CLS_CODE"
    return _frame(tr, [{"MKSC_SHRN_ISCD": sym, "STCK_CNTG_HOUR": t.strftime("%H%M%S"), "STCK_PRPR": px,
                        "CNTG_VOL": vol, side_col: side, "CTTR": cttr, "SHNU_CNTG_SMTN": buy_cum,
                        "SELN_CNTG_SMTN": sell_cum, "ACML_VOL": acml, "BSOP_DATE": t.strftime("%Y%m%d")}])


def book(tr, sym, t, bid1, tick, bq, aq):
    r = {"MKSC_SHRN_ISCD": sym, "BSOP_HOUR": t.strftime("%H%M%S"), "HOUR_CLS_CODE": "0",
         "TOTAL_BIDP_RSQN": sum(bq), "TOTAL_ASKP_RSQN": sum(aq)}
    for i in range(10):
        r[f"BIDP{i + 1}"] = bid1 - i * tick; r[f"ASKP{i + 1}"] = bid1 + (i + 1) * tick
        r[f"BIDP_RSQN{i + 1}"] = bq[i]; r[f"ASKP_RSQN{i + 1}"] = aq[i]
    return _frame(tr, [r])


def session(start: datetime, seconds=30, seed=0):
    """(recv_ts, frame) 목록. 3종목 체결·호가 + 구독응답 + PINGPONG."""
    rng = random.Random(seed); out = []
    for tr, sym in (("H0UNCNT0", "000660"), ("H0UNASP0", "000660"), ("H0STCNT0", "0193T0"),
                    ("H0STASP0", "0193T0"), ("H0STCNT0", "0197X0"), ("H0STASP0", "0197X0")):
        out.append((start, json.dumps({"header": {"tr_id": tr, "tr_key": sym, "encrypt": "N"},
                                       "body": {"rt_cd": "0", "msg_cd": "OPSP0000", "msg1": "SUBSCRIBE SUCCESS"}})))
    st = {"000660": [1_420_000, 1000, "H0UNCNT0", "H0UNASP0"], "0193T0": [6_750, 5, "H0STCNT0", "H0STASP0"],
          "0197X0": [5_770, 5, "H0STCNT0", "H0STASP0"]}
    cum = {s: [0, 0] for s in st}
    for k in range(seconds * 4):
        t = start + timedelta(milliseconds=250 * k + rng.randint(0, 200))
        for sym, (px, tick, ctr, atr) in st.items():
            if rng.random() < 0.6:
                side = rng.choice(["1", "5"]); v = rng.randint(1, 50)
                cum[sym][0 if side == "1" else 1] += v
                cttr = round(cum[sym][0] / max(1, cum[sym][1]) * 100, 2)
                out.append((t, trade(ctr, sym, t, px, v, side, cttr, cum[sym][0], cum[sym][1], sum(cum[sym]))))
            if rng.random() < 0.5:
                out.append((t, book(atr, sym, t, px, tick, [rng.randint(100, 900) for _ in range(10)],
                                    [rng.randint(100, 900) for _ in range(10)])))
        if k == 40:
            out.append((t, json.dumps({"header": {"tr_id": "PINGPONG", "datetime": t.strftime("%Y%m%d%H%M%S")}})))
    return sorted(out, key=lambda x: x[0])


def mu_trade(feed, t_kst, px, evol, tvol, recs=1):
    """HDFSCNT0 레코드(25필드). t_kst = 한국시각. 현지(ET) 시각은 -13시간(서머타임)."""
    loc = t_kst - timedelta(hours=13)
    r = {"SYMB": feed, "ZDIV": "4", "TYMD": loc.strftime("%Y%m%d"), "XYMD": loc.strftime("%Y%m%d"),
         "XHMS": loc.strftime("%H%M%S"), "KYMD": t_kst.strftime("%Y%m%d"), "KHMS": t_kst.strftime("%H%M%S"),
         "OPEN": px, "HIGH": px, "LOW": px, "LAST": px, "SIGN": "2", "DIFF": "1.0", "RATE": "0.10",
         "PBID": px - 0.01, "PASK": px + 0.01, "VBID": 10, "VASK": 12, "EVOL": evol, "TVOL": tvol,
         "TAMT": round(px * tvol), "BIVL": 0, "ASVL": 0, "STRN": "101.5", "MTYP": "1"}
    return _frame("HDFSCNT0", [r] * recs)


def mu_session(start: datetime, minutes=5, seed=0, feed="RBAQMU", per_min=6, lag_ms=300):
    """(recv_ts, frame). 분마다 per_min 건, 수신은 체결시각 + lag_ms."""
    rng = random.Random(seed); out = []; px = 120.0; tvol = 0
    for m in range(minutes):
        for k in range(per_min):
            t = start + timedelta(minutes=m, seconds=int(k * 60 / per_min) + rng.randint(0, 5))
            px = round(px + rng.choice([-0.05, 0, 0.05]), 2); v = rng.randint(1, 30); tvol += v
            out.append((t + timedelta(milliseconds=lag_ms), mu_trade(feed, t, px, v, tvol)))
    return sorted(out, key=lambda x: x[0])


def write(path, frames):
    with open(path, "w", encoding="utf-8") as fh:
        for t, f in frames:
            fh.write(f"{t.isoformat(timespec='milliseconds')}\t{f}\n")
