"""B 생략일 채우기 — A 재생에서 E_PENDING_ARMED 가 한 번도 없었던 날은 REGIME_E 우회가
호출될 일이 없으므로 B ≡ A 가 코드상 보장된다. 그 날은 A 산출을 B 이름으로 복사한다(filled=true).

  python f1_fill_b.py --a-out <A 폴더> --b-out <B 폴더> [--list-need]
"""
import argparse
import json
from pathlib import Path


def armed(js, day):
    return any(str(b.get("ev", "")).startswith("E_PENDING_ARMED") and b["t"][:10].replace("-", "") == day
               for b in js.get("brk", []))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--a-out", required=True)
    ap.add_argument("--b-out", required=True)
    ap.add_argument("--a-tag", default="L85_D")
    ap.add_argument("--b-tag", default="REGE_B")
    ap.add_argument("--from-day", default="20260803")
    ap.add_argument("--list-need", action="store_true")
    a = ap.parse_args()
    need, filled = [], []
    for f in sorted(Path(a.a_out).glob(f"{a.a_tag}_2026*.json")):
        day = f.stem.split("_")[-1]
        if day < a.from_day:
            continue
        js = json.loads(f.read_text(encoding="utf-8"))
        dst = Path(a.b_out) / f"{a.b_tag}_{day}.json"
        if armed(js, day):
            need.append(day)
            continue
        if a.list_need or dst.exists():
            continue
        js["filled_from_A"] = True
        dst.write_text(json.dumps(js, ensure_ascii=False), encoding="utf-8")
        filled.append(day)
    print("need:", " ".join(need))
    print(f"filled {len(filled)}: {' '.join(filled)}")


if __name__ == "__main__":
    main()
