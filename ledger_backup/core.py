"""MACD2 원장 일별 R2 백업 (2026-11-05) — 연구 재현성용. 실거래 worker 와 완전히 분리된 별도 프로세스에서만 돈다.

  python -m ledger_backup --date 2026-11-05            # 그 날짜 백업(이미 같은 내용이면 SKIP)
  python -m ledger_backup --date 2026-11-05 --verify   # R2 의 존재/sha256/row_count 확인
  python -m ledger_backup --list-past                  # 로컬 누적 원장에 남은 날짜별 행수만 출력(업로드 안 함)
  python -m ledger_backup --auto                       # 오늘 분 자동 실행 — ws_shadow_logger.backup_service 가 15:40 이후 호출

로거 패키지(ws_shadow_logger)는 매매 상태 파일을 읽지 않는다는 격리 규칙이 있어 이 모듈은 로거 밖에 두고,
backup_service 가 별도 프로세스(타임아웃)로만 실행한다. R2 서명·업로드는 ws_shadow_logger.backup 을 재사용한다.

원장은 날짜별 파일이 아니라 누적 파일이다(data/logs/macd2_*.csv). 원본은 읽기만 하고(이동·삭제·rename 없음),
csv 모듈로 한 행씩 읽어 그 날짜 행만 로거 data dir 의 state/ledger_backup/<날짜>/ 에 같은 파일명으로 쓴 뒤 올린다
(전체 적재 없음). 작은 상태 JSON 은 백업 시점 사본을 그대로 올린다.

R2 경로: {LEDGER_BACKUP_PREFIX=ai-gap-2-ledger/}YYYY/MM/DD/<파일명>, manifest.json, daily/YYYY-MM-DD.zip.
자격증명·버킷·엔드포인트는 기존 로거 백업(LOGGER_BACKUP_*)을 쓴다. LEDGER_BACKUP_ENABLED=0 이면 끈다.
자동 실행은 그날 최대 MAX_ATTEMPTS 회에서 멈춘다.
"""
from __future__ import annotations

import argparse
import csv
import dataclasses
import hashlib
import json
import os
import re
import sys
import time as _time
import zipfile
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

from ws_shadow_logger import backup, config

TAG = "[DAILY_LEDGER_BACKUP]"
RUN_AFTER = time(15, 40)
MAX_ATTEMPTS = 3                     # 하루 자동 시도 상한 (무한 재시도 금지)
PUT_RETRIES = (1.0, 3.0)             # 파일 하나당 짧은 재시도 2회
KST = timezone(timedelta(hours=9))
csv.field_size_limit(min(sys.maxsize, 2 ** 31 - 1))

#: (상대경로, 종류) — 실제 production 파일명 그대로. csv 는 그날 행만, json 은 백업 시점 사본.
FILES = (
    ("logs/macd2_signal_ledger.csv", "csv"),
    ("logs/macd2_execution_ledger.csv", "csv"),       # 주문·체결(거래) 원장 — 별도 거래·주문 원장 파일은 없다
    ("logs/macd2_mu_assist_ledger.csv", "csv"),
    ("logs/macd2_bar_ledger.csv", "csv"),
    ("logs/macd2_bar_revision_ledger.csv", "csv"),
    ("logs/macd2_early_stop_defer_ledger.csv", "csv"),     # 2026-11-06 진입 후 30분 손절 유예 이벤트
    ("logs/macd2_mu_catchup_last.json", "json"),
    ("state/macd2_runtime.json", "json"),             # 전략모드·토글 (백업 시점 값)
)
DATE_COLUMNS = ("trading_date", "timestamp", "bar_at", "evaluated_at")
FLAG_KEY = re.compile(r"(enabled|mode|strategy|filter|toggle|regime|xs|mu_|version|armed|sizing)", re.I)
SECRET_ENV = re.compile(r"(KEY|SECRET|TOKEN|PASS|ACCOUNT|CANO)", re.I)


def now_kst() -> datetime:
    return datetime.now(KST).replace(tzinfo=None)


def _p(msg: str):
    print(f"{TAG} {msg}", flush=True)


def enabled(env=None) -> bool:
    env = env if env is not None else os.environ
    return backup.enabled(env) and (env.get("LEDGER_BACKUP_ENABLED", "1").strip() != "0")


def data_root(env=None) -> Path:
    """production DATA_ROOT — app.utils.data_paths 와 같은 규칙(AI_GAP_DATA_DIR, 없으면 <repo>/data). import 는 하지 않는다."""
    env = env if env is not None else os.environ
    v = env.get("LEDGER_BACKUP_DATA_ROOT") or env.get("AI_GAP_DATA_DIR")
    return Path(v) if v else config.REPO / "data"


