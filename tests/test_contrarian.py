"""strategy/contrarian.py - 진입/청산 후보 필터링. DB를 mock으로 대체."""
from datetime import date

import pytest

from strategy import contrarian


def test_entry_candidates_excludes_held_and_filters_by_pos52w(mock_db, mock_settings, mocker):
    mocker.patch("strategy.filters.large_caps",
                 return_value={"000001", "000002", "000003", "000004"})
    held_rows = [{"code": "000001"}]
    candidate_rows = [
        {"code": "000001", "heat_score": 5.0, "signal": "neutral", "close_price": 100},  # 보유 중 -> 스킵
        {"code": "000002", "heat_score": 6.0, "signal": "neutral", "close_price": 200},  # pos52w 0.2 -> 통과
        {"code": "000003", "heat_score": 6.5, "signal": "neutral", "close_price": 300},  # pos52w 0.5 -> 탈락
        {"code": "000004", "heat_score": 6.8, "signal": "neutral", "close_price": 400},  # pos52w None -> 탈락
    ]
    mock_db.fetchall.side_effect = [
        held_rows, candidate_rows,
        [{"code": c["code"]} for c in candidate_rows],   # tradable: 거래대금 통과
    ]
    mock_db.fetchone.side_effect = [
        {"pos52w": 0.2},
        {"pos52w": 0.5},
        {"pos52w": None},
    ]

    result = contrarian.get_entry_candidates("2024-01-15")

    assert len(result) == 1
    assert result[0]["code"] == "000002"
    assert result[0]["pos52w"] == pytest.approx(0.2)
    assert result[0]["close"] == pytest.approx(200)


def test_entry_candidates_boundary_pos52w_030_is_included(mock_db, mock_settings, mocker):
    mocker.patch("strategy.filters.large_caps", return_value={"000005"})
    mock_db.fetchall.side_effect = [
        [],  # 보유 없음
        [{"code": "000005", "heat_score": 5.0, "signal": "neutral", "close_price": 100}],
        [{"code": "000005"}],   # tradable: 거래대금 통과
    ]
    mock_db.fetchone.side_effect = [{"pos52w": 0.30}]  # 정확히 경계값

    result = contrarian.get_entry_candidates("2024-01-15")

    assert len(result) == 1  # <= 0.30 이므로 포함


def test_missing_inputs_score_zero_and_would_rank_first(mock_settings):
    """데이터가 전혀 없는 종목은 heat=0.0/neutral이 되어 ORDER BY heat_score ASC의
    최상위를 차지한다 — '결측'이 '가장 안 과열됨'으로 둔갑한다. 그래서
    get_entry_candidates()가 3개 지표 결측 행을 걸러내야 한다."""
    import numpy as np
    from processor.signals import _heat

    assert _heat(np.nan, np.nan, np.nan) == (0.0, "neutral")


def test_entry_query_excludes_rows_with_missing_inputs(mock_db, mock_settings, mocker):
    mocker.patch("strategy.filters.large_caps", return_value=set())
    """위 결측-위장을 막는 가드가 쿼리에 실제로 걸려 있는지 고정한다."""
    mock_db.fetchall.side_effect = [[], [], []]

    contrarian.get_entry_candidates("2026-08-12")

    sql = mock_db.fetchall.call_args_list[1][0][0]
    assert "cs.individual_flow_ratio IS NOT NULL" in sql
    assert "cs.credit_surge_ratio IS NOT NULL" in sql
    assert "cs.volume_ratio IS NOT NULL" in sql


def test_exit_stop_takes_priority_over_expiry(mock_db, mock_settings):
    """손절가 이하이면서 동시에 만기 초과여도 사유는 'stop'이어야 함 (if/elif 우선순위)."""
    mock_db.fetchall.return_value = [{
        "code": "000001", "name": "테스트종목", "entry_date": date(2024, 1, 1),
        "entry_px": 10000, "qty": 1, "stop_px": 9000, "max_hold_days": 5,
        "mode": "paper", "close_price": 8000, "heat_score": 9.0, "signal": "sell",
        "held_days": 13,
    }]
    result = contrarian.get_exit_candidates("2024-01-20")  # 만기 초과 + 손절가 밑

    assert len(result) == 1
    assert result[0]["reason"] == "stop"


