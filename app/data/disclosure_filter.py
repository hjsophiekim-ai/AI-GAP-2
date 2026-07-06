"""
DisclosureFilter - DART 공시 제목을 키워드 기반으로 분류하여 점수를 산출합니다.

긍정 공시: +5점 (강한 긍정: +10점)
부정 공시: -10점 (강한 리스크: -20점)
최종 disclosure_score: -20 ~ +10 범위로 제한
공시 없음 / DART 오류: 0점, 로직 계속 진행
"""

from app.logger import logger

# ── 강한 긍정 키워드 (+10점) ─────────────────────────────────────────────
STRONG_POSITIVE_KEYWORDS = [
    "단일판매",
    "공급계약",
    "수주",
    "신규시설투자",
    "시설투자",
    "품목허가",
    "투자유치",
    "자금조달",
]

# ── 일반 긍정 키워드 (+5점) ──────────────────────────────────────────────
POSITIVE_KEYWORDS = [
    "자기주식취득",
    "무상증자",
    "특허권취득",
    "기술이전",
    "라이선스",
    "수주",
    "계약체결",
    "자사주매입",
    "주식배당",
    "배당",
    "특허",
    "신약",
    "임상",
    "인수합병",
    "지분취득",
]

# ── 강한 리스크 키워드 (-20점, severe_disclosure_risk=True) ──────────────
SEVERE_RISK_KEYWORDS = [
    "상장폐지",
    "관리종목",
    "거래정지",
    "불성실공시",
    "횡령",
    "배임",
    "영업정지",
    "회생절차",
    "파산",
    "감사의견",
    "의견거절",
    "계속기업",
]

# ── 일반 부정/리스크 키워드 (-10점) ─────────────────────────────────────
NEGATIVE_KEYWORDS = [
    "소송",
    "유상증자",
    "전환사채",
    "신주인수권부사채",
    "최대주주변경",
    "담보제공",
    "채무보증",
    "감자",
    "투자주의",
    "투자경고",
    "투자위험",
    "당기순손실",
    "분식회계",
    "수사",
]


class DisclosureFilter:
    def __init__(self, cfg=None):
        from app.config import get_config
        self.cfg = cfg or get_config()
        dart_cfg = self.cfg.dart
        self.enabled = dart_cfg.get("enabled", True)
        self.max_positive = int(dart_cfg.get("max_positive_bonus", 10))
        self.max_negative = int(dart_cfg.get("max_negative_penalty", -20))
        self.exclude_severe = dart_cfg.get("exclude_severe_risk_disclosure", True)

    def score_disclosures(self, disclosures: list[dict]) -> dict:
        """
        공시 목록을 분석해 점수와 요약을 반환합니다.

        Returns
        -------
        dict:
          disclosure_score : float  (-20 ~ +10)
          has_severe_risk  : bool
          positive_count   : int
          negative_count   : int
          severe_count     : int
          summary          : str
          matched_keywords : list[str]
        """
        if not disclosures:
            return self._empty_result()

        score = 0
        has_severe = False
        matched = []
        positive_count = 0
        negative_count = 0
        severe_count = 0

        for item in disclosures:
            title = item.get("report_nm", "") or ""

            # 강한 리스크 우선 확인 (-20점)
            for kw in SEVERE_RISK_KEYWORDS:
                if kw in title:
                    has_severe = True
                    score -= 20
                    severe_count += 1
                    matched.append(f"[심각리스크:{kw}]")
                    break
            else:
                # 일반 부정 (-10점)
                for kw in NEGATIVE_KEYWORDS:
                    if kw in title:
                        score -= 10
                        negative_count += 1
                        matched.append(f"[부정:{kw}]")
                        break
                else:
                    # 강한 긍정 (+10점)
                    for kw in STRONG_POSITIVE_KEYWORDS:
                        if kw in title:
                            score += 10
                            positive_count += 1
                            matched.append(f"[강호재:{kw}]")
                            break
                    else:
                        # 일반 긍정 (+5점)
                        for kw in POSITIVE_KEYWORDS:
                            if kw in title:
                                score += 5
                                positive_count += 1
                                matched.append(f"[호재:{kw}]")
                                break

        # 범위 제한: -20 ~ +10
        score = max(self.max_negative, min(self.max_positive, score))

        summary_parts = []
        if has_severe:
            summary_parts.append("⚠️ 심각한 공시")
        if positive_count > 0:
            summary_parts.append(f"호재{positive_count}건")
        if negative_count > 0:
            summary_parts.append(f"악재{negative_count}건")
        summary = " | ".join(summary_parts) if summary_parts else "공시 무해"

        return {
            "disclosure_score": float(score),
            "has_severe_risk": has_severe,
            "positive_count": positive_count,
            "negative_count": negative_count,
            "severe_count": severe_count,
            "summary": summary,
            "matched_keywords": matched[:5],
        }

    def score_all(
        self,
        symbol_disclosures: dict[str, list[dict]],
    ) -> dict[str, dict]:
        """종목별 공시 딕셔너리를 받아 종목별 점수 딕셔너리를 반환합니다."""
        result = {}
        for symbol, disclosures in symbol_disclosures.items():
            try:
                result[symbol] = self.score_disclosures(disclosures)
            except Exception as e:
                logger.warning("[DART] 점수 계산 오류 %s: %s", symbol, e)
                result[symbol] = self._empty_result()
        return result

    def should_exclude(self, score_dict: dict) -> bool:
        """심각한 위험 공시가 있으면 True 반환."""
        if not self.exclude_severe:
            return False
        return score_dict.get("has_severe_risk", False)

    @staticmethod
    def _empty_result() -> dict:
        return {
            "disclosure_score": 0.0,
            "has_severe_risk": False,
            "positive_count": 0,
            "negative_count": 0,
            "severe_count": 0,
            "summary": "공시 없음",
            "matched_keywords": [],
        }
