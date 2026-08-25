"""dashboard/app.py - 에이전트 실패 배너용 집계."""
from datetime import datetime

from agents import base
from dashboard.app import agent_alerts


def _row(label, detail, ts):
    return {"rationale": f"{label}{base.ERROR_SEP}{detail}", "ts": ts}


def test_no_alerts_when_no_failures(mock_db):
    mock_db.fetchall.return_value = []
    assert agent_alerts() == []


def test_alerts_group_by_reason_and_keep_latest(mock_db):
    """크레딧이 떨어지면 하루 수백 건이 쌓인다 — 사유별 한 줄로 묶고 건수만 센다."""
    latest = datetime(2026, 8, 19, 16, 12)
    mock_db.fetchall.return_value = [           # 쿼리가 최신순으로 준다
        _row(base.ERR_CREDIT, "잔액 0", latest),
        _row(base.ERR_CREDIT, "잔액 0", datetime(2026, 8, 19, 16, 11)),
        _row(base.ERR_RATE, "429", datetime(2026, 8, 19, 15, 0)),
    ]

    alerts = agent_alerts()

    assert [a["label"] for a in alerts] == [base.ERR_CREDIT, base.ERR_RATE]
    assert alerts[0]["n"] == 2
    assert alerts[0]["ts"] == latest        # 마지막 발생 시각
    assert alerts[0]["detail"] == "잔액 0"


def test_alert_query_only_reads_error_rows(mock_db):
    """정상 판단('관망' 등)이 배너로 새어 나오면 안 된다."""
    mock_db.fetchall.return_value = []
    agent_alerts()

    sql, params = mock_db.fetchall.call_args[0]
    assert "decision = %s" in sql
    assert params == (base.ERROR_DECISION,)


def test_entry_verdicts_fall_back_to_the_agent_decision_log():
    """판단은 사본이 없어도 화면에 떠야 한다 - 원본은 agent_decisions다.

    2026-08-24에 오리온홀딩스 78주를 실제로 샀는데 체결을 폴링 안에 못 봐서
    trades에도 positions에도 기록이 없었다. 게이트를 통과하고 산 종목인데 대시보드의
    진입 판단이 빈칸이었다. 사본은 체결을 봐야 남지만 agent_decisions는 에이전트가
    답한 순간 기록되므로 주문 결과와 무관하다.

    SQL 자체는 DB 없이 실행할 수 없어 조회 구조만 고정한다.
    """
    import re
    from pathlib import Path

    src = Path(__file__).resolve().parent.parent / "dashboard" / "app.py"
    sql = src.read_text(encoding="utf-8")
    q = sql[sql.index("SELECT p.code, p.name, p.entry_date"):sql.index('""", (mode,))')]

    assert "COALESCE(p.agents, t.agents, ad.agents)" in q, "폴백 순서가 바뀌었다"
    assert "FROM agent_decisions" in q, "원본을 안 본다"
    assert re.search(r"WHERE code = p\.code AND ts::date = p\.entry_date", q), \
        "진입일 판단이 아니라 아무 날 판단을 끌어오면 '진입 판단'이 아니다"