def test_exit_expiry_when_not_stopped(mock_db, mock_settings):
    mock_db.fetchall.return_value = [{
        "code": "000001", "name": "테스트종목", "entry_date": date(2024, 1, 1),
        "entry_px": 10000, "qty": 1, "stop_px": 9000, "max_hold_days": 5,
        "mode": "paper", "close_price": 9500, "heat_score": 3.0, "signal": "neutral",
        "held_days": 13,
    }]
    result = contrarian.get_exit_candidates("2024-01-20")  # 13거래일 경과, 손절가 위

    assert len(result) == 1
    assert result[0]["reason"] == "expiry"


def test_exit_heat_signal_when_not_stopped_or_expired(mock_db, mock_settings):
    mock_db.fetchall.return_value = [{
        "code": "000001", "name": "테스트종목", "entry_date": date(2024, 1, 15),
        "entry_px": 10000, "qty": 1, "stop_px": 9000, "max_hold_days": 20,
        "mode": "paper", "close_price": 9500, "heat_score": 9.0, "signal": "sell",
        "held_days": 3,
    }]
    result = contrarian.get_exit_candidates("2024-01-20")  # 3거래일 경과, HEAT_SELL(8.5) 초과

    assert len(result) == 1
    assert result[0]["reason"] == "heat_signal"


def test_exit_no_reason_returns_empty(mock_db, mock_settings):
    mock_db.fetchall.return_value = [{
        "code": "000001", "name": "테스트종목", "entry_date": date(2024, 1, 15),
        "entry_px": 10000, "qty": 1, "stop_px": 9000, "max_hold_days": 20,
        "mode": "paper", "close_price": 9500, "heat_score": 3.0, "signal": "neutral",
        "held_days": 3,
    }]
    result = contrarian.get_exit_candidates("2024-01-20")
    assert result == []


def test_entry_candidates_drop_non_ordinary_and_illiquid(mock_db, mock_settings, mocker):
    """신주인수권증서 같은 비보통주와 거래대금 미달 종목은 후보에 오르면 안 된다.
    heat_score가 동점이라 정렬이 사실상 코드 순이어서, 필터가 없으면 이런 것들이
    후보 최상위를 차지한다."""
    from strategy import filters

    rows = [
        {"code": "0015S0", "heat_score": 0.0, "signal": "neutral", "close_price": 7030},
        {"code": "000100", "heat_score": 0.0, "signal": "neutral", "close_price": 90000},
    ]
    mock_db.fetchall.side_effect = [
        [],                        # 보유 없음
        rows,
        [{"code": "000100"}],      # 거래대금 통과한 종목만
    ]
    mocker.patch("strategy.filters.large_caps", return_value={"000100"})
    mock_db.fetchone.side_effect = [{"pos52w": 0.2}]

    got = contrarian.get_entry_candidates("2026-08-18")

    assert [c["code"] for c in got] == ["000100"]
    assert filters.ordinary(["0015S0", "000100"]) == ["000100"]


def test_exit_counts_holding_period_in_trading_days(mock_db, mock_settings):
    """보유기간은 달력일이 아니라 거래일(stock_daily 봉 수)이어야 한다.

    백테스트(research/portfolio_backtest.py)는 거래일 인덱스 차이로 센다.
    달력일로 세면 MAX_HOLD_DAYS=20이 검증에서는 20거래일(약 28달력일)인데
    운용에서는 20달력일(약 14거래일)이 되어 30% 일찍 팔린다."""
    mock_db.fetchall.return_value = []
    contrarian.get_exit_candidates("2024-01-20")

    sql = mock_db.fetchall.call_args[0][0]
    assert "COUNT(*) FROM stock_daily" in sql, "거래일 봉 수를 세야 한다"
    assert "h.d > p.entry_date" in sql and "h.d <= %s::date" in sql
