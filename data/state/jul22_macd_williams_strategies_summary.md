# Jul22 Strategy Replay (2026-07-22)

## Focus comparison
A (3m MACD) | C (WR→MACD) | F (1m lead→3m) | W (weighted)

## Recommendation basis
**1분 지연 + 0.05% adverse** (not max 1-day profit / not immediate fill).

**Winner: A: MACD cross**

| Strategy | Net | Ret% | PF | MDD% | Wrong | N | Probe% | EarlyCap | ↓10:27 | ↑12:12 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---|
| A: MACD cross | +430,184 | +4.302 | 12.092 | -0.43 | 0 | 2 | None | 60.0 | 2026-07-22 10:39:00 | 2026-07-22 12:30:00 |
| F: 1m lead→3m MACD | +372,499 | +3.725 | 5.789 | -0.47 | 2 | 7 | 16.7 | 60.0 | None | 2026-07-22 12:30:00 |
| W: weighted RANGE | +0 | +0.000 | 0.0 | 0.00 | 0 | 0 | None | None | None | None |
| C: WR→MACD | -42,871 | -0.429 | 0.0 | -0.43 | 0 | 1 | None | 60.0 | None | 2026-07-22 12:30:00 |

## Re-run
```
python scripts/replay_jul22_macd_williams_strategies.py
python scripts/replay_jul22_macd_williams_strategies.py --refetch
```
