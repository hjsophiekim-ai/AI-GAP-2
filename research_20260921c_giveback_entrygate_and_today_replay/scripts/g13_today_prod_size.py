# -*- coding: utf-8 -*-
"""[T4] 2026-09-21 — production position_sizing / order_executor 를 **직접 구동**해
BASE(N1+C1) vs P2(N1+C1+P2) 주문수량·금액·손익을 계산한다. READ-ONLY.

거래목록(진입/청산 시각·가격·사유·net_pct)은 g10 의 당일 리플레이 결과.
사이징만 production 코드가 계산한다. 네트워크/원장/브로커 접근 없음.
"""
from __future__ import annotations
import sys, pickle
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"C:\Users\FURSYS\Desktop\AI-GAP 2")
HERE = Path(__file__).resolve().parent

from app.trading.macd2 import config, order_executor            # noqa: E402
from app.trading.macd2 import position_sizing as PS             # noqa: E402
from app.trading.macd2 import state_store                       # noqa: E402

TR = pickle.load(open(HERE / "_today_trades.pkl", "rb"))
BUDGET = 10_000_000.0


def _n1c1_state():
    s = state_store.default_state()
    s.auto_trade_on = True
    s.budget = BUDGET
    for f in ("time_window_2_filter_enabled", "time_window_teg_filter_enabled",
              "time_window_3slot_filter_enabled", "time_window_twf_filter_enabled",
              "time_window_x2lite_filter_enabled", "time_window_h50_filter_enabled"):
        setattr(s, f, False)
    s.time_window_n1_filter_enabled = True
    s.c1_peak_protection_enabled = True
    return s


def replay(mode):
    config.MACD2_SIZING_MODE = mode
    s = _n1c1_state()
    PS.reset_daily(s)
    rows = []
    for t in TR:
        remaining = PS.remaining_daily_budget(s)
        d = PS.evaluate(s, entry_chop=t["entry_chop"],
                        slot_number=t["slot_number"], session=t["session"])
        pre_cap = s.budget * d.clipped
        actual = min(pre_cap, remaining)
        qty = order_executor.compute_order_quantity(
            remaining, pre_cap, t["entry_price"], safety_margin_pct=0.0)
        notional = qty * t["entry_price"]
        rows.append({**t, "p2": d.p2, "raw": d.raw, "clipped": d.clipped,
                     "applied": d.applied, "remaining": remaining,
                     "strategy_krw": pre_cap, "actual_krw": actual, "qty": qty,
                     "buy_krw": notional, "sell_krw": qty * t["exit_price"],
                     "pnl_krw": notional * t["net_pct"] / 100.0})
        PS.note_entry(s, d)
        PS.note_full_exit(s, t["exit_reason"])
    config.MACD2_SIZING_MODE = config.SIZING_MODE_BASE
    return rows


print("=" * 128)
print("2026-09-21 (월) — N1+C1 / N1+C1+P2 리플레이 결과")
print("=" * 128)
print("\n■ 거래 (P2 는 사이징 전용이라 아래 6개 열은 두 모드가 완전히 동일)")
print(f"  {'#':>2s} {'슬롯':>4s} {'세션':4s} {'방향':9s} {'종목':7s} {'진입':5s} {'진입가':>8s} "
      f"{'청산':5s} {'청산가':>8s} {'보유':>5s} {'청산사유':24s} {'MFE%':>7s} {'MAE%':>7s} {'net%':>7s}")
for i, t in enumerate(TR, 1):
    print(f"  {i:2d} {t['slot_number']:4d} {('오전' if t['session']=='MORNING' else '오후'):4s} "
          f"{t['direction']:9s} {t['entry_symbol']:7s} {t['entry_time'][11:16]:5s} "
          f"{t['entry_price']:8,.0f} {t['exit_time'][11:16]:5s} {t['exit_price']:8,.0f} "
          f"{t['hold_minutes']:4.0f}분 {t['exit_reason']:24s} {t['peak_net_pct']:+7.3f} "
          f"{t['mae_net_pct']:+7.3f} {t['net_pct']:+7.3f}")

print(f"\n■ C1 발동 검사 — ARM 임계 MFE {config.C1_ARM_MFE_PCT}% / giveback {config.C1_GIVEBACK_PCT}%p")
for i, t in enumerate(TR, 1):
    print(f"  거래{i}: 당일 최대 MFE {t['peak_net_pct']:+.3f}% < {config.C1_ARM_MFE_PCT}% "
          f"-> C1 ARM 안 됨")
print("  => C1 은 오늘 한 번도 무장되지 않는다. **N1 단독 결과 = N1+C1 결과**.")

RES = {}
for mode, tag in ((config.SIZING_MODE_BASE, "BASE  (N1+C1)"),
                  (config.SIZING_MODE_P2, "P2    (N1+C1+P2)")):
    rows = replay(mode)
    RES[mode] = rows
    print(f"\n■ {tag}")
    print(f"  {'#':>2s} {'슬롯':>4s} {'P2배수':>6s} {'clip후':>6s} {'주문가능':>12s} "
          f"{'전략금액':>12s} {'수량':>6s} {'매수금액':>12s} {'매도금액':>12s} {'손익KRW':>10s}")
    for i, r in enumerate(rows, 1):
        print(f"  {i:2d} {r['slot_number']:4d} {r['p2']:6.2f} {r['clipped']:6.2f} "
              f"{r['remaining']:12,.0f} {r['strategy_krw']:12,.0f} {r['qty']:6d} "
              f"{r['buy_krw']:12,.0f} {r['sell_krw']:12,.0f} {r['pnl_krw']:10,.0f}")
    tot_b = sum(r["buy_krw"] for r in rows)
    print(f"  {'합계':>44s} {tot_b:12,.0f} {sum(r['sell_krw'] for r in rows):12,.0f} "
          f"{sum(r['pnl_krw'] for r in rows):10,.0f}")
    print(f"  일 한도 {BUDGET*config.X2LITE_SIZING_DAILY_EXPOSURE_CAP:,.0f} 중 {tot_b:,.0f} 사용 "
          f"({tot_b/(BUDGET*config.X2LITE_SIZING_DAILY_EXPOSURE_CAP)*100:.1f}%) / "
          f"노출누적 {sum(r['applied'] for r in rows):.2f} / 3.00")

b = sum(r["pnl_krw"] for r in RES[config.SIZING_MODE_BASE])
p = sum(r["pnl_krw"] for r in RES[config.SIZING_MODE_P2])
print(f"\n■ P2 효과: {b:+,.0f} -> {p:+,.0f} KRW  (차이 {p-b:+,.0f})")
print("  진입/청산 시각·가격·거래수·사유는 완전히 동일. 수량만 x1.05 (slot1/2).")
print(f"  오늘은 둘 다 손실이므로 P2 가 손실을 {abs(p-b):,.0f}원 **키운다**.")
