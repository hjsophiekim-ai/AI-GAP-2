"""
top15_selector.py

후보 리스트에서 최종 Top N을 선정합니다.

선정 원칙:
  - 하드 스킵 없음: open_to_current_rate < -1.5% 등은 제외가 아닌 위험 태깅
  - 최소 9개 보장: 일반 후보가 부족하면 위험 종목까지 포함해 9개 확보
  - 위험 종목은 risk_comment에 사유 기록 → UI에서 빨간 점으로 표시
"""
from __future__ import annotations

import pandas as pd
from pathlib import Path
from datetime import datetime

from app.models import Candidate
from app.config import get_config
from app.logger import logger
from app.strategy.candidate_quality_filter import CandidateQualityFilter

# 위험 판단 임계값
_TV_RISKY_THRESHOLD    = 3_000_000_000   # 30억 미만 → 거래대금 위험
_GAP_CAUTION_THRESHOLD = 15.0            # 15% 초과 → 갭 과대
_OTC_RISKY_THRESHOLD   = -1.5            # open_to_current < -1.5% → 시가 하회


class Top15Selector:
    def __init__(self, cfg=None):
        self.cfg = cfg or get_config()

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def select(self, candidates: list[Candidate]) -> list[Candidate]:
        """
        최종 종목 선정.

        1. final_score 내림차순 정렬
        2. 위험 태깅 (하드 제외 없음)
        3. 섹터 다양화 (max 3/섹터)
        4. 9개 미달 시 위험 종목으로 강제 보충
        5. max_positions 상한 적용
        """
        try:
            qcfg = self.cfg._raw.get("candidate_quality_filters", {})
        except AttributeError:
            qcfg = {}
        max_positions = self.cfg.trading.get("max_positions", 15)
        min_guaranteed = qcfg.get("target_min_candidates", 9)

        if not candidates:
            logger.warning("[Top15] 후보가 없습니다.")
            return []

        # Step 1: 정렬
        sorted_cands = sorted(candidates, key=lambda c: c.final_score, reverse=True)

        # Step 2: 위험 태깅 (기존 risk_comment에 추가)
        for c in sorted_cands:
            _tag_risk(c)

        # Step 3: 일반 / 갭과대 / 시가하회 분리 (모두 후보군 유지)
        normal:    list[Candidate] = []
        gap_high:  list[Candidate] = []   # gap > 15% but otc >= -1.5
        otc_drop:  list[Candidate] = []   # otc < -1.5  (가장 위험)

        for c in sorted_cands:
            if c.open_to_current_rate < _OTC_RISKY_THRESHOLD:
                otc_drop.append(c)
            elif c.gap_rate > _GAP_CAUTION_THRESHOLD:
                gap_high.append(c)
            else:
                normal.append(c)

        # Step 4: 섹터 다양화 (normal → gap_high 순으로 채우기)
        selected: list[Candidate] = []
        sector_counts: dict[str, int] = {}
        overflow_buf: list[Candidate] = []   # 섹터 cap 초과분

        for c in (normal + gap_high):
            sector = _sector_key(c)
            cnt = sector_counts.get(sector, 0)
            if cnt < 3 and len(selected) < max_positions:
                selected.append(c)
                sector_counts[sector] = cnt + 1
            else:
                overflow_buf.append(c)

        # Step 5: min_guaranteed 미달 → overflow → otc_drop 순으로 보충
        _fill_to_min(selected, overflow_buf + otc_drop, min_guaranteed, max_positions)

        # Step 6: max_positions 상한
        result = selected[:max_positions]

        # Step 7: 랭크 재할당
        for i, c in enumerate(result, 1):
            c.rank = i

        n_risky = sum(1 for c in result if c.risk_comment)
        logger.info(
            "[Top15] 선정 완료: %d개 (위험 %d개, 목표최소 %d개)",
            len(result), n_risky, min_guaranteed,
        )
        return result

    def select_from_csv(self, filepath: str) -> list[Candidate]:
        path = Path(filepath)
        if not path.exists():
            logger.warning(f"[Top15] CSV 파일 없음: {filepath}")
            return []
        df = pd.read_csv(path, dtype={"symbol": str})
        candidates = self._df_to_candidates(df)
        return self.select(candidates)

    def save_explain(
        self,
        candidates: list[Candidate],
        excluded: list[dict] = None,
        date_str: str = None,
        time_str: str = None,
    ) -> tuple[str, str]:
        qf = CandidateQualityFilter(cfg=self.cfg)
        return qf.save_explain_csv(
            candidates,
            excluded=excluded or [],
            date_str=date_str,
            time_str=time_str,
        )

    def save_top15(self, candidates: list[Candidate], date_str: str = None) -> str:
        if date_str is None:
            date_str = datetime.now().strftime("%Y%m%d")

        save_dir = Path(__file__).parent.parent.parent / "data" / "selected"
        save_dir.mkdir(parents=True, exist_ok=True)
        filepath = save_dir / f"{date_str}_top15.csv"

        columns = [
            "rank", "symbol", "name", "current_price", "open", "high", "low",
            "previous_close", "gap_rate", "open_to_current_rate", "trade_value",
            "ml_score", "rule_score", "final_score",
            "selected_reason", "risk_comment", "penalty_reason", "warning_reason",
            "fallback_included", "relaxed_mode_applied",
        ]

        rows = []
        for c in candidates:
            rows.append({
                "rank":                c.rank,
                "symbol":              c.symbol,
                "name":                c.name,
                "current_price":       c.current_price,
                "open":                c.open,
                "high":                c.high,
                "low":                 c.low,
                "previous_close":      c.previous_close,
                "gap_rate":            c.gap_rate,
                "open_to_current_rate": c.open_to_current_rate,
                "trade_value":         c.trade_value,
                "ml_score":            c.ml_score,
                "rule_score":          c.rule_score,
                "final_score":         c.final_score,
                "selected_reason":     c.selected_reason,
                "risk_comment":        c.risk_comment,
                "penalty_reason":      getattr(c, "penalty_reason", ""),
                "warning_reason":      getattr(c, "warning_reason", ""),
                "fallback_included":   getattr(c, "fallback_included", False),
                "relaxed_mode_applied": getattr(c, "relaxed_mode_applied", False),
            })

        df = pd.DataFrame(rows, columns=columns)
        df.to_csv(filepath, index=False, encoding="utf-8-sig")
        logger.info(f"[Top15] 저장 완료: {filepath}")
        return str(filepath)

    def load_top15(self, date_str: str = None) -> list[Candidate]:
        if date_str is None:
            date_str = datetime.now().strftime("%Y%m%d")

        filepath = (
            Path(__file__).parent.parent.parent
            / "data" / "selected"
            / f"{date_str}_top15.csv"
        )
        if not filepath.exists():
            logger.warning(f"[Top15] 파일 없음: {filepath}")
            return []

        df = pd.read_csv(filepath, dtype={"symbol": str})
        return self._df_to_candidates(df)

    # ------------------------------------------------------------------ #
    # Internal helpers                                                     #
    # ------------------------------------------------------------------ #

    def _df_to_candidates(self, df: pd.DataFrame) -> list[Candidate]:
        candidates = []
        for i, row in df.iterrows():
            try:
                c = Candidate(
                    rank=int(row.get("rank", i + 1)),
                    symbol=str(row.get("symbol", "")),
                    name=str(row.get("name", "")),
                    current_price=float(row.get("current_price", 0)),
                    open=float(row.get("open", 0)),
                    high=float(row.get("high", 0)),
                    low=float(row.get("low", 0)),
                    previous_close=float(row.get("previous_close", 0)),
                    gap_rate=float(row.get("gap_rate", 0)),
                    open_to_current_rate=float(row.get("open_to_current_rate", 0)),
                    trade_value=float(row.get("trade_value", 0)),
                    ml_score=float(row.get("ml_score", 0)),
                    rule_score=float(row.get("rule_score", 0)),
                    final_score=float(row.get("final_score", 0)),
                    selected_reason=str(row.get("selected_reason", "")),
                    risk_comment=str(row.get("risk_comment", "") or ""),
                    exclude_reason=str(row.get("exclude_reason", "")),
                    penalty_reason=str(row.get("penalty_reason", "") or ""),
                    warning_reason=str(row.get("warning_reason", "") or ""),
                    fallback_included=bool(row.get("fallback_included", False)),
                    relaxed_mode_applied=bool(row.get("relaxed_mode_applied", False)),
                )
                candidates.append(c)
            except Exception as e:
                logger.warning(f"[Top15] 행 변환 오류 (row={i}): {e}")
        return candidates


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _tag_risk(c: Candidate) -> None:
    """위험 요인을 risk_comment에 추가 (하드 제외 없음)."""
    tags: list[str] = []

    if c.open_to_current_rate < _OTC_RISKY_THRESHOLD:
        tags.append(f"시가하회 {c.open_to_current_rate:.1f}%")

    if c.gap_rate > _GAP_CAUTION_THRESHOLD:
        tags.append(f"갭과대 {c.gap_rate:.1f}%")

    if 0 < c.trade_value < _TV_RISKY_THRESHOLD:
        tags.append(f"거래대금 {c.trade_value / 1e8:.0f}억")

    if c.open_to_current_rate < -3.5:
        tags.append("급락주의")

    if tags:
        new_risk = ", ".join(tags)
        existing = (c.risk_comment or "").strip()
        c.risk_comment = f"{existing}, {new_risk}".strip(", ") if existing else new_risk


def _sector_key(c: Candidate) -> str:
    """섹터 키. sector 없으면 종목마다 고유키로 처리해 cap 회피."""
    sector = getattr(c, "sector", "") or ""
    if not sector.strip():
        sector = f"__unique_{c.symbol}__"
    return sector


def _fill_to_min(
    selected: list[Candidate],
    pool: list[Candidate],
    min_n: int,
    max_n: int,
) -> None:
    """selected에서 min_n개 미달 시 pool에서 보충 (in-place)."""
    if len(selected) >= min_n:
        return
    included = {c.symbol for c in selected}
    for c in pool:
        if len(selected) >= min_n:
            break
        if c.symbol not in included and len(selected) < max_n:
            selected.append(c)
            included.add(c.symbol)
            logger.debug("[Top15] 보충 포함: %s %s (risk=%s)", c.symbol, c.name, c.risk_comment)
