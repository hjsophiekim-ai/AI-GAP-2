import hashlib
import os
from pathlib import Path
from typing import Optional
import yaml
from dotenv import load_dotenv

_ROOT = Path(__file__).parent.parent
_ENV_PATH = _ROOT / ".env"
_ENV_LOADED_MTIME: float | None = None


def reload_environment(force: bool = False) -> None:
    """Reload .env without overriding Render/OS environment variables.

    Effective precedence:
    1. Render/OS environment variables
    2. Local .env
    3. config.yaml/default values
    """
    global _ENV_LOADED_MTIME
    try:
        mtime = _ENV_PATH.stat().st_mtime
    except FileNotFoundError:
        mtime = None
    if not force and mtime == _ENV_LOADED_MTIME:
        return
    load_dotenv(_ENV_PATH, override=False)
    _ENV_LOADED_MTIME = mtime


reload_environment(force=True)

_CONFIG_PATH = _ROOT / "config.yaml"

_DEFAULT_CONFIG = {
    "mode": "dry_run",
    "trading": {
        "total_budget": 10000000,
        "max_positions": 15,
        "max_shares_per_stock": 2,
        "min_gap_rate": 1.0,
        "max_gap_rate": 20.0,
        "min_trade_value": 300000000,
        "buy_start_time": "09:05",
        "buy_end_time": "09:10",
        "first_take_profit_rate": 3.0,
        "second_take_profit_rate": 5.0,
        "stop_loss_rate": -1.5,
        "bulk_sell_1150_time": "11:50",
        "force_sell_time": "13:00",
        "emergency_sell_time": "15:10",
        "order_type": "limit",
        "allow_market_order": False,
        "min_price": 1000,
    },
    "filters": {
        "exclude_etf": True,
        "exclude_etn": True,
        "exclude_preferred_stock": True,
        "exclude_spac": True,
        "exclude_reit": True,
        "exclude_warning_stock": True,
        "exclude_halt": True,
        "min_price": 1000,
        "max_spread_rate": 1.0,
    },
    "data_source": {
        "pre_market_primary": "naver",
        "regular_market_primary": "kis",
        "secondary": "naver",
        "use_naver_gap_tab": True,
        "use_naver_volume_tab": True,
        "market_open_time": "09:00",
    },
    "naver": {"sise_url": "https://finance.naver.com/sise/"},
    "kis": {
        "real": {
            "enabled": False,
            "app_key_env": "KIS_REAL_APP_KEY",
            "app_secret_env": "KIS_REAL_APP_SECRET",
            "account_no_env": "KIS_REAL_ACCOUNT_NO",
            "account_product_code_env": "KIS_REAL_ACCOUNT_PRODUCT_CODE",
            "product_code_env": "KIS_REAL_ACCOUNT_PRODUCT_CODE",
            "base_url": "https://openapi.koreainvestment.com:9443",
        },
        "mock": {
            "enabled": True,
            "app_key_env": "KIS_MOCK_APP_KEY",
            "app_secret_env": "KIS_MOCK_APP_SECRET",
            "account_no_env": "KIS_MOCK_ACCOUNT_NO",
            "account_product_code_env": "KIS_MOCK_ACCOUNT_PRODUCT_CODE",
            "product_code_env": "KIS_MOCK_ACCOUNT_PRODUCT_CODE",
            "base_url": "https://openapivts.koreainvestment.com:29443",
        },
    },
    "dart": {
        "enabled": True,
        "api_key_env": "DART_API_KEY",
        "lookback_days": 7,
        "use_disclosure_score": True,
        "disclosure_score_weight": 0.10,
        "max_positive_bonus": 10,
        "max_negative_penalty": -20,
        "exclude_severe_risk_disclosure": True,
    },
    "ml": {
        "use_model": True,
        "fallback_to_rule_score": True,
        "model_path": "models/gap_model.pkl",
        "feature_importance_path": "models/feature_importance.csv",
        "min_training_rows": 500,
        "ml_weight": 0.6,
        "rule_weight": 0.4,
    },
    "safety": {
        "enable_real_trading": False,
        "enable_real_buy": False,
        "enable_real_sell": False,
        "require_real_order_confirm_text": True,
        "real_order_confirm_text": os.getenv("REAL_ORDER_CONFIRM_TEXT", "live"),
        "real_trading_start_date": "2026-07-14",
        "max_order_amount": 10000000,
        "max_daily_order_amount": 10000000,
        "max_daily_loss_rate": -5.0,
        "require_real_confirm": True,
        "real_confirm_text": os.getenv("REAL_ORDER_CONFIRM_TEXT", "live"),
        "max_real_order_amount": 10000000,
        "max_real_daily_budget": 10000000,
    },
    "volume_spike": {
        "enabled": True,
        "source_url": "https://finance.naver.com/sise/sise_quant_high.naver",
        "target_top_n": 10,
        "min_price": 20000,
        "min_change_rate": 3.0,
        "max_change_rate": 18.0,
        "min_trading_value": 3000000000,
        "fallback_min_trading_value": 1000000000,
        "fallback_min_price": 10000,
        "exclude_etf": True,
        "exclude_etn": True,
        "exclude_preferred": True,
        "exclude_spac": True,
        "exclude_reit": True,
        "exclude_suspended": True,
        "quality_stock_preference": True,
        "max_candidates_to_score": 80,
    },
    "logging": {"save_csv": True, "save_db": False, "level": "INFO", "log_dir": "logs"},
    "auto_sell": {
        "enabled": False,
        "check_interval_seconds": int(os.getenv("AUTO_SELL_CHECK_INTERVAL_SECONDS", 10)),
        "market_start": "09:00",
        "market_end": "15:20",
        "first_take_profit_rate": float(os.getenv("AUTO_SELL_FIRST_TP_RATE", 3.0)),
        "first_take_profit_sell_ratio": float(os.getenv("AUTO_SELL_FIRST_TP_RATIO", 0.5)),
        "final_take_profit_rate": float(os.getenv("AUTO_SELL_FINAL_TP_RATE", 5.0)),
        "final_take_profit_sell_ratio": float(os.getenv("AUTO_SELL_FINAL_TP_RATIO", 1.0)),
        "stop_loss_rate": float(os.getenv("AUTO_SELL_STOP_LOSS_RATE", -2.0)),
        "order_type": "market",
        "prevent_duplicate_orders": True,
        "require_real_mode": True,
        "save_state": True,
        "state_file": "data/state/auto_sell_state.json",
        "log_file": "data/logs/auto_sell_orders.csv",
    },
    "candidate_quality_filters": {
        "enabled": True,
        "speed_mode": True,
        "relaxed_mode": True,
        "target_min_candidates": 10,
        "target_top_n": 15,
        "min_price": 1000,
        "absolute_min_trading_value": 300000000,
        "min_trading_value_general": 700000000,
        "min_trading_value_0920": 1000000000,
        "healthy_gap_min": 1.0,
        "healthy_gap_max": 9.0,
        "caution_gap_max": 15.0,
        "hard_exclude_gap_rate": 20.0,
        "caution_gap_rate": 7.0,
        "max_open_gap_rate": 12.0,
        "max_3d_return": 25.0,
        "max_5d_return": 35.0,
        "max_intraday_drop_from_high": 4.0,
        "max_ma20_extension_rate": 15.0,
        "max_same_theme_in_top15": 5,
        "max_same_subtheme_in_top15": 4,
        "max_candidates_for_heavy_filters": 30,
        "max_drop_from_open_rate": 50.0,
    },
}


