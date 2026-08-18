"""agents/disclosure.py, agents/financials.py - 프롬프트 구성. DB/Claude는 mock."""
from datetime import date

import pytest

from agents import disclosure as dis_agent
from agents import financials as fin_agent


def _prompt(mock_call):
    return mock_call.call_args[0][2]


# ---------- 공시 에이전트 ----------

def test_disclosure_prompt_lists_recent_filings(mock_db, mocker):
    mock_db.fetchall.return_value = [
        {"d": date(2026, 8, 14), "report_nm": "반기보고서 (2026.06)"},
        {"d": date(2026, 7, 2), "report_nm": "주요사항보고서(유상증자결정)"},
    ]
    mock_db.fetchone.return_value = {"n": 7}
    call = mocker.patch.object(dis_agent, "call", return_value={})

    dis_agent.analyze("005930", "2026-08-14")

    prompt = _prompt(call)
    assert "총 7건 중 최신 2건" in prompt
    assert "주요사항보고서(유상증자결정)" in prompt
    assert call.call_args[0][0] == "disclosure"


def test_disclosure_prompt_handles_no_filings(mock_db, mocker):
    mock_db.fetchall.return_value = []
    mock_db.fetchone.return_value = {"n": 0}
    call = mocker.patch.object(dis_agent, "call", return_value={})

    dis_agent.analyze("005930", "2026-08-14")

    assert "없음" in _prompt(call)


# ---------- 재무 에이전트 ----------

def _row(period, revenue, op, net, equity, debt, fs_div="CFS"):
    return {"period": period, "fs_div": fs_div, "revenue": revenue, "op_income": op,
            "net_income": net, "equity": equity, "liabilities": debt}


def test_financials_prompt_shows_ratios_oldest_first(mock_db, mocker):
    mock_db.fetchall.return_value = [                      # DB는 최신순으로 준다
        _row("2026Q2", 2e12, 4e11, 2e11, 1e12, 5e11),
        _row("2025Q2", 1e12, 1e11, 5e10, 9e11, 5e11),
    ]
    call = mocker.patch.object(fin_agent, "call", return_value={})

    fin_agent.analyze("005930", "2026-08-14")

    prompt = _prompt(call)
    assert prompt.index("2025Q2") < prompt.index("2026Q2")   # 오래된 것부터
    assert "영업이익 0.4조(20.0%)" in prompt                  # 영업이익률
    assert "ROE 20.0%" in prompt
    assert "부채비율 50%" in prompt


def test_financials_prompt_warns_about_cumulative_figures(mock_db, mocker):
    """누적치라는 사실을 프롬프트에 넣지 않으면 분기 비교를 잘못한다."""
    mock_db.fetchall.return_value = [_row("2026Q2", 1e12, 1e11, 5e10, 1e12, 5e11)]
    call = mocker.patch.object(fin_agent, "call", return_value={})

    fin_agent.analyze("005930", "2026-08-14")

    assert "누적" in _prompt(call)


def test_financials_prompt_tolerates_missing_values(mock_db, mocker):
    mock_db.fetchall.return_value = [_row("2026Q2", None, None, None, None, None)]
    call = mocker.patch.object(fin_agent, "call", return_value={})

    fin_agent.analyze("005930", "2026-08-14")

    assert "ROE -" in _prompt(call)


def test_financials_prompt_handles_no_data(mock_db, mocker):
    mock_db.fetchall.return_value = []
    call = mocker.patch.object(fin_agent, "call", return_value={})

    fin_agent.analyze("005930", "2026-08-14")

    assert "재무 데이터 없음" in _prompt(call)
