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


def write(path, frames):
    with open(path, "w", encoding="utf-8") as fh:
        for t, f in frames:
            fh.write(f"{t.isoformat(timespec='milliseconds')}\t{f}\n")
