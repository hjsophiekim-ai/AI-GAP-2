# Jul22 Strategy B mismatch audit

**Verdict: `IMPLEMENTATION_MISMATCH`**

| Source | Round-trips | Net (delay_1m_cons) |
|--------|------------:|--------------------:|
| Prior A–F `replay_jul22_macd_williams_strategies.py` **B: Hist 2-turn** | **2** | **+4.4024%** (`+440,239`) |
| New `scripts/replay_macd_hynix_jul21_22.py` → `macd_hynix_strategy.evaluate_macd_direction` | **14** | **+0.871%** (`+87,133`) |

Same 1m cache data. Same MACD EMA formula. **Different Strategy B definition + different flip/executor semantics.** Do not “fix” until the intended B is chosen.

Artifacts:
- `data/state/jul22_macd_williams_strategies_replay.json`
- `data/state/macd_hynix_jul21_22_replay.json`
- `data/state/macd_jul22_b_mismatch_audit.json`
- `data/state/macd_jul22_b_mismatch_signals.json`

Note: `scripts/compare_4_strategies_replay.py` Strategy B is **MACD hist sign-change crossover**, not Hist 2-turn. The +4.40%/2RT baseline is from the Jul22 A–F script’s **B: Hist 2-turn**.

---

## 1. Data parity

Both replays read the same KIS-mock cache files:

| Symbol | Path | Rows | Range |
|--------|------|-----:|-------|
| 000660 | `data/cache/replay_20260722_hynix_1m.csv` | 391 | 09:00 → 15:30 |
| 0193T0 | `data/cache/replay_20260722_long_1m.csv` | 391 | 09:00 → 15:30 |
| 0197X0 | `data/cache/replay_20260722_inverse_1m.csv` | 391 | 09:00 → 15:30 |

Sample 000660 OHLC (identical input for both):

| Time | O | H | L | C |
|------|--:|--:|--:|--:|
| 10:42 | 1,981,000 | 1,985,000 | 1,976,000 | 1,976,000 |
| 12:33 | 1,941,000 | 1,944,000 | 1,940,000 | 1,942,500 |
| 15:00 | 1,949,000 | 1,949,000 | 1,949,000 | 1,949,000 |

Resample / MACD on aligned bars:
- Prior `resample_3m`: requires `count >= 3` per bucket.
- New `resample_completed_3m`: no count filter; drops NaN close.
- Only incomplete bucket: **15:30** (count=1) — irrelevant to B signals.
- **max \|hist_prior − hist_new\| on aligned bars = 0.0** → data/MACD series are not the divergence source.

---

## 2. Code + Jul22 concrete comparison

### MACD EMA init

| Item | Prior | New |
|------|-------|-----|
| Code | `scripts/replay_jul22_macd_williams_strategies.py` `macd_series` L247–253 | `app/trading/macd_hynix_strategy.py` `macd_components` L86–96 |
| Formula | `ewm(span=12/26/9, adjust=False)` | identical |
| Min bars | signal loop `if i < 26: continue` | `len(bars) < MACD_SLOW` (26) then evaluate |
| Jul22 effect | First eligible index **i=26 → close 10:21**; UP onset at **10:15 (i=24) is skipped** | At close **10:18**, 26 completed bars exist → **fires UP_RED** |

### 1m→3m resample

| Item | Prior | New |
|------|-------|-----|
| Code | `resample_3m` L230–238 | `resample_completed_3m` L59–83 |
| Label | default pandas `resample("3min")` (left-labeled window start) | same |
| Closed | completed when `start+3m <= asof` | same (`now` cutoff) |
| Full-bucket rule | **keeps only count≥3** | no count filter |
| Jul22 | identical hist through signal hours (diff only 15:30 stub) | same |

### Completed-bar rule

Both only act on completed 3m closes (`close_time = bar_start + 3m`). Not a mismatch driver.

### Histogram 2-turn definition (**PRIMARY MISMATCH**)

**Prior `signals_B` (L514–543):**

```text
UP:   h1>0 AND h2>0 AND d1>0 AND d2>0   # same COLOR (hist>0) + 2 up-turns
DOWN: h1<0 AND h2<0 AND d1<0 AND d2<0   # same COLOR (hist<0) + 2 down-turns
Fire only on ONSET (previous bar did not already qualify).
```

**New `_pattern_direction` / `evaluate_macd_direction` (L99–176):**

```text
UP_RED:   h1>h2>h3 (two positive deltas) — NO hist sign requirement
DOWN_BLUE: h1<h2<h3 (two negative deltas) — NO hist sign requirement
Fire on FIRST TURN vs last_signal_direction (not vs pattern-onset edge).
```

Jul22 example of sign-gate gap:

