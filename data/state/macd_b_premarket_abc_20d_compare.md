# MACD B — Premarket handling A/B/C (≥20d)

- Generated: `2026-07-22T16:01:02`
- Days (20): 2026-06-24, 2026-06-25, 2026-06-26, 2026-06-29, 2026-06-30, 2026-07-01, 2026-07-02, 2026-07-03, 2026-07-06, 2026-07-07, 2026-07-08, 2026-07-09, 2026-07-10, 2026-07-13, 2026-07-14, 2026-07-15, 2026-07-16, 2026-07-20, 2026-07-21, 2026-07-22
- Capital: 10,000,000 | TP +3.0% / SL -1.5% | fill: next 1m open + adverse + TradeCostEngine | CONTINUATION_REENTRY=off
- Signed B helpers: `evaluate_macd_direction` / `signed_hist_two_turn_*`
- Warm-up: prior day last 100 completed 3m bars (≈300 1m)
- **Verdict: `IGNORE_PREMARKET`**

## Verdict reasons
- Real KIS NX NXT premaket coverage only 5% of days (1/20); remainder is 18 gap-proxy + 1 missing.
- Scores: {'IGNORE_PREMARKET': 5933325.9, 'FULL_ENTRY_AT_OPEN': 5173912.6, 'HALF_ENTRY_THEN_CONFIRM': 5629955.1}
- Best score: IGNORE_PREMARKET

## Premarket coverage (000660 08:00–08:50)

- Real KIS NX: **1** / 20 (5.0%)
- Overnight-gap proxy: **18**
- Missing: **1**
- Notes: KIS NX intraday API is today-only; historical minutes use overnight-gap proxy unless a local nxt_premarket cache exists.

| Day | Session src | Premarket src | Bars |
|-----|-------------|---------------|-----:|
| 2026-06-24 | synthetic_cached_1m | missing | 0 |
| 2026-06-25 | synthetic_cached_1m | synthetic_overnight_gap_proxy | 51 |
| 2026-06-26 | synthetic_cached_1m | synthetic_overnight_gap_proxy | 51 |
| 2026-06-29 | synthetic_cached_1m | synthetic_overnight_gap_proxy | 51 |
| 2026-06-30 | synthetic_cached_1m | synthetic_overnight_gap_proxy | 51 |
| 2026-07-01 | synthetic_cached_1m | synthetic_overnight_gap_proxy | 51 |
| 2026-07-02 | synthetic_cached_1m | synthetic_overnight_gap_proxy | 51 |
| 2026-07-03 | synthetic_cached_1m | synthetic_overnight_gap_proxy | 51 |
| 2026-07-06 | synthetic_cached_1m | synthetic_overnight_gap_proxy | 51 |
| 2026-07-07 | synthetic_cached_1m | synthetic_overnight_gap_proxy | 51 |
| 2026-07-08 | naver_fchart | synthetic_overnight_gap_proxy | 51 |
| 2026-07-09 | naver_fchart | synthetic_overnight_gap_proxy | 51 |
| 2026-07-10 | naver_fchart | synthetic_overnight_gap_proxy | 51 |
| 2026-07-13 | naver_fchart | synthetic_overnight_gap_proxy | 51 |
| 2026-07-14 | naver_fchart | synthetic_overnight_gap_proxy | 51 |
| 2026-07-15 | naver_fchart | synthetic_overnight_gap_proxy | 51 |
| 2026-07-16 | naver_fchart | synthetic_overnight_gap_proxy | 51 |
| 2026-07-20 | naver_fchart | synthetic_overnight_gap_proxy | 51 |
| 2026-07-21 | naver_fchart | synthetic_overnight_gap_proxy | 51 |
| 2026-07-22 | naver_fchart | kis_nx_cached | 51 |

## Baseline summary

| Metric | A IGNORE | B FULL_OPEN | C HALF_CONFIRM |
|--------|---------:|------------:|---------------:|
| Net PnL | 6,251,746 | 5,681,258 | 6,061,940 |
| Ret % | 62.517 | 56.813 | 60.619 |
| PF | 1.845 | 1.699 | 1.791 |
| MDD % | 13.689 | 16.527 | 15.111 |
| Win rate % | 52.63 | 51.52 | 52.04 |
| Round-trips | 95 | 99 | 98 |
| First-30m PnL Σ | 1,418,554 | 956,832 | 1,290,412 |
| Premaket held % | — | 20.0 | 20.0 |
| False PM days | 0 | 1 | 1 |
| False PM loss | 0 | -56,485 | -28,205 |
| PM entry Net | 0 | -350,832 | 72,129 |

## Early-entry effect vs A

- B vs A: `{'n_compared': 20, 'mean_sec_earlier_than_a': 123.0, 'median_sec_earlier_than_a': 0.0, 'days_earlier': 5, 'days_later': 0, 'days_same': 15, 'first_30m_pnl_delta_vs_a': -461721.42, 'net_delta_vs_a': -570488.38}`
- C vs A: `{'n_compared': 20, 'mean_sec_earlier_than_a': 108.0, 'median_sec_earlier_than_a': 0.0, 'days_earlier': 4, 'days_later': 0, 'days_same': 16, 'first_30m_pnl_delta_vs_a': -128141.61, 'net_delta_vs_a': -189805.92}`

