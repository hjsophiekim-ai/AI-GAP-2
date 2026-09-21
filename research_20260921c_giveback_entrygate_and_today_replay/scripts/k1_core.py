# -*- coding: utf-8 -*-
"""KRW 예산 시뮬레이션 코어 — READ-ONLY 연구. production 무수정.

근거(코드로 확인):
  1) trading_cost_engine: 수수료/세금/청산/슬리피지가 전부 quantity 에 비례하고
     min_commission_krw = 0 -> net_pct 는 주문수량과 무관하다.
  2) hengine5: w1a / mult / w1a_exposure 가 진입·청산 판정에 되먹임되지 않는다.
     (w1a_seq 와 w1a_first_stop 도 사이징 규칙 전용)
  => 사이징 변형은 기준 거래목록에서 해석적으로 정확히 재계산할 수 있다.
"""
from __future__ import annotations
import csv, io
from pathlib import Path
from collections import defaultdict

PROJ = Path(r"C:\Users\FURSYS\Desktop\AI-GAP 2")
CSV_C1 = PROJ / "data/validation/macd2/n1_production_20260920/trades_N1_plus_C1.csv"
CSV_N1 = PROJ / "data/validation/macd2/n1_production_20260920/trades_N1_production.csv"

# production 상수 (app/trading/macd2/config.py)
CHOP_MULT = 0.80
POST_STOP_MULT = 1.20
MIN_MULT = 0.25
MAX_MULT = 1.50
DAILY_EXPOSURE_CAP = 3.00
EXIT_STOP_LOSS = "TIME_WINDOW_STOP_LOSS"

# 실거래 예산 계약
BASE_BUDGET = 10_000_000.0      # config.DEFAULT_BUDGET
DAILY_CAPITAL = 30_000_000.0    # = BASE_BUDGET * DAILY_EXPOSURE_CAP

MORNING = "MORNING"
AFTERNOON = "AFTERNOON"


def load(path=CSV_C1) -> list:
    rows = list(csv.DictReader(io.open(path, encoding="utf-8-sig")))
    out = []
    for r in rows:
        out.append({
            "date": r["date"], "session": r["session"],
            "slot": int(r["slot_number"]), "direction": r["direction"],
            "entry_time": r["entry_time"], "exit_time": r["exit_time"],
            "entry_price": float(r["entry_price"]), "exit_price": float(r["exit_price"]),
            "exit_reason": r["exit_reason"], "net_pct": float(r["net_pct"]),
            "w1a_ref": float(r["w1a"]), "peak_net_pct": float(r["peak_net_pct"]),
            "mae_net_pct": float(r["mae_net_pct"]),
            "entry_chop": r["entry_chop"] == "True",
            "tp1_hit": r["tp1_hit"] == "True",
            "hold_minutes": float(r["hold_minutes"] or 0.0),
            "symbol": "0193T0" if r["direction"] == "UP_RED" else "0197X0",
        })
    out.sort(key=lambda t: (t["date"], t["entry_time"]))
    return out


def dates_of(trades) -> list:
    return sorted({t["date"] for t in trades})


