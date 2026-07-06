"""
market_data_validator.py — 시장 데이터 가격 범위·논리 검증.

실전 주문 기능과 절대 연결하지 않습니다.
MU(마이크론) 가격이 1,000USD 이상이면 소수점/환산 오류로 판단합니다.
"""

from __future__ import annotations

from typing import Optional


class DataValidationError(ValueError):
    """Raised when market or prediction data fails safety validation."""


# ── 유효 가격 범위 상수 ───────────────────────────────────────────────────────

MU_PRICE_MIN: float = 20.0        # USD
MU_PRICE_MAX: float = 500.0       # USD — 이 이상이면 단위 오류 의심
MU_PRICE_HARD_MAX: float = 1000.0  # 이 이상이면 즉시 무효

HYNIX_PRICE_MIN: int = 50_000     # KRW
HYNIX_PRICE_MAX: int = 5_000_000  # KRW
HYNIX_CODE: str = "000660"
HYNIX_NAME: str = "SK하이닉스"


# ── MU 가격 검증 ─────────────────────────────────────────────────────────────

def validate_mu_price(price: Optional[float]) -> tuple[bool, str]:
    """
    MU 가격이 유효 범위(20~500 USD)인지 검증.

    Returns
    -------
    (True, "ok") | (False, 오류메시지)
    """
    if price is None:
        return False, "MU 가격 없음 (None)"
    if price > MU_PRICE_HARD_MAX:
        return False, (
            f"MU 가격 {price:.2f}USD > {MU_PRICE_HARD_MAX:.0f} — "
            "소수점/환산 오류 의심 (예측 금지)"
        )
    if price > MU_PRICE_MAX:
        return False, (
            f"MU 가격 {price:.2f}USD > {MU_PRICE_MAX:.0f} — 비정상 고가"
        )
    if price < MU_PRICE_MIN:
        return False, (
            f"MU 가격 {price:.2f}USD < {MU_PRICE_MIN:.0f} — 비정상 저가"
        )
    return True, "ok"


def auto_fix_mu_price(price: Optional[float]) -> Optional[float]:
    """
    MU 가격 자동 보정.

    - 이미 정상 범위(20~500)이면 그대로 반환.
    - /10, /100 으로 범위 내로 들어오면 보정값 반환.
    - 여전히 범위 밖이면 None 반환.
    """
    if price is None:
        return None
    if MU_PRICE_MIN <= price <= MU_PRICE_MAX:
        return price
    for divisor in (10, 100):
        fixed = price / divisor
        if MU_PRICE_MIN <= fixed <= MU_PRICE_MAX:
            return fixed
    return None


def parse_mu_price_str(raw: object) -> Optional[float]:
    """
    KIS API 응답 문자열에서 MU 가격 파싱.

    - 콤마 제거 후 float 변환.
    - 범위 검증 후 자동 보정 시도.
    """
    if raw is None:
        return None
    try:
        cleaned = str(raw).replace(",", "").strip()
        if not cleaned or cleaned in ("0", "0.0", ""):
            return None
        price = float(cleaned)
        if price <= 0:
            return None
        return auto_fix_mu_price(price)
    except (ValueError, TypeError):
        return None


# ── SK하이닉스 가격 검증 ─────────────────────────────────────────────────────

def validate_hynix_price(price: Optional[float]) -> tuple[bool, str]:
    """
    SK하이닉스 종가가 유효 범위(50,000~1,000,000원)인지 검증.
    """
    if price is None:
        return False, "SK하이닉스 가격 없음 (None)"
    if price < HYNIX_PRICE_MIN:
        return False, (
            f"SK하이닉스 {price:,.0f}원 < {HYNIX_PRICE_MIN:,}원 — 비정상 저가"
        )
    if price > HYNIX_PRICE_MAX:
        return False, (
            f"SK하이닉스 {price:,.0f}원 > {HYNIX_PRICE_MAX:,}원 — 비정상 고가"
        )
    return True, "ok"


def validate_stock_identity(code: object, name: object) -> tuple[bool, str]:
    """Validate that the selected stock is SK Hynix (000660)."""
    normalized_code = str(code or "").strip().zfill(6)
    normalized_name = str(name or "").strip().replace(" ", "")
    if normalized_code != HYNIX_CODE:
        return False, f"stock code mismatch: expected {HYNIX_CODE}, got {normalized_code}"
    if normalized_name not in {"SK하이닉스", "에스케이하이닉스"}:
        return False, f"stock name mismatch: expected {HYNIX_NAME}, got {name}"
    return True, "ok"


