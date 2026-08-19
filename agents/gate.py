"""
최종 관문: 에이전트 합의 + 거부권 규칙
- 호출 실패(decision='오류')가 하나라도 있으면 판단 불가로 반려
- 거부권: market_state 또는 risk가 '관망'/'청산' 이면 전체 반려
- 합의: 나머지 에이전트가 전부 '매수' 이어야 통과
전략마다 합의 에이전트가 다르다 (역발상: 수급/신용, 펀더멘털: 공시/재무).
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agents import retail_flow, credit_heat, market_state, risk
from agents import disclosure, financials

VETO_AGENTS = ("market_state", "risk")
VETO_DECISIONS = ("관망", "청산")


def _gate(results: dict, consensus: tuple) -> dict:
    """
    반환: {
      "approved": bool,
      "agents": {에이전트명: {decision, score, rationale}},
      "reason": str
    }
    """
    # 호출 실패를 먼저 걸러낸다. 실패는 '관망'이 아니라 '판단 없음'이다 —
    # 거부권 사유로 섞이면 크레딧이 떨어진 날도 시장 판단으로 반려된 것처럼 보인다.
    failed = {n: r["error"] for n, r in results.items() if r.get("error")}
    if failed:
        return {
            "approved": False,
            "agents": results,
            "reason": "판단 불가(에이전트 오류) - "
                      + ", ".join(f"{n}: {label}" for n, label in failed.items()),
        }

    for name in VETO_AGENTS:
        decision = results[name]["decision"]
        if decision in VETO_DECISIONS:
            return {
                "approved": False,
                "agents": results,
                "reason": f"거부권: {name} → {decision} ({results[name]['rationale']})",
            }

    buy_votes = sum(1 for k in consensus if results[k]["decision"] == "매수")
    if buy_votes < len(consensus):
        return {
            "approved": False,
            "agents": results,
            "reason": f"합의 미달: 매수 {buy_votes}/{len(consensus)}",
        }

    avg_score = sum(r["score"] for r in results.values()) / len(results)
    return {
        "approved": True,
        "agents": results,
        "reason": f"전원 합의 통과 (평균 확신 {avg_score:.1f})",
    }


def decide(code: str, target_date: str, strategy: str) -> dict:
    """역발상 전략 게이트: 개인 수급 + 신용 과열 합의."""
    return _gate({
        "retail_flow": retail_flow.analyze(code, target_date),
        "credit_heat": credit_heat.analyze(code, target_date),
        "market_state": market_state.analyze(code, target_date, strategy),
        "risk": risk.analyze(code, target_date, strategy),
    }, consensus=("retail_flow", "credit_heat"))


def decide_fundamental(code: str, target_date: str, strategy: str) -> dict:
    """펀더멘털 전략 게이트: 공시 + 재무 합의."""
    return _gate({
        "disclosure": disclosure.analyze(code, target_date),
        "financials": financials.analyze(code, target_date),
        "market_state": market_state.analyze(code, target_date, strategy),
        "risk": risk.analyze(code, target_date, strategy),
    }, consensus=("disclosure", "financials"))
