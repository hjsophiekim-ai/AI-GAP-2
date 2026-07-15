"""
KISClient - 한국투자증권 Open API 공통 클라이언트.

mock(모의투자)과 real(실전투자)을 mode 파라미터로 분리합니다.
API 키/시크릿/토큰은 로그에 절대 출력하지 않습니다.
토큰은 메모리 + JSON 파일 이중 캐싱합니다 (5분 버퍼 만료 체크).
"""

import hashlib
import json
import requests
from datetime import datetime, timedelta
from pathlib import Path
from app.logger import logger
from app.utils.time_utils import kst_now

# ── token cache directory ───────────────────────────────────────────────────
_ROOT = Path(__file__).resolve().parent.parent.parent  # repo root
_TOKEN_CACHE_DIR = _ROOT / "data" / "cache"

# ── base URLs ──────────────────────────────────────────────────────────────
BASE_URL_MOCK = "https://openapivts.koreainvestment.com:29443"
BASE_URL_REAL = "https://openapi.koreainvestment.com:9443"

# ── TR IDs (공식 문서 기준) ────────────────────────────────────────────────
TR_CURRENT_PRICE = "FHKST01010100"

TR_BALANCE_REAL = "TTTC8434R"
TR_BALANCE_MOCK = "VTTC8434R"

TR_BUYABLE_REAL = "TTTC8908R"
TR_BUYABLE_MOCK = "VTTC8908R"

TR_BUY_REAL = "TTTC0802U"
TR_BUY_MOCK = "VTTC0802U"

TR_SELL_REAL = "TTTC0801U"
TR_SELL_MOCK = "VTTC0801U"

TR_ORDER_HISTORY_REAL = "TTTC8001R"
TR_ORDER_HISTORY_MOCK = "VTTC8001R"

TR_DAILY_PRICE = "FHKST01010400"
TR_INVESTOR_TREND = "FHKST01010900"

ORD_DVSN_LIMIT = "00"
ORD_DVSN_MARKET = "01"


def _first_present(mapping: dict, *keys, default=None):
    for key in keys:
        if key in mapping and mapping.get(key) not in (None, ""):
            return mapping.get(key)
    return default


def _to_float(value, default=None):
    try:
        if value in (None, ""):
            return default
        return float(str(value).replace(",", ""))
    except Exception:
        return default


def _to_int(value, default=0):
    try:
        if value in (None, ""):
            return default
        return int(float(str(value).replace(",", "")))
    except Exception:
        return default


class KISTokenError(Exception):
    """KIS oauth2/tokenP 오류 — 이 예외가 발생하면 배치 전체를 중단해야 합니다."""

    def __init__(
        self,
        message: str,
        http_status: int = 0,
        rt_cd: str = "",
        msg_cd: str = "",
        msg1: str = "",
        base_url_used: str = "",
    ) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.rt_cd = rt_cd
        self.msg_cd = msg_cd
        self.msg1 = msg1
        self.base_url_used = base_url_used