def validate_hynix_current_sources(source_prices: dict, tolerance_pct: float = 1.0) -> tuple[bool, str, dict]:
    """Validate KIS, Naver and Yahoo current prices before forecasting.

    At least two valid sources are required. The selected anchor follows the
    requested priority: KIS -> Naver -> Yahoo. If all available valid sources
    differ by >= tolerance, the caller must block prediction.
    """
    required = ["KIS", "naver", "yfinance"]
    cleaned: dict[str, float] = {}
    missing: list[str] = []
    invalid: dict[str, str] = {}
    for source in required:
        price = source_prices.get(source)
        ok, msg = validate_hynix_price(price)
        if not ok:
            missing.append(source)
            invalid[source] = msg
            continue
        cleaned[source] = float(price)

    if len(cleaned) < 2:
        return False, f"fewer than 2 valid current price sources: {invalid}", {
            "source_prices": source_prices,
            "selected_source": None,
            "selected_price": None,
            "max_diff_pct": None,
            "missing_sources": missing,
        }

    values = list(cleaned.values())
    low = min(values)
    high = max(values)
    max_diff_pct = (high / low - 1.0) * 100 if low > 0 else 100.0
    if max_diff_pct >= tolerance_pct:
        return False, f"current price source spread {max_diff_pct:.2f}% >= {tolerance_pct:.2f}%", {
            "source_prices": cleaned,
            "selected_source": None,
            "selected_price": None,
            "max_diff_pct": round(max_diff_pct, 4),
            "missing_sources": missing,
        }

    selected_source = next(source for source in required if cleaned.get(source) is not None)
    return True, "ok", {
        "source_prices": cleaned,
        "selected_source": selected_source,
        "selected_price": cleaned[selected_source],
        "max_diff_pct": round(max_diff_pct, 4),
        "missing_sources": missing,
    }


def validate_hynix_dataframe(df) -> tuple[bool, str, object]:
    """
    SK하이닉스 일봉 DataFrame 검증.

    - 최소 20개 행
    - close 가격이 유효 범위 내
    - 유효하지 않은 행 필터링 후 반환

    Returns
    -------
    (valid, message, filtered_df_or_original)
    """
    if df is None or df.empty:
        return False, "일봉 데이터 없음", df

    import pandas as pd

    df_work = df.copy()
    if "close" not in df_work.columns:
        return False, "close 컬럼 없음", df

    n_before = len(df_work)
    df_work = df_work[
        df_work["close"].apply(lambda x: HYNIX_PRICE_MIN <= x <= HYNIX_PRICE_MAX)
    ].reset_index(drop=True)
    n_after = len(df_work)

    if n_after < 20:
        return (
            False,
            f"유효 일봉 {n_after}개 < 최소 20개 필요 (검증 전 {n_before}개)",
            df_work,
        )
    return True, f"유효 일봉 {n_after}개", df_work


# ── 가격 구간 논리 검증 ───────────────────────────────────────────────────────

def validate_price_zones(
    target_price: Optional[float],
    stop_loss_price: Optional[float],
) -> tuple[bool, str]:
    """
    목표가 > 손절가 조건 검증.

    Returns
    -------
    (True, "ok") | (False, 오류메시지)
    """
    if target_price is None or stop_loss_price is None:
        return True, "ok (가격 구간 미설정)"
    if stop_loss_price >= target_price:
        return False, (
            f"손절가({stop_loss_price:,.0f}원) ≥ 목표가({target_price:,.0f}원) "
            "— 예측 결과 무효"
        )
    return True, "ok"


def validate_swing_result(swing: dict) -> tuple[bool, str]:
    """
    스윙 플래그 결과의 가격 구간 논리를 종합 검증.
    """
    ok, msg = validate_price_zones(
        swing.get("target_price"),
        swing.get("stop_loss_price"),
    )
    return ok, msg


def validate_prediction_prices(prediction: dict, current_price: Optional[float]) -> tuple[bool, str]:
    """Validate final displayed SK Hynix prediction prices against current_price."""
    if current_price is None or current_price <= 0:
        raise DataValidationError("현재가 없음")

    intraday_fields = [
        "today_open_expected",
        "today_high_expected",
        "today_low_expected",
        "today_close_expected",
        "target_price",
        "stop_loss_price",
    ]
    for field in intraday_fields:
        value = prediction.get(field)
        if value is None:
            continue
        ratio = float(value) / float(current_price)
        if ratio > 1.15 or ratio < 0.85:
            raise DataValidationError(
                f"{field}가 현재가 대비 ±15%를 초과합니다. 기준가격 오류 가능성 "
                f"(current_price={current_price:,.0f}, value={float(value):,.0f})"
            )

    for field in ["two_week_high_price", "two_week_low_price"]:
        value = prediction.get(field)
        if value is None:
            continue
        ratio = float(value) / float(current_price)
        if ratio > 1.40 or ratio < 0.60:
            return False, (
                f"{field}가 현재가 대비 ±40%를 초과합니다 "
                f"(current_price={current_price:,.0f}, value={float(value):,.0f})"
            )
    return True, "ok"
