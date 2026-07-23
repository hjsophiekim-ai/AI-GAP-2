# MACD live first-flag checklist (tomorrow morning)

Prep only. Do **not** treat this as today’s live verify.

`READY_FOR_MOCK` still stands from mock Stop→Start E2E. Strategy / thresholds are unchanged.

## After Render Stop→Start

1. **Stop → Start** MACD on Render (live) so the worker reloads bytecode.
2. Confirm `worker_code_sha` in `macd_hynix_state.json` is one of:
   - `5b47073`
   - `587e18d`
   - or current HEAD short (if HEAD moved after this prep, document it)
3. Run the read-only checklist script where state + ledger are visible:

```bash
python scripts/verify_macd_live_flag_checklist.py
python scripts/verify_macd_live_flag_checklist.py --poll --timeout-sec 7200
```

4. Walk items **1–9** (script prints PASS / FAIL / PENDING).
5. Save / keep the first real flag dump:

`data/state/macd_live_first_flag_YYYYMMDD.json`

## Items 1–9

1. Local / Origin / Render SHA match
2. `worker_code_sha` = `5b47073` or `587e18d` (or documented current HEAD)
3. Previous worker thread residual = 0
4. `last_tick` updates ~every 5s
5. First real flag linkage: `signal_id`, `decision_trace`, `order_requested_at`, KIS order no, `broker_executed_at`, `position_confirmed_at`, ledger — same order
6. If flag but no order → `primary_block_reason` shown
7. Same flag held → 0 duplicate orders
8. Opposite flag → full sell → qty 0 → opposite buy
9. 15:00 account holdings = 0
