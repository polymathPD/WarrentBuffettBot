"""agents/base.py - Claude API 호출 + 캐싱 로직"""
import pytest

from agents.base import _parse, call
import config


# ---------- _parse() : 순수 함수 ----------

def test_parse_normal_format():
    text = "결정: 매수\n확신: 7.5\n이유: 저평가 구간 진입"
    decision, score, rationale = _parse(text)
    assert decision == "매수"
    assert score == 7.5
    assert rationale == "저평가 구간 진입"


def test_parse_missing_decision_defaults_to_watch():
    text = "확신: 6\n이유: 애매함"
    decision, score, rationale = _parse(text)
    assert decision == "관망"


def test_parse_missing_score_defaults_to_5():
    text = "결정: 청산\n이유: 손절 라인 터치"
    decision, score, rationale = _parse(text)
    assert score == 5.0


def test_parse_missing_reason_falls_back_to_raw_text():
    text = "그냥 아무 텍스트"
    decision, score, rationale = _parse(text)
    assert rationale == text


# ---------- call() : DB 캐시 + Claude API ----------

def test_call_returns_cached_result_without_calling_claude(mock_db, mock_claude):
    mock_db.fetchone.return_value = {
        "decision": "매수", "score": 9.0, "rationale": "캐시된 결과",
    }
    result = call("retail_flow", "005930", "아무 프롬프트")
    assert result == {"decision": "매수", "score": 9.0, "rationale": "캐시된 결과"}
    mock_claude.mock.assert_not_called()


def test_call_cache_miss_calls_claude_and_stores_result(mock_db, mock_claude):
    mock_db.fetchone.return_value = None  # 캐시 없음
    mock_claude.return_value = "결정: 매수\n확신: 8\n이유: 신규 판단"

    result = call("credit_heat", "005930", "프롬프트")

    assert result == {"decision": "매수", "score": 8.0, "rationale": "신규 판단"}
    mock_claude.mock.assert_called_once()
    mock_db.execute.assert_called_once()
    # INSERT 파라미터에 agent명/종목코드가 정확히 들어갔는지 확인
    args, kwargs = mock_db.execute.call_args
    params = args[1]
    assert params[0] == "005930"       # code
    assert params[1] == "credit_heat"  # agent


def test_call_without_api_key_returns_default(mock_db, mock_claude, monkeypatch):
    monkeypatch.setattr(config, "CLAUDE_API_KEY", "")
    mock_db.fetchone.return_value = None

    result = call("risk", "005930", "프롬프트")

    assert result["decision"] == "관망"
    mock_claude.mock.assert_not_called()
    mock_db.execute.assert_not_called()


def test_call_claude_api_error_returns_default(mock_db, mocker):
    mock_db.fetchone.return_value = None
    fake_client = mocker.MagicMock()
    fake_client.messages.create.side_effect = RuntimeError("네트워크 오류")
    mocker.patch("agents.base._get_client", return_value=fake_client)

    result = call("market_state", "005930", "프롬프트")

    assert result["decision"] == "관망"
    assert "네트워크 오류" in result["rationale"]
    mock_db.execute.assert_not_called()
