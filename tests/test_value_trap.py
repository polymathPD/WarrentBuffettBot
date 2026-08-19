"""agents/value_trap.py - 공시 텍스트로 가치 함정을 판별한다."""
import datetime

import pytest

from agents import value_trap


def _prompt(mocker, mock_db, disclosures=(), total=None, price_row=None):
    """analyze()가 실제로 만들어 보내는 프롬프트를 가로챈다."""
    mock_db.fetchall.return_value = list(disclosures)
    mock_db.fetchone.side_effect = [
        {"n": len(disclosures) if total is None else total},
        price_row,
    ]
    spy = mocker.patch("agents.value_trap.call",
                       return_value={"decision": "매수", "score": 8.0, "rationale": "x"})
    value_trap.analyze("005930", "2026-08-18")
    return spy.call_args[0][2]


def test_prompt_excludes_indicators_the_filter_already_used(mock_db, mocker):
    """핵심 설계 규약: 후보 필터가 쓴 지표는 에이전트에 주지 않는다.

    heat_score와 그 구성 지표를 되물으면 판단이 아니라 동어반복이 된다 —
    2026-08-19에 retail_flow/credit_heat가 100/100 전건 '매수'였던 이유다."""
    prompt = _prompt(mock_db=mock_db, mocker=mocker)

    for banned in ("heat_score", "과열 점수", "개인 순매수 배율",
                   "신용잔고 배율", "거래대금 배율"):
        assert banned not in prompt


def test_disclosures_are_listed_in_the_prompt(mock_db, mocker):
    rows = [
        {"d": datetime.date(2026, 8, 11), "report_nm": "파생상품거래손실발생"},
        {"d": datetime.date(2026, 7, 31), "report_nm": "주요사항보고서(유무상증자결정)"},
    ]
    prompt = _prompt(mock_db=mock_db, mocker=mocker, disclosures=rows)

    assert "파생상품거래손실발생" in prompt
    assert "주요사항보고서(유무상증자결정)" in prompt
    assert "2026-08-11" in prompt


def test_absence_of_disclosures_is_not_a_reason_to_reject(mock_db, mocker):
    """근거 없음은 무죄다. '확실하지 않아서' 관망하면 아무것도 못 산다."""
    prompt = _prompt(mock_db=mock_db, mocker=mocker, disclosures=[])

    assert "없음" in prompt
    assert "근거 없음은 무죄다" in prompt


def test_price_context_is_included_when_history_exists(mock_db, mocker):
    prompt = _prompt(mock_db=mock_db, mocker=mocker,
                     price_row={"last_c": 8000, "c60": 10000, "c250": 16000})

    assert "-20.0%" in prompt   # 60거래일
    assert "-50.0%" in prompt   # 250거래일


def test_missing_price_history_does_not_crash(mock_db, mocker):
    """상장 1년 미만 종목은 250일 전 종가가 없다."""
    prompt = _prompt(mock_db=mock_db, mocker=mocker, price_row=None)

    assert "데이터 없음" in prompt
