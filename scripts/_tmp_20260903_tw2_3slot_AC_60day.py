"""READ-ONLY 60영업일 확장 검증 (2026-09-03 사용자 요청 3차):
A(현행 TW2 3-SLOT) vs C(A + Entry-CHOP Profit Lock, trigger +1.5% / floor +0.8%).

production 수정/commit/push 없음. 엔진은 2차 요청 스크립트를 그대로 import한다
(_tmp_20260903_tw2_3slot_ABCD_train_oos.run) — 진입/청산 판단 코드는 한 줄도
새로 쓰지 않았고, 데이터 구성(_load_all / resample_completed_3m / MACD 플래그
검출 / CHOP feature)도 1차 요청 스크립트의 build_ctx를 N_DAYS만 60으로 바꿔
그대로 재사용한다.

재튜닝 금지 준수
----------------
CHOP 정의(cross>=1 | flip>=3 | score>=3), lock trigger +1.5%, lock floor +0.8%
는 모두 2차 요청까지 30일 TRAIN에서 확정된 값을 상수로 그대로 쓴다
(FROZEN_CHOP_CFG / LOCK_TRIGGER_PCT / 0.8). 이 스크립트에는 스윕도, 선택
로직도 없다.

구간 겹침에 대한 정직한 표기
---------------------------
60일을 TRAIN40/OOS20으로 자르면 OOS = 08-03~08-31이다. 그런데 위 파라미터는
07-20~08-14 구간에서 확정됐으므로 OOS 20일 중 앞 8일(08-03~08-14)은 파라미터
확정 시 이미 본 데이터다. 요청받은 40/20 표는 그대로 내고, 파라미터가 절대
보지 못한 구간만 따로 뽑은 패널을 추가로 계산한다:
  - UNSEEN-PRE : 60일 구간 시작 ~ 07-16 (30일 구간이 시작되기 전)
  - UNSEEN-POST: 08-18 ~ 08-31 (파라미터 확정 구간 종료 후)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(PROJECT_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import _tmp_20260903_chop_adaptive_exit_train_oos as ce  # noqa: E402
import _tmp_20260903_tw2_3slot_ABCD_train_oos as eng  # noqa: E402
import _tmp_20260903_tw2_3slot_ABCD_report as rep  # noqa: E402

OUT = PROJECT_ROOT / "data" / "validation" / "tw2_3slot_AC_60day"
OUT.mkdir(parents=True, exist_ok=True)

N_DAYS = 60
N_TRAIN = 40
LOCK_FLOOR = 0.8                      # 30일 TRAIN 확정값 — 재튜닝 금지
PARAM_WINDOW = ("20260720", "20260814")  # CHOP 정의/floor가 확정된 구간


def build_60day_ctx():
    """1차 스크립트의 build_ctx를 그대로 쓰되 기간만 60일/40일로 바꾼다."""
    ce.N_DAYS = N_DAYS
    ce.N_TRAIN = N_TRAIN
    ce._CTX_CACHE = OUT / "_ctx_cache_60d.pkl"
    return ce.build_ctx()


def cell(v, w=16, nd=3):
    if v is None:
        return " " * (w - 1) + "-"
    if isinstance(v, float):
        return f"{v:>{w}.{nd}f}"
    return f"{str(v):>{w}}"


def main() -> int:
    ctx = build_60day_ctx()
    dates, train, oos = ctx.dates, ctx.train_dates, ctx.oos_dates

    unseen_pre = [d for d in dates if d < PARAM_WINDOW[0]]
    unseen_post = [d for d in dates if d > PARAM_WINDOW[1]]
    param_win = [d for d in dates if PARAM_WINDOW[0] <= d <= PARAM_WINDOW[1]]

    print("=" * 122)
    print("TW2 3-SLOT: A(현행) vs C(Entry-CHOP Profit Lock, +1.5% trigger / +0.8% floor)")
    print(f"60영업일 {dates[0]} ~ {dates[-1]} (워밍업 {ctx.warmup})  |  "
          f"TRAIN {train[0]}~{train[-1]} (40일)  |  OOS {oos[0]}~{oos[-1]} (20일)")
    print("공통: PRE15 승계 OFF, 하루 최대 3회(TW2_3SLOT_DAILY_CAP=3), MACD zero-cross/T+3/")
    print("      TW2 veto/TEGv2/whipsaw/TP1·TP2/trailing/손절/수수료·체결가 = production 그대로")
    print(f"C 파라미터: CHOP={eng.FROZEN_CHOP_CFG.key()}, trigger +{eng.LOCK_TRIGGER_PCT}%, "
          f"floor +{LOCK_FLOOR}%  (30일 TRAIN 확정값, 이 스크립트에 스윕/선택 로직 없음)")
    print(f"3분봉 {len(ctx.hynix_bars_3m)}개, 확정 플래그 {len(ctx.flags_by_idx)}개")
    print("=" * 122)

    checked, mism = ce.verify_cross30(ctx)
    print(f"\ncross30 검증: 표본 {checked}개, production _count_recent_confirmed_crossovers와 "
          f"불일치 {mism}개")
    assert mism == 0

    a, dec_a = eng.run(ctx, slot1_tq4=False, lock_floor_pct=None, record_decisions=True)
    c, _ = eng.run(ctx, slot1_tq4=False, lock_floor_pct=LOCK_FLOOR)
    cf, _ = eng.run(ctx, slot1_tq4=False, lock_floor_pct=LOCK_FLOOR, frozen=dec_a)
    A = [vars(t) for t in a]
    C = [vars(t) for t in c]
    Cf = [vars(t) for t in cf]

    k = lambda ts: [(t["entry_time"], t["entry_symbol"], t["exit_time"], round(t["net_pct"], 6))
                    for t in ts]
    consistent = (k(C) == k(Cf))
    print(f"A 거래 {len(A)}건 / C 거래 {len(C)}건  |  "
          f"C full-chain == C 진입동결본: {consistent}  "
          f"(C는 청산만 변경 → 진입 집합 동일해야 정상)")
    assert len(A) == len(C), "A/C 진입 집합 불일치 — pair 불가"

    panels = [
        ("FULL 60일", dates), ("TRAIN 40일", train), ("OOS 20일", oos),
        ("└ OOS중 파라미터확정구간 겹침 (08-03~08-14)", [d for d in oos if d in param_win]),
        ("└ OOS중 완전미지 (08-18~08-31)", [d for d in oos if d in unseen_post]),
        ("UNSEEN-PRE 완전미지 (~07-16)", unseen_pre),
        ("UNSEEN 합계 (PRE + POST)", unseen_pre + unseen_post),
    ]
    rowspec = [
        ("거래수", "n_trades", 0), ("일평균 거래수", "trades_per_day", 3),
        ("승/패", None, 0), ("승률 %", "win_rate_pct", 2),
        ("단순수익 %", "simple_cum_pct", 3), ("복리수익 %", "compound_cum_pct", 3),
        ("평균수익/거래 %", "avg_pct_per_trade", 4), ("PF", "pf", 3),
        ("MDD %p(단순)", "mdd_simple_pp", 3), ("MDD %(복리)", "mdd_compound_pct", 3),
        ("최대연속손실 건", "max_consec_losses", 0),
        ("수익일", "profit_days", 0), ("손실일", "loss_days", 0),
        ("Top10 비중 %", "top10_share_pct", 1),
        ("Top10제외 단순 %", "ex_top10_simple_pct", 3),
        ("Top10제외 복리 %", "ex_top10_compound_pct", 3),
    ]
    allm = {}
    for pname, pdates in panels:
        if not pdates:
            continue
        ma = rep.metrics(rep.sub(A, pdates), len(pdates))
        mc = rep.metrics(rep.sub(C, pdates), len(pdates))
        allm[pname] = {"A": ma, "C": mc}
        print("\n" + "=" * 122)
        print(f"[{pname}]  {pdates[0]} ~ {pdates[-1]}  ({len(pdates)}영업일)")
        print(f"{'지표':<22}{'A 현행':>16}{'C Lock+0.8%':>16}{'차이':>16}")
        for disp, key, nd in rowspec:
            if key is None:
                wl_a = "%d/%d" % (ma["wins"], ma["losses"])
                wl_c = "%d/%d" % (mc["wins"], mc["losses"])
                print(f"{disp:<22}{wl_a:>16}{wl_c:>16}{'':>16}")
                continue
            va, vc = ma.get(key), mc.get(key)
            if isinstance(va, float) and isinstance(vc, float):
                print(f"{disp:<22}{cell(va,16,nd)}{cell(vc,16,nd)}{cell(vc-va,16,nd)}")
            else:
                d = (vc - va) if isinstance(va, int) and isinstance(vc, int) else None
                print(f"{disp:<22}{str(va):>16}{str(vc):>16}"
                      f"{(f'{d:+d}' if d is not None else ''):>16}")

    # ── OOS 판정 ─────────────────────────────────────────────────────────
    print("\n" + "=" * 122)
    print("[판정] OOS에서 C가 A보다 복리↑ & PF↑ & MDD 비악화인가")
    for pname in ("OOS 20일", "└ OOS중 완전미지 (08-18~08-31)",
                  "└ OOS중 파라미터확정구간 겹침 (08-03~08-14)",
                  "UNSEEN-PRE 완전미지 (~07-16)", "UNSEEN 합계 (PRE + POST)"):
        if pname not in allm:
            continue
        ma, mc = allm[pname]["A"], allm[pname]["C"]
        c_ok = mc["compound_cum_pct"] > ma["compound_cum_pct"]
        p_ok = (mc["pf"] or 0) > (ma["pf"] or 0)
        d_ok = mc["mdd_compound_pct"] <= ma["mdd_compound_pct"] + 1e-9
        print(f"  {pname:<44} 복리 {ma['compound_cum_pct']:>8.3f}→{mc['compound_cum_pct']:>8.3f}"
              f"[{'O' if c_ok else 'X'}]  PF {ma['pf']:>6.3f}→{mc['pf']:>6.3f}"
              f"[{'O' if p_ok else 'X'}]  MDD {ma['mdd_compound_pct']:>6.3f}→"
              f"{mc['mdd_compound_pct']:>6.3f}[{'O' if d_ok else 'X'}]  → "
              f"{'통과' if (c_ok and p_ok and d_ok) else '탈락'}")

    # ── Profit Lock pair table (전 발동건) ──────────────────────────────
    print("\n" + "=" * 122)
    print("[Profit Lock 발동 거래 전량 pair table] — C는 A와 진입 동일, 1:1 대응")
    print("  러너 훼손 = A에서 최종 +3~6%로 끝난 거래가 lock 때문에 그보다 낮게 끝난 경우")
    print(f"{'날짜':<10}{'slot':>5}{'구간':>8}{'방향':>10}{'진입':>7}{'MFE%':>7}"
          f"{'A청산사유':>26}{'A최종%':>9}{'C청산사유':>24}{'C최종%':>9}"
          f"{'순차이%p':>10}{'손실→+':>8}{'러너훼손':>9}")

    def seg_of(d):
        if d in set(train):
            return "TRAIN"
        return "OOS*" if d in param_win else "OOS"

    fired = [i for i in range(len(A)) if C[i]["lock_fired"]]
    armed_only = [i for i in range(len(A)) if C[i]["lock_armed"] and not C[i]["lock_fired"]]
    for i in fired:
        ta, tc = A[i], C[i]
        diff = tc["net_pct"] - ta["net_pct"]
        flip = "Y" if (ta["net_pct"] <= 0 and tc["net_pct"] > 0) else "-"
        runner = "Y" if (3.0 <= ta["net_pct"] <= 6.0 and tc["net_pct"] < ta["net_pct"]) else "-"
        print(f"{ta['date']:<10}{str(ta['slot_number']):>5}{seg_of(ta['date']):>8}"
              f"{ta['direction']:>10}{ta['entry_time'][11:16]:>7}{ta['peak_net_pct']:>7.2f}"
              f"{str(ta['exit_reason'])[:24]:>26}{ta['net_pct']:>9.3f}"
              f"{str(tc['exit_reason'])[:22]:>24}{tc['net_pct']:>9.3f}"
              f"{diff:>+10.3f}{flip:>8}{runner:>9}")
    print("  (구간 OOS* = OOS 20일 중 파라미터 확정구간과 겹치는 08-03~08-14)")

    for pname, pdates in (("TRAIN 40일", train), ("OOS 20일", oos),
                          ("└ OOS 완전미지", [d for d in oos if d in unseen_post]),
                          ("UNSEEN-PRE", unseen_pre), ("FULL 60일", dates)):
        ds = set(pdates)
        ii = [i for i in fired if A[i]["date"] in ds]
        if not ii:
            print(f"  {pname}: lock 발동 0건")
            continue
        da = sum(A[i]["net_pct"] for i in ii)
        dc = sum(C[i]["net_pct"] for i in ii)
        print(f"  {pname}: 발동 {len(ii)}건  A합 {da:+.3f}%  C합 {dc:+.3f}%  순차이 {dc-da:+.3f}%p  "
              f"손실→플러스 {sum(1 for i in ii if A[i]['net_pct']<=0 and C[i]['net_pct']>0)}건  "
              f"러너(+3~6%)훼손 "
              f"{sum(1 for i in ii if 3.0<=A[i]['net_pct']<=6.0 and C[i]['net_pct']<A[i]['net_pct'])}건")

    print(f"\n  lock ARMED(진입CHOP & MFE>=+{eng.LOCK_TRIGGER_PCT}%)이지만 미발동: "
          f"{len(armed_only)}건 — A와 결과 동일 (floor에 닿기 전에 기존 래더가 청산)")
    dmg = 0
    for i in armed_only:
        ta = A[i]
        if ta["net_pct"] >= 3.0:
            dmg += 1
        print(f"    {ta['date']} slot{ta['slot_number']} {seg_of(ta['date']):<6} "
              f"MFE {ta['peak_net_pct']:>5.2f}%  A=C={ta['net_pct']:+.3f}% "
              f"({str(ta['exit_reason'])[:24]})")
    print(f"    이 중 A에서 +3% 이상으로 끝난 러너 {dmg}건 — 전부 훼손 없이 그대로 유지")

    entry_chop = [i for i in range(len(A)) if C[i]["entry_chop"]]
    print(f"\n  진입 확정시점 CHOP 판정 거래: {len(entry_chop)}건 / 전체 {len(A)}건 "
          f"({len(entry_chop)/len(A)*100:.1f}%)  중 MFE +1.5% 도달(armed) "
          f"{len(fired)+len(armed_only)}건, 실제 발동 {len(fired)}건")

    (OUT / "report_summary.json").write_text(json.dumps({
        "period": {"all": dates, "train": train, "oos": oos, "warmup": ctx.warmup,
                   "param_window": param_win, "unseen_pre": unseen_pre,
                   "unseen_post": unseen_post},
        "params": {"chop": eng.FROZEN_CHOP_CFG.key(),
                   "lock_trigger_pct": eng.LOCK_TRIGGER_PCT, "lock_floor_pct": LOCK_FLOOR,
                   "retuned": False},
        "c_frozen_consistent": consistent,
        "metrics": allm,
        "lock_fired": [{"A": A[i], "C": C[i]} for i in fired],
        "lock_armed_not_fired": [{"A": A[i], "C": C[i]} for i in armed_only],
        "trades": {"A": A, "C": C},
    }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\nsaved {OUT / 'report_summary.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
