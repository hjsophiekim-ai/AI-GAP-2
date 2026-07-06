#!/usr/bin/env python
"""
diagnose_real_trading_config.py

실전매매 설정 진단 스크립트.
- config.yaml 안전 플래그 출력 (환경변수 값 자체는 절대 출력 안 함)
- 환경변수 존재 여부만 확인 (SET / MISSING)

사용법:
  python scripts/diagnose_real_trading_config.py
"""
import os
import sys
from pathlib import Path

# 프로젝트 루트를 sys.path에 추가
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(_ROOT / ".env")
except ImportError:
    pass  # python-dotenv 없어도 os.environ에서 읽음


def _check_env(var_name: str) -> str:
    return "SET" if os.getenv(var_name, "") else "MISSING"


def _section(title: str) -> None:
    print(f"\n{'='*60}")
    print(f"  {title}")
    print('='*60)


def main() -> None:
    print("\nAI-GAP 실전매매 설정 진단")
    print(f"루트 경로: {_ROOT}")

    # ------------------------------------------------------------------
    # 1. config.yaml 로드
    # ------------------------------------------------------------------
    _section("1. config.yaml 로드")
    try:
        import yaml
        config_path = _ROOT / "config.yaml"
        with open(config_path, "r", encoding="utf-8") as f:
            cfg_raw = yaml.safe_load(f)
        print(f"OK: {config_path}")
    except Exception as exc:
        print(f"FAIL: config.yaml 로드 실패 — {exc}")
        sys.exit(1)

    # ------------------------------------------------------------------
    # 2. 운영 모드
    # ------------------------------------------------------------------
    _section("2. 운영 모드")
    mode = cfg_raw.get("mode", "(없음)")
    print(f"  mode: {mode}")
    if mode == "real":
        print("  WARNING: config.yaml mode가 'real'입니다. UI 실전모드 버튼과 무관하게 broker_factory가 real 브로커를 사용합니다.")
    else:
        print(f"  OK: mode='{mode}' (실전매매 비활성)")

    # ------------------------------------------------------------------
    # 3. KIS 실전 계좌 설정
    # ------------------------------------------------------------------
    _section("3. KIS 실전 계좌 설정 (config.yaml)")
    kis_real = cfg_raw.get("kis", {}).get("real", {})
    enabled = kis_real.get("enabled", False)
    print(f"  kis.real.enabled: {enabled}")
    if enabled:
        print("  WARNING: kis.real.enabled=true — 실전 계좌가 기본 활성화되어 있습니다.")
    else:
        print("  OK: kis.real.enabled=false (기본 안전)")

    # ------------------------------------------------------------------
    # 4. 환경변수 존재 여부 (값은 절대 출력하지 않음)
    # ------------------------------------------------------------------
    _section("4. 실전 계좌 환경변수 존재 여부")
    env_vars = {
        "APP_KEY":      kis_real.get("app_key_env",             "KIS_REAL_APP_KEY"),
        "APP_SECRET":   kis_real.get("app_secret_env",          "KIS_REAL_APP_SECRET"),
        "ACCOUNT_NO":   kis_real.get("account_no_env",          "KIS_ACCOUNT_NO"),
        "PRODUCT_CODE": kis_real.get("account_product_code_env", "KIS_ACCOUNT_PRODUCT_CODE"),
    }
    all_set = True
    for label, var_name in env_vars.items():
        status = _check_env(var_name)
        print(f"  [{status}] {var_name}")
        if status == "MISSING":
            all_set = False

    if all_set:
        print("\n  OK: 모든 실전 계좌 환경변수가 설정되어 있습니다.")
    else:
        print("\n  WARNING: 일부 환경변수가 없습니다. .env 파일을 확인하세요.")

    _section("5. 모의 계좌 환경변수 존재 여부")
    kis_mock = cfg_raw.get("kis", {}).get("mock", {})
    mock_env_vars = {
        "APP_KEY":      kis_mock.get("app_key_env",             "KIS_MOCK_APP_KEY"),
        "APP_SECRET":   kis_mock.get("app_secret_env",          "KIS_MOCK_APP_SECRET"),
        "ACCOUNT_NO":   kis_mock.get("account_no_env",          "KIS_MOCK_ACCOUNT_NO"),
        "PRODUCT_CODE": kis_mock.get("account_product_code_env", "KIS_MOCK_ACCOUNT_PRODUCT_CODE"),
    }
    for label, var_name in mock_env_vars.items():
        status = _check_env(var_name)
        print(f"  [{status}] {var_name}")

    # ------------------------------------------------------------------
    # 6. Safety 플래그
    # ------------------------------------------------------------------
    _section("6. Safety 플래그 (config.yaml)")
    safety = cfg_raw.get("safety", {})
    safety_keys = [
        "enable_real_trading",
        "enable_real_buy",
        "enable_real_sell",
        "require_real_order_confirm_text",
        "real_order_confirm_text",
        "max_order_amount",
        "max_daily_order_amount",
        "max_daily_loss_rate",
    ]
    for key in safety_keys:
        val = safety.get(key, "(없음)")
        print(f"  safety.{key}: {val}")

    # ------------------------------------------------------------------
    # 7. 진단 요약
    # ------------------------------------------------------------------
    _section("7. 진단 요약")
    issues = []
    if enabled:
        issues.append("kis.real.enabled=true — 기본값은 false 권장")
    if safety.get("enable_real_trading", False):
        issues.append("safety.enable_real_trading=true — 실전매매 전역 활성")
    if safety.get("enable_real_buy", False):
        issues.append("safety.enable_real_buy=true — 실전 매수 활성")
    if safety.get("enable_real_sell", False):
        issues.append("safety.enable_real_sell=true — 실전 매도 활성")
    if not all_set:
        issues.append("실전 계좌 환경변수 미설정")

    if issues:
        print("  주의 사항:")
        for i in issues:
            print(f"    - {i}")
    else:
        print("  OK: 모든 안전 플래그가 기본값(비활성)으로 설정되어 있습니다.")
        print("  실전매매는 UI에서 '실전모드 활성화' 버튼으로만 활성화됩니다.")

    print()


if __name__ == "__main__":
    main()
