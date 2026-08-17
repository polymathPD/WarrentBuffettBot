"""strategy/fundamental.py - 기간 파싱, 진입 조건, 최소 보유기간. DB는 mock."""
import pytest

from strategy import fundamental as fnd


def _fin(op, prev_op, equity=1000.0, net=100.0, debt=1000.0):
    cur = {"op_income": op, "net_income": net, "equity": equity, "liabilities": debt}
    prev = {"op_income": prev_op}
    return cur, prev


def test_period_parsing():
    assert fnd._period_of("반기보고서 (2026.06)") == "2026Q2"
    assert fnd._period_of("분기보고서 (2026.03)") == "2026Q1"
    assert fnd._period_of("사업보고서 (2025.12)") == "2025Q4"
    assert fnd._period_of("분기보고서 (2026.09)") == "2026Q3"
    assert fnd._period_of("반기보고서") is None          # 기간 표기 없음
    assert fnd._period_of("분기보고서 (2026.02)") is None  # 12월 결산이 아님


def test_prev_year_keeps_quarter():
    """손익이 누적치라 같은 분기끼리 비교해야 한다."""
    assert fnd._prev_year("2026Q2") == "2025Q2"
    assert fnd._prev_year("2025Q4") == "2024Q4"


def test_passes_requires_improvement_and_profit():
    assert fnd._passes(*_fin(200.0, 100.0))[0] is True
    assert fnd._passes(*_fin(100.0, 100.0))[0] is False   # 개선 없음
    assert fnd._passes(*_fin(50.0, 100.0))[0] is False    # 악화
    assert fnd._passes(*_fin(-10.0, -50.0))[0] is False   # 개선했지만 적자


def test_passes_applies_quality_screens():
    # ROE = 20/1000 = 2% < ROE_MIN(3%)
    assert fnd._passes(*_fin(200.0, 100.0, net=20.0))[0] is False
    # 부채비율 = 2500/1000 = 250% > DEBT_MAX(200%)
    assert fnd._passes(*_fin(200.0, 100.0, debt=2500.0))[0] is False
    # 자본잠식
    assert fnd._passes(*_fin(200.0, 100.0, equity=-100.0))[0] is False


def test_passes_handles_turnaround_without_blowing_up():
    """전년 적자면 개선율 분모가 0에 가까워 폭주하므로 자본으로 정규화한다."""
    ok, improvement = fnd._passes(*_fin(50.0, -1.0, equity=1000.0))

    assert ok is True
    assert improvement == pytest.approx(51.0 / 1000.0)


def test_passes_rejects_missing_financials():
    assert fnd._passes(None, {"op_income": 1.0})[0] is False
    assert fnd._passes({"op_income": 1.0}, None)[0] is False
    assert fnd._passes({"op_income": None, "net_income": 1.0, "equity": 1.0,
                        "liabilities": 1.0}, {"op_income": 1.0})[0] is False


def test_entry_candidates_ranked_by_improvement(mock_db):
    mock_db.fetchall.side_effect = [
        [{"code": "000001", "report_nm": "반기보고서 (2026.06)"},
         {"code": "000002", "report_nm": "반기보고서 (2026.06)"}],
        [],                                    # 보유 없음
        [   # financials
            {"code": "000001", "period": "2026Q2", "op_income": 150, "net_income": 100,
             "equity": 1000, "liabilities": 500},
            {"code": "000001", "period": "2025Q2", "op_income": 100, "net_income": 50,
             "equity": 900, "liabilities": 500},
            {"code": "000002", "period": "2026Q2", "op_income": 300, "net_income": 100,
             "equity": 1000, "liabilities": 500},
            {"code": "000002", "period": "2025Q2", "op_income": 100, "net_income": 50,
             "equity": 900, "liabilities": 500},
        ],
        [{"code": "000001", "c": 10000}, {"code": "000002", "c": 20000}],
    ]

    got = fnd.get_entry_candidates("2026-08-14")

    assert [c["code"] for c in got] == ["000002", "000001"]   # 개선율 200% > 50%
    assert got[0]["period"] == "2026Q2"


def test_entry_candidates_skip_held_positions(mock_db):
    mock_db.fetchall.side_effect = [
        [{"code": "000001", "report_nm": "반기보고서 (2026.06)"}],
        [{"code": "000001"}],   # 이미 보유 중
    ]

    assert fnd.get_entry_candidates("2026-08-14") == []


def test_exit_holds_through_minimum_period(mock_db):
    """만기가 지나도 최소 보유 거래일을 못 채웠으면 청산하지 않는다."""
    mock_db.fetchall.return_value = [{
        "code": "000001", "name": "테스트", "entry_px": 10000, "qty": 1,
        "stop_px": 9300, "max_hold_days": 2, "mode": "paper",
        "close_price": 10500, "held_days": 3,
    }]

    assert fnd.get_exit_candidates("2026-08-20") == []


def test_exit_stop_ignores_minimum_period(mock_db):
    mock_db.fetchall.return_value = [{
        "code": "000001", "name": "테스트", "entry_px": 10000, "qty": 1,
        "stop_px": 9300, "max_hold_days": 20, "mode": "paper",
        "close_price": 9000, "held_days": 1,
    }]

    got = fnd.get_exit_candidates("2026-08-20")

    assert len(got) == 1 and got[0]["reason"] == "stop"


def test_exit_expiry_after_minimum_period(mock_db):
    mock_db.fetchall.return_value = [{
        "code": "000001", "name": "테스트", "entry_px": 10000, "qty": 1,
        "stop_px": 9300, "max_hold_days": 20, "mode": "paper",
        "close_price": 10500, "held_days": 20,
    }]

    got = fnd.get_exit_candidates("2026-09-20")

    assert got[0]["reason"] == "expiry"
