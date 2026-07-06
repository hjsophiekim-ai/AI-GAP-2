import sys
sys.stdout.reconfigure(encoding="utf-8")
from dotenv import load_dotenv
load_dotenv(".env")

from app.data.naver_gap_collector import NaverGapCollector

c = NaverGapCollector()
stocks = c.collect_gap_stocks()
print(f"수집된 종목: {len(stocks)}개")
if stocks:
    for s in stocks[:10]:
        sym = s["symbol"]
        name = s["name"]
        price = s["current_price"]
        gap = s["gap_rate"]
        tv = s["trade_value"] / 1e8
        etf = s["is_etf"]
        print(f"  {sym} {name:12s} 현재가:{price:>8,.0f} 갭:{gap:>6.2f}% 거래대금:{tv:>6.1f}억 ETF:{etf}")
else:
    print("종목 없음")

# DataCollector 테스트
print()
from app.data.data_collector import DataCollector
dc = DataCollector()
result = dc.collect_gap_candidates()
print(f"DataCollector 반환 타입: {type(result)}")
print(f"  source: {result.get('source')}")
print(f"  is_sample: {result.get('is_sample')}")
print(f"  candidates 수: {len(result.get('candidates', []))}")
cands = result.get("candidates", [])
if cands:
    first = cands[0]
    print(f"  첫 번째 타입: {type(first).__name__}")
    print(f"  첫 번째: {first.symbol} {first.name}")
