# -*- coding: utf-8 -*-
"""WEAK-TP15 overlay 시뮬레이터. READ-ONLY. production 무수정.

weak regime 으로 판정된 거래만, net 이 thr 에 **최초로 닿는 1분봉**에서 전량청산.
strong 은 기존 N1+C1 결과 그대로.

진입집합/진입시각/진입가/사이징 규칙은 건드리지 않는다. 바뀌는 것은
exit_time / exit_price / exit_reason / net_pct 뿐이다.

주의 — 이게 사이징에 되먹임되는 유일한 경로: 그날 **첫 거래**의 exit_reason 이
STOP_LOSS 에서 TP15 로 바뀌면 W1a 의 POST_STOP(x1.20) 이 사라진다. size_chain 이
exit_reason 을 읽으므로 자동 반영된다(의도한 동작).
"""
from __future__ import annotations
import sys
sys.path.insert(0, r"C:\Users\FURSYS\Desktop\AI-GAP 2")
import t1_path as P

EXIT_TP15 = "WEAK_TP15_EXIT"


def _price_for_net(symbol, entry_price, target_net):
    """net(P) 는 P 의 1차식이므로 두 점으로 역산한다."""
    p0, p1 = entry_price, entry_price * 1.05
    n0, n1 = P.net_of(symbol, entry_price, p0), P.net_of(symbol, entry_price, p1)
    if abs(n1 - n0) < 1e-12:
        return p1
    return p0 + (target_net - n0) * (p1 - p0) / (n1 - n0)


def simulate(trades, regime_fn, thr=1.50, *, fill="bar_close", slip_pct=0.0,
             delay_bars=0):
    """반환: 새 거래 리스트(원본 불변) + 발동 진단 필드.

    fill:
      "touch"      thr 에 정확히 닿은 가격      (낙관)
      "bar_close"  도달 봉의 종가               (기본)
      "next_close" 그 다음 봉의 종가            (보수, 1분 지연)
    slip_pct   체결가를 그만큼 불리하게 (매도이므로 아래로)
    delay_bars 도달 후 N봉 뒤에 체결
    """
    out = []
    for t in trades:
        r = dict(t)
        r["tp15_fired"] = False
        r["tp15_touch_time"] = None
        r["tp15_touch_px"] = None
        r["tp15_fill_px"] = None
        r["regime_weak"] = bool(regime_fn(t))
        r["base_net_pct"] = t["net_pct"]
        r["base_exit_time"] = t["exit_time"]
        r["base_exit_reason"] = t["exit_reason"]
        if not r["regime_weak"]:
            out.append(r)
            continue
        i, row = P.first_touch(t, thr)
        if i is None:
            out.append(r)
            continue
        pth = P.path(t)
        j = min(i + max(0, int(delay_bars)), len(pth) - 1)
        if fill == "touch" and delay_bars == 0:
            px = _price_for_net(t["symbol"], t["entry_price"], thr)
            px = min(px, float(pth["high"].iloc[i]))
        elif fill == "next_close":
            j = min(i + 1 + max(0, int(delay_bars)), len(pth) - 1)
            px = float(pth["close"].iloc[j])
        else:
            px = float(pth["close"].iloc[j])
        px *= (1.0 - float(slip_pct) / 100.0)
        new_net = P.net_of(t["symbol"], t["entry_price"], px)
        r.update(
            tp15_fired=True,
            tp15_touch_time=str(pth["datetime"].iloc[i]),
            tp15_touch_px=float(pth["high"].iloc[i]),
            tp15_fill_px=px,
            exit_time=str(pth["datetime"].iloc[j]),
            exit_price=px,
            exit_reason=EXIT_TP15,
            net_pct=new_net,
        )
        out.append(r)
    out.sort(key=lambda x: (x["date"], x["entry_time"]))
    return out


def after_touch_stats(trade, thr=1.50):
    """도달 이후 경로 통계 — 반납/추가상승 분석용. (도달 안 하면 None)"""
    i, _ = P.first_touch(trade, thr)
    if i is None:
        return None
    pth = P.path(trade)
    sym, ep = trade["symbol"], trade["entry_price"]
    after = pth.iloc[i:]
    hi = max(P.net_of(sym, ep, float(h)) for h in after["high"])
    lo = min(P.net_of(sym, ep, float(l)) for l in after["low"])
    return {"touch_idx": i, "bars_after": len(after) - 1,
            "max_after": hi, "min_after": lo,
            "extra_up": hi - thr, "giveback": thr - lo,
            "ended_below_0": trade["net_pct"] <= 0.0,
            "ended_below_thr": trade["net_pct"] < thr,
            "final_net": trade["net_pct"]}