def target(env=None) -> backup.Target:
    env = env if env is not None else os.environ
    t = backup.load_target(env)
    return dataclasses.replace(t, prefix=(env.get("LEDGER_BACKUP_PREFIX") or "ai-gap-2-ledger/").lstrip("/"))


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for b in iter(lambda: fh.read(64 * 1024), b""):
            h.update(b)
    return h.hexdigest()


def _row_day(row: dict, cols) -> str:
    for c in cols:
        v = str(row.get(c) or "")
        digits = re.sub(r"\D", "", v[:10])
        if len(digits) == 8:
            return digits
    return ""


def slice_csv(src: Path, dst: Path, day: str) -> int:
    """src 의 그날(YYYY-MM-DD) 행만 dst 로. 헤더는 그대로. 반환: 데이터 행 수. 한 행씩 스트리밍."""
    ymd = day.replace("-", "")
    dst.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with open(src, newline="", encoding="utf-8", errors="replace") as fi, \
            open(dst, "w", newline="", encoding="utf-8") as fo:
        r = csv.reader(fi)
        header = next(r, None)
        if header is None:
            return 0
        w = csv.writer(fo)
        w.writerow(header)
        idx = {c: header.index(c) for c in DATE_COLUMNS if c in header}
        for row in r:
            rec = {c: (row[i] if i < len(row) else "") for c, i in idx.items()}
            if _row_day(rec, [c for c in DATE_COLUMNS if c in idx]) == ymd:
                w.writerow(row)
                n += 1
    return n


def count_csv_rows(p: Path) -> int:
    with open(p, newline="", encoding="utf-8", errors="replace") as fh:
        r = csv.reader(fh)
        next(r, None)
        return sum(1 for _ in r)


def _flags(runtime_json: Path, env) -> dict:
    out = {}
    try:
        st = json.loads(runtime_json.read_text(encoding="utf-8"))
        if isinstance(st, dict):
            for k, v in st.items():
                if FLAG_KEY.search(k) and (isinstance(v, (bool, int, float, str)) or v is None) and len(str(v)) <= 80:
                    out[k] = v
    except (OSError, ValueError):
        out["_runtime_json"] = "UNREADABLE"
    env_flags = {k: v for k, v in env.items() if k.startswith("MACD2_") and not SECRET_ENV.search(k)}
    return {"runtime_state": out, "env": dict(sorted(env_flags.items()))}


