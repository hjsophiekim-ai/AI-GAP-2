#!/usr/bin/env python
"""Verify TSLA_AUTO test/debug paths do not write operational data.

This script is read-only with respect to production TSLA_AUTO state, ledger,
cache, runtime, and command paths. It hashes those paths before and after
constructing test doubles with temp paths, then scans for fake order ids in
production files.
"""
from __future__ import annotations

import hashlib
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _hash_path(path: Path) -> str:
    h = hashlib.sha256()
    if not path.exists():
        return "MISSING"
    if path.is_file():
        h.update(path.read_bytes())
        return h.hexdigest()
    for item in sorted(path.rglob("*")):
        if item.is_file():
            h.update(str(item.relative_to(path)).encode("utf-8"))
            h.update(item.read_bytes())
    return h.hexdigest()


def _operational_paths() -> list[Path]:
    from app.trading.tsla_auto import ledger, market_data, service, state_store

    return [
        state_store.STATE_DIR_PATH,
        ledger.LOGS_DIR_PATH,
        market_data.CACHE_DIR,
        service.LOCK_DIR,
        service.COMMANDS_DIR,
    ]


def _scan_fake_order_ids(paths: list[Path]) -> list[tuple[str, str]]:
    needles = ("FAKE-", "TEST-", "MOCK-")
    hits: list[tuple[str, str]] = []
    for base in paths:
        if not base.exists():
            continue
        files = [base] if base.is_file() else [p for p in base.rglob("*") if p.is_file()]
        for path in files:
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for needle in needles:
                if needle in text:
                    hits.append((str(path), needle))
    return hits


def main() -> int:
    from tests.tsla_auto.fake_broker import FakeBroker

    op_paths = _operational_paths()
    before = {str(p): _hash_path(p) for p in op_paths}

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        FakeBroker(storage_path=tmp_path / "fake_broker_state.json")
        try:
            FakeBroker(storage_path=ROOT / "data" / "state" / "tsla_auto" / "tsla_auto_runtime.json")
        except RuntimeError:
            pass
        else:
            raise AssertionError("FakeBroker accepted an operational TSLA_AUTO path")

    after = {str(p): _hash_path(p) for p in op_paths}
    if before != after:
        print("FAIL: operational TSLA_AUTO paths changed")
        for path in before:
            if before[path] != after[path]:
                print(f"changed: {path}")
        return 1

    hits = _scan_fake_order_ids(op_paths)
    if hits:
        print("FAIL: fake/test/mock order ids found in operational TSLA_AUTO paths")
        for path, needle in hits:
            print(f"{needle}: {path}")
        return 1

    print("PASS: operational TSLA_AUTO data hashes unchanged")
    print("PASS: FakeBroker rejects operational TSLA_AUTO paths")
    print("PASS: FAKE-/TEST-/MOCK- order ids in operational TSLA_AUTO paths = 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