| Close | Last3 hist | Prior B | New B |
|-------|------------|---------|-------|
| 10:33 | 2043, 1535, 859 (all **>0**, falling) | no DOWN (needs hist<0) | **DOWN_BLUE** |
| 10:51 | −1285, −969, −865 (all **<0**, rising) | no UP (needs hist>0) | **UP_RED** |
| 10:42 | 335, −36, −897 | **DOWN onset** | already DOWN from 10:33 → no new_signal |
| 12:33 | −738, 651, 755 | **UP onset** (color now green) | already UP from 12:21 → no new_signal |

### Direction state / flip / same-dir / re-entry

| Item | Prior | New |
|------|-------|-----|
| Storage | no persistent `direction_state`; emits every onset | `last_signal_direction` + `last_signal_bar_ts` in replay loop |
| Same-dir repeat signals | emitted (e.g. 5 DOWN onsets 10:42–12:12) | suppressed (`last_dir == pattern`) |
| Executor same-dir while held | **ignored** (`execute_signal_strategy` L828–834) | N/A (no same-dir new_signal) |
| Opposite switch | yes | yes on every opposite first-turn |
| Reset after TP/SL | N/A (no TP/SL in this replay) | N/A — `jul21_22` replay has **no TP/SL**; only SWITCH / 15:00_FORCE |
| Same MACD dir re-entry after flat | only via new opposite then back, or new onset while flat | after flat would need first-turn again; not exercised on Jul22 (always flipped before flat) |
| Jul22 RT math | **10 signals → 2 RTs** (enter 10:42 DOWN, switch 12:33 UP, force 15:15) | **14 signals → 14 RTs** (every flip trades) |

### Fill timing / slippage / costs (**secondary — PnL, not +12 count**)

| Item | Prior delay_1m_cons | New delay_1m_cons |
|------|---------------------|-------------------|
| Code | `resolve_fill` L280–328: next open **strictly after** signal minute | `_fill_price` L113–137: first bar with `datetime >= signal_close` |
| Jul22 @ 10:42 BUY 0197X0 | **10:43** open → 9504.75 | **10:42** open → 9439.72 |
| Jul22 @ 12:33 BUY 0193T0 | **12:34** → 16028.01 | **12:33** → 16003.00 |
| Cost | flat `RT_COST_PCT=0.05%` of entry notional | `TradeCostEngine` market round-trip |
| Force exit | **15:15** | **15:00** |
| Entry cutoff | 14:50 | 14:55 |

Fill/cost/force differences change marks on shared conceptual trades; they do **not** create the +12 extra round-trips.

---

## 3. Side-by-side signals (Jul22)

### Prior B signals (10) — executor opens only 2 trades

| Time | Dir | Kind | Last5 hist | Opens trade? | Exit if closes |
|------|-----|------|------------|--------------|----------------|
| 10:42 | DOWN | HIST_2DN_TURN | 1838, 2043, 1535, 859, 335→…→−897 | **YES** (flat→inv) | — |
| 11:03 | DOWN | HIST_2DN_TURN | … | no (same dir held) | — |
| 11:36 | DOWN | HIST_2DN_TURN | … | no | — |
| 11:57 | DOWN | HIST_2DN_TURN | … | no | — |
| 12:12 | DOWN | HIST_2DN_TURN | … | no | — |
| 12:33 | UP | HIST_2UP_TURN | … | **YES** (opposite switch) | closes 10:42 DOWN via `OPPOSITE:HIST_2UP_TURN` |
| 12:42 | UP | HIST_2UP_TURN | … | no | — |
| 13:06 | UP | HIST_2UP_TURN | … | no | — |
| 13:24 | UP | HIST_2UP_TURN | … | no | — |
| 14:00 | UP | HIST_2UP_TURN | … | no | — |
| 15:15 | — | force | — | closes UP | `15:15_FORCE_CLOSE` |

Prior trades (delay_1m_cons): DOWN 10:42→12:33 net **+426,626**; UP 12:33→15:15 net **+13,613**.

### New B signals (14) — all open trades