def _load_yaml() -> dict:
    if not _CONFIG_PATH.exists():
        import logging
        logging.getLogger(__name__).warning(
            "config.yaml not found at %s — using safe defaults.", _CONFIG_PATH
        )
        return _DEFAULT_CONFIG.copy()
    with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


class Config:
    def __init__(self):
        self._raw = _load_yaml()

    def get(self, *keys, default=None):
        node = self._raw
        for k in keys:
            if not isinstance(node, dict):
                return default
            node = node.get(k, default)
            if node is None:
                return default
        return node

    @property
    def mode(self) -> str:
        return self._raw.get("mode", "dry_run")

    @property
    def trading(self) -> dict:
        return self._raw.get("trading", {})

    @property
    def filters(self) -> dict:
        return self._raw.get("filters", {})

    @property
    def data_source(self) -> dict:
        return self._raw.get("data_source", {})

    @property
    def ml(self) -> dict:
        return self._raw.get("ml", {})

    @property
    def safety(self) -> dict:
        return self._raw.get("safety", {})

    @property
    def logging_cfg(self) -> dict:
        return self._raw.get("logging", {})

    @property
    def dart(self) -> dict:
        return self._raw.get("dart", {})

    @staticmethod
    def _env_flag_override(env_name: str) -> Optional[bool]:
        """환경변수가 설정되어 있으면 그 값을(true/false), 없으면 None을 반환한다."""
        raw = os.getenv(env_name)
        if raw is None or raw == "":
            return None
        return raw.strip().lower() in ("true", "1", "yes")

    def real_trading_enabled(self) -> bool:
        """실전투자 마스터 스위치. ENABLE_REAL_TRADING(.env)이 설정되어 있으면 그 값이
        config.yaml의 safety.enable_real_trading보다 우선한다."""
        override = self._env_flag_override("ENABLE_REAL_TRADING")
        if override is not None:
            return override
        return bool(self.safety.get("enable_real_trading", False))

    def real_buy_enabled(self) -> bool:
        override = self._env_flag_override("ENABLE_REAL_BUY")
        if override is not None:
            return override
        return bool(self.safety.get("enable_real_buy", False))

    def real_sell_enabled(self) -> bool:
        override = self._env_flag_override("ENABLE_REAL_SELL")
        if override is not None:
            return override
        return bool(self.safety.get("enable_real_sell", False))

    def require_real_confirm(self) -> bool:
        """새 키 우선, 구 키 fallback."""
        val = self.safety.get("require_real_order_confirm_text")
        if val is None:
            val = self.safety.get("require_real_confirm", True)
        return bool(val)

    def real_confirm_text(self) -> str:
        """REAL 확인 문구. safety.real_confirm_text를 기준값으로 사용한다."""
        return (
            self.safety.get("real_confirm_text")
            or self.safety.get("real_order_confirm_text", "live")
        )

    def real_trading_start_date(self) -> str:
        return str(self.safety.get("real_trading_start_date", "2026-07-14"))

    def real_trading_date_allowed(self) -> bool:
        from datetime import date
        try:
            start = date.fromisoformat(self.real_trading_start_date())
        except ValueError:
            return True
        return date.today() >= start

    @property
    def hynix_auto_trade(self) -> dict:
        defaults = {
            "max_daily_buy_pct": 0.20,
            "max_symbol_pct": 0.70,
            "min_cash_ratio_for_buy": 0.20,
            "daily_loss_limit_pct": -3.0,
        }
        defaults.update(self._raw.get("hynix_auto_trade", {}) or {})
        return defaults

    @property
    def trading_cost(self) -> dict:
        """한국투자증권 실거래 기준 수수료/거래세/슬리피지 설정(docs/requirements.md
        섹션 2). 하드코딩 금지 — 반드시 이 설정을 통해서만 읽는다. 예시값이며 실제
        KIS 고시 요율로 운영 전 재확인이 필요하다."""
        defaults = {
            "domestic_buy_fee_rate": 0.00015, "domestic_sell_fee_rate": 0.00015,
            "etf_buy_fee_rate": 0.00015, "etf_sell_fee_rate": 0.00015,
            "transaction_tax_rate": 0.0018, "etf_transaction_tax_rate": 0.0,
            "clearing_fee_rate": 0.0, "slippage_rate_default": 0.0002,
            "slippage_rate_market_order": 0.0003, "slippage_rate_limit_order": 0.0001,
            "min_commission_krw": 0.0,
        }
        defaults.update(self._raw.get("trading_cost", {}) or {})
        return defaults

    def full_auto_enabled(self) -> bool:
        """ENABLE_FULL_AUTO=true 여부. 기본값 false(제안+승인 모드)."""
        import os
        return os.getenv("ENABLE_FULL_AUTO", "false").strip().lower() in ("true", "1", "yes")

    def full_auto_real_confirm_ok(self) -> bool:
        """완전자동 REAL 실행 허가 여부."""
        return self.enhanced_real_gate_status(current_mode="real")["ready"]

    def enhanced_real_gate_status(self, current_mode: str = "real") -> dict:
        """Enhanced REAL 자동매매 게이트의 단일 진단 결과.

        UI와 주문 risk_manager가 이 함수의 ready 값을 함께 사용한다.
        비밀값은 반환하지 않고, 환경변수 존재 여부와 마스킹 계좌만 반환한다.
        """
        reload_environment()
        import os
        from datetime import datetime

        def _env_bool(name: str) -> bool:
            return os.getenv(name, "").strip().lower() in ("true", "1", "yes")

        def _present(name: str) -> bool:
            return bool(os.getenv(name, "").strip())

        expected_confirm = str(self.real_confirm_text() or "").strip()
        actual_confirm = os.getenv("FULL_AUTO_REAL_CONFIRM_TEXT", "").strip()
        config_enable_real = bool(self.safety.get("enable_real_trading", False))

        checks = {
            "current_mode_is_real": current_mode == "real",
            "enable_full_auto": _env_bool("ENABLE_FULL_AUTO"),
            "config_enable_real_trading": config_enable_real,
            "env_enable_real_trading": _env_bool("ENABLE_REAL_TRADING"),
            "enable_real_buy": _env_bool("ENABLE_REAL_BUY"),
            "enable_real_sell": _env_bool("ENABLE_REAL_SELL"),
            "confirm_text_present": bool(actual_confirm),
            "confirm_text_matched": bool(actual_confirm) and actual_confirm == expected_confirm,
            "real_app_key_present": _present("KIS_REAL_APP_KEY"),
            "real_app_secret_present": _present("KIS_REAL_APP_SECRET"),
            "real_account_present": any(_present(name) for name in ("KIS_REAL_ACCOUNT_NO", "KIS_REAL_CANO", "KIS_ACCOUNT_NO")),
            "real_product_code_present": any(_present(name) for name in ("KIS_REAL_ACCOUNT_PRODUCT_CODE", "KIS_REAL_ACNT_PRDT_CD", "KIS_ACCOUNT_PRODUCT_CODE")),
            "real_trading_start_date_allowed": self.real_trading_date_allowed(),
        }

        account_info = {}
        try:
            account_cfg = get_kis_account_config("real")
            checks["real_account_config_ok"] = True
            checks["real_account_conflict"] = bool(account_cfg.get("account_conflict"))
            account_info = {
                "account_source": account_cfg.get("account_source") or account_cfg.get("cano_source"),
                "masked_account": account_cfg.get("masked_account"),
                "account_conflict_vars": account_cfg.get("account_conflict_vars", []),
                "account_fingerprint": account_cfg.get("account_fingerprint"),
            }
        except Exception as exc:
            checks["real_account_config_ok"] = False
            checks["real_account_conflict"] = False
            account_info = {"account_error": str(exc)}

        blocking_map = {
            "current_mode_is_real": "CURRENT_MODE_NOT_REAL",
            "enable_full_auto": "ENABLE_FULL_AUTO_NOT_TRUE",
            "config_enable_real_trading": "CONFIG_REAL_TRADING_DISABLED",
            "env_enable_real_trading": "ENV_ENABLE_REAL_TRADING_NOT_TRUE",
            "enable_real_buy": "ENABLE_REAL_BUY_NOT_TRUE",
            "enable_real_sell": "ENABLE_REAL_SELL_NOT_TRUE",
            "confirm_text_present": "FULL_AUTO_REAL_CONFIRM_TEXT_MISSING",
            "confirm_text_matched": "FULL_AUTO_REAL_CONFIRM_TEXT_MISMATCH",
            "real_app_key_present": "KIS_REAL_APP_KEY_MISSING",
            "real_app_secret_present": "KIS_REAL_APP_SECRET_MISSING",
            "real_account_present": "KIS_REAL_ACCOUNT_MISSING",
            "real_product_code_present": "KIS_REAL_PRODUCT_CODE_MISSING",
            "real_trading_start_date_allowed": f"REAL_TRADING_START_DATE_NOT_REACHED({self.real_trading_start_date()})",
            "real_account_config_ok": "KIS_REAL_ACCOUNT_CONFIG_INVALID",
        }
        blocking_reasons = [reason for key, reason in blocking_map.items() if not checks.get(key)]
        if checks.get("real_account_conflict"):
            blocking_reasons.append("KIS_REAL_ACCOUNT_ENV_CONFLICT")

        try:
            config_mtime = datetime.fromtimestamp(_CONFIG_PATH.stat().st_mtime).isoformat(timespec="seconds")
        except Exception:
            config_mtime = None

        return {
            "ready": not blocking_reasons,
            "checks": checks,
            "blocking_reasons": blocking_reasons,
            "loaded_config_path": str(_CONFIG_PATH),
            "loaded_config_modified_time": config_mtime,
            "final_safety_enable_real_trading": self.real_trading_enabled(),
            "real_trading_start_date": self.real_trading_start_date(),
            "real_trading_date_allowed": self.real_trading_date_allowed(),
            "config_safety_enable_real_trading": config_enable_real,
            "env_precedence": "Render/OS environment variables > local .env > config.yaml/default",
            "expected_confirm_text_present": bool(expected_confirm),
            **account_info,
        }

    def get_real_order_limits(self) -> dict:
        """실계좌 주문 안전한도 조회. 우선순위: env vars → config.yaml → 기본값."""
        import os
        safety = self.safety

        def _read(env_names: list, config_keys: list, default: float) -> float:
            for env in env_names:
                v = os.getenv(env, "")
                if v:
                    try:
                        return float(v)
                    except ValueError:
                        pass
            for key in config_keys:
                v = safety.get(key)
                if v is not None:
                    try:
                        return float(v)
                    except (ValueError, TypeError):
                        pass
            return default

        per_order = _read(
            ["REAL_MAX_ORDER_AMOUNT", "MAX_REAL_ORDER_AMOUNT", "REAL_ORDER_MAX_AMOUNT"],
            ["max_order_amount", "max_real_order_amount"],
            10_000_000.0,
        )
        daily = _read(
            ["REAL_MAX_DAILY_ORDER_AMOUNT", "MAX_REAL_DAILY_BUDGET"],
            ["max_daily_order_amount", "max_real_daily_budget"],
            10_000_000.0,
        )
        per_symbol = _read(
            ["REAL_MAX_POSITION_AMOUNT_PER_SYMBOL"],
            ["max_position_amount_per_symbol"],
            10_000_000.0,
        )
        auto_reduce = os.getenv("AUTO_REDUCE_QUANTITY_ON_SAFETY_LIMIT", "true").lower() in ("true", "1", "yes")

        return {
            "per_order": per_order,
            "daily": daily,
            "per_symbol": per_symbol,
            "auto_reduce": auto_reduce,
        }


