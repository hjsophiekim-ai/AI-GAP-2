from __future__ import annotations

from datetime import date, datetime, time
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.trading.tsla_auto import market_session


DATES = [
    date(2026, 3, 6),
    date(2026, 3, 9),
    date(2026, 8, 3),
    date(2026, 10, 30),
    date(2026, 11, 2),
    date(2026, 12, 1),
    date(2026, 1, 1),
]


def _fmt(dt):
    return dt.strftime("%Y-%m-%d %H:%M %Z") if dt else "-"


def main() -> None:
    early = sorted(market_session.us_early_close_dates(2026))
    dates = list(DATES)
    if early:
        dates.append(early[0])
    headers = [
        "date_et", "trading_day", "holiday", "early_close", "dst", "tz_abbr",
        "open_et", "close_et", "open_kst", "close_kst", "entry_block_kst", "liquidation_kst",
    ]
    print("\t".join(headers))
    for d in dates:
        state = market_session.get_us_market_state(datetime.combine(d, time(12, 0), tzinfo=market_session.ET))
        print("\t".join([
            d.isoformat(),
            str(state.is_trading_day),
            str(state.is_holiday),
            str(state.is_early_close),
            str(state.is_dst),
            state.timezone_abbr,
            _fmt(state.session_open_et),
            _fmt(state.session_close_et),
            _fmt(state.session_open_kst),
            _fmt(state.session_close_kst),
            _fmt(state.entry_block_at_kst),
            _fmt(state.liquidation_at_kst),
        ]))


if __name__ == "__main__":
    main()
