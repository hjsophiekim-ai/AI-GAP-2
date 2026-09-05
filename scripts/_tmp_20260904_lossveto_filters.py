"""READ-ONLY 2/2 (2026-09-04): Loss-Veto 후보 A/B/C 를 TRAIN 40일에서만 설계·동결하고
OOS 20일에서 재튜닝 없이 검증한다.

■ 적용 방식 (사용자 요구사항 그대로)
  "현행 A 의 진입을 절대 늘리지 말고 기존 승인 거래를 차단만 하게 해"
  -> 현행 A 체인이 실제로 체결한 137건에서 veto 조건에 걸린 건을 **제거만** 한다.
     차단으로 슬롯이 비어 새 진입이 생기는 2차 효과는 의도적으로 배제한다
     (그래야 "차단만"이 성립하고 러너 보존율/막은 BIG_LOSS 수가 잘 정의된다).
     실배포 시에는 슬롯 소비 여부에 따라 2차 효과가 생길 수 있음은 별도 명시.

■ TRAIN 에서 고른 임계값 (동결, OOS 재튜닝 없음)
  TRAIN ema_spread_exp3 q60 = 4749.8705
  TRAIN ema_spread_exp3 q75 = 7031.7074

■ 후보 (전부 2~3개 조건, 전부 설명 가능)
  A  Slot1 AND entry_chop
     그날 첫 진입인데 진입시점 CHOP 판정 — 그날 방향성이 아직 확립되지 않았고
     난타전 신호까지 겹친 상태. (entry_chop 은 production early_take_profit.
     evaluate_entry_chop 그대로)
  B  Slot1 AND ema_spread_exp3 >= q75
     그날 첫 진입인데 EMA10-EMA20 spread 가 최근 3봉 만에 급격히 벌어진 상태 —
     이미 크게 움직인 뒤의 추격 진입.
  C  Slot1 AND zero_dist_signed <= 0 AND ema_spread_exp3 >= q60
     그날 첫 진입 + MACD 가 아직 0선의 신호 반대편(구조 미전환) + spread 급확대.

■ 왜 이 피처들인가 (TRAIN BIG_LOSS 30 vs BIG_WIN 13 단독 구분력 상위)
  ema_spread_exp3  구분력 0.385 (AUC 0.692)  BIG_LOSS 가 더 크다
  gap_exp3         구분력 0.323 (AUC 0.662)  BIG_LOSS 가 더 크다
  is_slot1         구분력 0.315 (AUC 0.658)  BIG_LOSS 70% vs BIG_WIN 38.5%
  zero_dist_signed 구분력 0.246 (AUC 0.377)  BIG_LOSS 음수(-4855) / BIG_WIN 양수(+4913)
  Slot1 은 TRAIN 37거래에 합계 +1.01% 뿐인데 BIG_LOSS 30건 중 21건을 차지한다.

Production 코드 무수정. 피처는 전부 진입확정시점 이하 인덱스만 사용(미래정보 없음).
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BASE = PROJECT_ROOT / "data" / "validation" / "lossveto"

Q60 = 4749.8705
Q75 = 7031.7074

VETOES = {
    "A": ("Slot1 AND entry_chop",
          lambda d: (d.is_slot1 == 1) & (d.entry_chop == 1)),
    "B": (f"Slot1 AND ema_spread_exp3 >= {Q75:.1f}",
          lambda d: (d.is_slot1 == 1) & (d.ema_spread_exp3 >= Q75)),
    "C": (f"Slot1 AND zero_dist_signed <= 0 AND ema_spread_exp3 >= {Q60:.1f}",
          lambda d: (d.is_slot1 == 1) & (d.zero_dist_signed <= 0)
                    & (d.ema_spread_exp3 >= Q60)),
}


def metrics(d: pd.DataFrame) -> dict:
    if len(d) == 0:
        return {}
    df = d.sort_values("exit_time").reset_index(drop=True)
    n = len(df)
    w, l = df[df.net_pct > 0], df[df.net_pct < 0]
    eq = (1 + df.net_pct / 100).cumprod()
    gp, gl = w.net_pct.sum(), -l.net_pct.sum()
    dd = (eq / eq.cummax() - 1) * 100
    daily = df.groupby("date")["net_pct"].sum()
    return {
        "trades": n,
        "win_rate_pct": round(len(w) / n * 100, 2),
        "simple_pct": round(float(df.net_pct.sum()), 4),
        "compound_pct": round(float((eq.iloc[-1] - 1) * 100), 4),
        "avg_pct": round(float(df.net_pct.mean()), 4),
        "pf": round(float(gp / gl), 4) if gl > 0 else None,
        "mdd_pct": round(float(dd.min()), 4),
        "big_wins": int((df.label == "BIG_WIN").sum()),
        "big_losses": int((df.label == "BIG_LOSS").sum()),
        "profit_days": int((daily > 0).sum()), "loss_days": int((daily < 0).sum()),
    }


def main() -> int:
    df = pd.read_csv(BASE / "trades_features.csv")
    L: list[str] = []
    add = L.append

    add("Loss-Veto 후보 검증 — 현행 A(TW2 3-SLOT + 조기익절) 60영업일 실거래 137건 기준")
    add("적용 방식: 승인된 거래를 '차단만' (진입 추가 없음, 정적 제거)")
    add(f"TRAIN 동결 임계값: ema_spread_exp3 q60={Q60:.4f} / q75={Q75:.4f}")
    add("")
    for k, (desc, _) in VETOES.items():
        add(f"  Loss-Veto {k}: {desc}")
    add("")

    # ── 0) 원본 분포 ──
    add("=" * 100)
    add("[0] 현행 A 라벨 분포 (BIG_LOSS 는 LOSS 의 부분집합)")
    add("=" * 100)
    for sp in ("TRAIN", "OOS", "ALL"):
        d = df if sp == "ALL" else df[df.split == sp]
        bw = int((d.label == "BIG_WIN").sum()); wn = int((d.label == "WIN").sum())
        sl = int((d.label == "SMALL_LOSS").sum()); bl = int((d.label == "BIG_LOSS").sum())
        add(f"  {sp:<6} n={len(d):<4} BIG_WIN={bw:<3} WIN={wn:<3} "
            f"LOSS={sl + bl:<3}(BIG_LOSS={bl:<3}) 단순={d.net_pct.sum():+.2f}%")
    add("")

    results = {}
    blocked_all = {}
    for k, (desc, fn) in VETOES.items():
        mask = fn(df).fillna(False)
        blocked_all[k] = df[mask]
        for sp in ("TRAIN", "OOS", "ALL"):
            d = df if sp == "ALL" else df[df.split == sp]
            m = fn(d).fillna(False)
            blk, keep = d[m], d[~m]
            base = metrics(d)
            aft = metrics(keep)
            bw_tot = int((d.label == "BIG_WIN").sum())
            bw_blk = int((blk.label == "BIG_WIN").sum())
            results[f"{k}|{sp}"] = {
                "desc": desc, "base": base, "after": aft,
                "blocked": len(blk),
                "blocked_sum_pct": round(float(blk.net_pct.sum()), 4),
                "big_loss_blocked": int((blk.label == "BIG_LOSS").sum()),
                "big_loss_total": int((d.label == "BIG_LOSS").sum()),
                "big_win_missed": bw_blk, "big_win_total": bw_tot,
                "runner_keep_pct": round((bw_tot - bw_blk) / bw_tot * 100, 1) if bw_tot else None,
            }

    # ── 1) 차단 거래 전부 ──
    add("=" * 100)
    add("[1] 차단된 거래 전부와 원래 손익")
    add("=" * 100)
    for k, (desc, _) in VETOES.items():
        blk = blocked_all[k].sort_values("entry_time")
        add(f"-- Loss-Veto {k}: {desc}  — 총 {len(blk)}건, 합계 "
            f"{blk.net_pct.sum():+.3f}%")
        add(f"   {'split':<6}{'날짜':<10}{'진입':<7}{'방향':<11}{'슬롯':>5}"
            f"{'손익%':>9}{'MFE%':>8}{'라벨':>11}  청산사유")
        for _, r in blk.iterrows():
            add(f"   {r['split']:<6}{r['date']:<10}{r['entry_time'][11:16]:<7}"
                f"{r['direction']:<11}{int(r['slot_number']):>5}"
                f"{r['net_pct']:>+9.3f}{r['peak_net_pct']:>+8.2f}{r['label']:>11}  "
                f"{r['exit_reason']}")
        for sp in ("TRAIN", "OOS"):
            s = blk[blk.split == sp]
            if len(s):
                add(f"     {sp}: {len(s)}건 합계 {s.net_pct.sum():+.3f}% "
                    f"(BIG_LOSS {int((s.label=='BIG_LOSS').sum())} / "
                    f"BIG_WIN {int((s.label=='BIG_WIN').sum())})")
        add("")

    # ── 2) 막은 BIG_LOSS / 놓친 BIG_WIN + 보존율 ──
    add("=" * 100)
    add("[2] 막은 BIG_LOSS / 놓친 BIG_WIN / +3% 러너 보존율")
    add("=" * 100)
    add(f"{'후보':<6}{'구간':<7}{'차단':>5}{'차단합계%':>10}"
        f"{'막은BL':>8}{'/전체':>7}{'놓친BW':>8}{'/전체':>7}{'러너보존%':>10}")
    add("-" * 100)
    for k in VETOES:
        for sp in ("TRAIN", "OOS", "ALL"):
            r = results[f"{k}|{sp}"]
            add(f"{k:<6}{sp:<7}{r['blocked']:>5}{r['blocked_sum_pct']:>+10.3f}"
                f"{r['big_loss_blocked']:>8}{r['big_loss_total']:>7}"
                f"{r['big_win_missed']:>8}{r['big_win_total']:>7}"
                f"{r['runner_keep_pct']:>10.1f}")
        add("")

    # ── 3) 성과 지표 ──
    add("=" * 100)
    add("[3] 성과 지표 (현행 vs 각 Loss-Veto 적용 후)")
    add("=" * 100)
    for sp in ("TRAIN", "OOS", "ALL"):
        add(f"-- {sp}")
        add(f"   {'안':<26}{'거래':>6}{'승률%':>8}{'단순%':>10}{'복리%':>10}"
            f"{'평균%':>9}{'PF':>9}{'MDD%':>9}{'BW':>4}{'BL':>4}")
        b = results[f"A|{sp}"]["base"]
        add(f"   {'현행 A':<26}{b['trades']:>6}{b['win_rate_pct']:>8.2f}"
            f"{b['simple_pct']:>+10.3f}{b['compound_pct']:>+10.3f}{b['avg_pct']:>+9.4f}"
            f"{b['pf']:>9.4f}{b['mdd_pct']:>9.3f}{b['big_wins']:>4}{b['big_losses']:>4}")
        for k in VETOES:
            a = results[f"{k}|{sp}"]["after"]
            add(f"   {'+ Loss-Veto ' + k:<26}{a['trades']:>6}{a['win_rate_pct']:>8.2f}"
                f"{a['simple_pct']:>+10.3f}{a['compound_pct']:>+10.3f}{a['avg_pct']:>+9.4f}"
                f"{a['pf']:>9.4f}{a['mdd_pct']:>9.3f}{a['big_wins']:>4}{a['big_losses']:>4}")
        add("")

    # ── 4) OOS 채택 판정 ──
    add("=" * 100)
    add("[4] OOS 채택 판정  (복리↑ + PF↑ + MDD 개선/동일  AND  러너 보존율 >= 80%)")
    add("=" * 100)
    verdicts = {}
    for k in VETOES:
        r = results[f"{k}|OOS"]
        b, a = r["base"], r["after"]
        c1 = a["compound_pct"] > b["compound_pct"]
        c2 = a["pf"] > b["pf"]
        c3 = a["mdd_pct"] >= b["mdd_pct"] - 1e-9      # 덜 깊거나 동일
        c4 = (r["runner_keep_pct"] or 0) >= 80.0
        ok = c1 and c2 and c3 and c4
        verdicts[k] = ok
        add(f"-- Loss-Veto {k}")
        add(f"   복리   {b['compound_pct']:+.3f}% -> {a['compound_pct']:+.3f}%  "
            f"({'PASS' if c1 else 'FAIL'})")
        add(f"   PF     {b['pf']:.4f} -> {a['pf']:.4f}  ({'PASS' if c2 else 'FAIL'})")
        add(f"   MDD    {b['mdd_pct']:.3f}% -> {a['mdd_pct']:.3f}%  "
            f"({'PASS' if c3 else 'FAIL'})")
        add(f"   러너보존 {r['runner_keep_pct']:.1f}%  ({'PASS' if c4 else 'FAIL'})")
        add(f"   => {'채택 후보' if ok else '탈락'}")
        add("")
    add("최종: " + (", ".join(f"Loss-Veto {k}" for k, v in verdicts.items() if v)
                    or "채택 가능한 후보 없음"))
    add("")

    (BASE / "filters_report.txt").write_text("\n".join(L), encoding="utf-8")
    (BASE / "filters_result.json").write_text(json.dumps({
        "thresholds": {"q60": Q60, "q75": Q75},
        "vetoes": {k: v[0] for k, v in VETOES.items()},
        "results": results, "verdicts": verdicts,
        "blocked": {k: blocked_all[k].to_dict("records") for k in VETOES},
    }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"saved {BASE / 'filters_report.txt'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
