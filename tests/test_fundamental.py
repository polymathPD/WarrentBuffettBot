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


def test_improvement_is_scaled_by_equity_not_prior_profit():
    """전년 이익이 0에 가까운 종목이 랭킹을 독식하지 않도록 자본으로 나눈다."""
    _, tiny_base = fnd._passes(*_fin(50.0, 0.01, equity=1000.0))
    _, big_base = fnd._passes(*_fin(150.0, 100.0, equity=1000.0))

    assert tiny_base == pytest.approx(49.99 / 1000.0)
    assert big_base == pytest.approx(50.0 / 1000.0)
    assert big_base > tiny_base   # 전년 대비 증가율로 쟀다면 정반대로 뒤집힌다


def test_improvement_handles_turnaround():
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
        [{"code": "000001"}, {"code": "000002"}],   # 거래대금 통과
    ]

    got = fnd.get_entry_candidates("2026-08-14", apply_marcap=False)

    assert [c["code"] for c in got] == ["000002", "000001"]   # 자본 대비 20% > 5%
    assert got[0]["period"] == "2026Q2"


def test_entry_candidates_drop_illiquid_names(mock_db):
    """거래대금이 얇으면 슬리피지 가정이 성립하지 않으므로 제외한다."""
    mock_db.fetchall.side_effect = [
        [{"code": "000001", "report_nm": "반기보고서 (2026.06)"}],
        [],
        [{"code": "000001", "period": "2026Q2", "op_income": 300, "net_income": 100,
          "equity": 1000, "liabilities": 500},
         {"code": "000001", "period": "2025Q2", "op_income": 100, "net_income": 50,
          "equity": 900, "liabilities": 500}],
        [{"code": "000001", "c": 1000}],
        [],                                         # 거래대금 미달
    ]

    assert fnd.get_entry_candidates("2026-08-14", apply_marcap=False) == []


def test_entry_candidates_skip_held_positions(mock_db):
    mock_db.fetchall.side_effect = [
        [{"code": "000001", "report_nm": "반기보고서 (2026.06)"}],
        [{"code": "000001"}],   # 이미 보유 중
    ]

    assert fnd.get_entry_candidates("2026-08-14", apply_marcap=False) == []


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


def test_entry_candidates_drop_small_caps_in_live_path(mock_db, mocker):
    """실전 경로에서는 시가총액 하한도 적용한다."""
    rows = [
        [{"code": "000001", "report_nm": "반기보고서 (2026.06)"}],
        [],
        [{"code": "000001", "period": "2026Q2", "op_income": 300, "net_income": 100,
          "equity": 1000, "liabilities": 500},
         {"code": "000001", "period": "2025Q2", "op_income": 100, "net_income": 50,
          "equity": 900, "liabilities": 500}],
        [{"code": "000001", "c": 1000}],
        [{"code": "000001"}],
    ]
    mock_db.fetchall.side_effect = list(rows)
    mocker.patch("strategy.filters.large_caps", return_value=set())      # 소형주

    assert fnd.get_entry_candidates("2026-08-14") == []

    mock_db.fetchall.side_effect = list(rows)
    mocker.patch("strategy.filters.large_caps", return_value={"000001"})  # 대형주

    assert len(fnd.get_entry_candidates("2026-08-14")) == 1


def test_backtest_path_skips_marcap_lookup(mock_db, mocker):
    """백테스트는 현재 시총을 보지 않는다 (생존 편향 방지)."""
    mock_db.fetchall.side_effect = [
        [{"code": "000001", "report_nm": "반기보고서 (2026.06)"}],
        [],
        [{"code": "000001", "period": "2026Q2", "op_income": 300, "net_income": 100,
          "equity": 1000, "liabilities": 500},
         {"code": "000001", "period": "2025Q2", "op_income": 100, "net_income": 50,
          "equity": 900, "liabilities": 500}],
        [{"code": "000001", "c": 1000}],
        [{"code": "000001"}],
    ]
    caps = mocker.patch("strategy.filters.large_caps", return_value=set())

    got = fnd.get_entry_candidates("2026-08-14", apply_marcap=False)

    assert len(got) == 1
    caps.assert_not_called()
