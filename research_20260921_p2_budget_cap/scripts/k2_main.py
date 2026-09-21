# -*- coding: utf-8 -*-
"""§1~3, §9, §10 — 실금액 KRW 시뮬레이션 본체. READ-ONLY."""
import sys, pickle, csv, io
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"C:\Users\FURSYS\Desktop\AI-GAP 2")
import k1_core as K
import hengine5 as H
from common import compound

D = pickle.load(open("_ctx_B.pkl", "rb"))["dates"]
W78, W70, W30 = D, D[-70:], D[-30:]
TS = K.load()
M = K.MORNING


def eng(rows):
    return [{"date": t["date"], "entry_time": t["entry_time"], "exit_time": t["exit_time"],
             "net_pct": t["net_used"], "w1a": t["w1a_applied"], "exit_reason": t["exit_reason"],
             "peak_net_pct": t["peak_net_pct"], "direction": t["direction"],
             "slot_number": t["slot"], "session": t["session"]} for t in rows]


# 사용자 명세(오후 slot3 = 기존 배수 1.00)가 1차, 연구원안(오후 slot3 도 front)은 대조
def user(front, mo3=0.25):
    return K.make_extra(front, mo3, 1.00)


def research(front, mo3=0.25):
    return lambda s, ss, dt=None: mo3 if (s == 3 and ss == M) else front


VARIANTS = [
    ("A  N1+C1 현행",             None),
    ("D  P0-cap  1.00 / 0.25",    user(1.00)),
    ("E  P1-cap  1.02 / 0.25",    user(1.02)),
    ("C  P2-cap  1.05 / 0.25",    user(1.05)),
    ("F  P3-cap  1.10 / 0.25",    user(1.10)),
    ("B  P2 연구원안(오후3도1.05)", research(1.05)),
]

RUNS = {name: K.size_chain(TS, fn) for name, fn in VARIANTS}
BASE = RUNS["A  N1+C1 현행"]

print("=" * 108)
print("§0  실거래 예산 계약 확인")
print("=" * 108)
print(f"  config.DEFAULT_BUDGET                     = {K.BASE_BUDGET:,.0f} KRW  (1회 주문 기준금액)")
print(f"  config.X2LITE_SIZING_DAILY_EXPOSURE_CAP   = {K.DAILY_EXPOSURE_CAP}  (당일 누적 노출 상한)")
print(f"  => production 이 이미 강제하는 하루 원금한도 = {K.BASE_BUDGET * K.DAILY_EXPOSURE_CAP:,.0f} KRW")
print(f"  요청하신 DAILY_CAPITAL                     = {K.DAILY_CAPITAL:,.0f} KRW  -> 동일")
print()
print("  청산자금 재사용 여부: position_sizing.py docstring + 코드 확인 결과")
print("    'exposure 는 진입 시점 누적이다 - 부분익절이나 청산이 누적을 되돌리지 않는다'")
print("    => production 계약 = 당일 신규진입 누적 원금 기준 (가용현금 재사용 아님)")
print("    (주문 직전 min(budget, orderable_cash) 도 걸리지만, 동시보유 1포지션이라")
print("     현금은 거의 항상 여유 -> 구속조건은 누적노출 3.0 = 3,000만원)")

print()
print("=" * 108)
print("§3  기준자금 3,000만원 실제 손익  (고정 원금, 복리 재투자 없음)")
print("=" * 108)
hdr = (f"{'전략':28s} {'n':>4s} {'30d KRW':>13s} {'70d KRW':>14s} {'78d KRW':>14s} "
       f"{'78d수익률':>9s} {'복리%':>9s} {'PF':>6s} {'MDD%':>7s}")
print(hdr); print("-" * 108)
for name in RUNS:
    r = RUNS[name]
    s78 = K.summary(r, W78)
    print(f"{name:28s} {s78['n']:4d} {K.krw_pnl(r, W30):13,.0f} {K.krw_pnl(r, W70):14,.0f} "
          f"{s78['krw']:14,.0f} {s78['ret_pct']:8.1f}% {s78['compound_pct']:9.2f} "
          f"{s78['pf']:6.3f} {s78['mdd_pct']:7.2f}")

print()
print(f"{'전략':28s} {'평균일사용':>12s} {'평균미사용':>12s} {'예산사용률':>9s} {'최대일사용':>12s} "
      f"{'cap발동':>7s} {'초과시도':>7s} {'노출cap':>7s}")
print("-" * 108)
for name in RUNS:
    b = K.budget_stats(RUNS[name], W78)
    print(f"{name:28s} {b['avg_used']:12,.0f} {b['avg_unused']:12,.0f} {b['util_pct']:8.1f}% "
          f"{b['max_used']:12,.0f} {b['cap_hits']:7d} {b['over_attempts']:7d} {b['expo_cap_hits']:7d}")

print()
print("=" * 108)
print("§9  현금 미사용 페널티")
print("=" * 108)
print(f"{'전략':28s} {'평균':>12s} {'중앙':>12s} {'최대':>12s} {'>=500만원':>9s} {'>=1000만원':>10s}")
print("-" * 108)
for name in RUNS:
    b = K.budget_stats(RUNS[name], W78)
    print(f"{name:28s} {b['avg_unused']:12,.0f} {b['med_unused']:12,.0f} {b['max_unused']:12,.0f} "
          f"{b['n_cash_5m']:9d} {b['n_cash_10m']:10d}")

print()
print("  9-C  남은 현금을 오후 slot3 에만 remaining budget 까지 허용 (미래정보 없음)")
print("-" * 108)


