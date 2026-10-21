"""일별 분석용 export — 그날 로거 데이터를 zip 하나로 묶는다 (2026-10-16).

  python -m ws_shadow_logger.export_day --date YYYY-MM-DD [--root DIR] [--force]

포함 (원본은 읽기만 하고 지우지 않는다):
  snapshots/<날짜>/*.csv(.gz)   1초 스냅샷 (하이닉스·레버리지·인버스 10호가·체결강도·체결량)
  mu/<날짜>/*                   MU 체결 tick / 1분봉 / summary + MU 1호가 quotes / quote_summary
  logs/<날짜>.jsonl             접속·gap·health 이벤트
  daily/<날짜>.json             일별 요약 (15:30 maintenance 결과)
  manifest.json                 파일 수 / 행 수 / sha256 / 누락률 / MU tick 수 / snapshot 수
출력: $KIS_LOGGER_DATA_DIR/exports/ws_shadow_export_<날짜>.zip  (+ 같은 이름 .manifest.json, zip 밖 사본)

같은 날짜 재실행: 임시 파일에 만든 뒤 원자적으로 교체한다(os.replace). 원본 파일 구성(경로·크기·mtime)이
직전 export 와 같으면 다시 만들지 않는다 → 업로드 쪽 idempotency 와 맞물린다.
메모리: zip 은 파일을 청크로 스트리밍하고, 행 수는 한 줄씩 센다. pandas 를 쓰지 않는다.
이 모듈은 로거 프로세스와 무관하게 실행된다(로거 상태 파일을 쓰지 않는다).
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config

KST = timezone(timedelta(hours=9))
EXPORT_DIR = "exports"
PREFIX = "ws_shadow_export_"
MANIFEST_VERSION = 1
CHUNK = 64 * 1024
#: snapshot 누락률 기준 — 수집시간(08:00~15:30) 초 수 대비 실제 스냅샷 행 수
COLLECT_SECONDS = int((datetime.combine(datetime(2000, 1, 1), config.SESSION_END)
                       - datetime.combine(datetime(2000, 1, 1), config.SESSION_START)).total_seconds())


def now_kst() -> datetime:
    return datetime.now(KST).replace(tzinfo=None)


def zip_path(root: Path, day: str) -> Path:
    return Path(root) / EXPORT_DIR / f"{PREFIX}{day}.zip"


def sidecar_path(root: Path, day: str) -> Path:
    return Path(root) / EXPORT_DIR / f"{PREFIX}{day}.manifest.json"


def sources(root: Path, day: str) -> list[tuple[Path, str]]:
    """(원본 경로, zip 안 경로). 존재하는 것만."""
    root = Path(root)
    out = []
    for sub in (root / "snapshots" / day, root / "mu" / day):
        if sub.is_dir():
            for p in sorted(x for x in sub.rglob("*") if x.is_file() and not x.name.endswith((".part", ".tmp"))):
                out.append((p, p.relative_to(root).as_posix()))
    for p in (root / "logs" / f"{day}.jsonl", root / "daily" / f"{day}.json"):
        if p.is_file():
            out.append((p, p.relative_to(root).as_posix()))
    return out


def _open_text(p: Path):
    return gzip.open(p, "rt", encoding="utf-8", errors="replace") if p.suffix == ".gz" else \
        open(p, encoding="utf-8", errors="replace")


def _scan(p: Path) -> tuple[int, str]:
    """(데이터 행 수, sha256). csv 는 헤더 제외, jsonl 은 줄 수, 그 외 0."""
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for b in iter(lambda: fh.read(CHUNK), b""):
            h.update(b)
    name = p.name[:-3] if p.name.endswith(".gz") else p.name
    rows = 0
    if name.endswith((".csv", ".jsonl")):
        with _open_text(p) as fh:
            rows = sum(1 for line in fh if line.strip())
        if name.endswith(".csv") and rows:
            rows -= 1
    return rows, h.hexdigest()


def _fingerprint(srcs) -> str:
    """데이터 파일 구성(경로·크기·mtime). logs/ 는 제외한다 — 로거가 장외에도 1분마다 health 를 덧붙여
    지문이 매분 바뀌면 재export·재업로드가 반복되기 때문이다(zip 에는 그 시점까지의 로그가 들어간다)."""
    h = hashlib.sha256()
    for p, arc in srcs:
        if arc.startswith("logs/"):
            continue
        st = p.stat()
        h.update(f"{arc}|{st.st_size}|{int(st.st_mtime)}\n".encode())
    return h.hexdigest()


def _load_json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def build_manifest(root: Path, day: str, srcs, created_at: datetime) -> dict:
    files, snap, mu_ticks, mu_bars, mu_quotes = [], {}, 0, 0, 0
    for p, arc in srcs:
        rows, sha = _scan(p)
        files.append({"path": arc, "bytes": p.stat().st_size, "rows": rows, "sha256": sha})
        parts = arc.split("/")
        if parts[0] == "snapshots":
            sym = parts[-1].split(".")[0]
            snap[sym] = {"rows": rows, "missing_ratio_vs_collect_window": round(max(0.0, 1 - rows / COLLECT_SECONDS), 4)}
        elif parts[0] == "mu" and parts[-1].startswith("ticks.csv"):
            mu_ticks += rows
        elif parts[0] == "mu" and parts[-1].startswith("bars_1m.csv"):
            mu_bars += rows
        elif parts[0] == "mu" and parts[-1].startswith("quotes.csv"):
            mu_quotes += rows
    daily = _load_json(Path(root) / "daily" / f"{day}.json") or {}
    for sym, s in (daily.get("symbols") or {}).items():
        if sym in snap:
            snap[sym]["stale_ratio"] = s.get("stale_ratio")
            snap[sym]["max_ts_gap_s"] = s.get("max_ts_gap_s")
    mu_sum = _load_json(Path(root) / "mu" / day / "summary.json")
    mu_quote_sum = _load_json(Path(root) / "mu" / day / "quote_summary.json")
    wd = _load_json(Path(root) / "mu" / day / "watchdog.json") or {}
    return {
        "manifest_version": MANIFEST_VERSION, "day": day, "created_at_kst": created_at.isoformat(timespec="seconds"),
        "source_fingerprint": _fingerprint(srcs),
        "file_count": len(files), "total_rows": sum(f["rows"] for f in files),
        "total_bytes": sum(f["bytes"] for f in files),
        "snapshot_rows": sum(v["rows"] for v in snap.values()), "snapshots": snap,
        "mu_tick_rows": mu_ticks, "mu_bar_rows": mu_bars, "mu_summary": mu_sum,
        "mu_quote_rows": mu_quotes, "mu_quote_summary": mu_quote_sum,
        # MU watchdog (2026-10-21): 연구 파이프라인은 이 구간의 MU feature 를 제외한다.
        "mu_stale_intervals": wd.get("mu_stale_intervals", []),
        "mu_resubscribe_count": wd.get("resubscribe_count", 0),
        "mu_recovery_success": wd.get("recovery_success"),
        "has_daily_summary": bool(daily), "has_logs": any(a.startswith("logs/") for _, a in srcs),
        "collect_window": f"{config.SESSION_START:%H:%M}-{config.SESSION_END:%H:%M} KST",
        "files": files,
    }


def export(root, day: str, *, now: datetime | None = None, force: bool = False) -> dict:
    """결과: {"status": "CREATED"|"UNCHANGED"|"NO_DATA"|"NOT_FINISHED", "zip": 경로, "manifest": {...}}"""
    root = Path(root)
    now = now or now_kst()
    if day == now.strftime("%Y-%m-%d") and now.time() < config.SESSION_END and not force:
        return {"status": "NOT_FINISHED", "day": day}
    srcs = sources(root, day)
    if not srcs:
        return {"status": "NO_DATA", "day": day}
    zp, sp = zip_path(root, day), sidecar_path(root, day)
    old = _load_json(sp)
    fp = _fingerprint(srcs)
    if zp.exists() and old and old.get("source_fingerprint") == fp and not force:
        return {"status": "UNCHANGED", "day": day, "zip": str(zp), "manifest": old}
    man = build_manifest(root, day, srcs, now)
    zp.parent.mkdir(parents=True, exist_ok=True)
    tmp = zp.with_name(zp.name + f".{os.getpid()}.tmp")
    try:
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            for p, arc in srcs:
                # .gz 는 이미 압축돼 있어 STORED 로 넣는다
                z.write(p, arc, compress_type=zipfile.ZIP_STORED if p.suffix == ".gz" else zipfile.ZIP_DEFLATED)
            z.writestr("manifest.json", json.dumps(man, ensure_ascii=False, indent=1))
        with zipfile.ZipFile(tmp) as z:                       # 무결성 확인 후 교체
            bad = z.testzip()
            if bad:
                raise zipfile.BadZipFile(f"CRC 불일치: {bad}")
        man["zip_bytes"] = tmp.stat().st_size
        h = hashlib.sha256()
        with open(tmp, "rb") as fh:
            for b in iter(lambda: fh.read(CHUNK), b""):
                h.update(b)
        man["zip_sha256"] = h.hexdigest()
        os.replace(tmp, zp)
    finally:
        if tmp.exists():
            tmp.unlink()
    stmp = sp.with_name(sp.name + ".tmp")
    stmp.write_text(json.dumps(man, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(stmp, sp)
    return {"status": "CREATED", "day": day, "zip": str(zp), "manifest": man}


def data_root(arg: str | None) -> Path:
    raw = arg or os.environ.get(config.ENV_DATA_DIR)
    if not raw:
        raise SystemExit(f"--root 또는 {config.ENV_DATA_DIR} 가 필요합니다")
    return Path(raw)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m ws_shadow_logger.export_day")
    ap.add_argument("--date", required=True, help="YYYY-MM-DD")
    ap.add_argument("--root", help=f"기본 ${config.ENV_DATA_DIR}")
    ap.add_argument("--force", action="store_true", help="장중이거나 변경이 없어도 다시 만든다")
    a = ap.parse_args(argv)
    try:
        datetime.strptime(a.date, "%Y-%m-%d")
    except ValueError:
        print("--date 는 YYYY-MM-DD 형식", file=sys.stderr); return 2
    r = export(data_root(a.root), a.date, force=a.force)
    m = r.get("manifest") or {}
    print(f"[ws_shadow_export] {r['status']} day={a.date} zip={r.get('zip', '-')} files={m.get('file_count', 0)} "
          f"snapshot_rows={m.get('snapshot_rows', 0)} mu_ticks={m.get('mu_tick_rows', 0)} bytes={m.get('zip_bytes', '-')}")
    return 0 if r["status"] in ("CREATED", "UNCHANGED") else 1


if __name__ == "__main__":
    sys.exit(main())
