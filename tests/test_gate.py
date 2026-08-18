"""agents/gate.py - veto + 2/2 합의 로직. 4개 에이전트의 analyze()를 mock으로 대체."""
import pytest

from agents import gate


def _patch_agents(mocker, *, market_state, risk, retail_flow, credit_heat):
    mocker.patch("agents.market_state.analyze", return_value=market_state)
    mocker.patch("agents.risk.analyze", return_value=risk)
    mocker.patch("agents.retail_flow.analyze", return_value=retail_flow)
    mocker.patch("agents.credit_heat.analyze", return_value=credit_heat)


def _decision(d, score=8.0, rationale="테스트"):
    return {"decision": d, "score": score, "rationale": rationale}


def test_all_agree_buy_is_approved(mocker):
    _patch_agents(
        mocker,
        market_state=_decision("매수"),
        risk=_decision("매수"),
        retail_flow=_decision("매수"),
        credit_heat=_decision("매수"),
    )
    result = gate.decide("005930", "2024-01-15", "contrarian_v1")
    assert result["approved"] is True


def test_market_state_veto_rejects_regardless_of_others(mocker):
    _patch_agents(
        mocker,
        market_state=_decision("청산"),
        risk=_decision("매수"),
        retail_flow=_decision("매수"),
        credit_heat=_decision("매수"),
    )
    result = gate.decide("005930", "2024-01-15", "contrarian_v1")
    assert result["approved"] is False
    assert "market_state" in result["reason"]


def test_risk_veto_rejects_regardless_of_others(mocker):
    _patch_agents(
        mocker,
        market_state=_decision("매수"),
        risk=_decision("관망"),
        retail_flow=_decision("매수"),
        credit_heat=_decision("매수"),
    )
    result = gate.decide("005930", "2024-01-15", "contrarian_v1")
    assert result["approved"] is False
    assert "risk" in result["reason"]


def test_only_one_of_two_consensus_votes_is_rejected(mocker):
    _patch_agents(
        mocker,
        market_state=_decision("매수"),
        risk=_decision("매수"),
        retail_flow=_decision("매수"),
        credit_heat=_decision("관망"),  # 1/2 표만 매수
    )
    result = gate.decide("005930", "2024-01-15", "contrarian_v1")
    assert result["approved"] is False
    assert "합의" in result["reason"]


def test_veto_checked_before_consensus(mocker):
    """거부권 에이전트가 관망이면, 합의 투표가 2/2로 만족되어도 반려되어야 함."""
    _patch_agents(
        mocker,
        market_state=_decision("관망"),
        risk=_decision("매수"),
        retail_flow=_decision("매수"),
        credit_heat=_decision("매수"),
    )
    result = gate.decide("005930", "2024-01-15", "contrarian_v1")
    assert result["approved"] is False
    assert "market_state" in result["reason"]


def test_approved_result_includes_average_score(mocker):
    _patch_agents(
        mocker,
        market_state=_decision("매수", score=10.0),
        risk=_decision("매수", score=8.0),
        retail_flow=_decision("매수", score=6.0),
        credit_heat=_decision("매수", score=4.0),
    )
    result = gate.decide("005930", "2024-01-15", "contrarian_v1")
    assert result["approved"] is True
    assert "7.0" in result["reason"]  # (10+8+6+4)/4 = 7.0