def _parse_account_no(raw: str, product_code: str = "") -> tuple[str, str]:
    """계좌번호 원문을 (CANO 8자리, ACNT_PRDT_CD 2자리)로 파싱.

    지원 포맷:
    - "64282746-01"  → ("64282746", "01")
    - "6428274601"   → ("64282746", "01")
    - "64282746"     → ("64282746", "01")  (product_code 기본값 적용)
    """
    raw = raw.strip()
    if "-" in raw:
        parts = raw.split("-", 1)
        cano = parts[0].strip()
        pcode = parts[1].strip().zfill(2) if len(parts) > 1 else product_code
        return cano, pcode or product_code or "01"
    if len(raw) == 10 and raw.isdigit():
        return raw[:8], product_code or raw[8:] or "01"
    return raw, product_code or "01"


def mask_account(account_no: str, product_code: str = "01") -> str:
    digits = (account_no or "").strip()
    if not digits:
        return ""
    visible = digits[-2:] if len(digits) > 2 else digits
    masked = ("*" * max(len(digits) - len(visible), 0)) + visible
    return f"{masked}-{product_code or '01'}"


def _account_fingerprint(mode: str, account_no: str, product_code: str, app_key: str) -> str:
    raw = "|".join([mode, account_no or "", product_code or "", app_key or ""])
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _account_candidate(source: str, raw_no: str, raw_product_code: str = "") -> dict | None:
    raw_no = (raw_no or "").strip()
    raw_product_code = (raw_product_code or "").strip()
    if not raw_no:
        return None
    account_no, product_code = _parse_account_no(raw_no, raw_product_code)
    return {
        "source": source,
        "account_no": account_no,
        "product_code": product_code or "01",
        "normalized": f"{account_no}-{product_code or '01'}",
    }