class KISClient:
    """
    한국투자증권 Open API 클라이언트.

    Parameters
    ----------
    app_key : str
    app_secret : str
    account_no : str   예: "12345678"
    product_code : str 예: "01"
    mode : str         "mock" 또는 "real"
    """

    def __init__(
        self,
        app_key: str,
        app_secret: str,
        account_no: str,
        product_code: str = "01",
        mode: str = "mock",
    ) -> None:
        self._app_key = app_key
        self._app_secret = app_secret
        self.account_no = account_no
        self.product_code = product_code
        self.mode = mode
        self.base_url = BASE_URL_MOCK if mode == "mock" else BASE_URL_REAL
        self._token: str = ""
        self._token_expires_at: datetime = datetime.min
        self._session = requests.Session()
        self._session.headers.update({"Content-Type": "application/json; charset=utf-8"})

    # ── 공개 속성 ─────────────────────────────────────────────────────────

    @property
    def app_key(self) -> str:
        return self._app_key

    @property
    def app_secret(self) -> str:
        return self._app_secret

    # ── 팩토리 ────────────────────────────────────────────────────────────

    @classmethod
    def from_account_config(cls, account_cfg: dict) -> "KISClient":
        return cls(
            app_key=account_cfg["app_key"],
            app_secret=account_cfg["app_secret"],
            account_no=account_cfg["account_no"],
            product_code=account_cfg.get("product_code", "01"),
            mode=account_cfg.get("mode", "mock"),
        )

    def is_configured(self) -> bool:
        return bool(self._app_key and self._app_secret and self.account_no)

    # ── 토큰 파일 캐시 ────────────────────────────────────────────────────

    def _token_cache_path(self) -> Path:
        return _TOKEN_CACHE_DIR / f"kis_token_{self.mode}.json"

    def _app_key_hash(self) -> str:
        return hashlib.sha256(self._app_key.encode()).hexdigest()[:16]

    def _credential_fingerprint(self) -> str:
        raw = "|".join([
            self.mode,
            self.base_url,
            self._app_key,
            self._app_secret,
        ])
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def _legacy_credential_fingerprint(self) -> str:
        raw = "|".join([
            self.mode,
            self.base_url,
            self._app_key,
            self._app_secret,
            self.account_no,
            self.product_code,
        ])
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def _load_token_cache(self) -> bool:
        """Load a valid file token cache without calling tokenP.

        KIS access tokens are issued for app credentials, not for a specific
        account/product code. Older cache files and the overseas-minute module
        may not have the same metadata, so accept a fresh token when mode,
        base_url, and app_key metadata are either matching or absent.
        """
        try:
            path = self._token_cache_path()
            if not path.exists():
                return False
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)

            token = data.get("access_token", "")
            expires_at_str = data.get("expires_at", "")
            if not token or not expires_at_str:
                return False
            expires_at = datetime.fromisoformat(expires_at_str)
            if datetime.now() >= expires_at - timedelta(minutes=5):
                logger.debug(f"[KIS-{self.mode.upper()}] token cache expired")
                return False

            stored_mode = data.get("mode", "")
            if stored_mode and stored_mode != self.mode:
                logger.info(f"[KIS-{self.mode.upper()}] token cache invalid: mode mismatch")
                return False
            stored_base_url = data.get("base_url", "")
            if stored_base_url and stored_base_url != self.base_url:
                logger.info(f"[KIS-{self.mode.upper()}] token cache invalid: base_url mismatch")
                return False

            stored_hash = data.get("app_key_hash", "")
            if stored_hash and stored_hash != self._app_key_hash():
                logger.info(f"[KIS-{self.mode.upper()}] token cache invalid: app_key changed")
                return False

            stored_fingerprint = data.get("credential_fingerprint", "")
            valid_fingerprints = {
                self._credential_fingerprint(),
                self._legacy_credential_fingerprint(),
            }
            if stored_fingerprint and stored_fingerprint not in valid_fingerprints:
                if stored_hash == self._app_key_hash():
                    logger.info(
                        f"[KIS-{self.mode.upper()}] token cache accepted: app_key matched, fingerprint stale"
                    )
                else:
                    logger.info(f"[KIS-{self.mode.upper()}] token cache invalid: credentials changed")
                    return False
            elif not stored_fingerprint:
                logger.info(f"[KIS-{self.mode.upper()}] token cache accepted: legacy cache format")

            self._token = token
            self._token_expires_at = expires_at
            logger.info(
                f"[KIS-{self.mode.upper()}] file token cache loaded (expires: {expires_at:%H:%M:%S})"
            )
            return True
        except Exception as e:
            logger.debug(f"[KIS-{self.mode.upper()}] token cache load failed: {e}")
            return False

    def _save_token_cache(self) -> None:
        """현재 토큰을 파일 캐시에 저장."""
        try:
            _TOKEN_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            data = {
                "access_token": self._token,
                "expires_at": self._token_expires_at.isoformat(),
                "mode": self.mode,
                "app_key_hash": self._app_key_hash(),
                "credential_fingerprint": self._credential_fingerprint(),
                "base_url": self.base_url,
            }
            with open(self._token_cache_path(), "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            logger.debug(f"[KIS-{self.mode.upper()}] 토큰 파일 캐시 저장 완료")
        except Exception as e:
            logger.warning(f"[KIS-{self.mode.upper()}] 토큰 캐시 저장 실패: {e}")

    # ── 토큰 발급 ─────────────────────────────────────────────────────────

    def get_access_token(self) -> str:
        """
        액세스 토큰 발급/갱신.
        1) 메모리 캐시 (5분 버퍼) → 2) 파일 캐시 → 3) tokenP API 호출.
        비-200 또는 access_token 누락 시 KISTokenError(속성 포함) 발생.
        """
        now = datetime.now()
        # 1. 메모리 캐시
        if self._token and now < self._token_expires_at - timedelta(minutes=5):
            return self._token
        # 2. 파일 캐시
        if self._load_token_cache():
            return self._token
        # 3. API 호출
        url = f"{self.base_url}/oauth2/tokenP"
        body = {
            "grant_type": "client_credentials",
            "appkey": self._app_key,
            "appsecret": self._app_secret,
        }
        try:
            resp = self._session.post(url, json=body, timeout=(3, 10))
            http_status = resp.status_code

            # raise_for_status() 전에 KIS 응답 body 파싱 (403/500 원인 확인용)
            try:
                resp_data = resp.json()
            except Exception:
                resp_data = {}
            rt_cd = resp_data.get("rt_cd", "")
            msg_cd = resp_data.get("msg_cd", "")
            msg1 = resp_data.get("msg1", resp_data.get("error_description", ""))

            if http_status == 403:
                if self._load_token_cache():
                    logger.warning(
                        f"[KIS-{self.mode.upper()}] tokenP 403; using valid file token cache"
                    )
                    return self._token
                key_exists = bool(self._app_key and self._app_secret)
                cache_exists = self._token_cache_path().exists()
                raise KISTokenError(
                    f"[KIS-{self.mode.upper()}] tokenP 403 오류 | "
                    f"mode={self.mode} base_url={self.base_url} "
                    f"key_exists={key_exists} cache_exists={cache_exists} | "
                    f"rt_cd={rt_cd!r} msg_cd={msg_cd!r} msg1={msg1!r}",
                    http_status=403,
                    rt_cd=rt_cd,
                    msg_cd=msg_cd,
                    msg1=msg1,
                    base_url_used=self.base_url,
                )
            if not resp.ok:
                raise KISTokenError(
                    f"[KIS-{self.mode.upper()}] tokenP HTTP {http_status} 오류 | "
                    f"base_url={self.base_url} | "
                    f"rt_cd={rt_cd!r} msg_cd={msg_cd!r} msg1={msg1!r}",
                    http_status=http_status,
                    rt_cd=rt_cd,
                    msg_cd=msg_cd,
                    msg1=msg1,
                    base_url_used=self.base_url,
                )

            token = resp_data.get("access_token", "")
            if not token:
                raise KISTokenError(
                    f"[KIS-{self.mode.upper()}] tokenP 200이나 access_token 없음 | "
                    f"rt_cd={rt_cd!r} msg_cd={msg_cd!r} msg1={msg1!r}",
                    http_status=http_status,
                    rt_cd=rt_cd,
                    msg_cd=msg_cd,
                    msg1=msg1,
                    base_url_used=self.base_url,
                )

            self._token = token
            expires_in = int(resp_data.get("expires_in", 86400))
            self._token_expires_at = now + timedelta(seconds=expires_in)
            self._save_token_cache()
            logger.info(
                f"[KIS-{self.mode.upper()}] 토큰 발급 완료 (만료: {self._token_expires_at:%H:%M:%S})"
            )
            return self._token
        except KISTokenError:
            raise
        except Exception as e:
            logger.error(f"[KIS-{self.mode.upper()}] 토큰 발급 실패: {e}")
            raise

    def ensure_token(self) -> str:
        """get_access_token() 인터페이스 호환 alias — diagnose/test 스크립트용."""
        return self.get_access_token()

    def _auth_headers(self, tr_id: str) -> dict:
        token = self.get_access_token()
        return {
            "authorization": f"Bearer {token}",
            "appkey": self._app_key,
            "appsecret": self._app_secret,
            "tr_id": tr_id,
            "custtype": "P",
        }

    # ── hashkey ────────────────────────────────────────────────────────────

    def get_hashkey(self, body: dict) -> str:
        url = f"{self.base_url}/uapi/hashkey"
        headers = {
            "appkey": self._app_key,
            "appsecret": self._app_secret,
            "Content-Type": "application/json",
        }
        try:
            resp = self._session.post(url, json=body, headers=headers, timeout=(3, 10))
            resp.raise_for_status()
            return resp.json().get("HASH", "")
        except Exception as e:
            logger.warning(f"[KIS] hashkey 조회 실패: {e}")
            return ""

    # ── 현재가 조회 ────────────────────────────────────────────────────────

    def get_current_price(self, symbol: str) -> dict | None:
        """국내주식 현재가 조회. 실패 시 None 반환."""
        url = f"{self.base_url}/uapi/domestic-stock/v1/quotations/inquire-price"
        headers = self._auth_headers(TR_CURRENT_PRICE)
        params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol}
        try:
            resp = self._session.get(url, headers=headers, params=params, timeout=(3, 10))
            resp.raise_for_status()
            d = resp.json().get("output", {})
            if not d:
                logger.warning(f"[KIS] 현재가 응답 없음: {symbol}")
                return None
            return {
                "current_price": float(d.get("stck_prpr", 0)),
                "open": float(d.get("stck_oprc", 0)),
                "high": float(d.get("stck_hgpr", 0)),
                "low": float(d.get("stck_lwpr", 0)),
                "prev_close": float(d.get("stck_sdpr", 0)),
                "change_rate": float(d.get("prdy_ctrt", 0)),
                "volume": int(d.get("acml_vol", 0)),
                "trade_value": float(d.get("acml_tr_pbmn", 0)),
                # 종목명(한글) — inquire-price 응답에 포함되어 별도 API 호출 없이
                # 종목코드 검증(현재가+종목명 일치 확인)에 사용할 수 있다.
                "name": d.get("hts_kor_isnm", ""),
            }
        except Exception as e:
            logger.warning(f"[KIS] 현재가 조회 실패 {symbol}: {e}")
            return None

    # ── 잔고 조회 ──────────────────────────────────────────────────────────

    def get_balance(self) -> dict:
        """계좌 잔고 조회.
        반환: {
            "cash": float,           # 예탁금총금액(dnca_tot_amt) — 인출 기준
            "orderable_cash": float, # 주문가능현금(ord_psbl_cash) — 매수 기준
            "positions": list,
            "error": str (실패 시만)
        }
        """
        tr_id = TR_BALANCE_MOCK if self.mode == "mock" else TR_BALANCE_REAL
        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/inquire-balance"
        headers = self._auth_headers(tr_id)
        params = {
            "CANO": self.account_no,
            "ACNT_PRDT_CD": self.product_code,
            "AFHR_FLPR_YN": "N",
            "OFL_YN": "N",
            "INQR_DVSN": "02",
            "UNPR_DVSN": "01",
            "FUND_STTL_ICLD_YN": "N",
            "FNCG_AMT_AUTO_RDPT_YN": "N",
            "PRCS_DVSN": "01",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
        }
        try:
            resp = self._session.get(url, headers=headers, params=params, timeout=(3, 15))
            # raise_for_status() 전에 본문 파싱 — KIS 500/4xx 응답에도 rt_cd/msg_cd가 들어 있음
            try:
                data = resp.json()
            except Exception:
                data = {}
            if not resp.ok:
                rt_cd = data.get("rt_cd", "")
                msg_cd = data.get("msg_cd", "")
                msg1 = data.get("msg1", data.get("error_description", ""))
                logger.error(
                    f"[KIS-{self.mode.upper()}] 잔고 조회 HTTP {resp.status_code}: "
                    f"rt_cd={rt_cd!r} msg_cd={msg_cd!r} msg1={msg1!r} "
                    f"account=***{self.account_no[-2:]}-{self.product_code}"
                )
                detail = f"HTTP {resp.status_code}"
                if msg_cd:
                    detail += f" msg_cd={msg_cd}"
                if msg1:
                    detail += f": {msg1}"
                elif not msg_cd:
                    detail += f" (응답 본문 없음)"
                return {
                    "cash": None, "orderable_cash": None, "positions": [], "error": detail,
                    "rt_cd": rt_cd, "msg_cd": msg_cd, "msg1": msg1,
                    "response_field_names": sorted(list(data.keys())),
                }

            rt_cd = data.get("rt_cd", "")
            if rt_cd != "0":
                msg1 = data.get("msg1", "알 수 없는 오류")
                msg2 = data.get("msg2", "")
                logger.error(
                    f"[KIS-{self.mode.upper()}] 잔고 조회 실패: "
                    f"rt_cd={rt_cd} msg1={msg1} msg2={msg2}"
                )
                detail = f"{msg1}" + (f" / {msg2}" if msg2 else "")
                return {
                    "cash": None, "orderable_cash": None, "positions": [],
                    "error": f"rt_cd={rt_cd}: {detail}", "rt_cd": rt_cd,
                    "msg_cd": data.get("msg_cd", ""), "msg1": msg1,
                    "response_field_names": sorted(list(data.keys())),
                }

            output2 = data.get("output2") or [{}]
            if isinstance(output2, dict):
                o2 = output2
            else:
                o2 = output2[0] if output2 else {}
            # dnca_tot_amt: 예탁금총금액 (=인출가능금액 근사치, 결제 전 매도대금 미포함)
            cash = _to_float(_first_present(
                o2, "dnca_tot_amt", "tot_evlu_amt", "cash", "cash_balance", "withdrawable_amount",
            ))
            # ord_psbl_cash: 주문가능현금 (=실제 매수에 사용할 금액, D+2 매도대금 포함)
            orderable_cash = _to_float(_first_present(
                o2, "ord_psbl_cash", "nrcvb_buy_amt", "orderable_cash", "buyable_amount", "dnca_tot_amt",
            ))
            if cash is None:
                return {
                    "cash": None, "orderable_cash": orderable_cash, "positions": [],
                    "error": "cash field missing in KIS balance output2",
                    "rt_cd": rt_cd, "msg_cd": data.get("msg_cd", ""), "msg1": data.get("msg1", ""),
                    "response_field_names": sorted(list(data.keys())),
                    "output2_field_names": sorted(list(o2.keys())),
                }

            positions = []
            output1 = data.get("output1") or []
            if isinstance(output1, dict):
                output1 = [output1]
            output1_field_names = set()
            for item in output1:
                if not isinstance(item, dict):
                    continue
                output1_field_names.update(item.keys())
                qty = _to_int(_first_present(item, "hldg_qty", "evlu_qty", "qty", "quantity"), 0)
                if qty <= 0:
                    continue
                positions.append({
                    "symbol": _first_present(item, "pdno", "symb_code", "symbol", "isu_cd", default=""),
                    "name": _first_present(item, "prdt_name", "prdt_name120", "name", "hts_kor_isnm", default=""),
                    "quantity": qty,
                    "avg_price": _to_float(_first_present(item, "pchs_avg_pric", "pchs_avg_price", "avg_price", "pchs_avg_pric_amt"), 0.0) or 0.0,
                    "current_price": _to_float(_first_present(item, "prpr", "now_pric", "current_price"), 0.0) or 0.0,
                    "market_value": _to_float(_first_present(item, "evlu_amt", "market_value", "evlu_pfls_amt"), None),
                })
            logger.info(
                f"[KIS-{self.mode.upper()}] 잔고 조회 성공: "
                f"{len(positions)}종목 예탁금={cash:,.0f}원 주문가능={orderable_cash:,.0f}원"
            )
            return {
                "cash": cash, "orderable_cash": orderable_cash, "positions": positions,
                "response_field_names": sorted(list(data.keys())),
                "output1_field_names": sorted(list(output1_field_names)),
                "output2_field_names": sorted(list(o2.keys())),
                # KST 기준 — 호출부(hynix_switch_engine._recent_valid_account_snapshot 등)가
                # 이 값을 kst_now() 기준 age로 비교한다. 서버 로컬시각(UTC)로 찍으면 Render에서
                # 9시간 어긋나 정상 스냅샷이 "너무 오래됨/미래"로 오판된다.
                "as_of": kst_now().isoformat(timespec="seconds"),
            }
        except Exception as e:
            logger.error(f"[KIS-{self.mode.upper()}] 잔고 조회 예외: {e}")
            return {"cash": None, "orderable_cash": None, "positions": [], "error": str(e)}

    def get_account_cash_breakdown(self) -> dict:
        """계좌 현금 상세 분리 조회.
        반환: {
            "withdrawable_amount": float,   # 인출가능금액(dnca_tot_amt)
            "cash_balance": float,          # 예수금(예탁금총금액 동일)
            "orderable_cash": float,        # 주문가능현금 (ord_psbl_cash + nrcvb_buy_amt 최대값)
            "ord_psbl_cash": float,         # 순수 현금성 주문가능금액 (= 인출가능 근사)
            "nrcvb_buy_amt": float,         # 재매수가능금액 (D+2 매도대금 포함 — 앱 주문가능금액)
            "buyable_amount": float,        # orderable_cash 와 동일
            "settlement_pending_cash": float, # 결제 전 매도대금 추정 (orderable - withdrawable)
            "raw_fields": dict,
        }
        """
        bal = self.get_balance()
        raw_psbl = self.get_buyable_cash_raw("005930", 0)

        withdrawable = bal.get("cash")
        if withdrawable is None:
            withdrawable = 0.0
        orderable_from_bal = bal.get("orderable_cash")
        if orderable_from_bal is None:
            orderable_from_bal = 0.0
        ord_psbl_cash = raw_psbl["ord_psbl_cash"]
        nrcvb_buy_amt = raw_psbl["nrcvb_buy_amt"]

        # 앱 주문가능금액 = max(nrcvb_buy_amt, ord_psbl_cash)
        orderable = max(nrcvb_buy_amt, ord_psbl_cash)
        if orderable == 0:
            orderable = orderable_from_bal
        settlement_pending = max(0.0, orderable - withdrawable)

        return {
            "withdrawable_amount": withdrawable,
            "cash_balance": withdrawable,
            "orderable_cash": orderable,
            "ord_psbl_cash": ord_psbl_cash,
            "nrcvb_buy_amt": nrcvb_buy_amt,
            "buyable_amount": orderable,
            "settlement_pending_cash": settlement_pending,
            "raw_fields": {
                "dnca_tot_amt": withdrawable,
                "ord_psbl_cash_from_balance": orderable_from_bal,
                "ord_psbl_cash_from_psbl_order": ord_psbl_cash,
                "nrcvb_buy_amt_from_psbl_order": nrcvb_buy_amt,
                "psbl_order_raw_output": raw_psbl.get("output", {}),
            },
        }

    def get_stock_buyable_amount(self, symbol: str = "005930", price: int = 0) -> float:
        """종목별 매수가능금액 조회 (get_buyable_cash 동일 로직, 명시적 네이밍)."""
        return self.get_buyable_cash(symbol=symbol, price=price)

    # ── 주문 가능 금액 ────────────────────────────────────────────────────

    def get_buyable_cash_raw(
        self,
        symbol: str = "005930",
        price: int = 0,
        ord_dvsn: str | None = None,
        cma_incl: str = "Y",
    ) -> dict:
        """
        inquire-psbl-order 전체 raw output 반환.
        반환: {
            "output": dict,          # API 응답 output 전체
            "ord_psbl_cash": float,  # 주문가능현금 (현금성)
            "nrcvb_buy_amt": float,  # 재매수가능금액 (D+2 매도대금 포함 — 앱 주문가능금액)
            "psbl_qty": int,         # 주문가능수량
            "rt_cd": str,
            "msg_cd": str,
            "msg1": str,
            "params_used": dict,
            "error": str (실패 시),
        }
        """
        tr_id = TR_BUYABLE_MOCK if self.mode == "mock" else TR_BUYABLE_REAL
        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/inquire-psbl-order"
        headers = self._auth_headers(tr_id)
        _ord_dvsn = ord_dvsn if ord_dvsn is not None else (ORD_DVSN_MARKET if price == 0 else ORD_DVSN_LIMIT)
        params = {
            "CANO": self.account_no,
            "ACNT_PRDT_CD": self.product_code,
            "PDNO": symbol,
            "ORD_UNPR": str(price),
            "ORD_DVSN": _ord_dvsn,
            "CMA_EVLU_AMT_ICLD_YN": cma_incl,
            "OVRS_ICLD_YN": "N",
        }
        try:
            resp = self._session.get(url, headers=headers, params=params, timeout=(3, 10))
            try:
                data = resp.json()
            except Exception:
                data = {}
            if not resp.ok:
                rt_cd = data.get("rt_cd", "")
                msg_cd = data.get("msg_cd", "")
                msg1 = data.get("msg1", data.get("error_description", ""))
                logger.warning(
                    f"[KIS-{self.mode.upper()}] 주문가능금액 HTTP {resp.status_code}: "
                    f"rt_cd={rt_cd!r} msg_cd={msg_cd!r} msg1={msg1!r}"
                )
                detail = f"HTTP {resp.status_code}"
                if msg_cd:
                    detail += f" msg_cd={msg_cd}"
                if msg1:
                    detail += f": {msg1}"
                return {
                    "output": {},
                    "ord_psbl_cash": 0.0,
                    "nrcvb_buy_amt": 0.0,
                    "psbl_qty": 0,
                    "rt_cd": rt_cd,
                    "msg_cd": msg_cd,
                    "msg1": msg1,
                    "params_used": params,
                    "error": detail,
                }
            output = data.get("output", {})
            return {
                "output": output,
                "ord_psbl_cash": float(output.get("ord_psbl_cash", 0) or 0),
                "nrcvb_buy_amt": float(output.get("nrcvb_buy_amt", 0) or 0),
                "psbl_qty": int(output.get("psbl_qty", 0) or 0),
                "rt_cd": data.get("rt_cd", ""),
                "msg_cd": data.get("msg_cd", ""),
                "msg1": data.get("msg1", ""),
                "params_used": params,
            }
        except Exception as e:
            logger.warning(f"[KIS-{self.mode.upper()}] 주문가능금액 raw 조회 실패: {e}")
            return {
                "output": {},
                "ord_psbl_cash": 0.0,
                "nrcvb_buy_amt": 0.0,
                "psbl_qty": 0,
                "rt_cd": "",
                "msg_cd": "",
                "msg1": "",
                "params_used": params,
                "error": str(e),
            }

    def get_buyable_cash(self, symbol: str = "005930", price: int = 0) -> float:
        """
        주문가능금액 조회.
        실계좌(real)에서는 nrcvb_buy_amt(재매수가능금액, D+2 매도대금 포함)를
        ord_psbl_cash보다 우선 사용한다. 앱 "주문가능금액"과 일치하는 필드.
        두 값 모두 0이면 0 반환.

        주의: 이 메서드는 하위호환을 위해 실패/정상 0원을 모두 float 0.0으로
        반환한다 — 호출부가 "조회 실패"와 "실제 잔고 0원"을 구분해야 한다면
        get_buyable_cash_status()를 사용할 것.
        """
        raw = self.get_buyable_cash_raw(symbol=symbol, price=price)
        ord_psbl = raw["ord_psbl_cash"]
        nrcvb = raw["nrcvb_buy_amt"]
        # 실계좌: 재매수가능금액(nrcvb_buy_amt)이 앱 주문가능금액과 일치하는 경향
        # 두 값 중 큰 것을 매수 한도로 사용
        result = max(nrcvb, ord_psbl)
        if result != ord_psbl:
            logger.debug(
                f"[KIS-{self.mode.upper()}] 주문가능: ord_psbl_cash={ord_psbl:,.0f} "
                f"nrcvb_buy_amt={nrcvb:,.0f} → {result:,.0f} 사용"
            )
        return result

    def get_buyable_cash_status(self, symbol: str = "005930", price: int = 0) -> dict:
        """매수가능금액 조회 + "실패/정상 0원/필드누락"을 구분하는 진단 정보.

        요구사항: 정상 응답의 실제 0원, API 실패(rt_cd!=0/HTTP 오류/예외),
        필드 누락을 각각 다른 status로 구분해 UI/자동매매 판단이 "조회 실패로
        인한 0"을 "정말 잔고가 0원"으로 오인하지 않게 한다(2026-07-16 실측:
        모의계좌에 약 1000만원이 있는데 매수가능금액이 0으로 표시됨).

        반환: {
            "value": float,             # 조회된 매수가능금액(실패 시 0.0)
            "ok": bool,                 # True면 value를 신뢰 가능(정상 0원 포함)
            "status": "OK"|"API_ERROR"|"FIELD_MISSING",
            "rt_cd", "msg_cd", "msg1": str,  # KIS 원본 응답 필드
            "error": str|None,
            "raw_output": dict,         # inquire-psbl-order 원본 output(비밀값 없음)
        }
        """
        raw = self.get_buyable_cash_raw(symbol=symbol, price=price)
        output = raw.get("output") or {}
        base = {
            "rt_cd": raw.get("rt_cd", ""), "msg_cd": raw.get("msg_cd", ""), "msg1": raw.get("msg1", ""),
            "raw_output": output,
        }
        if raw.get("error"):
            return {"value": 0.0, "ok": False, "status": "API_ERROR", "error": raw.get("error"), **base}
        rt_cd = raw.get("rt_cd", "")
        if rt_cd not in ("", "0", None):
            return {
                "value": 0.0, "ok": False, "status": "API_ERROR",
                "error": raw.get("msg1") or f"rt_cd={rt_cd}", **base,
            }
        if "ord_psbl_cash" not in output and "nrcvb_buy_amt" not in output:
            return {
                "value": 0.0, "ok": False, "status": "FIELD_MISSING",
                "error": "ord_psbl_cash/nrcvb_buy_amt missing in inquire-psbl-order output", **base,
            }
        value = max(raw.get("nrcvb_buy_amt", 0.0), raw.get("ord_psbl_cash", 0.0))
        return {"value": value, "ok": True, "status": "OK", "error": None, **base}

    def get_token_status(self) -> dict:
        """토큰 상태 진단(비밀값 없음) — 토큰 문자열 자체는 절대 반환하지 않는다."""
        expires_at = self._token_expires_at
        now = datetime.now()
        return {
            "mode": self.mode,
            "has_token": bool(self._token),
            "expires_at": expires_at.isoformat() if expires_at and expires_at != datetime.min else None,
            "is_expired": bool(self._token) and now >= expires_at,
            "token_cache_path_exists": self._token_cache_path().exists(),
        }

    # ── 일별 주가 조회 ────────────────────────────────────────────────────

    def get_daily_prices(self, symbol: str, days: int = 65) -> list[dict]:
        url = f"{self.base_url}/uapi/domestic-stock/v1/quotations/inquire-daily-price"
        headers = self._auth_headers(TR_DAILY_PRICE)
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": symbol,
            "FID_PERIOD_DIV_CODE": "D",
            "FID_ORG_ADJ_PRC": "0",
        }
        try:
            resp = self._session.get(url, headers=headers, params=params, timeout=(3, 10))
            resp.raise_for_status()
            output = resp.json().get("output", [])
            result = []
            for row in output[:days]:
                close = float(row.get("stck_clpr", 0) or 0)
                if close <= 0:
                    continue
                result.append({
                    "date": row.get("stck_bsop_date", ""),
                    "close": close,
                    "open": float(row.get("stck_oprc", 0) or 0),
                    "high": float(row.get("stck_hgpr", 0) or 0),
                    "low": float(row.get("stck_lwpr", 0) or 0),
                    "volume": int(row.get("acml_vol", 0) or 0),
                })
            return result
        except Exception as e:
            logger.warning(f"[KIS] 일별주가 조회 실패 {symbol}: {e}")
            return []

    # ── 분봉 조회 ──────────────────────────────────────────────────────────

    def get_minute_candles(self, symbol: str, period_min: int = 1, count: int = 60) -> list[dict]:
        """국내주식 분봉 조회. 최신 순으로 count개 반환. 실패 시 [] 반환."""
        tr_id = "FHKST03010200"
        url = f"{self.base_url}/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice"
        headers = self._auth_headers(tr_id)
        params = {
            "FID_ETC_CLS_CODE": "",
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": symbol,
            "FID_INPUT_HOUR_1": "",
            "FID_PW_DATA_INCU_YN": "N",
        }
        try:
            resp = self._session.get(url, headers=headers, params=params, timeout=(3, 10))
            resp.raise_for_status()
            output = resp.json().get("output2", [])
            result = []
            for row in output[:count]:
                close = float(row.get("stck_prpr", 0) or 0)
                if close <= 0:
                    continue
                result.append({
                    "time": row.get("stck_cntg_hour", ""),
                    "open": float(row.get("stck_oprc", 0) or 0),
                    "high": float(row.get("stck_hgpr", 0) or 0),
                    "low": float(row.get("stck_lwpr", 0) or 0),
                    "close": close,
                    "volume": int(row.get("cntg_vol", 0) or 0),
                })
            return result
        except Exception as e:
            logger.warning(f"[KIS] 분봉 조회 실패 {symbol}: {e}")
            return []

    # ── 외국인/기관 매매동향 조회 ──────────────────────────────────────────

    def get_investor_trend(self, symbol: str) -> list[dict]:
        """종목별 외국인/기관 순매수 동향 조회. 최신순으로 반환. 실패 시 [] 반환.

        TR FHKST01010900 (KIS 공식 문서: 국내주식 종목별 투자자매매동향).
        """
        tr_id = TR_INVESTOR_TREND
        url = f"{self.base_url}/uapi/domestic-stock/v1/quotations/inquire-investor"
        headers = self._auth_headers(tr_id)
        params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol}
        try:
            resp = self._session.get(url, headers=headers, params=params, timeout=(3, 10))
            resp.raise_for_status()
            output = resp.json().get("output", [])
            result = []
            for row in output:
                result.append({
                    "date": row.get("stck_bsop_date", ""),
                    "foreign_net_buy": int(row.get("frgn_ntby_qty", 0) or 0),
                    "institution_net_buy": int(row.get("orgn_ntby_qty", 0) or 0),
                })
            return result
        except Exception as e:
            logger.warning(f"[KIS] 투자자매매동향 조회 실패 {symbol}: {e}")
            return []

    # ── 매수 주문 ──────────────────────────────────────────────────────────

    def buy(
        self,
        symbol: str,
        quantity: int,
        price: int,
        order_type: str = "limit",
    ) -> dict:
        tr_id = TR_BUY_MOCK if self.mode == "mock" else TR_BUY_REAL
        ord_dvsn = ORD_DVSN_MARKET if order_type == "market" else ORD_DVSN_LIMIT
        body = {
            "CANO": self.account_no,
            "ACNT_PRDT_CD": self.product_code,
            "PDNO": symbol,
            "ORD_DVSN": ord_dvsn,
            "ORD_QTY": str(quantity),
            "ORD_UNPR": "0" if order_type == "market" else str(price),
        }
        return self._place_order(tr_id, body, "buy", symbol, quantity, price)

    # ── 매도 주문 ──────────────────────────────────────────────────────────

    def sell(
        self,
        symbol: str,
        quantity: int,
        price: int,
        order_type: str = "limit",
    ) -> dict:
        tr_id = TR_SELL_MOCK if self.mode == "mock" else TR_SELL_REAL
        ord_dvsn = ORD_DVSN_MARKET if order_type == "market" else ORD_DVSN_LIMIT
        body = {
            "CANO": self.account_no,
            "ACNT_PRDT_CD": self.product_code,
            "PDNO": symbol,
            "ORD_DVSN": ord_dvsn,
            "ORD_QTY": str(quantity),
            "ORD_UNPR": "0" if order_type == "market" else str(price),
        }
        return self._place_order(tr_id, body, "sell", symbol, quantity, price)

    # ── 내부 공통 주문 처리 ────────────────────────────────────────────────

    def _place_order(
        self,
        tr_id: str,
        body: dict,
        side: str,
        symbol: str,
        quantity: int,
        price: int,
    ) -> dict:
        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/order-cash"
        hashkey = self.get_hashkey(body)
        headers = self._auth_headers(tr_id)
        if hashkey:
            headers["hashkey"] = hashkey

        logger.info(
            f"[KIS-{self.mode.upper()}] 주문 시도: side={side} symbol={symbol} "
            f"qty={quantity} price={price:,} tr_id={tr_id}"
        )

        try:
            resp = self._session.post(url, json=body, headers=headers, timeout=(3, 15))
            http_status = resp.status_code

            # raise_for_status 호출 전에 body 파싱 (500 오류 원인 확인)
            try:
                data = resp.json()
            except Exception:
                data = {}

            rt_cd = data.get("rt_cd", "")
            msg_cd = data.get("msg_cd", "")
            msg1 = data.get("msg1", "")

            if not resp.ok:
                logger.error(
                    f"[KIS-{self.mode.upper()}] order-cash HTTP {http_status}: "
                    f"rt_cd={rt_cd!r} msg_cd={msg_cd!r} msg1={msg1!r}"
                )
                return {
                    "success": False,
                    "order_id": "",
                    "message": f"HTTP {http_status}: rt_cd={rt_cd} msg_cd={msg_cd} msg1={msg1}",
                    "raw": data,
                    "http_status": http_status,
                    "rt_cd": rt_cd, "msg_cd": msg_cd, "msg1": msg1,
                }

            output = data.get("output", {})
            order_id = output.get("ODNO", "")

            if rt_cd == "0":
                logger.info(f"[KIS-{self.mode.upper()}] 주문 성공: order_id={order_id}")
                return {
                    "success": True,
                    "order_id": order_id,
                    "message": msg1,
                    "raw": output,
                    "http_status": http_status,
                    "rt_cd": rt_cd, "msg_cd": msg_cd, "msg1": msg1,
                }
            else:
                logger.warning(
                    f"[KIS-{self.mode.upper()}] 주문 실패: "
                    f"rt_cd={rt_cd} msg_cd={msg_cd} msg={msg1}"
                )
                return {
                    "success": False,
                    "order_id": "",
                    "message": msg1,
                    "raw": data,
                    "http_status": http_status,
                    "rt_cd": rt_cd, "msg_cd": msg_cd, "msg1": msg1,
                }
        except KISTokenError:
            raise
        except Exception as e:
            logger.error(f"[KIS-{self.mode.upper()}] 주문 예외: {e}")
            return {
                "success": False,
                "order_id": "",
                "message": str(e),
                "raw": {},
                "http_status": 0,
                "rt_cd": "", "msg_cd": "EXCEPTION", "msg1": str(e),
            }


