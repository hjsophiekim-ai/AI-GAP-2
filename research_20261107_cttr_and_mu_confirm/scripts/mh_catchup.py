"""'오전 ∧ MU-HY 괴리 >= 2.5' 와 MU CATCH-UP 게이트가 얼마나 겹치는가.

CATCH-UP 판정은 production mu_catchup.evaluate 를 그대로 부른다(읽기 전용).
게이트(전부 충족해야 PASS):
  · UP_RED 만 (BLUE 는 BLUE_SHADOW_ONLY 로 FAIL — 기록만)
  · 플래그봉 시작 09:00~11:00
  · mu_session_pct >= +0.5
  · mu_hy_gap >= +1.0
결측(MU/HY 세션 기준봉 없음)은 UNAVAILABLE.
"""
import csv
import sys
from datetime import datetime, timedelta

import cx_lib as X  # noqa: F401  (경로 설정)
import wx_lib as W
from app.trading.macd2 import config, mu_assist, mu_catchup
from app.trading.macd2.models import Direction

sys.stdout.reconfigure(encoding="utf-8")

RECON_DAYS = ["2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23",
              "2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30",
              "2026-11-02", "2026-11-03", "2026-11-04"]
LEDGER_DAYS = ["2026-11-05", "2026-11-06"]
CUT = 2.5

base = {}
for r in csv.DictReader(open("out_confirm.csv", encoding="utf-8-sig")):
    base[(r["day"], r["t3_at"])] = r

rows = []
for day in RECON_DAYS + LEDGER_DAYS:
    want = {k: v for k, v in base.items() if k[0] == day}
    if not want:
        continue
    led = day in LEDGER_DAYS
    hy = W.load_1m(day, "hynix", prefer_snapshot=led)
    mus = W.mu_series(day)
    for (_, t3s), r in sorted(want.items()):
        flag = datetime.strptime(f"{day} {r['flag_at']}", "%Y-%m-%d %H:%M").replace(tzinfo=W.KST)
        t3 = flag + timedelta(minutes=6)
        d = Direction(r["dir"])
        ev = mu_catchup.evaluate(d, flag, t3, hy, mu_series=mus,
                                 cttr={"cttr": None, "state": "UNAVAILABLE"})
        gap = None if r["mu_hy_gap"] == "" else float(r["mu_hy_gap"])     # 원장 우선값
        mus_pct = None if r["mu_session_pct"] == "" else float(r["mu_session_pct"])
        rows.append({**r, "flag_time": flag.time(), "catchup": ev["status"],
                     "reason": ev["reason"], "gap": gap, "mu_pct": mus_pct,
                     "win": r["touch"] == "UP", "r30": float(r["r30"])})
    del hy, mus

# 조건 A: 오전(T+3 < 11:00) ∧ 괴리 >= 2.5
for r in rows:
    r["A"] = r["session"] == "AM" and r["gap"] is not None and r["gap"] >= CUT
    r["B"] = r["catchup"] == mu_catchup.PASS
    # CATCH-UP 네 조건을 따로 — 어디서 갈리는지 보려고
    r["c_red"] = r["dir"] == "UP_RED"
    r["c_win"] = config.MU_CATCHUP_WINDOW_START <= r["flag_time"] < config.MU_CATCHUP_WINDOW_END
    r["c_mu"] = r["mu_pct"] is not None and r["mu_pct"] >= config.MU_CATCHUP_MU_MIN_PCT
    r["c_gap"] = r["gap"] is not None and r["gap"] >= config.MU_CATCHUP_GAP_MIN_PCT

print(f"MU ENTRY_CONFIRM {len(rows)}건에 production mu_catchup.evaluate 적용")
print(f"  A = 오전(T+3<11:00) ∧ 괴리 >= {CUT}")
print(f"  B = CATCH-UP PASS (RED ∧ 플래그 09:00~11:00 ∧ MU >= +0.5 ∧ 괴리 >= +1.0)")
print()
print("=== 교차표 ===")
for lab, sel in (("A ∧ B  (둘 다)", lambda r: r["A"] and r["B"]),
                 ("A only (내 조건만)", lambda r: r["A"] and not r["B"]),
                 ("B only (CATCH-UP 만)", lambda r: r["B"] and not r["A"]),
                 ("둘 다 아님", lambda r: not r["A"] and not r["B"])):
    g = [r for r in rows if sel(r)]
    w = sum(1 for r in g if r["win"])
    extra = f"  승리 {w}/{len(g)}  평균30분 {sum(r['r30'] for r in g)/len(g):+6.3f}%" if g else ""
    print(f"  {lab:24s} {len(g):2d}건{extra}")

print()
print("=== A 3건은 CATCH-UP 의 어느 조건에서 갈리나 ===")
print(f"  {'날짜 T+3':16s} {'방향':9s} {'플래그':6s} {'괴리':>7s} {'MUses':>7s} "
      f"{'RED':4s} {'09-11':6s} {'MU>=.5':7s} {'gap>=1':7s} {'CATCHUP':11s} 사유")
for r in rows:
    if not r["A"]:
        continue
    b = lambda x: " O " if x else " X "
    print(f"  {r['day'][5:]+' '+r['t3_at']:16s} {r['dir']:9s} {r['flag_at']:6s} "
          f"{r['gap']:+7.3f} {r['mu_pct']:+7.3f} {b(r['c_red']):4s} {b(r['c_win']):6s} "
          f"{b(r['c_mu']):7s} {b(r['c_gap']):7s} {r['catchup']:11s} {r['reason']}")

print()
print("=== CATCH-UP PASS 후보 전수 ===")
p = [r for r in rows if r["B"]]
if not p:
    print("  0건")
for r in p:
    print(f"  {r['day'][5:]} {r['t3_at']} {r['dir']} 괴리 {r['gap']:+.3f} MU {r['mu_pct']:+.3f} "
          f"30분 {r['r30']:+.3f} {'승리' if r['win'] else '손실/무'}  A={'O' if r['A'] else 'X'}")

print()
print("=== CATCH-UP FAIL 사유 분포 (28건 전체) ===")
cnt = {}
for r in rows:
    key = r["reason"] or r["catchup"]
    cnt[key] = cnt.get(key, 0) + 1
for k, v in sorted(cnt.items(), key=lambda x: -x[1]):
    print(f"  {v:2d}건  {k}")

print()
print("=== 조건별 통과 건수 (28건 중) ===")
for k, lab in (("c_red", "UP_RED"), ("c_win", "플래그 09:00~11:00"),
               ("c_mu", "MU 세션 >= +0.5"), ("c_gap", "괴리 >= +1.0")):
    print(f"  {lab:22s} {sum(1 for r in rows if r[k]):2d}건")