## Daily returns / first entry

| Day | A Net | B Net | C Net | A 1st | B 1st | C 1st | PM dir | Held? |
|-----|------:|------:|------:|-------|-------|-------|--------|-------|
| 2026-06-24 | 623,890 | 623,890 | 623,890 | UP_RED@11:58 | UP_RED@11:58 | UP_RED@11:58 | — | None |
| 2026-06-25 | 363,761 | 363,761 | 363,761 | UP_RED@09:07 | UP_RED@09:07 | UP_RED@09:07 | — | None |
| 2026-06-26 | 953,769 | 953,769 | 953,769 | DOWN_BLUE@09:07 | DOWN_BLUE@09:07 | DOWN_BLUE@09:07 | — | None |
| 2026-06-29 | 233,780 | 175,950 | 204,564 | DOWN_BLUE@09:07 | UP_RED@09:01 | UP_RED@09:01 | UP_RED | False |
| 2026-06-30 | 750,904 | 750,904 | 750,904 | DOWN_BLUE@09:07 | DOWN_BLUE@09:07 | DOWN_BLUE@09:07 | — | None |
| 2026-07-01 | 940,394 | 937,461 | 931,242 | DOWN_BLUE@09:22 | DOWN_BLUE@09:01 | DOWN_BLUE@09:01 | DOWN_BLUE | True |
| 2026-07-02 | 443,282 | 443,282 | 443,282 | DOWN_BLUE@09:07 | DOWN_BLUE@09:07 | DOWN_BLUE@09:07 | — | None |
| 2026-07-03 | 980,154 | 980,154 | 980,154 | UP_RED@09:13 | UP_RED@09:13 | UP_RED@09:13 | — | None |
| 2026-07-06 | 936,083 | 936,083 | 936,083 | DOWN_BLUE@09:22 | DOWN_BLUE@09:22 | DOWN_BLUE@09:22 | — | None |
| 2026-07-07 | 443,031 | 443,031 | 443,031 | DOWN_BLUE@09:07 | DOWN_BLUE@09:07 | DOWN_BLUE@09:07 | — | None |
| 2026-07-08 | 700,526 | 700,526 | 700,526 | UP_RED@09:22 | UP_RED@09:22 | UP_RED@09:22 | — | None |
| 2026-07-09 | -559,094 | -559,094 | -559,094 | UP_RED@09:07 | UP_RED@09:07 | UP_RED@09:07 | — | None |
| 2026-07-10 | 4,244 | 4,244 | 4,244 | UP_RED@09:07 | UP_RED@09:07 | UP_RED@09:07 | — | None |
| 2026-07-13 | -151,033 | -151,033 | -151,033 | DOWN_BLUE@09:04 | DOWN_BLUE@09:04 | DOWN_BLUE@09:04 | — | None |
| 2026-07-14 | -1,165,591 | -1,651,019 | -1,408,047 | DOWN_BLUE@09:07 | UP_RED@09:01 | UP_RED@09:01 | UP_RED | False |
| 2026-07-15 | 590,766 | 590,766 | 590,766 | UP_RED@09:07 | UP_RED@09:07 | UP_RED@09:07 | — | None |
| 2026-07-16 | -169,432 | -169,432 | -169,432 | DOWN_BLUE@09:04 | DOWN_BLUE@09:04 | DOWN_BLUE@09:04 | — | None |
| 2026-07-20 | -796,982 | -796,982 | -796,982 | UP_RED@09:19 | UP_RED@09:19 | UP_RED@09:19 | — | None |
| 2026-07-21 | 1,160,441 | 1,343,261 | 1,251,459 | DOWN_BLUE@09:04 | UP_RED@09:01 | UP_RED@09:01 | UP_RED | False |
| 2026-07-22 | -31,148 | -238,265 | -31,148 | UP_RED@09:07 | DOWN_BLUE@09:02 | UP_RED@09:07 | DOWN_BLUE | False |

## Stress

| Scenario | A Net | B Net | C Net | A MDD | B MDD | C MDD |
|----------|------:|------:|------:|------:|------:|------:|
| baseline | 6,251,746 | 5,681,258 | 6,061,940 | 13.689 | 16.527 | 15.111 |
| plus_1m_delay | 8,109,334 | 8,043,203 | 8,099,801 | 13.056 | 13.171 | 13.349 |
| plus_2m_slip10 | 8,075,054 | 7,914,945 | 7,881,567 | 10.809 | 10.987 | 11.369 |

## Data gaps

- Naver fchart / local `replay_*_hynix_1m.csv` contain **no** bars before 09:00.
- KIS `inquire-time-itemchartprice` + `FID_COND_MRKT_DIV_CODE=NX` returns **today's** NXT 08:00–08:50 only (date filter ignored).
- Historical days without a cached NX file use `synthetic_overnight_gap_proxy` (prev close→open linear 08:00–08:50) so B/C entry logic is measurable; treat those days as proxy, not true NXT.

## Re-run
```
python scripts/compare_macd_b_premarket_abc_20d.py
```
