# MACD B vs Williams Early 20% — ≥20 trading days

- Generated: `2026-07-22T15:00:55`
- Dates (20): `2026-06-24, 2026-06-25, 2026-06-26, 2026-06-29, 2026-06-30, 2026-07-01, 2026-07-02, 2026-07-03, 2026-07-06, 2026-07-07, 2026-07-08, 2026-07-09, 2026-07-10, 2026-07-13, 2026-07-14, 2026-07-15, 2026-07-16, 2026-07-20, 2026-07-21, 2026-07-22`
- Date sources: {"2026-06-24": "synthetic_cached_1m", "2026-06-25": "synthetic_cached_1m", "2026-06-26": "synthetic_cached_1m", "2026-06-29": "synthetic_cached_1m", "2026-06-30": "synthetic_cached_1m", "2026-07-01": "synthetic_cached_1m", "2026-07-02": "synthetic_cached_1m", "2026-07-03": "synthetic_cached_1m", "2026-07-06": "synthetic_cached_1m", "2026-07-07": "synthetic_cached_1m", "2026-07-08": "naver_fchart", "2026-07-09": "naver_fchart", "2026-07-10": "naver_fchart", "2026-07-13": "naver_fchart", "2026-07-14": "naver_fchart", "2026-07-15": "naver_fchart", "2026-07-16": "naver_fchart", "2026-07-20": "naver_fchart", "2026-07-21": "naver_fchart", "2026-07-22": "naver_fchart"}
- Capital: 10,000,000 | Fill baseline: next 1m open + 0.05% adverse + TradeCostEngine
- **Verdict: `NO_CLEAR_WINNER`** (A=1 B=6)

## Verdict reasons
- Net favors B by 4,305,627
- PF A 0.792 > B 0.589
- MDD better for B (1.932% vs 48.653%)
- Loss days/worst favor B
- A higher costs/trades
- A more delay-sensitive
- Early capture weak/noisy (earlier=95.09, success=0.0, wrong=12)
- THESIS: explore→confirm success=0% — B mainly avoids A's overtrading, not validated early capture

## Baseline summary

| Metric | A MACD_ONLY | B WILLIAMS_EARLY_20 |
|---|---:|---:|
| Cum Net | -4,477,021 | -171,394 |
| Cum Ret% | -44.7702 | -1.7139 |
| Mean / Median daily ret% | -2.2385 / -3.647 | -0.0857 / -0.0132 |
| Win/Loss days | 4/16 | 7/10 |
| RT / avg/day | 163 / 8.15 | 44 / 2.2 |
| WR% / PF / MDD% | 33.13 / 0.792 / 48.653 | 27.27 / 0.589 / 1.932 |
| Cost / cost÷gross% | 1,453,821 / 12.61 | 86,021 / 70.39 |
| Lev / Inv PnL | -4,461,534 / -15,487 | -183,988 / 12,594 |
| Worst day | 2026-07-16 (-754,548) | 2026-07-02 (-162,680) |
| Ex-best-trade Net | -4,921,407 | -202,080 |
| Signal→fill sec | 60.0 | 60.0 |

### Exit counts
- A: `{'TP': 28, 'SL': 80, 'OPPOSITE': 50, 'FORCE_1500': 5, 'EXPLORE_EXIT': 0}`
- B: `{'TP': 0, 'SL': 1, 'OPPOSITE': 1, 'FORCE_1500': 0, 'EXPLORE_EXIT': 42}`

### RANGE vs TREND
- Definition: TREND if day (high-low)/open% >= sample median else RANGE
- A: `{'RANGE': {'days': 10, 'net_pnl': -2881600.75, 'mean_ret_pct': -2.8816}, 'TREND': {'days': 10, 'net_pnl': -1595420.56, 'mean_ret_pct': -1.5954}, 'median_day_range_pct': 8.549}`
- B: `{'RANGE': {'days': 10, 'net_pnl': -70239.02, 'mean_ret_pct': -0.0702}, 'TREND': {'days': 10, 'net_pnl': -101155.26, 'mean_ret_pct': -0.1012}, 'median_day_range_pct': 8.549}`