def _worker_sha(signal_slice: Path) -> str:
    sha = ""
    try:
        with open(signal_slice, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                sha = row.get("worker_code_sha") or sha
    except OSError:
        pass
    return sha


def _put(client, key, path: Path, sha: str, ctype: str) -> str:
    """같은 sha 면 SKIP, 아니면 업로드(짧은 재시도 2회). 반환 상태 문자열. 실패는 예외."""
    last = None
    for wait in (0.0,) + PUT_RETRIES:
        if wait:
            _time.sleep(wait)
        try:
            if client.head_sha256(key) == sha:
                return "SKIP_SAME"
            client.put_file(key, path, sha, content_type=ctype)
            if client.head_sha256(key) != sha:
                raise IOError("업로드 후 HEAD sha256 불일치")
            return "UPLOADED"
        except Exception as e:  # noqa: BLE001
            last = e
    raise IOError(f"{type(last).__name__}: {str(last)[:160]}")


def backup_day(day: str, *, root: Path, work: Path, client, env=None, clock=now_kst) -> dict:
    """한 날짜 백업. 예외를 밖으로 내지 않고 manifest dict 를 반환한다."""
    env = env if env is not None else os.environ
    started = clock()
    out_dir = Path(work) / "state" / "ledger_backup" / day
    out_dir.mkdir(parents=True, exist_ok=True)
    files, ok_paths = [], []
    for rel, kind in FILES:
        src = Path(root) / rel
        name = Path(rel).name
        ent = {"filename": name, "source": rel, "kind": kind, "exists": src.exists()}
        if not src.exists():
            ent.update(upload_status="MISSING")
            files.append(ent)
            continue
        try:
            dst = out_dir / name
            ent["source_size_bytes"] = src.stat().st_size
            if kind == "csv":
                ent["row_count"] = slice_csv(src, dst, day)
                ent["scope"] = f"rows of {day} only (source is cumulative)"
            else:
                dst.write_bytes(src.read_bytes())          # 작은 상태 JSON 사본
                ent["row_count"] = None
                ent["scope"] = "snapshot at backup time"
            ent.update(size_bytes=dst.stat().st_size, sha256=sha256_file(dst))
            ok_paths.append((name, dst, ent))
        except Exception as e:  # noqa: BLE001
            ent.update(upload_status="READ_ERROR", error=f"{type(e).__name__}: {str(e)[:160]}")
        files.append(ent)
    man = {"date_kst": day, "backup_started_at": started.isoformat(timespec="seconds"),
           "worker_code_sha": _worker_sha(out_dir / "macd2_signal_ledger.csv"),
           "render_git_commit": env.get("RENDER_GIT_COMMIT", ""), "git_branch": env.get("RENDER_GIT_BRANCH", ""),
           "flags": _flags(out_dir / "macd2_runtime.json", env), "prefix": client.t.prefix if client else ""}
    for name, dst, ent in ok_paths:
        if client is None:
            ent["upload_status"] = "NOT_CONFIGURED"
            continue
        try:
            ctype = "text/csv" if name.endswith(".csv") else "application/json"
            ent["upload_status"] = _put(client, client.t.key_for(day, name), dst, ent["sha256"], ctype)
        except Exception as e:  # noqa: BLE001
            ent.update(upload_status="FAILED", error=str(e)[:200])
    # daily zip — 디스크 파일에서 바로 압축(메모리 버퍼 없음). 개별 파일이 우선이라 실패해도 상태만 남긴다.
    zinfo = {"filename": f"{day}.zip", "kind": "zip"}
    try:
        zp = out_dir.parent / f"{day}.zip"
        with zipfile.ZipFile(zp, "w", compression=zipfile.ZIP_DEFLATED) as z:
            for name, dst, _ in ok_paths:
                z.write(dst, arcname=name)
        zinfo.update(size_bytes=zp.stat().st_size, sha256=sha256_file(zp))
        if client is not None:
            zkey = f"{client.t.prefix}daily/{day}.zip"
            zinfo["key"] = zkey
            zinfo["upload_status"] = _put(client, zkey, zp, zinfo["sha256"], "application/zip")
        else:
            zinfo["upload_status"] = "NOT_CONFIGURED"
    except Exception as e:  # noqa: BLE001
        zinfo.update(upload_status="FAILED", error=str(e)[:200])
    man["daily_zip"] = zinfo
    man["files"] = files
    bad = [f for f in files if f.get("upload_status") in ("FAILED", "READ_ERROR")]
    good = [f for f in files if f.get("upload_status") in ("UPLOADED", "SKIP_SAME")]
    man["status"] = "ERROR" if (client is None or (bad and not good)) else ("PARTIAL" if bad else "OK")
    man["backup_completed_at"] = clock().isoformat(timespec="seconds")
    if client is not None:
        try:
            body = json.dumps(man, ensure_ascii=False, indent=1).encode("utf-8")
            client.put_bytes(client.t.key_for(day, "manifest.json"), body)
        except Exception as e:  # noqa: BLE001
            man["status"] = "PARTIAL" if man["status"] == "OK" else man["status"]
            man["manifest_error"] = str(e)[:200]
    nbytes = sum(int(f.get("size_bytes") or 0) for f in files)
    if man["status"] == "OK":
        _p(f"date={day} status=OK files={len(good)} bytes={nbytes} sha={man['worker_code_sha'][:12] or '-'}")
    else:
        reason = ";".join(f"{f['filename']}:{f.get('upload_status')}:{f.get('error', '')}" for f in bad)[:400] \
            or ("NOT_CONFIGURED" if client is None else man.get("manifest_error", ""))
        _p(f"date={day} status={man['status']} reason={reason}")
    for f in files:
        if f.get("upload_status") in ("FAILED", "READ_ERROR", "MISSING"):
            _p(f"  file={f['filename']} status={f['upload_status']} {f.get('error', '')}")
    return man


# ── 자동 실행 (backup_service 루프에서 호출) ─────────────────────────────────
def _state_path(work: Path, day: str) -> Path:
    return Path(work) / "state" / f"ledger_backup_{day}.json"


def run_due(work: Path, now: datetime, *, env=None, client_factory=None) -> dict | None:
    """그날 15:40 이후 1회. OK 면 다시 안 함, 실패면 다음 루프에서 재시도하되 하루 MAX_ATTEMPTS 회까지.
    어떤 예외도 밖으로 내지 않는다(실거래·로거·export 와 무관)."""
    try:
        env = env if env is not None else os.environ
        if now.weekday() >= 5 or now.time() < RUN_AFTER or not enabled(env):
            return None
        day = now.strftime("%Y-%m-%d")
        sp = _state_path(work, day)
        st = json.loads(sp.read_text(encoding="utf-8")) if sp.exists() else {}
        if st.get("status") == "OK" or int(st.get("attempts", 0)) >= MAX_ATTEMPTS:
            return None
        if st.get("last_attempt_at") and (now - datetime.fromisoformat(st["last_attempt_at"])).total_seconds() < 300:
            return None
        client = (client_factory or (lambda: backup.S3Client(target(env))))()
        man = backup_day(day, root=data_root(env), work=work, client=client, env=env, clock=lambda: now)
        sp.parent.mkdir(parents=True, exist_ok=True)
        sp.write_text(json.dumps({"status": man["status"], "attempts": int(st.get("attempts", 0)) + 1,
                                  "last_attempt_at": now.isoformat(timespec="seconds")}), encoding="utf-8")
        return man
    except Exception as e:  # noqa: BLE001
        _p(f"date={now:%Y-%m-%d} status=ERROR reason=internal {type(e).__name__}: {str(e)[:200]}")
        return None


# ── verify / list-past ───────────────────────────────────────────────────────
def verify(day: str, client) -> dict:
    """R2 manifest 를 읽고 각 파일의 존재·sha256(메타)·row_count(내려받아 스트리밍 카운트)를 확인."""
    r = client._req("GET", client.t.key_for(day, "manifest.json"))
    if r.status_code >= 300:
        _p(f"VERIFY date={day} manifest 없음 (HTTP {r.status_code})")
        return {"ok": False}
    man = r.json()
    ok = True
    for f in man.get("files", []):
        if f.get("upload_status") in ("MISSING", "READ_ERROR"):
            _p(f"VERIFY {f['filename']}: {f['upload_status']}")
            continue
        key = client.t.key_for(day, f["filename"])
        remote = client.head_sha256(key)
        rows = None
        if remote and f["filename"].endswith(".csv"):
            g = client._req("GET", key)
            lines = (ln.decode("utf-8", "replace") for ln in g.iter_lines() if ln is not None)
            rows = max(0, sum(1 for _ in csv.reader(lines)) - 1)
        good = remote == f.get("sha256") and (rows is None or rows == f.get("row_count"))
        ok &= good
        _p(f"VERIFY {f['filename']}: exists={remote is not None} sha256={'OK' if remote == f.get('sha256') else 'DIFF'} "
           f"rows={rows}/{f.get('row_count')} -> {'OK' if good else 'MISMATCH'}")
    _p(f"VERIFY date={day} result={'OK' if ok else 'MISMATCH'} worker_code_sha={man.get('worker_code_sha')}")
    return {"ok": ok, "manifest": man}


def list_past(root: Path) -> dict:
    """로컬 누적 원장에서 날짜별 행수만 집계해 출력. 업로드하지 않는다."""
    res = {}
    for rel, kind in FILES:
        src = Path(root) / rel
        if kind != "csv" or not src.exists():
            continue
        cnt = {}
        with open(src, newline="", encoding="utf-8", errors="replace") as fh:
            r = csv.reader(fh)
            header = next(r, None) or []
            idx = {c: header.index(c) for c in DATE_COLUMNS if c in header}
            for row in r:
                d = _row_day({c: (row[i] if i < len(row) else "") for c, i in idx.items()}, list(idx))
                cnt[d] = cnt.get(d, 0) + 1
        res[Path(rel).name] = cnt
        print(f"{Path(rel).name}  size={src.stat().st_size}B  dates={len(cnt)}")
        for d in sorted(cnt):
            print(f"   {d[:4]}-{d[4:6]}-{d[6:]}  rows={cnt[d]}" if d else f"   (날짜 없음) rows={cnt[d]}")
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m ledger_backup")
    ap.add_argument("--date", help="YYYY-MM-DD (KST)")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--list-past", action="store_true")
    ap.add_argument("--auto", action="store_true", help="오늘 분 자동 실행(15:40 이후, 하루 최대 3회)")
    a = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    if a.list_past:
        list_past(data_root())
        return 0
    if a.auto:
        dc = config.check_data_dir()
        if not dc.ok:
            _p(f"date={now_kst():%Y-%m-%d} status=ERROR reason=logger data dir ({dc.reason})")
            return 3
        man = run_due(Path(dc.path), now_kst())
        return 0 if (man is None or man.get("status") == "OK") else 1
    if not a.date:
        ap.error("--date / --list-past / --auto 중 하나")
    dc = config.check_data_dir()
    if not dc.ok:
        _p(f"date={a.date} status=ERROR reason=logger data dir ({dc.reason})")
        return 3
    try:
        client = backup.S3Client(target())
    except backup.BackupConfigError as e:
        _p(f"date={a.date} status=ERROR reason=config {e}")
        return 2
    if a.verify:
        return 0 if verify(a.date, client)["ok"] else 1
    man = backup_day(a.date, root=data_root(), work=Path(dc.path), client=client)
    return 0 if man["status"] == "OK" else 1


if __name__ == "__main__":
    sys.exit(main())
