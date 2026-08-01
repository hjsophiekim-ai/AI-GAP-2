# US Market Session Policy

This project uses `app.trading.tsla_auto.market_session` as the single source
of truth for US stock trading hours used by TSLA_AUTO.

## Timezones

- Market timezone: `America/New_York`
- Korean UI timezone: `Asia/Seoul`
- Current time is created in UTC and converted with `zoneinfo.ZoneInfo`.
- Naive datetimes are rejected.
- DST is never calculated with month ranges, fixed UTC offsets, or hardcoded
  Korean trading hours.

KST examples are display references only:

- During US daylight saving time, the regular session is commonly KST
  22:30 to next-day 05:00.
- During US standard time, the regular session is commonly KST 23:30 to
  next-day 06:00.
- Actual values always come from `America/New_York` to `Asia/Seoul`
  conversion.

## Calendar Source

`pandas_market_calendars` NYSE calendar is the fallback exchange calendar used
for trading days, official holidays, early closes, and actual open/close
times. If a trusted KIS market-status endpoint is later confirmed, it should be
wired behind the same `USMarketSessionState` interface.

If the calendar cannot be read, TSLA_AUTO fails closed:

- `phase = CALENDAR_UNAVAILABLE`
- `entry_allowed = False`
- BUY and position-increasing orders are blocked.

## Phases

| Phase | Time rule | BUY/new position | SELL liquidation |
| --- | --- | --- | --- |
| `WEEKEND` | No exchange session on Saturday/Sunday | Blocked | No repeated after-hours liquidation |
| `HOLIDAY` | Exchange holiday | Blocked | No repeated after-hours liquidation |
| `PRE_MARKET` | Before actual regular open | Blocked | Allowed only for explicit liquidation workflows |
| `REGULAR_ENTRY` | `session_open <= now < session_close - 15m` | Allowed | Allowed |
| `ENTRY_BLOCKED` | `session_close - 15m <= now < session_close - 10m` | Blocked | Allowed |
| `FORCE_LIQUIDATION` | `session_close - 10m <= now < session_close` | Blocked | Required |
| `AFTER_MARKET` | `now >= session_close` | Blocked | Warn and reconcile; do not loop market sells |
| `CALENDAR_UNAVAILABLE` | Calendar/API error | Blocked | Fail closed |

All cutoff times are computed from the actual exchange close. On a 13:00 ET
early close, entry is blocked at 12:45 ET and forced liquidation starts at
12:50 ET.

## Order Safety

The worker, signal dispatch path, order executor, and UI all consume the same
`USMarketSessionState`. The order executor performs the final BUY gate:

```python
if not market_state.entry_allowed:
    reject_buy()
```

For reversals after `ENTRY_BLOCKED`, the existing leg may be sold, but the
follow-up opposite ETF BUY is blocked. Ending flat is the expected safe result.

## Forced Liquidation

At `FORCE_LIQUIDATION`, TSLA_AUTO:

1. Blocks new entries.
2. Cancels open BUY or position-increasing orders when broker support exists.
3. Re-reads managed US positions.
4. Submits full SELL orders for managed TSLA_AUTO US symbols.
5. Reconciles balances to zero.
6. Stores liquidation status in TSLA_AUTO runtime state to avoid duplicate
   same-day same-symbol liquidation submissions.

The strategy does not intentionally open premarket or aftermarket positions.
