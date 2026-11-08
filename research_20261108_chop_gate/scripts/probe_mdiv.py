import os, sys, time
sys.path.insert(0, os.path.abspath("."))
from dotenv import load_dotenv
load_dotenv()
from app.trading.macd2 import config
from app.trading.kis_client import create_kis_client
sys.stdout.reconfigure(encoding="utf-8")
c = create_kis_client("real")
print("NXT code =", repr(config.NXT_MARKET_DIV_CODE), "symbol =", config.WATCH_SYMBOL)
for mdiv in (config.NXT_MARKET_DIV_CODE, "J", "UN"):
    try:
        got = c.get_minute_candles_for_date(config.WATCH_SYMBOL, "20261106", period_min=1,
                                            count=30, hour1="093300", market_div=mdiv) or []
    except Exception as e:
        print(f"  mdiv={mdiv!r} 예외 {e}"); continue
    ts = sorted(r["time"] for r in got if r.get("time"))
    print(f"  mdiv={mdiv!r} -> {len(got)}건  {ts[0] if ts else '-'}~{ts[-1] if ts else '-'}")
    time.sleep(0.5)
