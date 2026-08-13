"""strategy/contrarian.py - 진입/청산 후보 필터링. DB를 mock으로 대체."""
from datetime import date

import pytest

from strategy import contrarian


def test_entry_candidates_excludes_held_and_filters_by_pos52w(mock_db, mock_settings):
    held_rows = [{"code": "000001"}]
    candidate_rows = [
        {"code": "000001", "heat_score": 5.0, "signal": "neutral", "close_price": 100},  # 보유 중 -> 스킵
        {"code": "000002", "heat_score": 6.0, "signal": "neutral", "close_price": 200},  # pos52w 0.2 -> 통과
        {"code": "000003", "heat_score": 6.5, "signal": "neutral", "close_price": 300},  # pos52w 0.5 -> 탈락
        {"code": "000004", "heat_score": 6.8, "signal": "neutral", "close_price": 400},  # pos52w None -> 탈락
    ]
    mock_db.fetchall.side_effect = [held_rows, candidate_rows]
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


def test_entry_candidates_boundary_pos52w_030_is_included(mock_db, mock_settings):
    mock_db.fetchall.side_effect = [
        [],  # 보유 없음
        [{"code": "000005", "heat_score": 5.0, "signal": "neutral", "close_price": 100}],
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


def test_entry_query_excludes_rows_with_missing_inputs(mock_db, mock_settings):
    """위 결측-위장을 막는 가드가 쿼리에 실제로 걸려 있는지 고정한다."""
    mock_db.fetchall.side_effect = [[], []]

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
    }]
    result = contrarian.get_exit_candidates("2024-01-20")  # 19일 경과, 손절가 밑

    assert len(result) == 1
    assert result[0]["reason"] == "stop"


def test_exit_expiry_when_not_stopped(mock_db, mock_settings):
    mock_db.fetchall.return_value = [{
        "code": "000001", "name": "테스트종목", "entry_date": date(2024, 1, 1),
        "entry_px": 10000, "qty": 1, "stop_px": 9000, "max_hold_days": 5,
        "mode": "paper", "close_price": 9500, "heat_score": 3.0, "signal": "neutral",
    }]
    result = contrarian.get_exit_candidates("2024-01-20")  # 19일 경과, 손절가 위

    assert len(result) == 1
    assert result[0]["reason"] == "expiry"


def test_exit_heat_signal_when_not_stopped_or_expired(mock_db, mock_settings):
    mock_db.fetchall.return_value = [{
        "code": "000001", "name": "테스트종목", "entry_date": date(2024, 1, 15),
        "entry_px": 10000, "qty": 1, "stop_px": 9000, "max_hold_days": 20,
        "mode": "paper", "close_price": 9500, "heat_score": 9.0, "signal": "sell",
    }]
    result = contrarian.get_exit_candidates("2024-01-20")  # 5일 경과, HEAT_SELL(8.5) 초과

    assert len(result) == 1
    assert result[0]["reason"] == "heat_signal"


def test_exit_no_reason_returns_empty(mock_db, mock_settings):
    mock_db.fetchall.return_value = [{
        "code": "000001", "name": "테스트종목", "entry_date": date(2024, 1, 15),
        "entry_px": 10000, "qty": 1, "stop_px": 9000, "max_hold_days": 20,
        "mode": "paper", "close_price": 9500, "heat_score": 3.0, "signal": "neutral",
    }]
    result = contrarian.get_exit_candidates("2024-01-20")
    assert result == []
