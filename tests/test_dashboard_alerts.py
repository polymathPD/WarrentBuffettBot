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
