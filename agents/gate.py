"""
최종 관문: 에이전트 합의 + 거부권 규칙
- 호출 실패(decision='오류')가 하나라도 있으면 판단 불가로 반려
- 거부권: market_state 또는 risk가 '관망'/'청산' 이면 전체 반려
- 합의: 나머지 에이전트가 전부 '매수' 이어야 통과
전략마다 합의 에이전트가 다르다 (역발상: 가치 함정, 펀더멘털: 공시/재무).
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agents import value_trap, market_state, risk
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

    # float()로 못박는다. score는 DB에서 오면 Decimal, API에서 오면 float이라
    # 그냥 더하면 TypeError가 난다 — 그것도 승인될 때만 계산되는 줄이라
    # 통과하는 순간에만 터진다.
    avg_score = sum(float(r["score"]) for r in results.values()) / len(results)
    return {
        "approved": True,
        "agents": results,
        "reason": f"전원 합의 통과 (평균 확신 {avg_score:.1f})",
    }


def decide(code: str, target_date: str, strategy: str) -> dict:
    """
    역발상 전략 게이트: 가치 함정 판별 합의.

    retail_flow / credit_heat를 뺐다. 둘의 입력(individual_flow_ratio,
    credit_surge_ratio, volume_ratio)은 전부 heat_score의 구성 요소이고, 후보는
    이미 heat_score < HEAT_AVOID로 걸러 정렬된 상태로 들어온다. 과열되지 않도록
    미리 고른 종목에게 과열 여부를 되묻는 구조라 2026-08-19 기록에서 100/100
    전건 '매수'였다 — 크레딧만 쓰고 결정은 하나도 바꾸지 않았다.
    숫자 판정은 룰(필터)이 하고, LLM에는 룰로 표현할 수 없는 공시 텍스트만 준다.
    """
    return _gate({
        "value_trap": value_trap.analyze(code, target_date),
        "market_state": market_state.analyze(code, target_date, strategy),
        "risk": risk.analyze(code, target_date, strategy),
    }, consensus=("value_trap",))


def decide_fundamental(code: str, target_date: str, strategy: str) -> dict:
    """펀더멘털 전략 게이트: 공시 + 재무 합의."""
    return _gate({
        "disclosure": disclosure.analyze(code, target_date),
        "financials": financials.analyze(code, target_date),
        "market_state": market_state.analyze(code, target_date, strategy),
        "risk": risk.analyze(code, target_date, strategy),
    }, consensus=("disclosure", "financials"))
