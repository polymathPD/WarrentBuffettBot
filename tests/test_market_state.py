"""agents/market_state.py - 전략별 시장 판단 규칙."""
import pytest

from agents import market_state


def _prompt(mocker, strategy, code="005930", d="2026-08-18"):
    """analyze()가 실제로 만들어 보내는 프롬프트를 가로챈다."""
    spy = mocker.patch("agents.market_state.call",
                       return_value={"decision": "매수", "score": 8.0, "rationale": "x"})
    market_state.analyze(code, d, strategy)
    return spy.call_args


def test_contrarian_requires_broad_decline_to_buy(mock_db, mocker):
    """역발상은 공포에 산다 — 하락이 좁으면 진입하지 않는다."""
    args, kwargs = _prompt(mocker, "contrarian_v1")
    prompt = args[2]
    assert "하락 종목 비율 50% 미만이면 반드시 '관망'" in prompt
    assert "70% 이상이면 반드시 '청산'" not in prompt


def test_fundamental_avoids_broad_decline(mock_db, mocker):
    """펀더멘털은 개별 실적으로 산다 — 시장 급락은 회피 조건이다."""
    args, kwargs = _prompt(mocker, "fundamental_v1")
    prompt = args[2]
    assert "하락 종목 비율 70% 이상이면 반드시 '청산'" in prompt
    assert "50% 미만이면 반드시 '관망'" not in prompt


def test_unknown_strategy_falls_back_to_conservative_rule(mock_db, mocker):
    """규칙을 등록하지 않은 전략은 회피 쪽(기존 동작)으로 떨어진다."""
    args, kwargs = _prompt(mocker, "새전략_v9")
    assert market_state.DEFAULT_RULE in args[2]


def test_strategies_do_not_share_cached_market_judgment(mock_db, mocker):
    """규칙이 반대인데 캐시를 공유하면 한쪽이 남의 판단을 받아 쓴다."""
    scopes = []
    for strategy in ("contrarian_v1", "fundamental_v1"):
        args, kwargs = _prompt(mocker, strategy)
        scopes.append(kwargs["cache_scope"])
    assert scopes[0] != scopes[1]


def test_bearish_hint_removed_from_contrarian_prompt(mock_db, mocker):
    """'50% 초과 = 약세장' 힌트는 역발상 규칙과 정면으로 어긋난다 —
    사야 할 국면을 약세장이라 불러 관망으로 기울게 만든다."""
    args, kwargs = _prompt(mocker, "contrarian_v1")
    assert "약세장" not in args[2]