| Time | Last5 hist | Prev dir | New dir | New entry | Class | Exit reason |
|------|------------|----------|---------|-----------|-------|-------------|
| 10:18 | −844, −197, 218, 785, 1480 | None | UP_RED | yes | WARMUP | SWITCH_TO_DOWN_BLUE |
| 10:33 | 1641, 1838, 2043, 1535, 859 | UP_RED | DOWN_BLUE | yes | **NO_SIGN_GATE** | SWITCH_TO_UP_RED |
| 10:51 | −36, −897, −1285, −969, −865 | DOWN_BLUE | UP_RED | yes | **NO_SIGN_GATE** | SWITCH_TO_DOWN_BLUE |
| 11:03 | −865, −579, −575, −873, −1217 | UP_RED | DOWN_BLUE | yes | MATCH_PRIOR | SWITCH_TO_UP_RED |
| 11:15 | −1217, −1377, −1473, −1426, −1352 | DOWN_BLUE | UP_RED | yes | **NO_SIGN_GATE** | SWITCH_TO_DOWN_BLUE |
| 11:36 | −526, −406, −169, −318, −477 | UP_RED | DOWN_BLUE | yes | MATCH_PRIOR | SWITCH_TO_UP_RED |
| 12:21 | −2390, −3226, −4025, −3793, −3492 | DOWN_BLUE | UP_RED | yes | **NO_SIGN_GATE** | SWITCH_TO_DOWN_BLUE |
| 12:57 | 1897, 2798, 3007, 2541, 1990 | UP_RED | DOWN_BLUE | yes | **NO_SIGN_GATE** | SWITCH_TO_UP_RED |
| 13:06 | 2541, 1990, 1983, 1995, 2141 | DOWN_BLUE | UP_RED | yes | MATCH_PRIOR | SWITCH_TO_DOWN_BLUE |
| 13:15 | 1995, 2141, 2167, 1983, 1738 | UP_RED | DOWN_BLUE | yes | **NO_SIGN_GATE** | SWITCH_TO_UP_RED |
| 13:24 | 1983, 1738, 1654, 1735, 2104 | DOWN_BLUE | UP_RED | yes | MATCH_PRIOR | SWITCH_TO_DOWN_BLUE |
| 13:30 | 1654, 1735, 2104, 1735, 1493 | UP_RED | DOWN_BLUE | yes | **NO_SIGN_GATE** | SWITCH_TO_UP_RED |
| 14:00 | 41, 26, 26, 33, 46 | DOWN_BLUE | UP_RED | yes | MATCH_PRIOR | SWITCH_TO_DOWN_BLUE |
| 14:39 | 143, 145, 145, 144, 143 | UP_RED | DOWN_BLUE | yes | **NO_SIGN_GATE** | 15:00_FORCE |

---

## 4. Classify the extra ~12 trades (new module only)

New RT=14 vs prior RT=2 → **+12**. Every new signal opens a trade. Bucket by **why the signal exists / differs from prior B**:

| Bucket | Count of new signals/trades | Impact on +12 |
|--------|----------------------------:|---------------|
| **Missing hist sign/color gate** (`NO_SIGN_GATE`) | **8** | **Dominant** — flips on mono hist while still green/red |
| Warm-up `i<26` skip vs `len>=26` evaluate (`WARMUP…`) | **1** | Starts cascade at 10:18 before prior’s first legal bar |
| Time-aligned with a prior onset (`MATCH_PRIOR_ONSET`) | **5** | Would not alone produce 14 RTs; in this run they are intermediate flips inside the wrong-color cascade |
| Same-direction repeat signal | **0** | Prior has these; new suppresses them |
| De-facto re-entry after TP | **0** | TP not used in either Jul22 B replay |
| De-facto re-entry after SL | **0** | SL not used |
| Resample difference | **0** | hist_diff=0 on signal bars |
| MACD init formula difference | **0** | identical `adjust=False` EMAs |
| Fill/cost/force-exit | **0** for count | Secondary PnL only |

Causal chain for the +12:

1. New fires **10:18 UP** (warm-up) and **10:33 DOWN while hist still >0** (no sign gate).
2. That starts rapid opposite first-turns (many still wrong-color).
3. Prior instead waits for **true color+onset** at **10:42 DOWN**, ignores further DOWN onsets, switches once at **12:33 UP** → 2 RTs.

---

## 5. Conclusion — ranked mismatch causes

**Verdict: `IMPLEMENTATION_MISMATCH`**

Ranked by impact on the +12 extra trades:

1. **Hist sign/color gate removed in new B** (highest impact, ~8/14 signals)  
   Prior requires 2 bars same hist sign matching direction; new only needs 3-bar monotonic hist. Concrete Jul22: 10:33 DOWN on +hist; 10:51/11:15/12:21 UP on −hist; 12:57/13:15/13:30/14:39 DOWN on +hist.

2. **Flip / executor semantics** (amplifies #1 into +12 RTs)  
   Prior: many same-dir onset events, but held position ignores same-dir → 10 signals / 2 RTs.  
   New: every opposite first-turn switches → 14 signals / 14 RTs.

3. **Warm-up eligibility** (1 early signal that starts the cascade)  
   Prior skips `i<26` (misses 10:15 UP onset; no fire at 10:18 continuation).  
   New evaluates at 10:18 and arms UP_RED.

4. **Fill lag / cost engine / 15:00 vs 15:15 force** (PnL only)  
   New fills at signal-minute open (avg delay 0s); prior at +1m. Not the source of +12 trades.

**Not causes:** different CSV sources, MACD EMA formula, 3m label/closed convention, TP/SL re-entry (disabled / unused in these replays).

Same data + the **prior** Hist 2-turn definition must reproduce 2 RT / ~+4.4%. The new module implements a **different** “monotonic hist first-turn” B, so divergence is expected until definitions are unified.
