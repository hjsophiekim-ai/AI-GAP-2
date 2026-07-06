import sys
sys.stdout.reconfigure(encoding="utf-8")
import requests
from bs4 import BeautifulSoup

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0",
    "Referer": "https://finance.naver.com/",
    "Accept-Language": "ko-KR,ko;q=0.9",
}

url = "https://finance.naver.com/sise/item_gap.naver"
resp = requests.get(url, headers=headers, timeout=15)
resp.encoding = "cp949"
soup = BeautifulSoup(resp.text, "html.parser")

tables = soup.find_all("table")
print(f"tables found: {len(tables)}")
for i, t in enumerate(tables):
    cls = t.get("class", [])
    rows = t.find_all("tr")
    print(f"  table[{i}] class={cls} rows={len(rows)}")

# Find type_2 table
tbl = soup.find("table", class_="type_2")
if not tbl:
    for t in tables:
        if len(t.find_all("tr")) > 5:
            tbl = t
            break

if tbl:
    rows = tbl.find_all("tr")
    print(f"\ntype_2 table rows: {len(rows)}")
    # Show header row
    for r in rows[:3]:
        th_list = r.find_all("th")
        if th_list:
            print("  HEADER:", [t.get_text(strip=True) for t in th_list])
    # Show first data row
    for r in rows:
        a_tag = r.find("a")
        if a_tag and "code=" in (a_tag.get("href") or ""):
            cols = r.find_all("td")
            print(f"\nFIRST DATA ROW ({len(cols)} cols):")
            for j, c in enumerate(cols):
                print(f"  col[{j}] = '{c.get_text(strip=True)}'")
            break
else:
    print("type_2 table NOT FOUND")
    print("Page snippet:")
    print(resp.text[:2000])
