import json, os
L = [l.split() for l in open("jobsA.txt")]
trig, ctl = [], []
for _, d, p, p2 in L:
    o = json.load(open(f"out/REAL_UPFASTRS_{d}.json", encoding="utf-8"))
    m = max([e["peak"] for e in o["peaks"]] or [-9])
    (trig if m >= 2 else ctl).append((d, p, p2))
with open("jobsBC.txt", "w") as f:
    for d, p, p2 in trig:
        for v in ("PEAKLOCK", "PEAKPART"):
            f.write(f"{v} {d} {p} {p2}\n")
    for d, p, p2 in ctl[:2]:                      # 대조: 비트리거일 2일은 A 와 동일해야
        for v in ("PEAKLOCK", "PEAKPART"):
            f.write(f"{v} {d} {p} {p2}\n")
print("trigger", len(trig), "control", ctl[:2])
