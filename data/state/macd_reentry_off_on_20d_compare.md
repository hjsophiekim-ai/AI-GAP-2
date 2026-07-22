# MACD B + TP/SL — Continuation Re-entry OFF vs ON (≥20d)

- Generated: `2026-07-22T14:54:31`
- Days (20): 2026-06-24, 2026-06-25, 2026-06-26, 2026-06-29, 2026-06-30, 2026-07-01, 2026-07-02, 2026-07-03, 2026-07-06, 2026-07-07, 2026-07-08, 2026-07-09, 2026-07-10, 2026-07-13, 2026-07-14, 2026-07-15, 2026-07-16, 2026-07-20, 2026-07-21, 2026-07-22
- Data notes: Reused sibling date list from C:\Users\FURSYS\Desktop\AI-GAP 2\data\state\macd_vs_williams_early_20d_partial.json.
- Live `CONTINUATION_REENTRY_ENABLED` remains: **False** (confirmed unchanged; `live_flag_still_false=True`)
- Fill: next 1m open + 0.05% adverse + TradeCostEngine; base delay=1m; TP=3.0% / SL=-1.5%

## Day sources

| Day | Source |
|-----|--------|
| 2026-06-24 | synthetic_daily_anchor |
| 2026-06-25 | synthetic_daily_anchor |
| 2026-06-26 | synthetic_daily_anchor |
| 2026-06-29 | synthetic_daily_anchor |
| 2026-06-30 | synthetic_daily_anchor |
| 2026-07-01 | synthetic_daily_anchor |
| 2026-07-02 | synthetic_daily_anchor |
| 2026-07-03 | synthetic_daily_anchor |
| 2026-07-06 | synthetic_daily_anchor |
| 2026-07-07 | synthetic_daily_anchor |
| 2026-07-08 | naver_fchart |
| 2026-07-09 | naver_fchart |
| 2026-07-10 | naver_fchart |
| 2026-07-13 | naver_fchart |
| 2026-07-14 | naver_fchart |
| 2026-07-15 | naver_fchart |
| 2026-07-16 | naver_fchart |
| 2026-07-20 | naver_fchart |
| 2026-07-21 | kis_cache |
| 2026-07-22 | kis_cache |

## OFF vs ON summary

| Metric | OFF | ON | Δ |
|--------|-----|----|---|
| Round-trips | 190 | 196 | 6 |
| Net PnL | -5,106,267 | -4,008,017 | 1,098,250 |
| Return % | -51.063 | -40.08 | 10.983 |
| Profit Factor | 0.716 | 0.778 | 0.062 |
| MDD % | 55.109 | 45.687 | -9.422 |
| Win rate % | 32.11 | 33.67 | 1.56 |
| Cost/Gross % | 5.566 | 5.528 | -0.038 |
| Re-entry count | 0 | 6 | 6 |
| Re-entry Net PnL | — | 1,130,653 | — |
| Re-entry WR % | — | 83.33 | — |
| Re-entry PF | — | 81.004 | — |

## Adoption gates

| Gate | Pass? | Detail |
|------|-------|--------|
| net_pnl_increases | PASS | ON(-4008017.06) > OFF(-5106267.17) |
| pf_does_not_decrease | PASS | ON PF 0.778 ≥ OFF PF 0.716 |
| mdd_increase_le_0_2pp | PASS | MDD Δ=-9.422pp ≤ 0.2 |
| reentry_pf_ge_1_3 | PASS | reentry PF=81.004 (need ≥1.3, n=6) |
| reentry_win_rate_ge_55 | PASS | reentry WR=83.33% (need ≥55, n=6) |
| cost_gross_worsening_le_5pp | PASS | cost/gross Δ=-0.038pp (need Δ≤5pp) |

**Verdict: `ADOPT_RECOMMENDED`** (live flag stays False regardless).

## Stress (+1m fill delay)

- OFF Net=-3397816.11 PF=0.806 MDD=34.416 RT=188
- ON  Net=-2250252.84 PF=0.872 MDD=25.365 RT=194 reN=6

## Live flag confirmation

- `CONTINUATION_REENTRY_ENABLED` = `False`
- Continuation re-entry code remains present (`evaluate_continuation_reentry`).

## Artifacts

- `C:/Users/FURSYS/Desktop/AI-GAP 2/data/state/macd_reentry_off_on_20d_compare.json`
- `C:/Users/FURSYS/Desktop/AI-GAP 2/data/state/macd_reentry_off_on_20d_compare.md`