def verify_symbol(client: "KISClient", symbol: str, expected_name_substr: str = "") -> dict:
    """KIS 현재가 조회 + 종목명 조회로 종목코드를 검증한다.

    영문/숫자 혼용 코드(예: 0197X0)를 isdigit()/6자리 등으로 걸러내지 않는다 —
    symbol을 그대로 PDNO/FID_INPUT_ISCD에 전달해 KIS가 실제로 인식하는지만 확인한다.
    반환: {"symbol", "verified", "current_price", "name", "name_matched", "error"}.
    """
    try:
        quote = client.get_current_price(symbol)
    except Exception as exc:
        return {"symbol": symbol, "verified": False, "current_price": None, "name": "", "name_matched": False, "error": str(exc)}

    if not quote or not quote.get("current_price"):
        return {"symbol": symbol, "verified": False, "current_price": None, "name": "", "name_matched": False,
                "error": "현재가 조회 실패 또는 0원 — 종목코드를 확인하세요"}

    name = quote.get("name", "")
    name_matched = bool(expected_name_substr) and expected_name_substr in name
    return {
        "symbol": symbol,
        "verified": True,
        "current_price": quote.get("current_price"),
        "name": name,
        "name_matched": name_matched if expected_name_substr else None,
        "error": None,
    }


