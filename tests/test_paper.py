"""executor/paper.py - 모의 매수/매도. DB를 mock으로 대체."""
import pytest

import config
from executor import paper


def test_buy_rejected_when_slots_full(mock_db, mock_settings):
    mock_db.fetchone.return_value = {"n": config._DEFAULTS["SLOTS"]}  # 꽉 참

    result = paper.buy("005930", "삼성전자", "2024-01-15", 70000, 5.0, {}, "contrarian_v1")

    assert result is False
    mock_db.execute.assert_not_called()


def test_buy_rejected_when_code_has_no_bar_after_signal_date(mock_db, mock_settings):
    """상장폐지 등으로 신호일 이후 봉이 아예 없는 종목은 체결할 수 없다.

    주의: 이건 예외 케이스지, 일상 흐름이 아니다. 스케줄러가 '오늘' 신호를 넘겨서
    매번 이 경로로 빠지던 버그는 tests/test_scheduler.py가 막는다."""
    mock_db.fetchone.side_effect = [
        None, None,  # 중복 진입 가드 통과
        {"n": 0},    # 슬롯 확인
        None,        # 신호일 이후 봉 없음
    ]

    result = paper.buy("005930", "삼성전자", "2024-01-15", 70000, 5.0, {}, "contrarian_v1")

    assert result is False
    mock_db.execute.assert_not_called()


def test_buy_computes_entry_and_stop_price_correctly(mock_db, mock_settings):
    mock_db.fetchone.side_effect = [
        None, None,        # 중복 진입 가드 통과
        {"n": 0},          # 슬롯 확인
        {"o": 70000},      # 다음 거래일 시가
    ]

    result = paper.buy("005930", "삼성전자", "2024-01-15", 69000, 5.0, {}, "contrarian_v1")

    assert result is True
    expected_entry = 70000 * (1 + config.SLIP_BPS / 10000) * (1 + config.FEE_BPS / 10000)
    expected_stop = expected_entry * (1 - config._DEFAULTS["STOP_PCT"])

    assert mock_db.execute.call_count == 2  # positions insert + trades insert
    positions_call_args = mock_db.execute.call_args_list[0][0][1]
    assert positions_call_args[1] == "contrarian_v1"                # strategy
    assert positions_call_args[4] == pytest.approx(expected_entry)  # entry_px
    assert positions_call_args[6] == pytest.approx(expected_stop)   # stop_px


def test_buy_sizes_position_from_capital_and_slots(mock_db, mock_settings):
    """수량 = 1슬롯 금액(CAPITAL / SLOTS)으로 살 수 있는 주식 수(내림)."""
    mock_db.fetchone.side_effect = [
        None, None,        # 중복 진입 가드 통과
        {"n": 0},          # 슬롯 확인
        {"o": 70000},      # 다음 거래일 시가
    ]

    paper.buy("005930", "삼성전자", "2024-01-15", 69000, 5.0, {}, "contrarian_v1")

    entry_px = 70000 * (1 + config.SLIP_BPS / 10000) * (1 + config.FEE_BPS / 10000)
    expected_qty = int((config._DEFAULTS["CAPITAL"] / config._DEFAULTS["SLOTS"]) // entry_px)
    assert expected_qty > 1  # 1주 고정이 아님을 함께 고정

    positions_call_args = mock_db.execute.call_args_list[0][0][1]
    assert positions_call_args[5] == expected_qty  # qty


def test_buy_rejected_when_slot_cannot_afford_one_share(mock_db, mock_settings):
    mock_db.fetchone.side_effect = [
        None, None,
        {"n": 0},
        {"o": config._DEFAULTS["CAPITAL"]},   # 1슬롯 금액보다 비싼 주가
    ]

    result = paper.buy("005930", "삼성전자", "2024-01-15", 69000, 5.0, {}, "contrarian_v1")

    assert result is False
    mock_db.execute.assert_not_called()


def test_buy_counts_slots_per_strategy(mock_db, mock_settings):
    """슬롯은 전략별로 센다 — 다른 전략의 보유가 이 전략의 슬롯을 먹으면 안 된다."""
    mock_db.fetchone.side_effect = [
        None, None,        # 중복 진입 가드 통과
        {"n": 0},          # 슬롯 확인
        {"o": 70000},      # 다음 거래일 시가
    ]

    paper.buy("005930", "삼성전자", "2024-01-15", 69000, 5.0, {}, "fundamental_v1")

    # 0~1번은 중복 진입 가드, 2번이 슬롯 확인이다
    sql, params = mock_db.fetchone.call_args_list[2][0]
    assert "COUNT(*)" in sql and "FROM positions" in sql and "strategy=%s" in sql
    assert params == ("paper", "fundamental_v1")


def test_sell_computes_realized_pct_correctly(mock_db, mock_settings):
    entry_px = 70000.0
    close_px = 77000.0

    paper.sell("005930", "삼성전자", 1, entry_px, close_px, "heat_signal", "contrarian_v1")

    expected_fill = close_px * (1 - config.SLIP_BPS / 10000) * (1 - config.FEE_BPS / 10000 - config.TAX_BPS / 10000)
    expected_pct = expected_fill / entry_px - 1

    assert mock_db.execute.call_count == 3  # update trades + insert sell trade + delete position
    update_args = mock_db.execute.call_args_list[0][0][1]
    assert update_args[0] == "heat_signal"
    assert update_args[1] == pytest.approx(expected_pct)


def test_sell_loss_produces_negative_realized_pct(mock_db, mock_settings):
    paper.sell("005930", "삼성전자", 1, entry_px=70000.0, close_px=60000.0,
               reason="stop", strategy="contrarian_v1")

    update_args = mock_db.execute.call_args_list[0][0][1]
    realized_pct = update_args[1]
    assert realized_pct < 0


def test_buy_rejected_when_position_already_open(mock_db, mock_settings):
    """스케줄러가 같은 날 두 번 돌아도 매수 기록이 중복되면 안 된다."""
    mock_db.fetchone.side_effect = [{"x": 1}]      # 보유 중

    result = paper.buy("005930", "삼성전자", "2024-01-15", 69000, 5.0, {}, "contrarian_v1")

    assert result is False
    mock_db.execute.assert_not_called()


def test_buy_rejected_when_already_bought_today(mock_db, mock_settings):
    """같은 날 진입 후 청산됐어도 그날 다시 사지 않는다."""
    mock_db.fetchone.side_effect = [None, {"x": 1}]

    result = paper.buy("005930", "삼성전자", "2024-01-15", 69000, 5.0, {}, "contrarian_v1")

    assert result is False
    mock_db.execute.assert_not_called()