def get_kis_account_config(mode: str) -> dict:
    """
    Returns KIS account credentials for the given mode ('mock' or 'real').
    Reads values from environment variables — never returns raw key values in logs.
    Raises ValueError with a descriptive message (not the key values) if required vars are missing.

    계좌번호 우선순위:
    - mock: KIS_MOCK_ACCOUNT_NO → KIS_MOCK_CANO(+KIS_MOCK_ACNT_PRDT_CD)
    - real: KIS_REAL_ACCOUNT_NO → KIS_REAL_CANO(+KIS_REAL_ACNT_PRDT_CD)
            → KIS_ACCOUNT_NO(+KIS_ACCOUNT_PRODUCT_CODE)
    """
    reload_environment()
    cfg = get_config()
    kis_cfg = cfg._raw.get("kis", {})

    if mode == "mock":
        section = kis_cfg.get("mock", {})
        priority = [
            (
                section.get("account_no_env", "KIS_MOCK_ACCOUNT_NO"),
                section.get("product_code_env", "KIS_MOCK_ACCOUNT_PRODUCT_CODE"),
            ),
            ("KIS_MOCK_CANO", "KIS_MOCK_ACNT_PRDT_CD"),
        ]
    elif mode == "real":
        section = kis_cfg.get("real", {})
        priority = [
            (
                section.get("account_no_env", "KIS_REAL_ACCOUNT_NO"),
                section.get("product_code_env", "KIS_REAL_ACCOUNT_PRODUCT_CODE"),
            ),
            ("KIS_REAL_CANO", "KIS_REAL_ACNT_PRDT_CD"),
            ("KIS_ACCOUNT_NO", "KIS_ACCOUNT_PRODUCT_CODE"),
        ]
    else:
        raise ValueError(f"Unknown KIS mode: {mode}. Use 'mock' or 'real'.")

    app_key_env = section.get("app_key_env", "")
    app_secret_env = section.get("app_secret_env", "")

    app_key = os.getenv(app_key_env, "")
    app_secret = os.getenv(app_secret_env, "")

    # 환경변수 존재 체크 (진단용)
    env_checks: dict[str, bool] = {
        app_key_env: bool(app_key),
        app_secret_env: bool(app_secret),
    }

    candidates = []
    for account_env, product_env in priority:
        raw_no = os.getenv(account_env, "").strip()
        raw_prdt = os.getenv(product_env, "").strip()
        env_checks[account_env] = bool(raw_no)
        env_checks[product_env] = bool(raw_prdt)
        candidate = _account_candidate(account_env, raw_no, raw_prdt)
        if candidate is not None:
            candidates.append(candidate)

    selected = candidates[0] if candidates else None
    account_no = selected["account_no"] if selected else ""
    product_code = selected["product_code"] if selected else "01"
    account_source = selected["source"] if selected else ""

    normalized_values = {c["normalized"] for c in candidates}
    account_conflict = len(normalized_values) > 1
    account_conflict_vars = [c["source"] for c in candidates] if account_conflict else []

    missing = []
    if not app_key:
        missing.append(app_key_env)
    if not app_secret:
        missing.append(app_secret_env)
    if not account_no:
        missing.append(" 또는 ".join(account_env for account_env, _ in priority))

    if missing:
        raise ValueError(f"필수 환경변수 누락: {', '.join(missing)}")

    masked_account = mask_account(account_no, product_code)
    account_fingerprint = _account_fingerprint(mode, account_no, product_code, app_key)

    return {
        "app_key": app_key,
        "app_secret": app_secret,
        "account_no": account_no,
        "product_code": product_code,
        "account_source": account_source,
        "masked_account": masked_account,
        "account_conflict": account_conflict,
        "account_conflict_vars": account_conflict_vars,
        "account_fingerprint": account_fingerprint,
        "base_url": section.get("base_url", ""),
        "mode": mode,
        "enabled": section.get("enabled", False),
        "env_checks": env_checks,
        "cano_source": account_source,
    }


