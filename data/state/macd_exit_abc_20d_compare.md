# MACD Signed-B Exit A/B/C (≥20d)

- Generated: `2026-07-23T07:48:49`
- Days (20): 2026-06-24, 2026-06-25, 2026-06-26, 2026-06-29, 2026-06-30, 2026-07-01, 2026-07-02, 2026-07-03, 2026-07-06, 2026-07-07, 2026-07-08, 2026-07-09, 2026-07-10, 2026-07-13, 2026-07-14, 2026-07-15, 2026-07-16, 2026-07-20, 2026-07-21, 2026-07-22
- Entry: signed B NEW_TURN_ONLY (`evaluate_macd_direction`)
- Fill: next 1m open + 0.05% adverse + costs

## Profit-lock proxy (variant C)

Peak net PnL% uses intra-bar favorable extreme (1m high for long ETF, low for inverse) at each minute — approximating 5s polling within 1m resolution. Profit-lock giveback is evaluated on bar close vs running peak; lock activates at +1.5% net, exits when giveback ≥ 0.8pp.

## Summary

| Variant | Cum Net | Mean daily% | Median daily% | PF | MDD% | WR% | Trades | Avg hold (m) | Max hold (m) |
|---------|---------|-------------|---------------|----|------|-----|--------|--------------|--------------|
| A FIXED_TP | 6,556,058 | 3.278 | 4.7567 | 1.906 | 12.743 | 53.19 | 94 | 22.37 | 78.0 |
| B OPPOSITE_ONLY | 33,277,980 | 16.639 | 10.4368 | 5.305 | 5.968 | 47.87 | 94 | 46.61 | 171.0 |
| C PROFIT_LOCK | 33,370,690 | 16.6853 | 8.0159 | 6.796 | 3.121 | 60.22 | 93 | 34.88 | 169.0 |

## Win / loss & capture

| Variant | Avg win | Avg loss | Avg capture | Med capture | >3% trades | Extra vs +3% TP | Avg giveback wait |
|---------|---------|----------|-------------|-------------|------------|-----------------|-------------------|
| FIXED_TP | 275,875 | -164,492 | 0.5972 | 0.8894 | 39 | 571,466 | 1.4313 |
| OPPOSITE_ONLY | 911,287 | -157,754 | 0.4713 | 0.4263 | 39 | 26,692,627 | 2.2277 |
| PROFIT_LOCK | 698,724 | -155,618 | 0.584 | 0.7139 | 30 | 26,056,327 | 1.0926 |

## Exit reason counts

- A: {'TP_EXIT': 39, 'OPPOSITE_SWITCH': 13, 'SL_EXIT': 37, '15:00_FORCE_LIQUIDATE': 5}
- B: {'OPPOSITE_SWITCH': 38, '15:00_FORCE_LIQUIDATE': 15, 'SL_EXIT': 40, 'EOD_FLAT': 1}
- C: {'PROFIT_LOCK_GIVEBACK': 43, '15:00_FORCE_LIQUIDATE': 14, 'OPPOSITE_SWITCH': 9, 'SL_EXIT': 27}

## Adoption gates (C vs A)

- **net_c_gt_a**: PASS — C net 33,370,690 > A net 6,556,058
- **pf_c_not_worse**: PASS — C PF 6.796 ≥ A PF 1.906
- **mdd_delta_le_0_5pp**: PASS — MDD Δ=-9.622pp ≤ 0.5

**Verdict: `ADOPT_C`** (live enable: False)

## Stress

| Scenario | A Net | A PF | A MDD | B Net | C Net |
|----------|-------|------|-------|-------|-------|
| baseline | 6,556,058 | 1.906 | 12.743 | 33,277,980 | 33,370,690 |
| plus_1m_delay | 8,280,355 | 2.2 | 12.938 | 33,214,260 | 32,485,653 |
| plus_2m_slip10 | 8,234,829 | 2.225 | 10.721 | 30,824,361 | 31,133,165 |

- JSON: `C:/Users/FURSYS/Desktop/AI-GAP 2/data/state/macd_exit_abc_20d_compare.json`
