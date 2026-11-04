"""MU 플래그에 기존 production 게이트를 하나씩 걸어본 단변량 표 (research only).

새 임계값을 만들지 않는다 — 전부 production 이 이미 쓰는 이진 게이트의 통과/탈락 대조다.
표본이 작으므로(10일) 각 칸의 n 을 항상 같이 본다.
"""
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.stdout.reconfigure(encoding="utf-8")
OUT = ROOT / "research_20261104_mu_primary" / "out"

d = pd.read_csv(OUT / "mu_flags.csv")
t = d[d["tradable"] & d["entry_px"].notna()].copy()
t["tp1"] = t["path"] == "TP1"
t["sl"] = t["path"] == "SL"
t["xs_ok"] = t["xs"] == "MATCH"
t["opp_le400"] = t["hy_gap_opp"] <= 400
t["teg"] = t["teg_ok"] == True            # noqa: E712
t["cross30_le1"] = t["hy_cross30"] <= 1
t["score_lt45"] = t["hy_score"] < 45      # sideways_filter v5 의 기존 기준(45) — 새 값 아님
t["tp_pass"] = t["tp_decision"].astype(str).str.contains("APPROV|PASS", case=False, na=False)

GATES = ["xs_ok", "opp_le400", "hy_aligned", "hy_gap_expand", "hy_vwap_ok",
         "teg", "cross30_le1", "hy_breakout", "cd_trend_ok", "cd_path_chop",
         "score_lt45", "tp_pass"]


def line(label, sub):
    if not len(sub):
        return f"  {label:34s}      0        -        -        -        -        -"
    return (f"  {label:34s} {len(sub):6d} {sub['r30'].mean():+8.3f} {sub['r_eod'].mean():+8.3f} "
            f"{100 * sub['tp1'].mean():7.1f}% {100 * sub['sl'].mean():7.1f}% "
            f"{len(sub) / 10:7.1f}")


def table(title, base):
    print(f"\n{'=' * 104}\n{title}   (n={len(base)}, {len(base) / 10:.1f}건/일)\n{'=' * 104}")
    print(f"  {'게이트':34s} {'n':>6s} {'r30평균':>8s} {'종가평균':>8s} {'TP1선':>8s} {'SL선':>8s} {'건/일':>7s}")
    print(line("(전체)", base))
    for g in GATES:
        if g not in base.columns:
            continue
        col = base[g]
        yes = base[col == True]       # noqa: E712
        no = base[col == False]       # noqa: E712
        na = len(base) - len(yes) - len(no)
        print(line(f"{g} = True", yes))
        print(line(f"{g} = False", no) + (f"   (판정불가 {na})" if na else ""))


print("MU 플래그 346건 중 거래가능 시간대(09:00~14:50) · ETF 진입가 산출 가능 = "
      f"{len(t)}건 / 10영업일 (2026-10-19~10-30)\n"
      "r30 = 진입 후 30분 종가 순수익률%, 종가평균 = 14:50 까지 보유 순수익률%\n"
      "TP1선 = +3.0% 를 -1.3% 보다 먼저 터치한 비율, SL선 = 그 반대")

table("① MU 상태 무관 — 전체 MU 플래그", t)
for st in ("CONFIRM", "STALL", "DIVERGE"):
    table(f"② MU {st}", t[t["mu_state"] == st])

print(f"\n{'=' * 104}\n③ 방향별 / 시간대별\n{'=' * 104}")
for key in ("dir", "window", "mu_state"):
    print(f"\n-- {key} --")
    g = t.groupby(key).agg(n=("r30", "size"), r30=("r30", "mean"), eod=("r_eod", "mean"),
                           tp1=("tp1", "mean"), sl=("sl", "mean"))
    g[["r30", "eod"]] = g[["r30", "eod"]].round(3)
    g[["tp1", "sl"]] = (100 * g[["tp1", "sl"]]).round(1)
    print(g.to_string())

print(f"\n{'=' * 104}\n④ 사용자 규칙 B (MU CONFIRM 진입 / DIVERGE 차단 / STALL 은 체결강도+반대gap<=400)\n{'=' * 104}")
b_confirm = t[t["mu_state"] == "CONFIRM"]
wait = t[t["mu_state"] == "STALL"]
wait_ok = wait[wait["xs_ok"] & wait["opp_le400"]]
wait_no = wait.drop(wait_ok.index)
bad = t[t["mu_state"] == "DIVERGE"]
print(line("CONFIRM 진입", b_confirm))
print(line("WAIT(STALL) 승인", wait_ok))
print(line("WAIT(STALL) 차단", wait_no))
print(line("BAD(DIVERGE) 차단", bad))
print(line(">> B 진입 전체", pd.concat([b_confirm, wait_ok])))

print(f"\n{'=' * 104}\n⑤ MU 가 HY 같은방향 플래그보다 먼저인가\n{'=' * 104}")
lead = t["hy_same_flag_lead_min"].dropna()
print(f"  1시간 안에 같은방향 HY 플래그가 뒤따른 MU 플래그 {len(lead)}/{len(t)}건 "
      f"({100 * len(lead) / len(t):.0f}%)")
print(f"  선행 분수: 중앙값 {lead.median():.0f}분 · 평균 {lead.mean():.1f}분 · "
      f"0분 {int((lead == 0).sum())}건 · 1~5분 {int(((lead >= 1) & (lead <= 5)).sum())}건 · "
      f"6~15분 {int(((lead >= 6) & (lead <= 15)).sum())}건 · 16분+ {int((lead > 15).sum())}건")
print(f"  HY 가 따라온 플래그 r30 {t[t['hy_same_flag_lead_min'].notna()]['r30'].mean():+.3f}% vs "
      f"안 따라온 것 {t[t['hy_same_flag_lead_min'].isna()]['r30'].mean():+.3f}%")
