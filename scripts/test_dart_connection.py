"""
DART API 연결 테스트 스크립트.

- DART_API_KEY 존재 여부 확인 (값 출력 금지)
- 최근 공시 조회 테스트
- 공시 키워드 점수 계산 테스트

실행:
    python scripts/test_dart_connection.py
"""

import sys
from pathlib import Path
from datetime import datetime, timedelta

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

from app.config import get_dart_api_key
from app.data.dart_client import create_dart_client
from app.data.disclosure_filter import DisclosureFilter


def section(title: str) -> None:
    print(f"\n{'='*55}")
    print(f"  {title}")
    print(f"{'='*55}")


def ok(msg: str) -> None:
    print(f"  ✅ {msg}")


def warn(msg: str) -> None:
    print(f"  ⚠️  {msg}")


def fail(msg: str) -> None:
    print(f"  ❌ {msg}")


def main() -> None:
    print("\n🔍 DART API 연결 테스트")

    # ── 1. API 키 존재 여부 ──────────────────────────────────────────────
    section("1. DART_API_KEY 확인")
    dart_key = get_dart_api_key()
    if dart_key:
        ok("DART_API_KEY: SET (값은 출력하지 않습니다)")
    else:
        fail("DART_API_KEY: NOT SET — .env에 DART_API_KEY를 설정하세요.")
        warn("공시 필터가 비활성화 상태입니다.")
        print()
        return

    # ── 2. DartClient 초기화 ────────────────────────────────────────────
    section("2. DartClient 초기화")
    client = create_dart_client()
    if client.is_configured():
        ok("DartClient 초기화 완료")
    else:
        fail("DartClient 초기화 실패")
        return

    # ── 3. 최근 공시 조회 ───────────────────────────────────────────────
    section("3. 최근 7일 공시 조회 테스트")
    bgn_de = (datetime.now() - timedelta(days=7)).strftime("%Y%m%d")
    end_de = datetime.now().strftime("%Y%m%d")
    print(f"  조회 기간: {bgn_de} ~ {end_de}")

    try:
        disclosures = client.get_recent_disclosures(
            bgn_de=bgn_de,
            end_de=end_de,
            page_count=5,
        )
        if disclosures:
            ok(f"공시 조회 성공: {len(disclosures)}건")
            for d in disclosures[:3]:
                title = d.get("report_nm", "")
                corp = d.get("corp_name", "")
                print(f"    - [{corp}] {title}")
        else:
            warn("조회된 공시가 없습니다 (API는 정상).")
    except Exception as e:
        fail(f"공시 조회 실패: {e}")

    # ── 4. 삼성전자 공시 조회 ────────────────────────────────────────────
    section("4. 삼성전자 공시 조회 테스트")
    try:
        disclosures_sec = client.get_recent_disclosures(
            corp_name="삼성전자",
            bgn_de=bgn_de,
            end_de=end_de,
            page_count=3,
        )
        ok(f"삼성전자 공시: {len(disclosures_sec)}건")
    except Exception as e:
        warn(f"삼성전자 공시 조회 실패: {e}")

    # ── 5. 키워드 점수 분류 테스트 ──────────────────────────────────────
    section("5. 공시 키워드 점수 분류 테스트")

    class _FakeCfg:
        dart = {
            "enabled": True,
            "max_positive_bonus": 10,
            "max_negative_penalty": -20,
            "exclude_severe_risk_disclosure": True,
        }
        _raw = {}

    disc_filter = DisclosureFilter(cfg=_FakeCfg())

    test_cases = [
        ({"report_nm": "단일판매공급계약체결"}, "강호재: 단일판매/공급계약"),
        ({"report_nm": "자기주식취득 결정"}, "호재: 자기주식취득"),
        ({"report_nm": "유상증자 결정"}, "부정: 유상증자"),
        ({"report_nm": "횡령배임혐의 공시"}, "강리스크: 횡령/배임"),
        ({"report_nm": "상장폐지 예고"}, "강리스크: 상장폐지"),
    ]

    for disclosure, desc in test_cases:
        result = disc_filter.score_disclosures([disclosure])
        score = result["disclosure_score"]
        is_severe = result["has_severe_risk"]
        score_sign = "+" if score >= 0 else ""
        severe_flag = " ⚠️ SEVERE" if is_severe else ""
        print(f"  [{desc}] score={score_sign}{score:.0f}{severe_flag}")

    ok("키워드 분류 테스트 완료")

    print(f"\n{'='*55}")
    print("  DART 연결 테스트 완료")
    print(f"{'='*55}\n")


if __name__ == "__main__":
    main()
