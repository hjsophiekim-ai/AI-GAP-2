"""R2 실시간 로거 백업 목록·품질 확인 (2026-10-18). READ-ONLY.

.env 의 R2_READ_* 로 ListObjectsV2 + manifest.json 만 읽는다. 다운로드는 하지 않는다.
서명은 ws_shadow_logger/backup.py 의 SigV4 구현을 그대로 재사용한다(GET 용으로만 호출).
키는 출력하지 않는다.
"""
import json
import os
import sys
import xml.etree.ElementTree as ET
from urllib.parse import quote

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.stdout.reconfigure(encoding="utf-8")
from ws_shadow_logger.backup import EMPTY_SHA256, sign  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def env():
    out = {}
    for ln in open(os.path.join(ROOT, ".env"), encoding="utf-8", errors="replace"):
        ln = ln.strip()
        if ln and not ln.startswith("#") and "=" in ln:
            k, v = ln.split("=", 1)
            out[k.strip().lstrip("﻿")] = v.strip()
    return out


class R2:
    def __init__(self, e):
        self.host = e["R2_READ_ENDPOINT"].strip().rstrip("/").replace("https://", "")
        self.bucket = e["R2_READ_BUCKET"].strip()
        self.ak, self.sk = e["R2_READ_ACCESS_KEY"].strip(), e["R2_READ_SECRET_KEY"].strip()
        self.prefix = e.get("R2_READ_PREFIX", "ws_shadow/").strip().lstrip("/")
        self.region = "auto"

    def _get(self, path, query=""):
        import datetime
        amz = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        hdr = {"x-amz-date": amz, "x-amz-content-sha256": EMPTY_SHA256}
        auth = sign("GET", self.host, path, query, hdr, EMPTY_SHA256,
                    region=self.region, service="s3", access_key=self.ak,
                    secret_key=self.sk, amz_date=amz)
        hdr["Authorization"] = auth
        url = f"https://{self.host}{path}" + (f"?{query}" if query else "")
        return requests.get(url, headers=hdr, timeout=30)

    def list(self, prefix, maxkeys=1000):
        keys, token = [], None
        while True:
            q = f"list-type=2&max-keys={maxkeys}&prefix={quote(prefix, safe='')}"
            if token:
                q += f"&continuation-token={quote(token, safe='')}"
            q = "&".join(sorted(q.split("&")))
            r = self._get(f"/{self.bucket}", q)
            if r.status_code != 200:
                return None, f"HTTP {r.status_code}: {r.text[:300]}"
            ns = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
            root = ET.fromstring(r.text)
            for c in root.findall("s3:Contents", ns):
                keys.append((c.find("s3:Key", ns).text,
                             int(c.find("s3:Size", ns).text),
                             c.find("s3:LastModified", ns).text))
            tok = root.find("s3:NextContinuationToken", ns)
            if root.find("s3:IsTruncated", ns) is not None and root.find("s3:IsTruncated", ns).text == "true" and tok is not None:
                token = tok.text
                continue
            return keys, None

    def get_json(self, key):
        r = self._get(f"/{self.bucket}/{key}")
        if r.status_code != 200:
            return None
        try:
            return r.json()
        except Exception:
            return None


def main():
    e = env()
    miss = [k for k in ("R2_READ_BUCKET", "R2_READ_ENDPOINT", "R2_READ_ACCESS_KEY", "R2_READ_SECRET_KEY") if not e.get(k)]
    if miss:
        print("없는 키:", miss)
        return
    r2 = R2(e)
    print(f"bucket={r2.bucket}  prefix={r2.prefix}  endpoint=***.r2.cloudflarestorage.com\n")
    keys, err = r2.list(r2.prefix)
    if err:
        print("목록 실패:", err)
        return
    print(f"객체 {len(keys)}개")
    zips = [k for k in keys if k[0].endswith(".zip")]
    mans = [k for k in keys if k[0].endswith("manifest.json")]
    print(f"  export zip {len(zips)}개 / manifest {len(mans)}개\n")
    if not zips:
        for k, s, m in keys[:20]:
            print(f"  {k}  {s:,}B  {m}")
        return
    print(f"  {'날짜':12s} {'zip 크기':>12s} {'업로드시각(UTC)':22s} {'manifest':8s}")
    days = []
    for k, s, m in sorted(zips):
        day = k.split("ws_shadow_export_")[-1].replace(".zip", "")
        has_m = any(x[0] == k.rsplit("/", 1)[0] + "/manifest.json" for x in mans)
        days.append((day, k, s, has_m))
        print(f"  {day:12s} {s:12,} {m:22s} {'O' if has_m else 'X'}")
    print(f"\n수집일 {len(days)}일: {days[0][0]} ~ {days[-1][0]}")

    print("\n=== manifest 품질 지표 ===")
    rows = []
    for day, k, s, has_m in days:
        mj = r2.get_json(k.rsplit("/", 1)[0] + "/manifest.json")
        if not mj:
            print(f"  {day}: manifest 없음/읽기 실패")
            continue
        rows.append((day, mj))
        flat = json.dumps(mj, ensure_ascii=False)
        print(f"  {day}: {flat[:400]}")
    if rows:
        out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "out")
        os.makedirs(out, exist_ok=True)
        json.dump({d: m for d, m in rows}, open(out + "/r1_manifests.json", "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        print(f"\n-> out/r1_manifests.json ({len(rows)}일)")


if __name__ == "__main__":
    main()
