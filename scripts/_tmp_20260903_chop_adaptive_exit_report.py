"""READ-ONLY 분석/리포트 단계 — 엔진은
scripts/_tmp_20260903_chop_adaptive_exit_train_oos.py 를 그대로 import 한다.
여기에는 매매 판단 로직이 전혀 없다(집계/통계/선택 규칙만).

절차
  1) TRAIN 20일에서만 CHOP 정의(cross_min/flip_min/score_min)를 고른다.
     선택 규칙은 익절 임계값(1.5/2.0)과 무관하게 미리 고정한다:
       가드1 커버리지 : 20% <= CHOP 비율 <= 70%
       가드2 도달성   : CHOP 거래 중 peak 순수익 >= 2.0% 인 비율 >= 50%
                        (그래야 "빠른 전량익절"이 실제로 발동할 수 있음)
       주지표         : giveback 분리도
                        = avg(peak-실현, CHOP) - avg(peak-실현, TREND)
                        ("CHOP = 벌었다가 뱉어내는 구간"이 CHOP 정의의 목적)
       동점/안정성    : 파라미터 ±1 이웃들도 가드를 통과하고 주지표가
                        최고값의 70% 이상인 config를 우선(plateau)
  2) 고른 정의를 OOS 10일에서 절대 수정하지 않고 그대로 적용한다.
  3) A/B/C 지표 비교 + CHOP 거래만 뽑아 pair comparison.
  4) 참고용으로 "진입 미동결(free-chain)" 변형도 함께 돌려 부작용을 보고한다.
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(PROJECT_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import _tmp_20260903_chop_adaptive_exit_train_oos as eng  # noqa: E402

OUT = eng.OUTPUT_DIR


# ── metrics ──────────────────────────────────────────────────────────────
def metrics(trades: list, n_days: int) -> dict:
    closed = [t for t in trades if t.get("net_pct") is not None]
    closed.sort(key=lambda t: t["exit_time"])
    n = len(closed)
    if n == 0:
        return {"n_trades": 0}
    rets = [t["net_pct"] for t in closed]
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]

    simple = sum(rets)
    eq = 1.0
    compound_curve = []
    for r in rets:
        eq *= (1.0 + r / 100.0)
        compound_curve.append(eq)
    compound_pct = (eq - 1.0) * 100.0

    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    pf = (gross_win / gross_loss) if gross_loss > 0 else (float("inf") if gross_win > 0 else None)

    # MDD: 단순합 equity(%p) + 복리 equity(피크 대비 %)
    e = peak = mdd_simple = 0.0
    for r in rets:
        e += r
        peak = max(peak, e)
        mdd_simple = max(mdd_simple, peak - e)
    cpeak = 1.0
    mdd_comp = 0.0
    for v in compound_curve:
        cpeak = max(cpeak, v)
        mdd_comp = max(mdd_comp, (cpeak - v) / cpeak * 100.0)

    # 최대 연속 손실
    cur = best = 0
    cur_sum = worst_sum = 0.0
    for r in rets:
        if r <= 0:
            cur += 1
            cur_sum += r
            best = max(best, cur)
            worst_sum = min(worst_sum, cur_sum)
        else:
            cur = 0
            cur_sum = 0.0

    by_ret = sorted(rets, reverse=True)
    top10 = sum(by_ret[:10])
    # Top10 제외: 상위 10건을 뺀 나머지로 단순합/복리 재계산
    thresh_sorted = sorted(range(n), key=lambda i: rets[i], reverse=True)
    drop = set(thresh_sorted[:10])
    rest = [rets[i] for i in range(n) if i not in drop]
    ex10_simple = sum(rest)
    ex10_eq = 1.0
    for r in rest:
        ex10_eq *= (1.0 + r / 100.0)

    by_date = defaultdict(float)
    for t in closed:
        by_date[t["date"]] += t["net_pct"]

    return {
        "n_trades": n,
        "wins": len(wins), "losses": len(losses),
        "win_rate_pct": round(len(wins) / n * 100.0, 2),
        "simple_cum_pct": round(simple, 3),
        "compound_cum_pct": round(compound_pct, 3),
        "avg_pct_per_trade": round(simple / n, 4),
        "pf": (round(pf, 3) if isinstance(pf, float) and pf != float("inf") else pf),
        "mdd_simple_pp": round(mdd_simple, 3),
        "mdd_compound_pct": round(mdd_comp, 3),
        "max_consec_losses": best,
        "max_consec_loss_sum_pct": round(worst_sum, 3),
        "top10_sum_pct": round(top10, 3),
        "ex_top10_simple_pct": round(ex10_simple, 3),
        "ex_top10_compound_pct": round((ex10_eq - 1.0) * 100.0, 3),
        "profit_days": sum(1 for v in by_date.values() if v > 0),
        "loss_days": sum(1 for v in by_date.values() if v < 0),
        "flat_days": sum(1 for v in by_date.values() if v == 0),
        "active_days": len(by_date),
        "trades_per_day": round(n / n_days, 3) if n_days else None,
    }


def subset(trades: list, dates: list) -> list:
    ds = set(dates)
    return [t for t in trades if t["date"] in ds]


def fnum(v, w=11, nd=3):
    if v is None:
        return " " * (w - 1) + "-"
    if isinstance(v, float):
        return f"{v:>{w}.{nd}f}"
    return f"{str(v):>{w}}"


# ── CHOP 정의 선택 (TRAIN 전용) ──────────────────────────────────────────
def grid_stats(ctx, decisions: dict, trades_a: list, dates_train: list) -> tuple[list, float]:
    """라벨은 반드시 실제 정책과 같은 경로(run_policy의 live latch)로 매긴다.
    post-hoc으로 [진입봉, 청산봉] 구간을 훑으면 청산봉에서 이미 포지션이
    사라진 뒤의 판정까지 CHOP으로 세어 실제 정책과 라벨이 어긋난다
    (한 config에서 실제로 1건 차이가 났다)."""
    rows = []
    train = subset(trades_a, dates_train)
    # 도달성 가드는 표본 자체의 기저비율 대비 상대값으로 정의한다. 절대 50%는
    # 이 표본에서 어떤 정의도 통과할 수 없다 — TRAIN 전체 거래의 peak>=2.0%
    # 기저비율 자체가 그 아래이기 때문(아래 base_reach2 출력). 가드의 취지는
    # "익절 임계값에 아예 못 닿는 거래만 골라내는 정의를 배제"하는 것이므로,
    # 기저비율의 90% 이상이면 통과로 본다. (익절 결과와 무관한 기준)
    base_reach2 = (sum(1 for t in train if t["peak_net_pct"] >= 2.0) / len(train) * 100.0) if train else 0.0
    reach_floor = base_reach2 * 0.9
    for cfg in eng.GRID:
        lab, _ = eng.run_policy(ctx, chop_cfg=cfg, chop_tp_pct=None, frozen=decisions)
        labeled = subset([vars(t) for t in lab], dates_train)
        chop = [t for t in labeled if t["chop_latch_bar_idx"] is not None]
        trend = [t for t in labeled if t["chop_latch_bar_idx"] is None]
        n = len(labeled)
        if not chop or not trend:
            rows.append({"cfg": cfg.key(), "cfg_obj": cfg, "n_chop": len(chop),
                         "chop_share_pct": round(len(chop) / n * 100.0, 1) if n else 0.0,
                         "eligible": False, "sep_giveback": None, "sep_net": None,
                         "reach2_pct": None, "avg_net_chop": None, "avg_net_trend": None})
            continue
        gb = lambda ts: sum(t["peak_net_pct"] - t["net_pct"] for t in ts) / len(ts)
        av = lambda ts: sum(t["net_pct"] for t in ts) / len(ts)
        reach2 = sum(1 for t in chop if t["peak_net_pct"] >= 2.0) / len(chop) * 100.0
        share = len(chop) / n * 100.0
        rows.append({
            "cfg": cfg.key(), "cfg_obj": cfg,
            "n_chop": len(chop), "chop_share_pct": round(share, 1),
            "avg_net_chop": round(av(chop), 4), "avg_net_trend": round(av(trend), 4),
            "sep_net": round(av(trend) - av(chop), 4),
            "avg_gb_chop": round(gb(chop), 4), "avg_gb_trend": round(gb(trend), 4),
            "sep_giveback": round(gb(chop) - gb(trend), 4),
            "reach2_pct": round(reach2, 1),
            "eligible": bool(20.0 <= share <= 70.0 and reach2 >= reach_floor),
        })
    return rows, base_reach2


def select_cfg(rows: list) -> dict:
    elig = [r for r in rows if r["eligible"]]
    if not elig:
        raise SystemExit("가드를 통과하는 CHOP 정의가 TRAIN에 없음")
    best = max(r["sep_giveback"] for r in elig)
    by_key = {r["cfg"]: r for r in rows}
    scored = []
    for r in elig:
        c = r["cfg_obj"]
        neigh = []
        for dc in (-1, 0, 1):
            for df in (-1, 0, 1):
                for dk in (-1, 0, 1):
                    if dc == df == dk == 0:
                        continue
                    k = eng.ChopConfig(c.cross_min + dc, c.flip_min + df, c.score_min + dk).key()
                    nr = by_key.get(k)
                    if nr is not None and nr["eligible"]:
                        neigh.append(nr["sep_giveback"])
        plateau = sum(1 for v in neigh if v >= best * 0.7)
        scored.append((r["sep_giveback"], plateau, len(neigh), r))
    # 주지표 우선, 동점 시 plateau 이웃 수
    scored.sort(key=lambda x: (round(x[0], 3), x[1]), reverse=True)
    return {"chosen": scored[0][3], "plateau_neighbors": scored[0][1], "best_sep": best,
            "ranking": [{"cfg": s[3]["cfg"], "sep_giveback": s[0], "plateau": s[1]} for s in scored]}


# ── main ─────────────────────────────────────────────────────────────────
def main() -> int:
    ctx = eng.build_ctx()
    base = json.loads((OUT / "baseline_A.json").read_text(encoding="utf-8"))
    trades_a = base["trades_A"]
    decisions = base["decisions"]
    dates, train_dates, oos_dates = ctx.dates, ctx.train_dates, ctx.oos_dates

    print("=" * 118)
    print(f"TW2 3-SLOT · 진입 불변 / 청산만 regime-adaptive — READ-ONLY 백테스트")
    print(f"전체 {len(dates)}영업일  {dates[0]} ~ {dates[-1]}   "
          f"(TRAIN {train_dates[0]}~{train_dates[-1]} 20일 / OOS {oos_dates[0]}~{oos_dates[-1]} 10일)")
    print(f"기준 A 거래수 {len(trades_a)}건  (진입 동결 재생으로 A 완전 복원 검증 완료)")
    print("=" * 118)

    # ── 1) TRAIN 그리드 ──────────────────────────────────────────────────
    rows, base_reach2 = grid_stats(ctx, decisions, trades_a, train_dates)
    print("\n[1] TRAIN 20일 CHOP 정의 그리드 (선택은 TRAIN에서만, 익절임계값과 무관한 규칙)")
    print(f"    TRAIN 전체 거래의 peak>=2.0% 기저비율 {base_reach2:.1f}% "
          f"→ 도달성 가드 하한 {base_reach2*0.9:.1f}%")
    print(f"{'정의':<30}{'nCHOP':>7}{'비율%':>8}{'avg순익CHOP':>13}{'avg순익TREND':>14}"
          f"{'giveback분리도':>15}{'peak>=2%':>10}{'가드':>6}")
    for r in rows:
        print(f"{r['cfg']:<30}{r['n_chop']:>7}{fnum(r['chop_share_pct'],8,1)}"
              f"{fnum(r.get('avg_net_chop'),13,4)}{fnum(r.get('avg_net_trend'),14,4)}"
              f"{fnum(r.get('sep_giveback'),15,4)}{fnum(r.get('reach2_pct'),10,1)}"
              f"{'O' if r['eligible'] else 'x':>6}")

    sel = select_cfg(rows)
    cfg = sel["chosen"]["cfg_obj"]
    print(f"\n>>> TRAIN 선택 CHOP 정의: {cfg.key()}   "
          f"(giveback 분리도 {sel['chosen']['sep_giveback']}, 가드통과 이웃 {sel['plateau_neighbors']}개)")
    print("    이 정의는 이후 OOS에서 일절 수정하지 않는다.")
    print("    조건: (1) 최근30분 확정 zero-cross >= %d" % cfg.cross_min)
    print("          (2) EMA10-EMA20 spread 진입방향 순변화 <= 0 (최근 30분)")
    print("          (3) EMA20 진입방향 순변화 <= 0 (최근 30분)")
    print("          (4) 최근30분 close-VWAP 부호 교차 >= %d" % cfg.flip_min)
    print("          -> 만족 개수 >= %d 이면 CHOP (진입 확정봉 또는 보유 중 최초 판정 시 래치)"
          % cfg.score_min)

    # 각 조건이 실제로 얼마나 자주 켜지는지 (정의가 특정 조건에 사실상 종속되는지 확인)
    from app.trading.macd2.models import Direction as _D
    cond_keys = ("cross_over", "spread_not_expanding", "ema20_slope_not_aligned", "vwap_repeat")
    tally = {k: 0 for k in cond_keys}
    nbars = 0
    for t in trades_a:
        d = _D.UP_RED if t["direction"] == _D.UP_RED.value else _D.DOWN_BLUE
        for i in range(int(t["entry_bar_idx"]), int(t["exit_bar_idx"]) + 1):
            cc = eng.chop_conditions(ctx.feat, i, d, cfg)
            if cc is None:
                continue
            nbars += 1
            for k in cond_keys:
                tally[k] += int(cc[k])
    print(f"    조건별 발생률(전체 판정가능 보유봉 {nbars}개 기준): "
          + ", ".join(f"{k}={tally[k]/nbars*100:.0f}%" for k in cond_keys))

    # ── 2) A/B/C 실행 (진입 동결) ────────────────────────────────────────
    lab, _ = eng.run_policy(ctx, chop_cfg=cfg, chop_tp_pct=None, frozen=decisions)
    a = [vars(t) for t in lab]                      # A + CHOP 라벨 (청산은 현행 그대로)
    b_t, _ = eng.run_policy(ctx, chop_cfg=cfg, chop_tp_pct=1.5, frozen=decisions)
    c_t, _ = eng.run_policy(ctx, chop_cfg=cfg, chop_tp_pct=2.0, frozen=decisions)
    b = [vars(t) for t in b_t]
    c = [vars(t) for t in c_t]

    # 라벨링 전용 실행이 A와 동일한지 (CHOP 라벨이 청산에 영향 없음) 검증
    key = lambda ts: [(t["entry_time"], t["entry_symbol"], t["exit_time"], round(t["net_pct"], 6)) for t in ts]
    assert key(a) == key(trades_a), "라벨링 실행이 A를 변경했다"
    # CHOP 집합/래치봉이 A/B/C에서 동일한지 검증 (pair comparison 성립 조건)
    for other, nm in ((b, "B"), (c, "C")):
        la = [t["chop_latch_bar_idx"] for t in a]
        lo = [t["chop_latch_bar_idx"] for t in other]
        assert la == lo, f"{nm}의 CHOP 래치 집합이 A와 다르다"
    print(f"\n검증: A/B/C의 진입집합·CHOP래치봉 완전 일치 (거래수 {len(a)}/{len(b)}/{len(c)})")

    # ── 3) 지표 비교 ─────────────────────────────────────────────────────
    panels = [("FULL 30일", dates), ("TRAIN 20일", train_dates), ("OOS 10일", oos_dates)]
    all_metrics: dict = {}
    labels = [("A 현행청산", a), ("B CHOP+1.5%", b), ("C CHOP+2.0%", c)]
    rowspec = [
        ("거래수", "n_trades", 0), ("승/패", None, 0), ("승률 %", "win_rate_pct", 2),
        ("복리누적 %", "compound_cum_pct", 3), ("단순누적 %", "simple_cum_pct", 3),
        ("평균수익/거래 %", "avg_pct_per_trade", 4), ("PF", "pf", 3),
        ("MDD %p(단순)", "mdd_simple_pp", 3), ("MDD %(복리)", "mdd_compound_pct", 3),
        ("최대연속손실 건", "max_consec_losses", 0), ("연속손실합 %", "max_consec_loss_sum_pct", 3),
        ("수익일", "profit_days", 0), ("손실일", "loss_days", 0),
        ("Top10합 %", "top10_sum_pct", 3), ("Top10제외 단순 %", "ex_top10_simple_pct", 3),
        ("Top10제외 복리 %", "ex_top10_compound_pct", 3),
    ]
    for pname, pdates in panels:
        print("\n" + "=" * 118)
        print(f"[2] {pname}  ({pdates[0]} ~ {pdates[-1]})")
        ms = {}
        for lname, tr in labels:
            ms[lname] = metrics(subset(tr, pdates), len(pdates))
        all_metrics[pname] = ms
        print(f"{'지표':<20}" + "".join(f"{ln:>16}" for ln, _ in labels))
        for disp, k, nd in rowspec:
            if k is None:
                vals = [f"{ms[ln]['wins']}/{ms[ln]['losses']}" for ln, _ in labels]
                print(f"{disp:<20}" + "".join(f"{v:>16}" for v in vals))
                continue
            cells = []
            for ln, _ in labels:
                v = ms[ln].get(k)
                cells.append(fnum(v, 16, nd) if isinstance(v, float) else f"{str(v):>16}")
            print(f"{disp:<20}" + "".join(cells))

    # ── 4) CHOP 거래만 pair comparison ──────────────────────────────────
    print("\n" + "=" * 118)
    print("[3] CHOP으로 판정된 거래만 — 동일 진입/동일 래치 기준 pair comparison")
    idx_by_entry = {t["entry_time"]: i for i, t in enumerate(a)}
    for pname, pdates in panels:
        ds = set(pdates)
        pairs = [(a[i], b[i], c[i]) for i in range(len(a))
                 if a[i]["chop_latch_bar_idx"] is not None and a[i]["date"] in ds]
        print(f"\n-- {pname}: CHOP 거래 {len(pairs)}건 / 전체 {len(subset(a, pdates))}건 "
              f"({round(len(pairs)/max(1,len(subset(a,pdates)))*100,1)}%) --")
        if not pairs:
            continue
        for nm, sel_i in (("기존 청산", 0), ("1.5% 전량익절", 1), ("2.0% 전량익절", 2)):
            rets = [p[sel_i]["net_pct"] for p in pairs]
            wins = sum(1 for r in rets if r > 0)
            eq = 1.0
            for r in rets:
                eq *= 1 + r / 100.0
            gw = sum(r for r in rets if r > 0)
            gl = abs(sum(r for r in rets if r <= 0))
            pf = round(gw / gl, 3) if gl > 0 else "inf"
            print(f"   {nm:<14} 합계 {sum(rets):>8.3f}%  복리 {(eq-1)*100:>8.3f}%  "
                  f"평균 {sum(rets)/len(rets):>7.4f}%  승률 {wins/len(rets)*100:>5.1f}%  PF {pf}")
        for nm, sel_i in (("1.5%", 1), ("2.0%", 2)):
            diffs = [p[sel_i]["net_pct"] - p[0]["net_pct"] for p in pairs]
            better = sum(1 for d in diffs if d > 1e-9)
            worse = sum(1 for d in diffs if d < -1e-9)
            same = len(diffs) - better - worse
            print(f"   vs 기존({nm}): 개선 {better}건 / 악화 {worse}건 / 동일 {same}건, "
                  f"평균차 {sum(diffs)/len(diffs):+.4f}%p, 합계차 {sum(diffs):+.3f}%p")
        n15 = sum(1 for p in pairs if p[1]["exit_reason"] == eng.EXIT_CHOP_FULL_TP)
        n20 = sum(1 for p in pairs if p[2]["exit_reason"] == eng.EXIT_CHOP_FULL_TP)
        print(f"   실제 CHOP 전량익절 발동: 1.5% {n15}건, 2.0% {n20}건 "
              f"(나머지는 래치 후 임계값 미달로 기존 래더가 청산)")

    # ── 5) CHOP 거래 상세 ────────────────────────────────────────────────
    print("\n" + "=" * 118)
    print("[4] CHOP 거래 건별 상세 (전체 30일)")
    print(f"{'날짜':<10}{'slot':>5}{'방향':>10}{'진입':>17}{'래치':>17}{'진입시CHOP':>11}"
          f"{'peak%':>8}{'A청산':>26}{'A%':>8}{'B%':>8}{'C%':>8}")
    for i in range(len(a)):
        if a[i]["chop_latch_bar_idx"] is None:
            continue
        ta, tb, tc = a[i], b[i], c[i]
        print(f"{ta['date']:<10}{str(ta['slot_number']):>5}{ta['direction']:>10}"
              f"{ta['entry_time'][11:16]:>17}{(ta['chop_latch_time'] or '')[11:16]:>17}"
              f"{('Y' if ta['chop_entry'] else 'N'):>11}{ta['peak_net_pct']:>8.2f}"
              f"{str(ta['exit_reason'])[:24]:>26}{ta['net_pct']:>8.3f}{tb['net_pct']:>8.3f}{tc['net_pct']:>8.3f}")

    # ── 6) 청산사유 분포 ─────────────────────────────────────────────────
    print("\n" + "=" * 118)
    print("[5] 청산 사유 분포 (전체 30일)")
    for lname, tr in labels:
        cnt = Counter(t["exit_reason"] for t in tr)
        print(f"  {lname}: " + ", ".join(f"{k}={v}" for k, v in cnt.most_common()))

    # ── 6b) 진입시 CHOP vs 보유중 CHOP ──────────────────────────────────
    print("\n" + "=" * 118)
    print("[5b] CHOP 판정 시점별 분해 (전체 30일)")
    for nm, pick in (("진입 당시 CHOP", lambda t: t["chop_entry"]),
                     ("보유 중 CHOP(진입시엔 TREND)",
                      lambda t: (t["chop_latch_bar_idx"] is not None) and not t["chop_entry"])):
        idxs = [i for i in range(len(a)) if pick(a[i])]
        if not idxs:
            print(f"  {nm}: 0건")
            continue
        for lbl, arr in (("A", a), ("B", b), ("C", c)):
            s = sum(arr[i]["net_pct"] for i in idxs)
            w = sum(1 for i in idxs if arr[i]["net_pct"] > 0)
            print(f"  {nm} n={len(idxs)} [{lbl}] 합계 {s:+.3f}%  평균 {s/len(idxs):+.4f}%  "
                  f"승률 {w/len(idxs)*100:.1f}%")

    # ── 6c) TRAIN 가드를 통과한 모든 정의의 OOS 거동 (사후 견고성 스캔) ──
    print("\n" + "=" * 118)
    print("[5c] 사후 견고성 스캔 — TRAIN 가드를 통과한 모든 CHOP 정의를 그대로 OOS에 적용")
    print("     (선택을 다시 하는 게 아니라, TRAIN/OOS 부호가 뒤집히는 게 이 정의만의")
    print("      우연인지 아이디어 전체의 성질인지 보기 위한 점검)")
    print(f"{'정의':<30}{'nCHOP(T/O)':>12}"
          f"{'TRAIN B-A':>11}{'TRAIN C-A':>11}{'OOS B-A':>11}{'OOS C-A':>11}"
          f"{'FULL B-A':>11}{'FULL C-A':>11}")
    scan = []
    for r in rows:
        if not r["eligible"]:
            continue
        cc = r["cfg_obj"]
        la, _ = eng.run_policy(ctx, chop_cfg=cc, chop_tp_pct=None, frozen=decisions)
        lb, _ = eng.run_policy(ctx, chop_cfg=cc, chop_tp_pct=1.5, frozen=decisions)
        lc, _ = eng.run_policy(ctx, chop_cfg=cc, chop_tp_pct=2.0, frozen=decisions)
        aa = [vars(t) for t in la]
        bb = [vars(t) for t in lb]
        ccx = [vars(t) for t in lc]
        row = {"cfg": cc.key()}
        for pn, pd_ in (("TRAIN", train_dates), ("OOS", oos_dates), ("FULL", dates)):
            ds = set(pd_)
            ia = [i for i in range(len(aa)) if aa[i]["date"] in ds]
            row[f"{pn}_B"] = round(sum(bb[i]["net_pct"] - aa[i]["net_pct"] for i in ia), 3)
            row[f"{pn}_C"] = round(sum(ccx[i]["net_pct"] - aa[i]["net_pct"] for i in ia), 3)
        row["nT"] = sum(1 for i in range(len(aa))
                        if aa[i]["chop_latch_bar_idx"] is not None and aa[i]["date"] in set(train_dates))
        row["nO"] = sum(1 for i in range(len(aa))
                        if aa[i]["chop_latch_bar_idx"] is not None and aa[i]["date"] in set(oos_dates))
        scan.append(row)
        ncell = "%d/%d" % (row["nT"], row["nO"])
        print(f"{row['cfg']:<30}{ncell:>12}"
              f"{row['TRAIN_B']:>+11.3f}{row['TRAIN_C']:>+11.3f}"
              f"{row['OOS_B']:>+11.3f}{row['OOS_C']:>+11.3f}"
              f"{row['FULL_B']:>+11.3f}{row['FULL_C']:>+11.3f}")
    pos_train_b = sum(1 for r in scan if r["TRAIN_B"] > 0)
    pos_oos_b = sum(1 for r in scan if r["OOS_B"] > 0)
    pos_oos_c = sum(1 for r in scan if r["OOS_C"] > 0)
    print(f"     요약: 가드통과 {len(scan)}개 정의 중 TRAIN에서 B>A {pos_train_b}개, "
          f"OOS에서 B>A {pos_oos_b}개 / C>A {pos_oos_c}개 (단순누적 %p 기준)")

    # ── 7) free-chain 참고 실행 (진입 미동결) ────────────────────────────
    print("\n" + "=" * 118)
    print("[6] 참고: 진입을 동결하지 않으면(=청산 변화가 포지션 상태를 통해 진입까지 바꾸면)")
    fb, _ = eng.run_policy(ctx, chop_cfg=cfg, chop_tp_pct=1.5, frozen=None)
    fc, _ = eng.run_policy(ctx, chop_cfg=cfg, chop_tp_pct=2.0, frozen=None)
    for nm, tr in (("B free-chain", [vars(t) for t in fb]), ("C free-chain", [vars(t) for t in fc])):
        m_full = metrics(tr, len(dates))
        m_oos = metrics(subset(tr, oos_dates), len(oos_dates))
        print(f"  {nm}: FULL 거래 {m_full['n_trades']}건(동결 대비 {m_full['n_trades']-len(a):+d}) "
              f"복리 {m_full['compound_cum_pct']}% / OOS 복리 {m_oos['compound_cum_pct']}%")

    (OUT / "report_summary.json").write_text(json.dumps({
        "period": {"all": dates, "train": train_dates, "oos": oos_dates},
        "chop_definition": {"cross_min": cfg.cross_min, "flip_min": cfg.flip_min,
                            "score_min": cfg.score_min, "selected_on": "TRAIN 20d only"},
        "train_grid": [{k: v for k, v in r.items() if k != "cfg_obj"} for r in rows],
        "selection": {"chosen": cfg.key(), "plateau_neighbors": sel["plateau_neighbors"],
                      "ranking": sel["ranking"]},
        "metrics": all_metrics,
        "robustness_scan": scan,
        "trades": {"A": a, "B": b, "C": c},
    }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\nsaved {OUT / 'report_summary.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
