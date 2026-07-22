# MACD Jul21 Trade Audit — Strategy B Implementation Comparison

**Generated:** 2026-07-22 (read-only audit)  
**Sources:** `data/state/macd_hynix_jul21_22_replay.json`, `data/state/jul22_macd_williams_strategies_replay.json`, `app/trading/macd_hynix_strategy.py`, `scripts/replay_macd_hynix_jul21_22.py`, `scripts/replay_jul22_macd_williams_strategies.py`  
**Detail JSON:** `data/state/macd_jul21_trade_audit.json`  
**Fill scenario:** `delay_1m_cons` (intended: next 1m open + 0.05% adverse + costs)

---

## Verdict

### `IMPLEMENTATION_MISMATCH`

Prior compare **“B: Hist 2-turn”** ≠ new module / live **`evaluate_macd_direction` Strategy B**.

| Check | Old B (`signals_B`) | New B (`macd_hynix_strategy`) |
|---|---|---|
| Signal definition | 2 consecutive hist deltas **and** last 2 hist **same sign** (UP: both `>0`, DOWN: both `<0`) | Monotonic last-3 hist only (`h1>h2>h3` or `h1<h2<h3`) — **no sign filter** |
| Anti-spam / state | Edge: fire only when prior bar did **not** already qualify | `last_signal_direction` flip gate: fire only if `last_dir !=` new dir |
| Jul22 `delay_1m_cons` | **2 RT, Net +440,239 (+4.40%)** | **14 RT, Net +87,133 (+0.87%)** |
| Jul21 signals | **12** old-B edges (recomputed) | **17** new-B flips → **16 RT** |

**Not apples-to-apples:** Jul21 “16 RT” is the **new** slope-only module on 2026-07-21. The famous **+4.40% / 2 RT** is **old** Hist-2-turn B on **2026-07-22**. Same label “B”, different rules and different day.

---

## Count reconcile (Jul21 new module)

| Metric | Value | Note |
|---|---:|---|
| Signals | **17** | Includes post-cutoff 15:15 |
| Round-trips | **16** | Matches earlier “16 RT / 17 signals” |
| Orphan signal | 15:15 `DOWN_BLUE` | After `ENTRY_CUTOFF` 14:55; position already `15:00_FORCE` |

Day totals (`delay_1m_cons`): **Net −956,616 (−9.57%)**, Gross −817,971, Costs **138,646**, WR 31.25%, MDD 10.1%.

---

## 1. Per-trade table (all 16 Jul21 RT)

Fill model as stored: entry/exit times equal signal close minute (see fill mismatch §2). All exits are opposite switch except #16 (`15:00_FORCE`). No TP/SL exits in this replay.

