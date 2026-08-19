"""agents/base.py - Claude API 호출 + 캐싱 로직"""
import pytest

import httpx
import anthropic

from agents import base
from agents.base import _parse, call
import config


def _fake_response(status: int) -> httpx.Response:
    """anthropic 예외 생성에 필요한 최소 httpx.Response."""
    return httpx.Response(status, request=httpx.Request("POST", "https://api.anthropic.com"))


# ---------- _parse() : 순수 함수 ----------

def test_parse_normal_format():
    text = "결정: 매수\n확신: 7.5\n이유: 저평가 구간 진입"
    decision, score, rationale = _parse(text)
    assert decision == "매수"
    assert score == 7.5
    assert rationale == "저평가 구간 진입"


def test_parse_unrecognized_decision_returns_none():
    """못 알아본 결정을 '관망'으로 눙치면 거부권과 구분이 안 된다."""
    decision, score, rationale = _parse("확신: 6\n이유: 애매함")
    assert decision is None


@pytest.mark.parametrize("text", [
    "**결정: 청산 / 확신: 10 / 이유: 약세장**",        # 줄 전체가 굵게
    "**결정: 청산** / **확신: 10** / **이유: 약세장**",  # 항목마다 굵게
    "결정: **청산** / 확신: 10 / 이유: 약세장",         # 값만 굵게
    "**결정**: 청산 / 확신: 10 / 이유: 약세장",         # 라벨만 굵게
])
def test_parse_tolerates_markdown_emphasis(text):
    """모델이 매번 같은 형태로 주지 않는다. 결정을 \\S+로 통째로 집으면 '청산**'이
    나와 게이트의 문자열 비교를 빗나가고, 거부권이 조용히 무시된다."""
    decision, score, rationale = _parse(text)
    assert decision == "청산"
    assert score == 10.0
    assert rationale == "약세장"


def test_parse_strips_emphasis_from_rationale():
    _, _, rationale = _parse("결정: 매수 / 확신: 8 / 이유: **저평가 구간**")
    assert rationale == "저평가 구간"


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


def test_call_without_api_key_reports_error_not_watch(mock_db, mock_claude, monkeypatch):
    monkeypatch.setattr(config, "CLAUDE_API_KEY", "")
    mock_db.fetchone.return_value = None

    result = call("risk", "005930", "프롬프트")

    assert result["decision"] == base.ERROR_DECISION
    assert result["error"] == base.ERR_NO_KEY
    mock_claude.mock.assert_not_called()
    mock_db.execute.assert_called_once()   # 조용히 넘어가지 않고 기록에 남긴다


def test_call_claude_api_error_reports_error_not_watch(mock_db, mocker):
    """API 실패를 '관망'으로 떨어뜨리면 게이트의 거부권과 구분이 안 된다."""
    mock_db.fetchone.return_value = None
    fake_client = mocker.MagicMock()
    fake_client.messages.create.side_effect = RuntimeError("네트워크 오류")
    mocker.patch("agents.base._get_client", return_value=fake_client)

    result = call("market_state", "005930", "프롬프트")

    assert result["decision"] == base.ERROR_DECISION
    assert result["decision"] != "관망"
    assert "네트워크 오류" in result["rationale"]


def test_call_failure_is_recorded_without_input_hash(mock_db, mocker):
    """실패 행은 input_hash=NULL로 남아야 한다 — 캐시에 얹히면 다음 호출까지 오염된다."""
    mock_db.fetchone.return_value = None
    fake_client = mocker.MagicMock()
    fake_client.messages.create.side_effect = RuntimeError("펑")
    mocker.patch("agents.base._get_client", return_value=fake_client)

    call("market_state", "005930", "프롬프트")

    sql, params = mock_db.execute.call_args[0]
    assert "input_hash" in sql and "NULL" in sql
    assert params[0] == "005930"                     # code는 그대로 남는다
    assert params[3] == base.ERROR_DECISION          # decision
    assert len(params) == 6                          # input_hash는 SQL에 NULL 리터럴


def test_classify_credit_exhaustion_separately_from_bad_key():
    """크레딧 소진은 400 invalid_request_error로 온다 — 401 인증 실패와 대응이 다르다
    (충전 vs 키 교체). 같은 라벨로 묶이면 배너를 보고 엉뚱한 조치를 하게 된다."""
    credit = anthropic.BadRequestError(
        "Your credit balance is too low to access the Anthropic API.",
        response=_fake_response(400), body=None,
    )
    assert base._classify(credit) == base.ERR_CREDIT

    bad_key = anthropic.AuthenticationError(
        "invalid x-api-key", response=_fake_response(401), body=None,
    )
    assert base._classify(bad_key) == base.ERR_AUTH


def test_classify_unknown_error_falls_back(mocker):
    assert base._classify(RuntimeError("알 수 없음")) == base.ERR_OTHER


def test_call_cache_scope_shares_key_across_codes(mock_db, mock_claude):
    """프롬프트가 종목과 무관한 에이전트는 종목이 달라도 캐시 키가 같아야 한다.

    market_state는 시장 전체만 보고 판단하므로 프롬프트에 종목코드가 없다.
    캐시 키가 code로 갈리면 똑같은 질문을 후보 종목 수만큼 결제하게 된다."""
    mock_db.fetchone.return_value = None
    mock_claude.return_value = "결정: 매수 / 확신: 8 / 이유: 강세장"

    call("market_state", "005930", "시장 프롬프트", cache_scope="market")
    call("market_state", "000660", "시장 프롬프트", cache_scope="market")
    scoped = [c[0][1][0] for c in mock_db.fetchone.call_args_list]
    assert scoped[0] == scoped[1]

    # 기본값(code 기준)은 여전히 종목마다 갈린다 — 다른 에이전트가 이 변경에
    # 휩쓸려 캐시를 공유하게 되면 안 된다.
    mock_db.fetchone.reset_mock()
    call("retail_flow", "005930", "종목 프롬프트")
    call("retail_flow", "000660", "종목 프롬프트")
    per_code = [c[0][1][0] for c in mock_db.fetchone.call_args_list]
    assert per_code[0] != per_code[1]


def test_market_state_caches_per_market_not_per_code(mock_db, mock_claude):
    """market_state.analyze()가 실제로 시장 단위 캐시 키를 넘기는지 고정."""
    from agents import market_state

    mock_db.fetchone.return_value = None
    mock_claude.return_value = "결정: 매수 / 확신: 8 / 이유: 강세장"

    market_state.analyze("005930", "2026-08-19", "contrarian_v1")
    market_state.analyze("000660", "2026-08-19", "contrarian_v1")

    keys = [
        c[0][1][0]
        for c in mock_db.fetchone.call_args_list
        if "agent_decisions" in c[0][0]
    ]
    assert len(keys) == 2
    assert keys[0] == keys[1]
    # 종목코드는 캐시 키에서만 빠지고, 기록에는 그대로 남는다
    codes = [c[0][1][0] for c in mock_db.execute.call_args_list]
    assert codes == ["005930", "000660"]


def test_unparseable_response_is_recorded_as_error(mock_db, mock_claude):
    """형식이 깨진 응답도 조용히 '관망'이 되지 않는다."""
    mock_db.fetchone.return_value = None
    mock_claude.return_value = "음... 잘 모르겠습니다"

    result = call("retail_flow", "005930", "프롬프트")

    assert result["decision"] == base.ERROR_DECISION
    assert result["error"] == base.ERR_PARSE
    sql, params = mock_db.execute.call_args[0]
    assert "NULL" in sql          # 깨진 응답은 캐시하지 않는다
