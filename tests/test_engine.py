"""
backtester/engine.py 회귀 테스트.

2026-08-10에 발견한 버그: `active` 딕셔너리가 선언만 되고 갱신되지 않아
같은 종목에 대해 보유 중에도 새 신호가 뜨면 중복 포지션이 무한정 쌓였다
(수정 전 거래 수 848,416건 -> 수정 후 64,403건). 이 테스트는 같은 종목에
겹치는 신호가 오면 스킵되고, 청산 이후 신호는 다시 진입 가능한지 확인한다.
"""
from datetime import date

from backtester import engine


def test_no_duplicate_overlapping_positions_for_same_code(mocker):
    signals_query_result = [
        {"code": "000001", "signal_date": date(2024, 1, 2), "heat_score": 5.0, "signal": "neutral",
         "entry_date": date(2024, 1, 3), "entry_open": 100.0},
        # 아래 신호는 위 포지션 보유 기간(2024-01-03 ~ 2024-01-08) 중에 발생 -> 스킵되어야 함
        {"code": "000001", "signal_date": date(2024, 1, 3), "heat_score": 5.0, "signal": "neutral",
         "entry_date": date(2024, 1, 4), "entry_open": 101.0},
        # 첫 포지션 청산(2024-01-08) 이후 신호는 다시 진입 가능해야 함
        {"code": "000001", "signal_date": date(2024, 1, 9), "heat_score": 5.0, "signal": "neutral",
         "entry_date": date(2024, 1, 10), "entry_open": 110.0},
    ]

    # max_hold=3 -> 진입일 포함 4개 봉(future[0]=진입일, future[1:]=보유 감시 대상)
    future_for_first = [
        {"d": date(2024, 1, 3), "o": 100.0, "h": 105.0, "l": 99.0, "c": 102.0},
        {"d": date(2024, 1, 4), "o": 102.0, "h": 103.0, "l": 101.0, "c": 102.0},
        {"d": date(2024, 1, 5), "o": 102.0, "h": 104.0, "l": 101.0, "c": 103.0},
        {"d": date(2024, 1, 8), "o": 103.0, "h": 105.0, "l": 102.0, "c": 104.0},  # 3일 경과 -> expiry
    ]
    future_for_third = [
        {"d": date(2024, 1, 10), "o": 110.0, "h": 115.0, "l": 109.0, "c": 112.0},
        {"d": date(2024, 1, 11), "o": 112.0, "h": 113.0, "l": 111.0, "c": 112.0},
        {"d": date(2024, 1, 12), "o": 112.0, "h": 114.0, "l": 111.0, "c": 113.0},
        {"d": date(2024, 1, 15), "o": 113.0, "h": 115.0, "l": 112.0, "c": 114.0},
    ]
    future_queue = [future_for_first, future_for_third]

    def fetchall_side_effect(sql, params=None):
        if "contrarian_signals cs" in sql:
            return signals_query_result
        if "FROM stock_daily WHERE code=" in sql:
            return future_queue.pop(0)
        raise AssertionError(f"예상치 못한 쿼리: {sql}")

    mocker.patch("db.connection.fetchall", side_effect=fetchall_side_effect)
    mocker.patch("db.connection.fetchone", return_value=None)  # heat_signal 청산 미발생 처리

    trades = engine.run("2024-01-01", "2024-01-31", max_hold=3, stop_pct=0.07)

    # 겹치는 두번째 신호는 스킵되어 총 2건(첫/세번째)만 체결되어야 함
    assert len(trades) == 2
    assert trades[0]["entry_date"] == "2024-01-03"
    assert trades[0]["exit_date"] == "2024-01-08"
    assert trades[0]["exit_reason"] == "expiry"
    assert trades[1]["entry_date"] == "2024-01-10"
    assert not future_queue  # 두번째 신호에 대해서는 future 조회가 아예 없었어야 함


def test_stop_loss_exits_before_expiry(mocker):
    signals_query_result = [
        {"code": "000002", "signal_date": date(2024, 1, 2), "heat_score": 5.0, "signal": "neutral",
         "entry_date": date(2024, 1, 3), "entry_open": 100.0},
    ]
    # 손절가는 entry_px*(1-0.07) 근처 -> 둘째날 저가가 크게 하락해 손절 트리거
    future = [
        {"d": date(2024, 1, 3), "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.0},
        {"d": date(2024, 1, 4), "o": 95.0, "h": 96.0, "l": 80.0, "c": 85.0},  # 손절 트리거
        {"d": date(2024, 1, 5), "o": 85.0, "h": 86.0, "l": 84.0, "c": 85.0},
    ]

    def fetchall_side_effect(sql, params=None):
        if "contrarian_signals cs" in sql:
            return signals_query_result
        return future

    mocker.patch("db.connection.fetchall", side_effect=fetchall_side_effect)
    mocker.patch("db.connection.fetchone", return_value=None)

    trades = engine.run("2024-01-01", "2024-01-31", max_hold=20, stop_pct=0.07)

    assert len(trades) == 1
    assert trades[0]["exit_reason"] == "stop"
    assert trades[0]["exit_date"] == "2024-01-04"