| # | Entry→Exit | Sym / Dir | Prev→New state | Flip type | Hist last5 (oldest→newest) | Δ hist (4) | Exit | Gross | Cost | Net |
|---:|---|---|---|---|---|---|---|---:|---:|---:|
| 1 | 10:27→10:36 | 0197X0 DOWN | NONE→DOWN | **Initial** | 2729.9, 1975.3, 2131.8, 1314.9, **629.5** | −755, +157, −817, −685 | OPP | −275,838 | 8,873 | **−284,711** |
| 2 | 10:36→10:45 | 0193T0 UP | DOWN→UP | True flip | 1314.9, 629.5, 380.2, 1309.9, **1459.1** | −685, −249, +930, +149 | OPP | −2,901 | 8,740 | **−11,641** |
| 3 | 10:45→11:27 | 0197X0 DOWN | UP→DOWN | True flip | 1309.9, 1459.1, 1809.8, 1753.9, **1250.5** | +149, +351, −56, −503 | OPP | +217,037 | 8,823 | **+208,214** |
| 4 | 11:27→11:57 | 0193T0 UP | DOWN→UP | True flip | −3573.6, −3770.0, −4405.7, −4060.7, **−3556.6** | −196, −636, +345, +504 | OPP | +156,395 | 8,986 | **+147,409** |
| 5 | 11:57→12:03 | 0197X0 DOWN | UP→DOWN | True flip | 387.4, 691.7, 943.1, 380.3, **293.1** | +304, +251, −563, −87 | OPP | −129,464 | 8,991 | **−138,455** |
| 6 | 12:03→12:27 | 0193T0 UP | DOWN→UP | True flip | 943.1, 380.3, 293.1, 380.2, **826.4** | −563, −87, +87, +446 | OPP | +86,776 | 8,968 | **+77,808** |
| 7 | 12:27→12:36 | 0197X0 DOWN | UP→DOWN | True flip | 1431.0, 1771.4, 2223.8, 1760.3, **1253.6** | +340, +452, −463, −507 | OPP | −221,612 | 8,890 | **−230,502** |
| 8 | 12:36→12:48 | 0193T0 UP | DOWN→UP | True flip | 1760.3, 1253.6, 1190.3, 1277.8, **1643.8** | −507, −63, +88, +366 | OPP | +72,824 | 8,821 | **+64,003** |
| 9 | 12:48→12:54 | 0197X0 DOWN | UP→DOWN | True flip | 1643.8, 2161.4, 2550.6, 2450.1, **2170.0** | +518, +389, −100, −280 | OPP | −234,378 | 8,742 | **−243,120** |
| 10 | 12:54→13:00 | 0193T0 UP | DOWN→UP | True flip | 2550.6, 2450.1, 2170.0, 2297.7, **2796.6** | −100, −280, +128, +499 | OPP | −107,483 | 8,570 | **−116,053** |
| 11 | 13:00→13:39 | 0197X0 DOWN | UP→DOWN | True flip | 2170.0, 2297.7, 2796.6, 2219.3, **1535.2** | +128, +499, −577, −684 | OPP | +119,664 | 8,578 | **+111,085** |
| 12 | 13:39→13:57 | 0193T0 UP | DOWN→UP | True flip | −2816.6, −3111.5, −3287.4, −3180.5, **−3086.1** | −295, −176, +107, +94 | OPP | −167,518 | 8,544 | **−176,061** |
| 13 | 13:57→14:06 | 0197X0 DOWN | UP→DOWN | True flip | −2805.4, −2258.7, −2039.5, −2606.8, **−3120.8** | +547, +219, −567, −514 | OPP | −77,680 | 8,424 | **−86,104** |
| 14 | 14:06→14:15 | 0193T0 UP | DOWN→UP | True flip | −2606.8, −3120.8, −3248.3, −3183.3, **−2802.3** | −514, −127, +65, +381 | OPP | −142,599 | 8,324 | **−150,923** |
| 15 | 14:15→14:27 | 0197X0 DOWN | UP→DOWN | True flip | −3183.3, −2802.3, −2364.4, −2419.0, **−2837.8** | +381, +438, −55, −419 | OPP | −70,932 | 8,219 | **−79,151** |
| 16 | 14:27→15:00 | 0193T0 UP | DOWN→UP | True flip | −2837.8, −3584.0, −3786.1, −3245.1, **−2323.2** | −746, −202, +541, +922 | 15:00 | −40,260 | 8,154 | **−48,415** |

### Signal IDs / episode IDs

