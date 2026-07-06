"""
intraday_budget_allocator.py — Top3 종목 중요도별 예산 차등 배분
"""
import math


class IntradayBudgetAllocator:
    """Top3 종목 예산 차등 배분.

    기본 비중: [0.5, 0.3, 0.2]
    최종 비중 = 기본비중 * 0.7 + score_weight * 0.3
    제한: 종목당 최소 15%, 최대 60%
    """

    BASE_WEIGHTS = [0.5, 0.3, 0.2]

    def allocate(self, top3: list[dict], total_budget: float) -> list[dict]:
        """top3 각 항목에 allocated_budget / allocated_weight / allocated_quantity 추가."""
        if not top3:
            return []

        n = len(top3)
        base = self.BASE_WEIGHTS[:n]
        # n < 3이면 균등 보완
        while len(base) < n:
            base.append(1.0 / n)

        # score_weight 계산
        scores = [float(s.get("final_score", 0) or 0) for s in top3]
        total_score = sum(scores)
        if total_score > 0:
            score_weights = [sc / total_score for sc in scores]
        else:
            score_weights = [1.0 / n] * n

        # 블렌딩
        blended = [b * 0.7 + sw * 0.3 for b, sw in zip(base, score_weights)]

        # 정규화
        total = sum(blended)
        blended = [w / total for w in blended]

        # 클램프 [0.15, 0.60]
        clamped = [max(0.15, min(0.60, w)) for w in blended]

        # 재정규화
        total2 = sum(clamped)
        final_weights = [w / total2 for w in clamped]

        result = []
        for stock, weight in zip(top3, final_weights):
            alloc = dict(stock)
            alloc["allocated_weight"] = round(weight, 4)
            alloc["allocated_budget"] = round(weight * total_budget, 0)
            price = float(alloc.get("current_price", 0) or 0)
            if price > 0:
                alloc["allocated_quantity"] = int(alloc["allocated_budget"] / price)
            else:
                alloc["allocated_quantity"] = 0
            result.append(alloc)

        return result
