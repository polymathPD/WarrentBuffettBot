"""
scheduler._entry_signal_date() 회귀 테스트.

2026-08-12에 발견한 버그: 스케줄러가 16:10에 '오늘' 신호로 paper.buy()를 호출했는데,
체결 규칙이 "신호일 다음 거래일 시가"라서 _next_open()이 아직 존재하지 않는 내일 봉을
조회했다. 결과적으로 모든 매수가 "다음 거래일 데이터 없음"으로 거부되어 모의매매가
단 한 건도 체결된 적이 없었다(trades/positions 0행). 오늘 체결할 대상은 '직전 거래일
신호'여야 한다.
"""
from datetime import date

import scheduler


def test_entry_signal_date_is_previous_trading_day(mock_db):
    mock_db.fetchone.side_effect = [
        {"d": date(2026, 8, 11)},  # 오늘 이전 마지막 신호일
        {"d": date(2026, 8, 12)},  # 그 신호의 체결일 = 오늘
    ]

    assert scheduler._entry_signal_date("2026-08-12") == "2026-08-11"


def test_entry_skipped_when_no_signals_exist(mock_db):
    mock_db.fetchone.side_effect = [{"d": None}]

    assert scheduler._entry_signal_date("2026-08-12") is None


def test_entry_skipped_when_signal_is_stale(mock_db, capsys):
    """신호 계산이 며칠 밀린 경우, 이미 지나간 날 시가로 체결하면 안 된다."""
    mock_db.fetchone.side_effect = [
        {"d": date(2026, 8, 5)},   # 일주일 전 신호
        {"d": date(2026, 8, 6)},   # 체결일이 오늘(08-12)이 아님
    ]

    assert scheduler._entry_signal_date("2026-08-12") is None
    assert "체결일이 오늘" in capsys.readouterr().out


def test_previous_day_signal_actually_produces_a_fill(mock_db, mock_settings):
    """회귀 핵심: 직전 거래일 신호를 넘기면 오늘 시가가 존재하므로 체결되어야 한다.
    (수정 전에는 여기서 _next_open()이 None이라 항상 False가 반환됐다)"""
    from executor import paper

    mock_db.fetchone.side_effect = [
        {"d": date(2026, 8, 11)},  # _entry_signal_date: 마지막 신호일
        {"d": date(2026, 8, 12)},  # _entry_signal_date: 체결일 = 오늘
        None, None,                # paper.buy: 중복 진입 가드 통과
        {"n": 0},                  # paper.buy: 슬롯 여유
        {"o": 70000},              # paper.buy: 신호일 다음 거래일(=오늘) 시가
    ]

    signal_date = scheduler._entry_signal_date("2026-08-12")
    assert signal_date == "2026-08-11"

    assert paper.buy("005930", "삼성전자", signal_date, 69000, 5.0, {},
                     "contrarian_v1") is True
    assert mock_db.execute.call_count == 2  # positions + trades


def test_prev_trading_day_is_last_bar_before_today(mock_db):
    """공시는 장중에도 나오므로 그날 시가로 체결하면 미래 참조다.
    직전 거래일 공시를 오늘 시가로 체결한다."""
    mock_db.fetchone.return_value = {"d": date(2026, 8, 14)}

    assert scheduler._prev_trading_day("2026-08-18") == "2026-08-14"


def test_prev_trading_day_none_when_no_bars(mock_db):
    mock_db.fetchone.return_value = {"d": None}

    assert scheduler._prev_trading_day("2026-08-18") is None
