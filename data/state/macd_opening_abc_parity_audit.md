# MACD Opening A/B/C Parity Audit

**Generated:** 2026-07-22 (audit) / **Retest:** 2026-07-23  
**Scope:** A (`NEW_TURN_ONLY`) vs C (`IMMEDIATE_50_THEN_CONFIRM`) 20-day replay  
**Status:** **`PENDING_RETEST` → resolved**  
**Verdict:** **`INSUFFICIENT_SAMPLE`** (parity restored; probe fired 0 times)

---

## Executive summary

Previous invalid `DO_NOT_ADOPT` was caused by an implementation mismatch: C unconditionally skipped the 09:03 `new_signal` even when `open_probe_attempts=0`.

**Fix applied** in `scripts/compare_macd_opening_abc_20d.py`: skip 09:03 only when `partial_await_confirm` is active. Live `macd_hynix_worker.py` already gated on `awaiting_09_03_confirm` — no change required.

After retest:

| Metric | A | C | Δ |
|--------|---|---|---|
| Round-trips | 95 | 95 | **0** |
| Net PnL | 7,750,573 | 7,750,573 | **0** |
| `open_probe_attempts` | — | 0 | 0 |
| Timeline key diffs | — | — | **empty** |

Because the probe never fires in this 20d sample, performance comparison cannot judge opening-probe merit → **`INSUFFICIENT_SAMPLE`**. Live default remains `OPENING_PROBE_ENABLED=False`.

---

## Root cause (fixed)

```python
# Before (bug): always skip 09:03 for C
if ev.get("new_signal") and not (close_ts.hour == 9 and close_ts.minute == 3):
    direction = ev["signal_direction"]

# After: skip only while probe partial awaits confirm
skip_903 = partial_await_confirm and close_ts.hour == 9 and close_ts.minute == 3
if ev.get("new_signal") and not skip_903:
    direction = ev["signal_direction"]
```

---

## Artifacts

| File | Purpose |
|------|---------|
| `macd_opening_abc_20d_compare.json` | Post-fix 20d compare |
| `macd_opening_abc_20d_compare.md` | Summary MD |
| `tests/test_macd_opening_abc_parity.py` | Parity regression tests |
