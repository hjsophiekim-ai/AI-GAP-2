"""연구2 PM-BLUE VWAP VETO — 1단계 관측 스크리닝 (2026-10-16). READ-ONLY.

13:30~14:30 DOWN_BLUE 확정플래그만 본다. 현행 production 에서 이 창의 BLUE 는
두 겹의 VWAP 관문을 지난다:
  (1) time_window_filter.evaluate_tw2_extra_vetoes 의 VWAP 역행 veto
      (확정봉 종가가 방향기준 VWAP 보다 1.0% 이상 불리 -> TW2_REJECT_VWAP_VETO)
  (2) 오후 후보는 teg_gate.evaluate_teg 가 필수 AND 게이트이고, 그 7조건 중
      COND_VWAP 은 "VWAP 유리한 쪽" 을 엄격히 요구한다(여유 0%).

이 스크립트는 재생 없이 "기회의 크기" 만 잰다. 각 플래그의 사후수익(ret, 그
방향 ETF 를 결정시각 종가에 사서 다음 반대확정/15:00 에 판 %)과 결정시각의
vwap_pos(방향정렬 %, 음수 = 불리)를 쓴다. 여기서 8~10월 합이 뚜렷한 음수면
재생을 돌릴 가치가 없다.
"""
import os
import sys

import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
ROOT = "G:/다른 컴퓨터/내 노트북 (2)/Desktop/AI-GAP 2"
FLAGS = ROOT + "/research_20261015d_exit_synthesis/flag_select.csv"
OUT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/out"
os.makedirs(OUT, exist_ok=True)


def stat(name, g):
    if not len(g):
        return f"  {name:38s}    0"
    return (f"  {name:38s} {len(g):4d} 승률 {(g.ret > 0).mean() * 100:5.1f}% "
            f"평균 {g.ret.mean():+6.2f} 중앙 {g.ret.median():+6.2f} 합 {g.ret.sum():+8.1f} "
            f"일수 {g.day.nunique():3d}")


def main():
    df = pd.read_csv(FLAGS, dtype={"day": str}, parse_dates=["t"])
    df["hm"] = df.t.dt.hour * 60 + df.t.dt.minute
    df["half"] = np.where(df.day < "20260801", "5-7월", "8-10월")
    pm = df[(df.d == -1) & (df.hm >= 810) & (df.hm < 870)].copy()
    pm.to_csv(OUT + "/s2_pmblue_flags.csv", index=False, encoding="utf-8-sig")

    print(f"13:30~14:30 DOWN_BLUE 확정플래그 {len(pm)}건 / {pm.day.nunique()}일 "
          f"(05-27~10-16, N1 재생 원장)\n")
    print("차단사유별 (사후수익 기준)")
    for r, g in sorted(pm.groupby(pm.reason.fillna("(통과)")), key=lambda kv: -len(kv[1])):
        print(stat(str(r), g))
    print(stat("── 전체", pm))

    print("\n기간 분리")
    for h, g in pm.groupby("half"):
        print(f"\n [{h}] {len(g)}건")
        for r, x in sorted(g.groupby(g.reason.fillna("(통과)")), key=lambda kv: -len(kv[1])):
            print(stat(str(r), x))
        print(stat("── 소계", g))

    print("\n\nVWAP 관문이 실제로 막고 있는 것 (vwap_pos = 방향정렬 %, 음수 = 불리)")
    vw = pm[pm.reason == "TW2_REJECT_VWAP_VETO"]
    teg = pm[pm.reason == "TW2_3SLOT_REJECT_TEG"]
    cand = pm[pm.reason.isin(["TW2_REJECT_VWAP_VETO", "TW2_3SLOT_REJECT_TEG"])]
    for name, g in (("VWAP veto 로 차단", vw), ("TEG 로 차단", teg), ("둘 합(완화/비적용 후보 상한)", cand)):
        print(stat(name, g))
        for h, x in g.groupby("half"):
            print(stat(f"   └ {h}", x))

    print("\nvwap_pos 분포 (VWAP veto 차단분)")
    if len(vw):
        print("  " + vw.vwap_pos.describe().round(2).to_string().replace("\n", "\n  "))

    print("\n세 변형이 '새로 들여보낼 수 있는' 플래그 상한 (사후수익 기준, 재생 아님)")
    # 현행 = 지금 통과한 것, 완화 = VWAP 불리폭이 작은 것만 추가, 비적용 = 후보 전부 추가
    passed = pm[pm.result == "EXECUTED"]
    for name, add in (("V0 현행 (추가 없음)", cand.iloc[0:0]),
                      ("V1 완화 (vwap_pos >= -2.0%)", cand[cand.vwap_pos >= -2.0]),
                      ("V2 비적용 (VWAP 조건 전부 해제)", cand)):
        tot = pd.concat([passed, add])
        print(f"\n  {name}")
        print(stat("    전체", tot))
        for h, x in tot.groupby("half"):
            print(stat(f"    └ {h}", x))
        if len(add):
            s = add.sort_values("ret", ascending=False)
            print(f"    추가분만: 합 {add.ret.sum():+.1f}%p / 상위3 제외 {s.ret.iloc[3:].sum():+.1f}%p "
                  f"/ 최악 {add.ret.min():+.1f} / 최고 {add.ret.max():+.1f}")

    print("\n\n실제로 체결됐던 13:30~14:30 BLUE 5건")
    p = passed[["day", "t", "ret", "vwap_pos"]].copy()
    p["t"] = p["t"].dt.strftime("%H:%M")
    print(p.round(2).to_string(index=False))

    print("\n후보 플래그 전체 목록 (VWAP/TEG 차단분)")
    c = cand[["day", "t", "reason", "ret", "vwap_pos", "flips60", "swing"]].copy()
    c["t"] = c["t"].dt.strftime("%H:%M")
    print(c.sort_values(["day"]).round(2).to_string(index=False))


if __name__ == "__main__":
    main()
