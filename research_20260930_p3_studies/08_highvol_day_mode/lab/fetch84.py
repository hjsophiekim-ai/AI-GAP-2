"""READ-ONLY 수신: 20260930 (장중 부분본) 하이닉스/레버리지/인버스 1분봉 -> lab/cache84 만. 토큰 캐시 유효할 때만."""
import sys, os
from pathlib import Path
REPO = Path(r"C:\Users\FURSYS\Desktop\AI-GAP 2"); HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO)); sys.path.insert(0, str(REPO / "scripts"))
from dotenv import load_dotenv; load_dotenv(REPO / ".env")
import fetch_and_analyze_macd2_today as fa
from app.trading import kis_client as kc
c = kc.KISClient(os.environ["KIS_REAL_APP_KEY"], os.environ["KIS_REAL_APP_SECRET"], os.environ["KIS_ACCOUNT_NO"],
                 os.environ.get("KIS_REAL_ACCOUNT_PRODUCT_CODE", "01"), mode="real")
assert c._load_token_cache(), "no cached token -> abort"
D = "20260930"
for tag, df in fa.fetch_today(c, D).items():
    if df is None or df.empty:
        print(D, tag, "EMPTY"); continue
    o = df.copy(); o["datetime"] = o["datetime"].dt.tz_localize(None)
    o.to_csv(HERE / "cache84" / f"replay_{D}_{tag}_1m.csv", index=False)
    print(D, tag, len(o), o["datetime"].iloc[0], "..", o["datetime"].iloc[-1])
