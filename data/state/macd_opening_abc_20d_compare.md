# MACD Opening Probe A/B/C (≥20d)

- Generated: `2026-07-23T07:48:31`
- Days: 2026-06-24, 2026-06-25, 2026-06-26, 2026-06-29, 2026-06-30, 2026-07-01, 2026-07-02, 2026-07-03, 2026-07-06, 2026-07-07, 2026-07-08, 2026-07-09, 2026-07-10, 2026-07-13, 2026-07-14, 2026-07-15, 2026-07-16, 2026-07-20, 2026-07-21, 2026-07-22
- Live `OPENING_PROBE_ENABLED`: **False** → replay verdict **`INSUFFICIENT_SAMPLE`**

## Summary

| Variant | Net | PF | MDD% | WR% | Avg 1st entry (s) | 09:00 success | Unconf exit PnL | 1st-30m PnL |
|---------|-----|----|------|-----|-------------------|---------------|-----------------|-------------|
| A NEW_TURN | 7,750,573 | 2.066 | 9.996 | 54.74 | 1095.0 | — | — | 2085234.43 |
| B 09:03 BAR | 304,060 | 2.407 | 2.055 | 33.33 | 240.0 | — | — | 304059.96 |
| C IMMEDIATE+CONFIRM | 7,750,573 | 2.066 | 9.996 | 54.74 | 1095.0 | None | 0.0 | 2085234.43 |

## Adoption gates (C vs A)

- **sufficient_probe_sample**: FAIL — open_probe_attempts=0; cannot judge opening-probe merit

**Verdict: `INSUFFICIENT_SAMPLE`**

## Stress (+1m delay)

- A: Net=7869599.06 PF=2.014 MDD=13.863
- B: Net=524646.25 PF=4.95 MDD=1.246
- C: Net=7869599.06 PF=2.014 MDD=13.863

- JSON: `C:/Users/FURSYS/Desktop/AI-GAP 2/data/state/macd_opening_abc_20d_compare.json`
