"""READ-ONLY: Naver 1분봉(OHLC) 수집 -> scratchpad. production 무수정."""
import urllib.request, json, sys, os, time
import pandas as pd
sys.stdout.reconfigure(encoding="utf-8")
SP = os.path.dirname(os.path.abspath(__file__))
HDR = {"User-Agent": "Mozilla/5.0", "Referer": "https://m.stock.naver.com/"}
SYM = {"hynix": "000660", "long": "0193T0", "inverse": "0197X0"}

def fetch(code, day):
    u = (f"https://api.stock.naver.com/chart/domestic/item/{code}/minute"
         f"?startDateTime={day}0800&endDateTime={day}2000&interval=1")
    b = urllib.request.urlopen(urllib.request.Request(u, headers=HDR), timeout=20).read()
    rows = json.loads(b.decode("utf-8"))
    out = []
    for r in rows:
        t = r["localDateTime"]
        if t[:8] != day:
            continue
        out.append({"datetime": f"{t[:4]}-{t[4:6]}-{t[6:8]} {t[8:10]}:{t[10:12]}:00",
                    "open": float(r["openPrice"]), "high": float(r["highPrice"]),
                    "low": float(r["lowPrice"]), "close": float(r["currentPrice"]),
                    "volume": int(r["accumulatedTradingVolume"])})
    return pd.DataFrame(out).drop_duplicates("datetime").sort_values("datetime").reset_index(drop=True)

if __name__ == "__main__":
    for day in sys.argv[1:]:
        for tag, code in SYM.items():
            p = f"{SP}/data/naver_{day}_{tag}_1m.csv"
            if os.path.exists(p):
                print("skip", p); continue
            df = fetch(code, day)
            df.to_csv(p, index=False)
            print(f"{day} {tag}: {len(df)} rows {df.datetime.iloc[0]} ~ {df.datetime.iloc[-1]}")
            time.sleep(0.4)
