"""종목별 상태 → 1초 스냅샷 (순수 로직, 네트워크/파일 없음).

스냅샷 행(종목 1개, 초 경계 ts 에 대해 (ts-1s, ts] 구간 집계):
  ts, symbol, price, best_bid, best_ask, spread, spread_bp,
  bid_qty_1..10, ask_qty_1..10, bid_total, ask_total,
  imb_1, imb_5, imb_10, imb_total             imbalance = (bid-ask)/(bid+ask), 레벨 1 / 1~5 / 1~10 / 총잔량
  cttr                                        체결강도 (KIS CTTR, 마지막 체결 기준)
  buy_vol_1s, sell_vol_1s, trades_1s          CCLD_DVSN/CNTG_CLS_CODE 1=매수 5=매도
  buy_vol_cum, sell_vol_cum                   SHNU_CNTG_SMTN / SELN_CNTG_SMTN (당일 누적)
  buy_ratio_5s, buy_ratio_10s                 최근 5/10초 매수체결량 / (매수+매도)
  trades_per_s_5, trades_per_s_10, vol_per_s_5, vol_per_s_10   체결속도
  d_imb1_{1,5,10}s, d_imb5_{1,5,10}s, d_cttr_{1,5,10}s         최근 1/5/10초 변화 (현재 - k초 전 스냅샷)
  acml_vol, book_age_s, trade_age_s, exch_book_hour, exch_trade_hour
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta

from .config import MAX_HISTORY_SEC
from .parser import num

WINDOWS = (1, 5, 10)
#: 메모리 상한 — 시간(최근 MAX_HISTORY_SEC 초)과 개수 둘 다로 묶는다. 장중 내내 커지지 않는다.
MAX_TRADES = 4096
MAX_HIST = MAX_HISTORY_SEC + 4


def _imb(b, a):
    s = b + a
    return (b - a) / s if s > 0 else None


class SymbolState:
    def __init__(self, symbol: str):
        self.symbol = symbol
        self.book = None            # dict
        self.book_at = None
        self.trade_at = None
        self.last_price = None
        self.cttr = None
        self.buy_cum = self.sell_cum = None
        self.acml_vol = None
        self.exch_trade_hour = self.exch_book_hour = None
        self.trades = deque(maxlen=MAX_TRADES)   # (recv_ts, vol, side) — 최근 체결만
        self.hist = deque(maxlen=MAX_HIST)      # (ts, imb_1, imb_5, cttr) — 지난 스냅샷 (델타용)

    def on_book(self, rec: dict, recv: datetime):
        bq = [num(rec.get(f"BIDP_RSQN{i}"), 0.0) for i in range(1, 11)]
        aq = [num(rec.get(f"ASKP_RSQN{i}"), 0.0) for i in range(1, 11)]
        self.book = dict(bid=num(rec.get("BIDP1")), ask=num(rec.get("ASKP1")), bq=bq, aq=aq,
                         bt=num(rec.get("TOTAL_BIDP_RSQN"), sum(bq)), at=num(rec.get("TOTAL_ASKP_RSQN"), sum(aq)))
        self.book_at = recv
        self.exch_book_hour = rec.get("BSOP_HOUR")

    def on_trade(self, rec: dict, recv: datetime):
        self.last_price = num(rec.get("STCK_PRPR"), self.last_price)
        self.cttr = num(rec.get("CTTR"), self.cttr)
        self.buy_cum = num(rec.get("SHNU_CNTG_SMTN"), self.buy_cum)
        self.sell_cum = num(rec.get("SELN_CNTG_SMTN"), self.sell_cum)
        self.acml_vol = num(rec.get("ACML_VOL"), self.acml_vol)
        side = rec.get("CCLD_DVSN", rec.get("CNTG_CLS_CODE"))
        self.trades.append((recv, num(rec.get("CNTG_VOL"), 0.0), "B" if side == "1" else ("S" if side == "5" else "?")))
        self.trade_at = recv
        self.exch_trade_hour = rec.get("STCK_CNTG_HOUR")

    def snapshot(self, ts: datetime) -> dict:
        while self.trades and self.trades[0][0] <= ts - timedelta(seconds=max(WINDOWS)):
            self.trades.popleft()

        def win(k):
            lo = ts - timedelta(seconds=k)
            xs = [x for x in self.trades if lo < x[0] <= ts]
            b = sum(v for _, v, s in xs if s == "B"); s_ = sum(v for _, v, s in xs if s == "S")
            return xs, b, s_

        row = dict(ts=ts.isoformat(timespec="seconds"), symbol=self.symbol, price=self.last_price)
        bk = self.book
        if bk:
            row.update(best_bid=bk["bid"], best_ask=bk["ask"])
            if bk["bid"] and bk["ask"]:
                row["spread"] = bk["ask"] - bk["bid"]
                row["spread_bp"] = row["spread"] / ((bk["ask"] + bk["bid"]) / 2) * 1e4
            for i in range(10):
                row[f"bid_qty_{i + 1}"] = bk["bq"][i]; row[f"ask_qty_{i + 1}"] = bk["aq"][i]
            row.update(bid_total=bk["bt"], ask_total=bk["at"],
                       imb_1=_imb(bk["bq"][0], bk["aq"][0]), imb_5=_imb(sum(bk["bq"][:5]), sum(bk["aq"][:5])),
                       imb_10=_imb(sum(bk["bq"]), sum(bk["aq"])), imb_total=_imb(bk["bt"], bk["at"]))
        xs1, b1, s1 = win(1)
        row.update(cttr=self.cttr, buy_vol_1s=b1, sell_vol_1s=s1, trades_1s=len(xs1),
                   buy_vol_cum=self.buy_cum, sell_vol_cum=self.sell_cum, acml_vol=self.acml_vol)
        for k in (5, 10):
            xs, b, s_ = win(k)
            row[f"buy_ratio_{k}s"] = b / (b + s_) if (b + s_) > 0 else None
            row[f"trades_per_s_{k}"] = len(xs) / k
            row[f"vol_per_s_{k}"] = sum(v for _, v, _ in xs) / k
        cur = (ts, row.get("imb_1"), row.get("imb_5"), self.cttr)
        for k in WINDOWS:
            past = next((h for h in reversed(self.hist) if h[0] <= ts - timedelta(seconds=k)), None)
            for j, nm in ((1, "imb1"), (2, "imb5"), (3, "cttr")):
                a, p = cur[j], (past[j] if past else None)
                row[f"d_{nm}_{k}s"] = (a - p) if (a is not None and p is not None) else None
        self.hist.append(cur)
        while self.hist and self.hist[0][0] < ts - timedelta(seconds=max(WINDOWS) + 2):
            self.hist.popleft()
        row["book_age_s"] = (ts - self.book_at).total_seconds() if self.book_at else None
        row["trade_age_s"] = (ts - self.trade_at).total_seconds() if self.trade_at else None
        row["exch_book_hour"] = self.exch_book_hour; row["exch_trade_hour"] = self.exch_trade_hour
        return row


SNAPSHOT_COLUMNS = (["ts", "symbol", "price", "best_bid", "best_ask", "spread", "spread_bp"]
                    + [f"bid_qty_{i}" for i in range(1, 11)] + [f"ask_qty_{i}" for i in range(1, 11)]
                    + ["bid_total", "ask_total", "imb_1", "imb_5", "imb_10", "imb_total", "cttr",
                       "buy_vol_1s", "sell_vol_1s", "trades_1s", "buy_vol_cum", "sell_vol_cum", "acml_vol",
                       "buy_ratio_5s", "buy_ratio_10s", "trades_per_s_5", "trades_per_s_10", "vol_per_s_5", "vol_per_s_10"]
                    + [f"d_{n}_{k}s" for k in WINDOWS for n in ("imb1", "imb5", "cttr")]
                    + ["book_age_s", "trade_age_s", "exch_book_hour", "exch_trade_hour"])