def get_dart_api_key() -> str:
    """Returns DART API key from environment. Returns empty string if not set."""
    cfg = get_config()
    key_env = cfg.dart.get("api_key_env", "DART_API_KEY")
    return os.getenv(key_env, "")


_instance: "Config | None" = None


def get_config() -> Config:
    global _instance
    if _instance is None:
        _instance = Config()
    return _instance


def reload_config() -> Config:
    global _instance
    reload_environment(force=True)
    _instance = Config()
    return _instance


def reload_runtime_configuration() -> Config:
    """Reload config/env and clear runtime caches without touching in-flight orders."""
    cfg = reload_config()
    for module_name, func_names in (
        ("app.trading.kis_client", ("clear_kis_client_cache",)),
        ("app.trading.broker_factory", ("clear_broker_cache",)),
        ("app.trading.dynamic_exit_watcher", ("clear_runtime_caches",)),
    ):
        try:
            module = __import__(module_name, fromlist=["*"])
            for func_name in func_names:
                func = getattr(module, func_name, None)
                if callable(func):
                    func()
        except Exception:
            pass
    return cfg


_REAL_ACCOUNT_STATE_PATH = _ROOT / "data" / "state" / "kis_real_account_state.json"


def get_real_account_change_status() -> dict:
    """Return whether the current real account fingerprint differs from the last confirmed one."""
    import json

    try:
        cfg = get_kis_account_config("real")
    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc),
            "changed": False,
            "sync_required": False,
            "current_fingerprint": "",
            "masked_account": "",
        }

    current = cfg.get("account_fingerprint", "")
    state = {}
    try:
        if _REAL_ACCOUNT_STATE_PATH.exists():
            with _REAL_ACCOUNT_STATE_PATH.open("r", encoding="utf-8") as f:
                state = json.load(f) or {}
    except Exception:
        state = {}

    previous = state.get("account_fingerprint", "")
    sync_confirmed = state.get("sync_confirmed", False)
    changed = bool(previous and previous != current)
    return {
        "ok": True,
        "changed": changed,
        "sync_required": changed and not sync_confirmed,
        "current_fingerprint": current,
        "previous_fingerprint": previous,
        "masked_account": cfg.get("masked_account", ""),
        "account_source": cfg.get("account_source", ""),
        "sync_confirmed": sync_confirmed if not changed else False,
    }


