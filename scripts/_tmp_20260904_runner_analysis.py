"""READ-ONLY 연구 2/2 단계1 (2026-09-04): AFTERNOON RUNNER 후보의
BIG/SUPER WIN vs LOSER 단독 피처 구분력 분석 (TRAIN 40일에서만).

OOS 20일은 여기서 절대 보지 않는다(재튜닝 방지). 출력은
data/validation/afternoon_runner/feature_rank.txt / .json.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BASE = PROJECT_ROOT / "data" / "validation" / "afternoon_runner"
CSV = BASE / "candidates.csv"
OUT_TXT = BASE / "feature_rank.txt"
OUT_JSON = BASE / "feature_rank.json"

NON_FEATURES = {
    "date", "split", "decision_idx", "flag_idx", "direction", "flag_bar_at",
    "decision_at", "a_entered", "net_pct", "net_pct_etp", "peak_net_pct",
    "trough_net_pct", "exit_reason", "exit_time", "entry_price", "exit_price",
    "hold_bars", "label", "label_etp", "exhausted", "teg_total",
}


def auc(pos: pd.Series, neg: pd.Series) -> float | None:
    """P(pos > neg) + 0.5*P(tie) — 순위기반, 분포가정 없음."""
    p = pos.dropna()
    n = neg.dropna()
    if len(p) < 3 or len(n) < 3:
        return None
    allv = pd.concat([p, n])
    ranks = allv.rank(method="average")
    rp = ranks.iloc[: len(p)].sum()
    return float((rp - len(p) * (len(p) + 1) / 2) / (len(p) * len(n)))


def cohend(pos: pd.Series, neg: pd.Series) -> float | None:
    p = pos.dropna()
    n = neg.dropna()
    if len(p) < 3 or len(n) < 3:
        return None
    sp = ((len(p) - 1) * p.var(ddof=1) + (len(n) - 1) * n.var(ddof=1)) / (len(p) + len(n) - 2)
    if sp <= 0:
        return None
    return float((p.mean() - n.mean()) / (sp ** 0.5))


def main() -> int:
    df = pd.read_csv(CSV)
    L: list[str] = []
    add = L.append

    add("=" * 100)
    add("[A] 후보군 / 라벨 분포")
    add("=" * 100)
    add(f"전체 후보 {len(df)}건")
    for split in ("TRAIN", "OOS", "ALL"):
        d = df if split == "ALL" else df[df["split"] == split]
        vc = d["label"].value_counts()
        n = len(d)
        add(f"  {split:<6} n={n:<5} "
            + "  ".join(f"{k}={int(vc.get(k, 0))}({vc.get(k, 0)/n*100:.1f}%)"
                        for k in ("SUPER_WIN", "BIG_WIN", "MID", "LOSER")))
        add(f"         평균수익 {d['net_pct'].mean():+.4f}%  합계 {d['net_pct'].sum():+.2f}%  "
            f"중앙값 {d['net_pct'].median():+.4f}%")
    add("")
    add("조기익절 필터까지 켠 라벨(net_pct_etp) 분포 — 라벨 민감도 참고:")
    for split in ("TRAIN", "OOS"):
        d = df[df["split"] == split]
        vc = d["label_etp"].value_counts()
        add(f"  {split:<6} " + "  ".join(f"{k}={int(vc.get(k, 0))}"
                                         for k in ("SUPER_WIN", "BIG_WIN", "MID", "LOSER")))
    add("")
    add("후보 구성:")
    add(f"  11:00 이후만        : {int(((df['is_afternoon']==1) & (~df['exhausted'])).sum())}")
    add(f"  3슬롯 소진 후만      : {int(((df['is_afternoon']==0) & (df['exhausted'])).sum())}")
    add(f"  둘 다               : {int(((df['is_afternoon']==1) & (df['exhausted'])).sum())}")
    add(f"  A 체인이 실제 진입한 후보: {int(df['a_entered'].sum())}")
    add("")

    # ── TRAIN 전용 단독 피처 구분력 ────────────────────────────────
    tr = df[df["split"] == "TRAIN"]
    big = tr[tr["label"].isin(["BIG_WIN", "SUPER_WIN"])]
    los = tr[tr["label"] == "LOSER"]
    mid = tr[tr["label"] == "MID"]
    add("=" * 100)
    add(f"[B] TRAIN 40일 단독 피처 구분력  (BIG+SUPER n={len(big)} vs LOSER n={len(los)}, "
        f"MID n={len(mid)})")
    add("=" * 100)

    feats = [c for c in df.columns if c not in NON_FEATURES
             and pd.api.types.is_numeric_dtype(df[c])]
    stats = []
    for f in feats:
        a = auc(big[f], los[f])
        if a is None:
            continue
        stats.append({
            "feature": f,
            "auc": round(a, 4),
            "sep": round(abs(a - 0.5) * 2, 4),      # 0=구분력 없음, 1=완전분리
            "cohen_d": round(cohend(big[f], los[f]) or 0.0, 4),
            "big_mean": round(float(big[f].mean()), 4),
            "big_med": round(float(big[f].median()), 4),
            "los_mean": round(float(los[f].mean()), 4),
            "los_med": round(float(los[f].median()), 4),
            "mid_med": round(float(mid[f].median()), 4),
            "big_n": int(big[f].notna().sum()),
            "los_n": int(los[f].notna().sum()),
        })
    stats.sort(key=lambda s: -s["sep"])

    add(f"{'피처':<24}{'AUC':>8}{'구분력':>8}{'d':>8}"
        f"{'BIG평균':>11}{'BIG중앙':>11}{'LOSER평균':>11}{'LOSER중앙':>11}{'MID중앙':>10}")
    add("-" * 100)
    for s in stats:
        add(f"{s['feature']:<24}{s['auc']:>8.3f}{s['sep']:>8.3f}{s['cohen_d']:>8.2f}"
            f"{s['big_mean']:>11.3f}{s['big_med']:>11.3f}"
            f"{s['los_mean']:>11.3f}{s['los_med']:>11.3f}{s['mid_med']:>10.3f}")
    add("")
    add("AUC > 0.5 = 값이 클수록 BIG WIN 쪽 / AUC < 0.5 = 값이 작을수록 BIG WIN 쪽")
    add("구분력 = |AUC-0.5|*2  (0 = 무구분, 1 = 완전분리)")
    add("")

    # ── 상위 피처의 임계값 후보 (TRAIN 분위수) ─────────────────────
    add("=" * 100)
    add("[C] 상위 12개 피처의 TRAIN 분위수 (임계값 설계 참고)")
    add("=" * 100)
    for s in stats[:12]:
        f = s["feature"]
        add(f"-- {f}  (AUC {s['auc']:.3f})")
        qs = [0.1, 0.25, 0.4, 0.5, 0.6, 0.75, 0.9]
        add("     전체 분위수 " + "  ".join(
            f"q{int(q*100)}={tr[f].quantile(q):.3f}" for q in qs))
        add(f"     BIG+SUPER  q10={big[f].quantile(0.1):.3f} q25={big[f].quantile(0.25):.3f} "
            f"q50={big[f].quantile(0.5):.3f} q75={big[f].quantile(0.75):.3f}")
        add(f"     LOSER      q25={los[f].quantile(0.25):.3f} q50={los[f].quantile(0.5):.3f} "
            f"q75={los[f].quantile(0.75):.3f} q90={los[f].quantile(0.9):.3f}")
        # 단독 컷 스캔
        best = []
        for q in (0.3, 0.4, 0.5, 0.6, 0.7, 0.8):
            thr = tr[f].quantile(q)
            for op in (">=", "<="):
                m = (tr[f] >= thr) if op == ">=" else (tr[f] <= thr)
                sel = tr[m]
                if len(sel) < 8:
                    continue
                nb = int(sel["label"].isin(["BIG_WIN", "SUPER_WIN"]).sum())
                nl = int((sel["label"] == "LOSER").sum())
                best.append((f"{op}{thr:.3f}", len(sel), nb, nl,
                             round(float(sel["net_pct"].mean()), 3),
                             round(nb / max(len(big), 1) * 100, 1),
                             round(1 - nl / max(len(los), 1), 3)))
        best.sort(key=lambda x: -x[4])
        for b in best[:4]:
            add(f"     컷 {b[0]:<12} n={b[1]:<4} BIG={b[2]:<3} LOSER={b[3]:<3} "
                f"평균={b[4]:+.3f}%  BIG포착률={b[5]}%  LOSER차단률={b[6]*100:.1f}%")
        add("")

    OUT_TXT.write_text("\n".join(L), encoding="utf-8")
    OUT_JSON.write_text(json.dumps({"stats": stats}, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    print(f"saved {OUT_TXT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
