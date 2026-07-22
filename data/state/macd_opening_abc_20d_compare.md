# MACD Opening Probe A/B/C (≥20d)

- Generated: `2026-07-22T16:27:28`
- Days: 2026-06-24, 2026-06-25, 2026-06-26, 2026-06-29, 2026-06-30, 2026-07-01, 2026-07-02, 2026-07-03, 2026-07-06, 2026-07-07, 2026-07-08, 2026-07-09, 2026-07-10, 2026-07-13, 2026-07-14, 2026-07-15, 2026-07-16, 2026-07-20, 2026-07-21, 2026-07-22
- Live `OPENING_PROBE_ENABLED`: **False** → replay verdict **`DO_NOT_ADOPT`**

## Summary

| Variant | Net | PF | MDD% | WR% | Avg 1st entry (s) | 09:00 success | Unconf exit PnL | 1st-30m PnL |
|---------|-----|----|------|-----|-------------------|---------------|-----------------|-------------|
| A NEW_TURN | 7,750,573 | 2.066 | 9.996 | 54.74 | 1095.0 | — | — | 2085234.43 |
| B 09:03 BAR | 304,060 | 2.407 | 2.055 | 33.33 | 240.0 | — | — | 304059.96 |
| C IMMEDIATE+CONFIRM | 7,421,648 | 2.047 | 10.158 | 55.43 | 1500.0 | None% | 0.0 | 1726573.8 |

## Adoption gates (C vs A)

- **net_c_gt_a**: FAIL — C net 7421648.5 > A net 7750572.7
- **pf_c_not_worse**: FAIL — C PF 2.047 ≥ A PF 2.066
- **mdd_delta_le_0_5pp**: PASS — MDD Δ=0.162pp ≤ 0.5

**Verdict: `DO_NOT_ADOPT`**

## Stress (+1m delay)

- A: Net=7869599.06 PF=2.014 MDD=13.863
- B: Net=524646.25 PF=4.95 MDD=1.246
- C: Net=7264233.82 PF=1.944 MDD=13.999

- JSON: `C:/Users/FURSYS/Desktop/AI-GAP 2/data/state/macd_opening_abc_20d_compare.json`