def mark_real_account_sync_confirmed() -> dict:
    """Mark the current real account as externally synchronized after balance/order checks."""
    import json
    from datetime import datetime

    cfg = get_kis_account_config("real")
    _REAL_ACCOUNT_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "account_fingerprint": cfg.get("account_fingerprint", ""),
        "masked_account": cfg.get("masked_account", ""),
        "account_source": cfg.get("account_source", ""),
        "sync_confirmed": True,
        "confirmed_at": datetime.now().isoformat(timespec="seconds"),
    }
    with _REAL_ACCOUNT_STATE_PATH.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    return state


_MARKET_REGIME_CONFIG_PATH = _ROOT / "config" / "market_regime.yaml"
_TRADING_POLICY_CONFIG_PATH = _ROOT / "config" / "trading_policy.yaml"

_market_regime_cfg_cache: dict | None = None
_trading_policy_cfg_cache: dict | None = None


def _load_extra_yaml(path: Path) -> dict:
    if not path.exists():
        import logging
        logging.getLogger(__name__).warning("설정파일 없음: %s — 빈 dict 사용", path)
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def get_market_regime_config(reload: bool = False) -> dict:
    """config/market_regime.yaml 의 market_regime 블록을 반환한다."""
    global _market_regime_cfg_cache
    if _market_regime_cfg_cache is None or reload:
        raw = _load_extra_yaml(_MARKET_REGIME_CONFIG_PATH)
        _market_regime_cfg_cache = raw.get("market_regime", {})
    return _market_regime_cfg_cache


def get_trading_policy_config(reload: bool = False) -> dict:
    """config/trading_policy.yaml 전체(trading_mode/exit_rules/... 블록 포함)를 반환한다."""
    global _trading_policy_cfg_cache
    if _trading_policy_cfg_cache is None or reload:
        _trading_policy_cfg_cache = _load_extra_yaml(_TRADING_POLICY_CONFIG_PATH)
    return _trading_policy_cfg_cache


def real_order_triple_gate_ok(cfg: "Config" = None) -> bool:
    """REAL 주문 3중 안전장치 확인.

    config.yaml safety.enable_real_trading + trading_policy.yaml
    trading_mode.(order_mode=='REAL' and real_trading_enabled and
    user_confirmed_real_risk) 가 모두 true 여야 REAL 주문이 가능하다.
    그 외에는 무조건 PAPER.
    """
    cfg = cfg or get_config()
    trading_mode = get_trading_policy_config().get("trading_mode", {})
    return bool(
        cfg.real_trading_enabled()
        and trading_mode.get("order_mode") == "REAL"
        and trading_mode.get("real_trading_enabled", False)
        and trading_mode.get("user_confirmed_real_risk", False)
    )
