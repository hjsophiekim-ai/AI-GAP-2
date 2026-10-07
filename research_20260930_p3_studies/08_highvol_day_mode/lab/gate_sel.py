"""9월 R0 가 거절/미진입한 플래그의 실제 성과를 사유별로. abc_diag.py 함수 재사용(출력 끔). READ-ONLY."""
import sys
from collections import defaultdict
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8")
src = Path("abc_diag.py").read_text(encoding="utf-8").replace('sys.stdout.reconfigure(encoding="utf-8")', "pass").replace("p = lambda *a: print(*a, flush=True)", "p = lambda *a: None")
g = {"__file__": str(Path("abc_diag.py").resolve()), "__name__": "lib"}
exec(compile(src, "abc_diag.py", "exec"), g)
R0, P2, SEP, key, why_not = g["R0"], g["P2"], g["SEP"], g["key"], g["why_not"]
r0k = {key(t) for t in R0}
agg = defaultdict(list)
for t in P2:
    if t["date"] in SEP and key(t) not in r0k:
        agg[why_not(t)].append(float(t["net_pct"]))
print("# 9월 R0 가 거절/미진입한 플래그 거래의 실제 성과 (한도 해제 풀, 수량 1.0)\n")
print("| 사유 | 건수 | 승 | 승률 | 합계 net | 평균 |")
print("|---|---|---|---|---|---|")
tot = []
for w, v in sorted(agg.items(), key=lambda kv: -len(kv[1])):
    tot += v
    print(f"| {w} | {len(v)} | {sum(x > 0 for x in v)} | {100 * sum(x > 0 for x in v) / len(v):.0f}% | {sum(v):+.2f} | {sum(v) / len(v):+.2f} |")
print(f"| **전체** | {len(tot)} | {sum(x > 0 for x in tot)} | {100 * sum(x > 0 for x in tot) / len(tot):.0f}% | {sum(tot):+.2f} | {sum(tot) / len(tot):+.2f} |")
r0s = [float(t["net_pct"]) for t in R0 if t["date"] in SEP]
print(f"\n참고: 같은 기간 R0 실제 거래 {len(r0s)}건 승률 {100 * sum(x > 0 for x in r0s) / len(r0s):.0f}% 평균 {sum(r0s) / len(r0s):+.2f}")