def size_chain(trades, extra_fn=None, *, order="pre_clip",
               daily_capital=DAILY_CAPITAL, base_budget=BASE_BUDGET,
               cash_reuse=False, net_key="net_pct", extra_of=None):
    """production position_sizing 체인 + KRW 예산 계약을 그대로 재현.

    extra_fn(slot, session, date) -> P 후보 배수 (None 이면 현행 = 1.0).
    order="pre_clip"  : clip(규칙배수 x extra) -> 일노출상한      (연구/훅 방식)
    order="post_clip" : clip(규칙배수) x extra -> 일노출상한      (사용자 명시 순서)
    cash_reuse=False  : 당일 신규진입 누적 원금 기준 (production 계약)
    cash_reuse=True   : 청산된 원금을 같은 날 재사용 (별도 보고용)
    """
    out = []
    by_day = defaultdict(list)
    for t in trades:
        by_day[t["date"]].append(t)
    for day in sorted(by_day):
        exposure = 0.0
        seq = 0
        first_stop = False
        used_krw = 0.0
        day_rows = []
        for t in sorted(by_day[day], key=lambda x: x["entry_time"]):
            released_krw = 0.0
            if cash_reuse:
                released_krw = sum(o["actual_krw"] for o in day_rows
                                   if o["exit_time"] <= t["entry_time"])
            if extra_of is not None:
                ex = float(extra_of(t))
            elif extra_fn is None:
                ex = 1.0
            else:
                ex = float(extra_fn(t["slot"], t["session"], day))
            rules = 1.0
            if t["entry_chop"]:
                rules *= CHOP_MULT
            if first_stop:
                rules *= POST_STOP_MULT
            if order == "post_clip":
                clipped = max(MIN_MULT, min(MAX_MULT, rules)) * ex
            else:
                clipped = max(MIN_MULT, min(MAX_MULT, rules * ex))
            room = DAILY_EXPOSURE_CAP - exposure
            applied = 0.0 if room <= 0 else min(clipped, room)
            expo_capped = bool(room <= 0 or clipped > room + 1e-12)

            # ---- KRW 예산 계약 -------------------------------------------
            strategy_krw = base_budget * clipped          # 전략상 계산 주문금액
            remaining = max(0.0, daily_capital - used_krw + released_krw)
            actual_krw = min(strategy_krw, remaining)
            cut_krw = strategy_krw - actual_krw
            budget_capped = cut_krw > 1e-6
            qty = int(actual_krw // t["entry_price"]) if t["entry_price"] > 0 else 0
            notional = qty * t["entry_price"]
            pnl_krw = notional * t[net_key] / 100.0
            used_krw += actual_krw

            r = dict(t)
            r.update(
                w1a_rules=rules, w1a_extra=ex, w1a_clipped=clipped,
                w1a_room=room, w1a_applied=applied, expo_capped=expo_capped,
                strategy_krw=strategy_krw, remaining_krw=remaining,
                actual_krw=actual_krw, cut_krw=cut_krw,
                budget_capped=budget_capped, qty=qty, notional_krw=notional,
                pnl_krw=pnl_krw, day_used_after=used_krw, seq=seq + 1,
                net_used=t[net_key],
            )
            day_rows.append(r)
            exposure += applied
            seq += 1
            if seq == 1 and t["exit_reason"] == EXIT_STOP_LOSS:
                first_stop = True
        out.extend(day_rows)
    out.sort(key=lambda t: (t["date"], t["entry_time"]))
    return out


# ── 지표 ────────────────────────────────────────────────────────────────
def krw_pnl(trades, dates=None) -> float:
    ds = set(dates) if dates is not None else None
    return sum(t["pnl_krw"] for t in trades if ds is None or t["date"] in ds)


def daily_krw(trades, dates) -> dict:
    ds = set(dates); d = {x: 0.0 for x in dates}
    for t in trades:
        if t["date"] in ds:
            d[t["date"]] += t["pnl_krw"]
    return d


def compound_pct(trades, dates):
    """엔진과 동일한 복리 정의: (1 + net*w1a/100) 누적 (노출=자기자본 비율)."""
    ds = set(dates); eq = 1.0
    for t in sorted((x for x in trades if x["date"] in ds), key=lambda x: x["exit_time"]):
        eq *= (1 + t["net_used"] * t["w1a_applied"] / 100.0)
    return (eq - 1) * 100.0


def krw_compound(trades, dates, start=DAILY_CAPITAL):
    """KRW 복리: 자기자본이 커지면 base_budget 도 같은 비율로 커진다고 볼 때."""
    ds = set(dates); eq = 1.0
    for t in sorted((x for x in trades if x["date"] in ds), key=lambda x: x["exit_time"]):
        # 자기자본이 eq 배가 되면 주문금액(notional)도 eq 배로 키운다
        eq *= (1 + t["notional_krw"] * t["net_used"] / 100.0 / start)
    return (eq - 1) * 100.0


def pf(trades, dates):
    ds = set(dates)
    g = sum(t["pnl_krw"] for t in trades if t["date"] in ds and t["pnl_krw"] > 0)
    l = -sum(t["pnl_krw"] for t in trades if t["date"] in ds and t["pnl_krw"] < 0)
    return (g / l) if l > 0 else None


def mdd_krw(trades, dates):
    """고정 3,000만원 기준 누적 KRW 곡선의 최대낙폭 (기준자금 대비 %)."""
    d = daily_krw(trades, dates)
    eq = 0.0; peak = 0.0; mdd = 0.0
    for day in dates:
        eq += d[day]
        peak = max(peak, eq)
        mdd = min(mdd, eq - peak)
    return mdd / DAILY_CAPITAL * 100.0


def excl_top_krw(trades, dates, k):
    ds = set(dates)
    ts = [t for t in trades if t["date"] in ds]
    top = sorted(ts, key=lambda x: x["pnl_krw"], reverse=True)[:k]
    tid = {id(x) for x in top}
    return sum(t["pnl_krw"] for t in ts if id(t) not in tid)


def budget_stats(trades, dates):
    ds = set(dates)
    used = {d: 0.0 for d in dates}
    for t in trades:
        if t["date"] in ds:
            used[t["date"]] += t["actual_krw"]
    vals = [used[d] for d in dates]
    unused = [DAILY_CAPITAL - v for v in vals]
    srt = sorted(unused)
    med = srt[len(srt) // 2] if srt else 0.0
    return {
        "avg_used": sum(vals) / len(vals) if vals else 0.0,
        "max_used": max(vals) if vals else 0.0,
        "avg_unused": sum(unused) / len(unused) if unused else 0.0,
        "med_unused": med, "max_unused": max(unused) if unused else 0.0,
        "util_pct": (sum(vals) / (DAILY_CAPITAL * len(vals)) * 100.0) if vals else 0.0,
        "n_cash_5m": sum(1 for u in unused if u >= 5_000_000),
        "n_cash_10m": sum(1 for u in unused if u >= 10_000_000),
        "cap_hits": sum(1 for t in trades if t["date"] in ds and t["budget_capped"]),
        "expo_cap_hits": sum(1 for t in trades if t["date"] in ds and t["expo_capped"]),
        "over_attempts": sum(1 for t in trades if t["date"] in ds
                             and t["strategy_krw"] > t["remaining_krw"] + 1e-6),
        "max_over_day": max(vals) if vals else 0.0,
    }


def summary(trades, dates):
    return {
        "n": sum(1 for t in trades if t["date"] in set(dates)),
        "krw": krw_pnl(trades, dates),
        "ret_pct": krw_pnl(trades, dates) / DAILY_CAPITAL * 100.0,
        "compound_pct": compound_pct(trades, dates),
        "pf": pf(trades, dates),
        "mdd_pct": mdd_krw(trades, dates),
        "top10x": excl_top_krw(trades, dates, 10),
    }


# ── P 후보 정의 ─────────────────────────────────────────────────────────
def make_extra(front=1.0, morning_slot3=1.0, afternoon_slot3=1.0):
    def fn(slot, session, date=None):
        if slot == 3:
            return morning_slot3 if session == MORNING else afternoon_slot3
        return front
    return fn


STRATS = [
    ("A  N1+C1 현행",            None),
    ("D  P0 cap (1.00/0.25)",    make_extra(1.00, 0.25, 1.00)),
    ("E  P1 cap (1.02/0.25)",    make_extra(1.02, 0.25, 1.00)),
    ("C  P2 cap (1.05/0.25)",    make_extra(1.05, 0.25, 1.00)),
    ("F  P3 cap (1.10/0.25)",    make_extra(1.10, 0.25, 1.00)),
]
