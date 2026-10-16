"""S3 호환 object storage 업로드 (AWS S3 / Cloudflare R2 같은 인터페이스) — 2026-10-16.

boto3 없이 requests + AWS Signature V4 로 직접 서명한다(메모리·의존성 최소화). path-style 주소
(`{endpoint}/{bucket}/{key}`)라 S3·R2 모두 같은 코드로 동작한다. 본문은 파일을 스트리밍으로 보내고
`x-amz-content-sha256: UNSIGNED-PAYLOAD` 로 서명한다(HTTPS 전제, S3·R2 모두 지원).

환경변수:
  LOGGER_BACKUP_ENABLED=1
  LOGGER_BACKUP_PROVIDER=s3 | r2
  LOGGER_BACKUP_BUCKET=...
  LOGGER_BACKUP_ENDPOINT=https://s3.ap-northeast-2.amazonaws.com  |  https://<account_id>.r2.cloudflarestorage.com
  LOGGER_BACKUP_ACCESS_KEY=... / LOGGER_BACKUP_SECRET_KEY=...
  LOGGER_BACKUP_PREFIX=ws_shadow/            (기본값)
  LOGGER_BACKUP_REGION=...                   (선택. 기본: r2=auto, s3 는 endpoint 의 s3.<region>.amazonaws.com, 없으면 us-east-1)
키 원문은 로그·상태파일에 절대 남기지 않는다.

idempotency: 객체 메타데이터 x-amz-meta-sha256 에 zip 의 sha256 을 넣고, 올리기 전 HEAD 로 같은 값이
이미 있으면 업로드하지 않는다(ALREADY_PRESENT). 같은 날짜 재실행·중복 실행 모두 안전하다.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

ENV = {k: f"LOGGER_BACKUP_{k}" for k in ("ENABLED", "PROVIDER", "BUCKET", "ENDPOINT", "ACCESS_KEY", "SECRET_KEY",
                                         "PREFIX", "REGION")}
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
UNSIGNED = "UNSIGNED-PAYLOAD"
TIMEOUT = (10, 300)


class BackupConfigError(Exception):
    pass


@dataclass(frozen=True)
class Target:
    provider: str
    endpoint: str
    bucket: str
    region: str
    access_key: str
    secret_key: str
    prefix: str

    def key_for(self, day: str, name: str) -> str:
        y, m, d = day.split("-")
        return f"{self.prefix}{y}/{m}/{d}/{name}"

    def describe(self) -> str:
        """로그용 — 키 없이."""
        return f"provider={self.provider} endpoint={self.endpoint} bucket={self.bucket} region={self.region} prefix={self.prefix}"


def enabled(env=None) -> bool:
    return (env or os.environ).get(ENV["ENABLED"], "0") == "1"


def load_target(env=None) -> Target:
    env = env or os.environ
    prov = (env.get(ENV["PROVIDER"]) or "s3").strip().lower()
    if prov not in ("s3", "r2"):
        raise BackupConfigError(f"{ENV['PROVIDER']} 는 s3 또는 r2")
    missing = [ENV[k] for k in ("BUCKET", "ENDPOINT", "ACCESS_KEY", "SECRET_KEY") if not env.get(ENV[k])]
    if missing:
        raise BackupConfigError("미설정: " + ", ".join(missing))
    ep = env[ENV["ENDPOINT"]].strip().rstrip("/")
    if not ep.startswith("https://"):
        raise BackupConfigError(f"{ENV['ENDPOINT']} 는 https:// 여야 한다(UNSIGNED-PAYLOAD 전제)")
    if urlsplit(ep).path not in ("", "/"):
        # path-style 이라 버킷은 코드가 붙인다. endpoint 에 /<bucket> 을 넣으면 /<bucket>/<bucket>/... 로 실패한다.
        raise BackupConfigError(f"{ENV['ENDPOINT']} 에는 경로 없이 호스트만 넣는다 (예: https://<ACCOUNT_ID>.r2.cloudflarestorage.com)")
    if prov == "r2" and not ep.endswith(".r2.cloudflarestorage.com"):
        raise BackupConfigError(f"provider=r2 의 {ENV['ENDPOINT']} 는 https://<ACCOUNT_ID>.r2.cloudflarestorage.com 형식")
    for k in ("ACCESS_KEY", "SECRET_KEY"):
        if env[ENV[k]].strip().lower().startswith("cfat_"):
            # Cloudflare API Token(cfat_...) 은 S3 서명 키가 아니다. 값 자체는 메시지에 넣지 않는다.
            raise BackupConfigError(f"{ENV[k]} 에 Cloudflare API Token(cfat_...) 이 들어 있다 — R2 > Manage API Tokens 에서 "
                                    "만든 S3 호환 Access Key ID / Secret Access Key 를 넣어야 한다")
    region = (env.get(ENV["REGION"]) or "").strip()
    if not region:
        m = re.search(r"s3[.-]([a-z0-9-]+)\.amazonaws\.com", ep)
        region = "auto" if prov == "r2" else (m.group(1) if m else "us-east-1")
    prefix = env.get(ENV["PREFIX"], "ws_shadow/")
    prefix = prefix if (not prefix or prefix.endswith("/")) else prefix + "/"
    return Target(prov, ep, env[ENV["BUCKET"]].strip(), region, env[ENV["ACCESS_KEY"]].strip(),
                  env[ENV["SECRET_KEY"]].strip(), prefix.lstrip("/"))


# ── SigV4 ───────────────────────────────────────────────────────────────────
def _hmac(k: bytes, msg: str) -> bytes:
    return hmac.new(k, msg.encode("utf-8"), hashlib.sha256).digest()


def sign(method: str, host: str, path: str, query: str, headers: dict, payload_hash: str, *, region: str,
         access_key: str, secret_key: str, amz_date: str, service: str = "s3") -> str:
    """Authorization 헤더 값. headers 에는 서명할 헤더(소문자 키)를 넣는다 — host 는 자동 포함."""
    hs = {k.lower(): " ".join(str(v).strip().split()) for k, v in headers.items()}
    hs["host"] = host
    signed = ";".join(sorted(hs))
    canon = "\n".join([method, path, query, "".join(f"{k}:{hs[k]}\n" for k in sorted(hs)), signed, payload_hash])
    scope = f"{amz_date[:8]}/{region}/{service}/aws4_request"
    sts = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canon.encode()).hexdigest()])
    k = _hmac(_hmac(_hmac(_hmac(("AWS4" + secret_key).encode(), amz_date[:8]), region), service), "aws4_request")
    sig = hmac.new(k, sts.encode(), hashlib.sha256).hexdigest()
    return f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, SignedHeaders={signed}, Signature={sig}"


class S3Client:
    """PUT / HEAD 만 쓰는 최소 S3 호환 클라이언트. session 은 테스트에서 주입한다."""

    def __init__(self, target: Target, session=None, clock=None):
        import requests
        self.t = target
        self.s = session or requests.Session()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        u = urlsplit(target.endpoint)
        self.host, self.base_path = u.netloc, u.path.rstrip("/")

    def _req(self, method, key, *, extra=None, payload_hash=EMPTY_SHA256, data=None):
        path = f"{self.base_path}/{quote(self.t.bucket, safe='')}/{quote(key, safe='/~')}"
        amz = self.clock().strftime("%Y%m%dT%H%M%SZ")
        hdr = {"x-amz-date": amz, "x-amz-content-sha256": payload_hash, **(extra or {})}
        auth = sign(method, self.host, path, "", hdr, payload_hash, region=self.t.region,
                    access_key=self.t.access_key, secret_key=self.t.secret_key, amz_date=amz)
        return self.s.request(method, f"{self.t.endpoint.split('://')[0]}://{self.host}{path}",
                              headers={**hdr, "Authorization": auth}, data=data, timeout=TIMEOUT)

    def head_sha256(self, key) -> str | None:
        """객체가 있으면 메타 sha256(없으면 ''), 없으면 None."""
        r = self._req("HEAD", key)
        if r.status_code == 404:
            return None
        if r.status_code >= 300:
            raise IOError(f"HEAD {r.status_code}")
        return r.headers.get("x-amz-meta-sha256", "")

    def put_file(self, key, path: Path, sha256: str, content_type="application/zip"):
        size = Path(path).stat().st_size
        with open(path, "rb") as fh:
            r = self._req("PUT", key, payload_hash=UNSIGNED, data=fh,
                          extra={"content-length": str(size), "content-type": content_type,
                                 "x-amz-meta-sha256": sha256})
        if r.status_code >= 300:
            raise IOError(f"PUT {r.status_code} {r.text[:200]}")
        return r.headers.get("ETag", "")

    def put_bytes(self, key, body: bytes, content_type="application/json"):
        sha = hashlib.sha256(body).hexdigest()
        r = self._req("PUT", key, payload_hash=sha, data=body,
                      extra={"content-length": str(len(body)), "content-type": content_type, "x-amz-meta-sha256": sha})
        if r.status_code >= 300:
            raise IOError(f"PUT {r.status_code} {r.text[:200]}")
        return r.headers.get("ETag", "")


def upload_day(client: S3Client, zip_file: Path, day: str, manifest: dict) -> dict:
    """zip → {prefix}YYYY/MM/DD/ws_shadow_export_<day>.zip, 그 다음 manifest.json (업로드 완료 표식).

    반환: {"status": "UPLOADED"|"ALREADY_PRESENT", "key": ..., "sha256": ...}
    """
    sha = manifest.get("zip_sha256")
    if not sha:
        h = hashlib.sha256()
        with open(zip_file, "rb") as fh:
            for b in iter(lambda: fh.read(64 * 1024), b""):
                h.update(b)
        sha = h.hexdigest()
    key = client.t.key_for(day, Path(zip_file).name)
    mkey = client.t.key_for(day, "manifest.json")
    if client.head_sha256(key) == sha and client.head_sha256(mkey) is not None:
        return {"status": "ALREADY_PRESENT", "key": key, "sha256": sha}
    etag = client.put_file(key, zip_file, sha)
    if client.head_sha256(key) != sha:                       # 올라간 것을 다시 확인
        raise IOError("업로드 후 HEAD sha256 불일치")
    remote_manifest = {k: v for k, v in manifest.items() if k != "files"}
    remote_manifest.update(uploaded_key=key, uploaded_etag=etag, zip_sha256=sha,
                           uploaded_at_utc=client.clock().strftime("%Y-%m-%dT%H:%M:%SZ"),
                           files=manifest.get("files", []))
    client.put_bytes(mkey, json.dumps(remote_manifest, ensure_ascii=False, indent=1).encode("utf-8"))
    return {"status": "UPLOADED", "key": key, "sha256": sha, "etag": etag}
