"""
최종 관문: 4개 에이전트 합의 + 거부권 규칙
- 거부권: market_state 또는 risk가 '관망'/'청산' 이면 전체 반려
- 합의: 나머지 에이전트 중 2개 이상 '매수' 이어야 통과
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agents import retail_flow, credit_heat, market_state, risk


def decide(code: str, target_date: str, strategy: str) -> dict:
    """
    반환: {
      "approved": bool,
      "agents": {에이전트명: {decision, score, rationale}},
      "reason": str
    }
    """
    results = {
        "retail_flow": retail_flow.analyze(code, target_date),
        "credit_heat": credit_heat.analyze(code, target_date),
        "market_state": market_state.analyze(code, target_date),
        "risk": risk.analyze(code, target_date, strategy),
    }

    # 거부권 에이전트 체크
    for veto_agent in ("market_state", "risk"):
        d = results[veto_agent]["decision"]
        if d in ("관망", "청산"):
            return {
                "approved": False,
                "agents": results,
                "reason": f"거부권: {veto_agent} → {d} ({results[veto_agent]['rationale']})",
            }

    # 나머지 에이전트 합의 (2/2 이상 매수)
    buy_votes = sum(
        1 for k in ("retail_flow", "credit_heat")
        if results[k]["decision"] == "매수"
    )
    if buy_votes < 2:
        return {
            "approved": False,
            "agents": results,
            "reason": f"합의 미달: 매수 {buy_votes}/2",
        }

    avg_score = sum(r["score"] for r in results.values()) / len(results)
    return {
        "approved": True,
        "agents": results,
        "reason": f"전원 합의 통과 (평균 확신 {avg_score:.1f})",
    }