_CLIENT_CACHE_GENERATION = 0


def clear_kis_client_cache() -> None:
    """Clear in-memory KIS client runtime cache markers.

    create_kis_client currently returns a fresh client each call; the generation
    counter exists so reload paths and tests can verify cache invalidation
    without exposing credentials.
    """
    global _CLIENT_CACHE_GENERATION
    _CLIENT_CACHE_GENERATION += 1


def create_kis_client(mode: str = "mock") -> "KISClient | None":
    """
    환경변수에서 인증 정보를 읽어 KISClient를 생성합니다.
    환경변수가 없으면 None을 반환합니다 (dry_run fallback용).
    """
    from app.config import get_kis_account_config
    try:
        account_cfg = get_kis_account_config(mode)
        if account_cfg.get("account_conflict"):
            vars_text = ", ".join(account_cfg.get("account_conflict_vars", []))
            logger.warning("[KIS] 클라이언트 초기화 실패 (%s): 계좌 환경변수 충돌: %s", mode, vars_text)
            return None
        client = KISClient.from_account_config(account_cfg)
        logger.info("[KIS] %s 클라이언트 초기화 완료 fp=%s", mode, account_cfg.get("account_fingerprint", ""))
        return client
    except ValueError as e:
        logger.warning(f"[KIS] 클라이언트 초기화 실패 ({mode}): {e}")
        return None
