"""B/C 재생을 생략한 날의 산출을 A 사본으로 채운다 (B/C ≡ A 인 날).

보호선이 ARM 되지 않는 날은 오버레이가 한 번도 호출되지 않으므로 결과가 A 와 같다.
집계 스크립트가 날짜 결측 없이 돌도록 A 산출을 그 tag 이름으로 복사한다.
`arm.filled=true` 를 찍어 복사본임을 남긴다.

    python .../a4_fill.py --strat CHOPX
"""
import argparse
import json
import os
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
OUT = Path(os.environ.get("ARM3_OUT", r"C:/Users/KIMHYU~1/AppData/Local/Temp/claude/arm3_sp/out"))
DAYS = ["20261016", "20261019", "20261020", "20261021", "20261022", "20261023",
        "20261026", "20261027", "20261028", "20261029", "20261030"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strat", default="CHOPX")
    a = ap.parse_args()
    n = 0
    for v in ("B", "C"):
        for d in DAYS:
            dst = OUT / f"{a.strat}_{v}_{d}.json"
            if dst.exists():
                continue
            src = OUT / f"{a.strat}_A_{d}.json"
            if not src.exists():
                print(f"  !! {a.strat}_A_{d} 없음 — 채울 수 없다")
                continue
            o = json.loads(src.read_text(encoding="utf-8"))
            o["arm"] = dict(o.get("arm") or {}, kind={"B": "SOFT", "C": "MEDIUM"}[v], filled=True)
            dst.write_text(json.dumps(o, ensure_ascii=False, indent=1), encoding="utf-8")
            n += 1
    print(f"A 사본으로 채운 날 {n}개 (보호선 미ARM -> B/C ≡ A)")


if __name__ == "__main__":
    main()