| # | `signal_id` | `direction_episode_id` | Old-B sign OK? |
|---:|---|---|---|
| 1 | `MACD3M:DOWN_BLUE:2026-07-21T10:24:00` | `EP:DOWN_BLUE:…10:24:00` | **No** (pos hist falling) |
| 2 | `MACD3M:UP_RED:…10:33:00` | `EP:UP_RED:…10:33:00` | Yes |
| 3 | `MACD3M:DOWN_BLUE:…10:42:00` | `EP:DOWN_BLUE:…10:42:00` | **No** |
| 4 | `MACD3M:UP_RED:…11:24:00` | `EP:UP_RED:…11:24:00` | **No** (neg hist rising = “UP”) |
| 5 | `MACD3M:DOWN_BLUE:…11:54:00` | `EP:DOWN_BLUE:…11:54:00` | **No** |
| 6 | `MACD3M:UP_RED:…12:00:00` | `EP:UP_RED:…12:00:00` | Yes |
| 7 | `MACD3M:DOWN_BLUE:…12:24:00` | `EP:DOWN_BLUE:…12:24:00` | **No** |
| 8 | `MACD3M:UP_RED:…12:33:00` | `EP:UP_RED:…12:33:00` | Yes |
| 9 | `MACD3M:DOWN_BLUE:…12:45:00` | `EP:DOWN_BLUE:…12:45:00` | **No** |
| 10 | `MACD3M:UP_RED:…12:51:00` | `EP:UP_RED:…12:51:00` | Yes |
| 11 | `MACD3M:DOWN_BLUE:…12:57:00` | `EP:DOWN_BLUE:…12:57:00` | **No** |
| 12 | `MACD3M:UP_RED:…13:36:00` | `EP:UP_RED:…13:36:00` | **No** |
| 13 | `MACD3M:DOWN_BLUE:…13:54:00` | `EP:DOWN_BLUE:…13:54:00` | Yes |
| 14 | `MACD3M:UP_RED:…14:03:00` | `EP:UP_RED:…14:03:00` | **No** |
| 15 | `MACD3M:DOWN_BLUE:…14:12:00` | `EP:DOWN_BLUE:…14:12:00` | Yes |
| 16 | `MACD3M:UP_RED:…14:24:00` | `EP:UP_RED:…14:24:00` | **No** |

**Same-direction repeat entries:** **0** (flip-gate blocks re-arm until opposite).  
**Re-entry after SL:** **0** (replay has no TP/SL exits; `CONTINUATION_REENTRY_ENABLED=False`).

---

## 2. Code-level B comparison

### 2.1 Signal definition — **MISMATCH (critical)**

**Old** (`scripts/replay_jul22_macd_williams_strategies.py` `signals_B` ~514–543):

```python
# UP: two positive hist bars + two positive deltas; edge vs prior bar
if h1 > 0 and h2 > 0 and d1 > 0 and d2 > 0:
    ...
# DOWN: two negative hist bars + two negative deltas
if h1 < 0 and h2 < 0 and d1 < 0 and d2 < 0:
    ...
```

**New** (`app/trading/macd_hynix_strategy.py` `_pattern_direction` / `evaluate_macd_direction` ~99–176):

```python
# newest=h1 — slope only, no sign requirement
if h1 > h2 > h3 and d1 > 0 and d2 > 0: return DIR_UP
if h1 < h2 < h3 and d1 < 0 and d2 < 0: return DIR_DOWN
# new_signal only if last_dir != pattern dir (first turn)
```

Consequence: new B treats **positive-hist pullbacks** as DOWN and **negative-hist bounces** as UP. That alone explains Jul21 over-trading (17 vs 12 signals) and most losses.

### 2.2 Direction state initialization — **MISMATCH**

| | Old | New |
|---|---|---|
| Init | No persistent dir; stream of edge events | `last_dir=None`, `last_bar=None` in replay / worker |
| First fire | First time pattern becomes newly true (`not prev_ok`) | First completed 3m with slope pattern |

Jul21: new fires **10:27 DOWN** (falling but still **+** hist). Old first fire is **10:36 UP**.

### 2.3 Same-direction duplicate handling — **similar intent, different mechanism**

- **Old:** signal can reappear on new edges while already DOWN/UP; executor ignores same-dir while flat-in-position (`execute_signal_strategy` ~828–834).
- **New:** `last_dir != DIR_UP/DOWN` blocks same-dir at **signal** layer; replay also `continue` if same ETF (~256–257).

Both end up “no pyramid,” but old still **emits** same-dir events (Jul22: 10 old edges → 2 RT).

### 2.4 Re-entry after TP/SL — **MISMATCH (feature presence)**

- **Old compare B:** no TP/SL; exit only opposite or **15:15** force.
- **New live module:** `check_tp_sl` (TP +3% / SL −1.5% net), `evaluate_continuation_reentry`, but **`CONTINUATION_REENTRY_ENABLED = False`**; **Jul21/22 replay script does not call TP/SL at all** — exits are only `SWITCH_TO_*` / `15:00_FORCE`.

### 2.5 Opposite signal detection — **similar**

Both: on opposite direction event → close then open other ETF.  
Reason strings differ: `OPPOSITE:HIST_2UP_TURN` vs `SWITCH_TO_UP_RED`.

