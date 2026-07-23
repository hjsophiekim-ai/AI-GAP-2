# MACD Pipeline Audit A–G (authoritative)

Generated: 2026-07-23 — pre-rebuild baseline for
`refactor: rebuild isolated MACD trading pipeline end to end`.

## A. KIS current-price timeout — call flow + cause

**Call chain:** Worker/QuoteUpdater → `broker.kis.get_current_price(symbol)` →
`KISClient.get_current_price` → `requests.Session.get(inquire-price, timeout=(3.0, 8.0))`.

**Timeout layers (current disk):**
- HTTP: connect 3s / read 8s (`kis_client.py`)
- Outer future: `HOT_QUOTE_TIMEOUT_SEC=10` via `ThreadPoolExecutor` + `_KIS_IO_LOCK`
- Sequential 3-symbol phase can hit fabricated `QUOTE_PHASE_CAP` for later symbols

**Root cause of live Render failures (earlier revision / live state):**
1. Previous HTTP read timeout was **2.0s** — futures timed out before real KIS latency surfaced.
2. Sequential quotes on the **5s worker tick** spent budget on 000660/0193T0; 0197X0 got remaining ~0.4s → `TIMEOUT:…limit=0.4s` / `QUOTE_PHASE_CAP`.
3. Future timeout **does not cancel** the in-flight HTTP call; abandoned work holds the I/O lock.
4. Mock rate limiter ≥1.1s/request serializes all symbols → 3 symbols alone ≈3.3s+ before MACD work.
5. Broker `get_current_price` historically returned only a float, discarding `rt_cd`/`http_status` audit fields.

**Observed live state:** `quote_status=FAILED`, orders blocked; worker mean ~9s.

## B. Prior-day warm-up failure

**Exact paging bug:** KIS `FID_INPUT_HOUR_1` is **inclusive** of the oldest bar.
Paging with `hour1 = oldest.strftime("%H%M%S")` re-fetches the same ~30-bar page.
After dedupe, `merged` does not grow → `next_h == hour1` aborts → **today-only ~59×1m**.

**Dependencies:**
- Prior day primarily from local `replay_YYYYMMDD_hynix_1m.csv` / naver multi cache.
- On Render without those caches, KIS paging never crossed into prior session.
- Bootstrap success was wrongly tied to total 3m count alone (could pass with today-only once enough same-day bars accumulated) — must **fail if only today expands**.

**Fix required:** advance cursor **1 minute before** oldest; require prior-day bars; log `request_no/requested_to/received_count/oldest/newest`.

## C. UI vs Worker signal computation

- **Worker** alone calls `evaluate_macd_direction` and writes `completed_signal` / `macd`.
- **UI** does not recompute MACD histogram; it reads state.
- **Conflict:** UI still calls `ensure_worker_running()` on every Streamlit rerun, can trigger stall recovery / lifecycle side effects, and displays duplicate fields (`macd`, `last_signal_eval`, `opening_probe`, `completed_signal`) so “ready-looking” numbers can appear while `warmup_ready=NO` / `bars_ok=False`.

## D. Shutdown executor reuse

- Module-level `_kis_executor` historically lived for process lifetime; `stop_worker()` only set events.
- After Streamlit/interpreter teardown: `cannot schedule new futures after interpreter shutdown`.
- Partial patch added create/shutdown per lifecycle, but **`importlib.reload` of the worker module** still swaps globals while daemon threads may hold old references — must be **removed** (SHA change → new process / new Worker object only).

## E. Stage timings → Worker >5s

| Stage | Budget leak |
|---|---|
| Quotes on tick (old) | 3×(2 attempts)×2–10s, phase cap 4–30s |
| Incremental minute fetch | up to 10s HTTP on every 5s tick |
| Bootstrap on Start | up to 20 pages × (HTTP + 0.12s sleep), overlaps first tick |
| Order confirm | up to 5×1s sleeps |
| State JSON R/W every tick | lock contention / Windows atomic replace failures |

Target: 5s tick does **light work only** — read quote cache + completed bars + signal snapshot + enqueue order intent.

## F. Early-returns that blocked orders when flags existed

1. `auto_trade_off` / STRATEGY_OFF  
2. Broker create / real_confirm failure  
3. `WARMUP_BOOTSTRAP` / NOT_READY  
4. `QUOTE_ERROR` / stale / all-three-symbols required  
5. Zombie pending (>45s) / orphan pending_id  
6. `FORCE_LIQUIDATE_PENDING` leftover across day (rollover must clear UI runtime)  
7. Outside session / after 14:55 no-new-switch  
8. Duplicate `signal_id` / same-dir episode lock  
9. Enhanced mutex / legacy auto_trade_on ON  
10. Truncated feed → wrong HOLD despite chart showing turns  
11. Shared `exit_order_coordinator` duplicate records (Enhanced coupling)

## G. State files — roles + duplication

| File | Role | Conflict |
|---|---|---|
| `macd_hynix_state.json` | Everything: ON/OFF, signals, quotes, pipeline, worker heartbeat | Too many truths |
| `macd_hynix_mutex.json` | MACD vs Enhanced ownership | OR’d with `auto_trade_on` |
| `macd_hynix_state.lock` | Declared | **Unused** — no real interprocess lock |
| `macd_hynix_execution_ledger.csv` | Fills | OK separate |
| `macd_hynix_signal_ledger.csv` | Signal audit | Overlaps `flag_events_today` |
| `macd_legacy_truth_debug.json` | Enhanced truth dump | Extra write every check |
| In-state: `opening_probe` / `bootstrap` / `macd` / `completed_signal` / `last_signal_eval` | Warm-up + signal | Duplicate display truths |

**Target single store:** `data/state/macd_hynix_runtime.json` (atomic + file lock). Mutex only for MACD↔Enhanced ownership.

## Half-done patches to delete/replace

- Nested ThreadPool quote + parallel_quote fallback maze  
- `reload_macd_trading_stack` / stale bytecode / Start-time importlib reload  
- UI-owned `ensure_worker_running` lifecycle  
- Force-arm `FLAT_FLAG_MUST_ORDER` violating strict signed-B onset  
- Multi-file ON/OFF/signal duplication  

## Target architecture (4 components only)

1. **MarketDataService** — bootstrap ≥300×1m+≥100×3m prior day; QuoteCache updater thread; KIS I/O serialized  
2. **MacdSignalEngine** — single signed-B function; `completed_signal_snapshot`; DETECTED→…→LEDGER_RECORDED  
3. **MacdOrderExecutor** — existing order/exit rules; MOCK no REAL gates  
4. **ReadOnly UI** — read snapshot + write start/stop commands only  

Supervisor: process-global singleton — Worker×1, Quote updater×1, zero executor reuse, no module reload.