### Strategy B explore
- `{'explore_starts': 43, 'explore_scaled': 0, 'explore_success_rate_pct': 0.0, 'explore_invalidated': 35, 'explore_timeout': 7, 'explore_pnl': -16220.09, 'avg_minutes_earlier_than_A': 95.09, 'wrong_explores_count': 12, 'wrong_explores': [{'time': '2026-06-25T12:59:00', 'direction': 'DOWN', 'true_dir': 'UP'}, {'time': '2026-06-25T13:26:00', 'direction': 'DOWN', 'true_dir': 'UP'}, {'time': '2026-06-26T13:17:00', 'direction': 'UP', 'true_dir': 'DOWN'}, {'time': '2026-06-26T13:20:00', 'direction': 'UP', 'true_dir': 'DOWN'}, {'time': '2026-06-30T13:00:00', 'direction': 'DOWN', 'true_dir': 'UP'}, {'time': '2026-06-30T13:25:00', 'direction': 'DOWN', 'true_dir': 'UP'}, {'time': '2026-07-07T13:24:00', 'direction': 'UP', 'true_dir': 'DOWN'}, {'time': '2026-07-08T10:47:00', 'direction': 'UP', 'true_dir': 'DOWN'}, {'time': '2026-07-10T12:40:00', 'direction': 'UP', 'true_dir': 'DOWN'}, {'time': '2026-07-15T11:37:00', 'direction': 'UP', 'true_dir': 'DOWN'}, {'time': '2026-07-16T11:50:00', 'direction': 'UP', 'true_dir': 'DOWN'}, {'time': '2026-07-21T09:53:00', 'direction': 'DOWN', 'true_dir': 'UP'}]}`

## Stress

| Scenario | A Net | B Net |
|---|---:|---:|
| baseline | -4,477,021 | -171,394 |
| plus_1m_delay | -3,749,153 | -138,186 |
| plus_2m_slip10 | -5,676,287 | -270,503 |

## Daily PnL (baseline)

| Day | Src | A Net | B Net | A Ret% | B Ret% |
|---|---|---:|---:|---:|---:|
| 2026-06-24 | synthetic_cached_1m | -493,857 | 134 | -4.9386 | 0.0013 |
| 2026-06-25 | synthetic_cached_1m | -216,782 | -9,018 | -2.1678 | -0.0902 |
| 2026-06-26 | synthetic_cached_1m | -476,921 | 4,830 | -4.7692 | 0.0483 |
| 2026-06-29 | synthetic_cached_1m | -472,032 | 0 | -4.7203 | 0.0 |
| 2026-06-30 | synthetic_cached_1m | -473,251 | -7,642 | -4.7325 | -0.0764 |
| 2026-07-01 | synthetic_cached_1m | -491,182 | -2,629 | -4.9118 | -0.0263 |
| 2026-07-02 | synthetic_cached_1m | -160,536 | -162,680 | -1.6054 | -1.6268 |
| 2026-07-03 | synthetic_cached_1m | -517,047 | 5,291 | -5.1705 | 0.0529 |
| 2026-07-06 | synthetic_cached_1m | -472,061 | -13,665 | -4.7206 | -0.1366 |
| 2026-07-07 | synthetic_cached_1m | -164,028 | -4,752 | -1.6403 | -0.0475 |
| 2026-07-08 | naver_fchart | 698,568 | 52,361 | 6.9857 | 0.5236 |
| 2026-07-09 | naver_fchart | 728,091 | -4,938 | 7.2809 | -0.0494 |
| 2026-07-10 | naver_fchart | -214,191 | -10,279 | -2.1419 | -0.1028 |
| 2026-07-13 | naver_fchart | -699,753 | 5,636 | -6.9975 | 0.0564 |
| 2026-07-14 | naver_fchart | 399,057 | 654 | 3.9906 | 0.0065 |
| 2026-07-15 | naver_fchart | -257,370 | -8,885 | -2.5737 | -0.0888 |
| 2026-07-16 | naver_fchart | -754,548 | 5,514 | -7.5455 | 0.0551 |
| 2026-07-20 | naver_fchart | 310,277 | 0 | 3.1028 | 0.0 |
| 2026-07-21 | naver_fchart | -707,819 | -21,327 | -7.0782 | -0.2133 |
| 2026-07-22 | naver_fchart | -41,639 | 0 | -0.4164 | 0.0 |

## Williams formula

- %R(14) = (HH-Close)/(HH-LL)*-100
- Signal = EMA(9) of %R; gap = %R − signal
- PRE does **not** use absolute −20/−80 thresholds

## Re-run
```
python scripts/compare_macd_vs_williams_early_20d.py
```