def afternoon_soak(front):
    """오후 slot3 는 '남은 예산 전부'를 쓰되 개별상한 1.5 를 넘지 않는다."""
    def fn(s, ss, dt=None):
        if s == 3 and ss == M:
            return 0.25
        if s == 3 and ss == K.AFTERNOON:
            return 1.5        # clip 이 1.5 로 막고, remaining 이 더 작으면 그쪽이 이긴다
        return front
    return fn


for tag, fn in (("A 현행 그대로", None), ("B P2 (현금 방치)", user(1.05)),
                ("C P2 + 오후slot3 흡수", afternoon_soak(1.05))):
    r = K.size_chain(TS, fn)
    b = K.budget_stats(r, W78)
    print(f"  {tag:24s} 78d KRW={K.krw_pnl(r, W78):13,.0f}  30d={K.krw_pnl(r, W30):12,.0f}  "
          f"평균미사용={b['avg_unused']:11,.0f}  사용률={b['util_pct']:5.1f}%")

print()
print("=" * 108)
print("§10  budget-cap 때문에 생기는 경로변화")
print("=" * 108)
kb = {(t["date"], t["entry_time"]) for t in BASE}
for name in RUNS:
    if name.startswith("A "):
        continue
    r = RUNS[name]
    kr = {(t["date"], t["entry_time"]) for t in r}
    dif_net = sum(1 for a, b in zip(r, BASE) if abs(a["net_used"] - b["net_used"]) > 1e-12)
    dif_exit = sum(1 for a, b in zip(r, BASE) if a["exit_time"] != b["exit_time"])
    print(f"  {name:28s} 진입집합 only_a={len(kr - kb)} only_b={len(kb - kr)}  "
          f"net_pct 변화={dif_net}  청산시각 변화={dif_exit}")
print()
print("  근거: trading_cost_engine 의 fee/tax/clearing/slippage 가 전부 quantity 비례이고")
print("        min_commission_krw = 0 -> net_pct 는 주문수량과 완전히 무관하다.")
print("        hengine5 에서 w1a/exposure 는 어떤 진입·청산 판정에도 들어가지 않는다.")
print()
print("  적용순서 비교 (base -> W1a -> P2 -> 일예산cap -> lot rounding)")
print("-" * 108)
for tag, order in (("pre_clip  (연구/훅: clip(규칙x P2))", "pre_clip"),
                   ("post_clip (명세: clip(규칙) x P2)", "post_clip")):
    r = K.size_chain(TS, user(1.05), order=order)
    print(f"  {tag:40s} 노출={sum(t['w1a_applied'] for t in r):7.2f} "
          f"78d KRW={K.krw_pnl(r, W78):13,.0f}  30d={K.krw_pnl(r, W30):12,.0f}  "
          f"cap발동={K.budget_stats(r, W78)['cap_hits']}")

print()
print("  lot rounding 손실 (주문예산 - 실제체결금액)")
print("-" * 108)
for name in ("A  N1+C1 현행", "C  P2-cap  1.05 / 0.25"):
    r = RUNS[name]
    lost = sum(t["actual_krw"] - t["notional_krw"] for t in r)
    print(f"  {name:28s} 총 {lost:,.0f} KRW / 총주문 {sum(t['actual_krw'] for t in r):,.0f} "
          f"({lost / sum(t['actual_krw'] for t in r) * 100:.4f}%)")

print()
print("=" * 108)
print("§0-b  청산현금 재사용 계약이라면 (별도 보고)")
print("=" * 108)
for name, fn in (("A 현행", None), ("C P2", user(1.05))):
    r = K.size_chain(TS, fn, cash_reuse=True)
    b = K.budget_stats(r, W78)
    print(f"  {name:10s} 78d KRW={K.krw_pnl(r, W78):13,.0f}  30d={K.krw_pnl(r, W30):12,.0f}  "
          f"평균일사용={b['avg_used']:11,.0f}  사용률={b['util_pct']:5.1f}%  cap발동={b['cap_hits']}")

# ── 원장 CSV 저장 ────────────────────────────────────────────────────────
FIELDS = ["date", "entry_time", "slot", "session", "direction", "w1a_rules", "w1a_extra",
          "w1a_clipped", "w1a_applied", "strategy_krw", "remaining_krw", "actual_krw",
          "cut_krw", "budget_capped", "expo_capped", "qty", "entry_price", "notional_krw",
          "net_pct", "pnl_krw", "exit_time", "exit_reason", "day_used_after"]
for tag, name in (("A", "A  N1+C1 현행"), ("P2", "C  P2-cap  1.05 / 0.25")):
    with io.open(f"ledger_{tag}.csv", "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        for t in RUNS[name]:
            w.writerow(t)
print(f"\n원장 저장: ledger_A.csv / ledger_P2.csv  ({len(BASE)}행)")

# 하루별 요약
with io.open("daily_P2.csv", "w", encoding="utf-8-sig", newline="") as fh:
    w = csv.writer(fh)
    w.writerow(["date", "n", "slot1_krw", "slot2_krw", "slot3_krw", "used_krw",
                "unused_krw", "pnl_krw"])
    r = RUNS["C  P2-cap  1.05 / 0.25"]
    for d in D:
        day = [t for t in r if t["date"] == d]
        sl = {i: sum(t["actual_krw"] for t in day if t["slot"] == i) for i in (1, 2, 3)}
        u = sum(t["actual_krw"] for t in day)
        w.writerow([d, len(day), f"{sl[1]:.0f}", f"{sl[2]:.0f}", f"{sl[3]:.0f}",
                    f"{u:.0f}", f"{K.DAILY_CAPITAL - u:.0f}",
                    f"{sum(t['pnl_krw'] for t in day):.0f}"])
print("일별 저장: daily_P2.csv")