### 2.6 Completed 3m bar rules — **MATCH (intent)**

Both resample 3m and require bar fully closed (`start+3m <= now`). New: `resample_completed_3m`. Old: `completed_3m_asof` / indicator frame with `close_time`.

### 2.7 Fill delay & costs — **MISMATCH**

| | Old `delay_1m_cons` | New replay `_fill_price(delay_min=1)` |
|---|---|---|
| Fill time | **Strictly after** signal minute (`sig_min+1`) | First 1m bar with `datetime >= signal_close` (**same minute** as close) |
| Adverse | +0.05% buy / −0.05% sell | Same |
| Costs | Flat `RT_COST_PCT=0.05%` of entry notional | `TradeCostEngine.compute_net_pnl` (fees/tax/slippage model) |
| Force exit | **15:15** | **15:00** |
| Entry cutoff | **14:50** | **14:55** |

Docstring on new replay claims “next 1m open after signal bar close,” but implementation matches **open of the close-timestamp bar**, not `+1m`.

---

## 3. Jul21 loss decomposition

Exclusive primary attribution (each RT in exactly one of flip / wiggle):

| Bucket | N | Gross | Cost | Net | Share of Net |
|---|---:|---:|---:|---:|---:|
| **False hist wiggle** (new slope fire, fails old same-sign rule) | 10 | −718,575 | 86,904 | **−805,479** | **84.2%** |
| **True MACD direction-flip** (passes old same-sign) | 6 | −99,396 | 51,742 | **−151,137** | **15.8%** |
| Same-direction re-entry | 0 | 0 | 0 | 0 | — |
| Re-entry after SL | 0 | 0 | 0 | 0 | — |
| **Trading costs (all 16, overlay)** | 16 | −817,971 | **138,646** | −956,616 | cost drag **14.5%** of \|gross+cost\| path |

Secondary overlay (not exclusive; holds ≤12 min with opposite exit):

| Bucket | N | Net |
|---|---:|---:|
| Late / fast opposite switch (≤12m hold) | 10 | **−1,276,656** |

Interpretation:
- **Dominant damage = slope-only “wiggle” flips** that old B would not have taken (especially mid-day positive-hist DOWN chops #7,#9 and negative-hist UP #4,#12,#14,#16).
- Even “sign-OK” flips still lost **−151k** on Jul21 — day was hard, but not the main story.
- **No SL-reentry path** in this replay.
- Costs (**−138.6k**) amplify churn; 16 RT × ~8.7k avg cost.

If Jul21 had used **old** B edges only (12 signals, likely ~few RT after same-dir suppress), trade count and loss profile would differ sharply — consistent with Jul22 old B (+4.40%, 2 RT) vs new Jul22 (+0.87%, 14 RT).

---

## 4. Jul22 cross-check (same fill label, different B)

| | Old B `delay_1m_cons` | New module `delay_1m_cons` |
|---|---:|---:|
| Day | 2026-07-22 | 2026-07-22 |
| Signals / edges | 10 edges (recomputed) | 14 armed flips |
| Round-trips | **2** | **14** |
| Net | **+440,239** | **+87,133** |
| Ret% | **+4.40%** | **+0.87%** |
| First entry | 10:42 DOWN 0197X0 | 10:18 UP 0193T0 |

Old B Jul22 trades: DOWN 10:42→12:33 (+426.6k net), UP 12:33→15:16 (+13.6k). New module churns 14 switches the same day.

---

## Bottom line

1. **`IMPLEMENTATION_MISMATCH`** — new “Strategy B” is **not** the Jul22 compare winner B.  
2. Jul21 **16 RT / 17 signals** is correct for the **new** module; 17th signal is post-cutoff orphan.  
3. **~84% of Jul21 net loss** comes from fires that **fail the old same-sign hist rule** (false wiggle).  
4. Jul21 16 RT vs Jul22 old B +4.40%/2 RT is **not comparable** (different implementation + different day). Compare Jul22 new (+0.87%/14 RT) vs Jul22 old (+4.40%/2 RT) for same-day mismatch proof.
