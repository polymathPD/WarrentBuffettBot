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


# --- _quality_rebalance_date -------------------------------------------------
#
# 체결 규칙이 '기준일 다음 거래일 시가'이므로, 기준일을 직전 거래일로 두면 체결일이
# 오늘이 된다. monthly면 오늘이 그 달의 첫 거래일일 때만 돈다.
# research/quality_backtest.rebalance_dates가 monthly와 같은 규칙을 쓴다 —
# 한쪽만 고치면 운용과 검증이 조용히 갈라진다.


def _db(mock_db, prev, every=None):
    """_prev_trading_day와 _rebalance_every가 같은 fetchone을 쓰므로 SQL로 가른다."""
    def _one(sql, params=None):
        if "REBALANCE_EVERY" in sql:
            return {"value": every} if every else None
        return {"d": prev}
    mock_db.fetchone.side_effect = _one


def test_rebalances_on_the_first_trading_day_of_the_month(mock_db):
    """직전 거래일이 지난달이면 오늘이 이 달의 첫 거래일이다."""
    _db(mock_db, date(2026, 8, 31))

    assert scheduler._quality_rebalance_date("2026-09-01") == "2026-08-31"


def test_does_not_rebalance_on_later_days_of_the_month(mock_db):
    """직전 거래일이 같은 달이면 첫 거래일이 이미 지났다."""
    _db(mock_db, date(2026, 9, 1))

    assert scheduler._quality_rebalance_date("2026-09-02") is None


def test_first_trading_day_is_found_across_a_year_boundary(mock_db):
    _db(mock_db, date(2026, 12, 30))

    assert scheduler._quality_rebalance_date("2027-01-04") == "2026-12-30"


def test_the_months_first_day_is_not_read_from_todays_bar(mock_db):
    """오늘 봉으로 판정하면 안 된다.

    리밸런싱은 10:30에 도는데 오늘 일봉은 16:10 배치가 넣는다. stock_daily에
    '이 달의 첫 거래일'을 물으면 오늘이 없어 매달 첫 거래일이 통째로 밀린다.
    직전 거래일 하나만 조회해야 한다.
    """
    _db(mock_db, date(2026, 8, 31))

    scheduler._quality_rebalance_date("2026-09-01")

    bars = [c for c in mock_db.fetchone.call_args_list if "stock_daily" in c[0][0]]
    assert len(bars) == 1
    assert "d < %s::date" in bars[0][0][0]


def test_no_rebalance_without_a_previous_trading_day(mock_db):
    _db(mock_db, None)

    assert scheduler._quality_rebalance_date("2026-09-01") is None


# --- 주기 설정 ---------------------------------------------------------------

def test_daily_setting_rebalances_on_any_trading_day(mock_db):
    """REBALANCE_EVERY=daily면 달력을 보지 않고 매 거래일 돈다."""
    _db(mock_db, date(2026, 9, 1), every="daily")

    assert scheduler._quality_rebalance_date("2026-09-02") == "2026-09-01"


def test_the_frequency_falls_back_to_monthly(mock_db):
    """설정이 없으면 백테스트가 검증한 주기로 돌아간다 — 회전 비용을 내는 쪽이
    기본값이 되면 안 된다."""
    _db(mock_db, date(2026, 9, 1))          # REBALANCE_EVERY 미설정

    assert scheduler._rebalance_every() == "monthly"
    assert scheduler._quality_rebalance_date("2026-09-02") is None
