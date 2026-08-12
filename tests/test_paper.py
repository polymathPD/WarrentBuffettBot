"""executor/paper.py - 모의 매수/매도. DB를 mock으로 대체."""
import pytest

import config
from executor import paper


def test_buy_rejected_when_slots_full(mock_db, mock_settings):
    mock_db.fetchone.return_value = {"n": config._DEFAULTS["SLOTS"]}  # 꽉 참

    result = paper.buy("005930", "삼성전자", "2024-01-15", 70000, 5.0, {})

    assert result is False
    mock_db.execute.assert_not_called()


def test_buy_rejected_when_no_next_day_data(mock_db, mock_settings):
    mock_db.fetchone.side_effect = [
        {"n": 0},   # 슬롯 확인
        None,       # 다음 거래일 시가 없음
    ]

    result = paper.buy("005930", "삼성전자", "2024-01-15", 70000, 5.0, {})

    assert result is False
    mock_db.execute.assert_not_called()


def test_buy_computes_entry_and_stop_price_correctly(mock_db, mock_settings):
    mock_db.fetchone.side_effect = [
        {"n": 0},          # 슬롯 확인
        {"o": 70000},      # 다음 거래일 시가
    ]

    result = paper.buy("005930", "삼성전자", "2024-01-15", 69000, 5.0, {})

    assert result is True
    expected_entry = 70000 * (1 + config.SLIP_BPS / 10000) * (1 + config.FEE_BPS / 10000)
    expected_stop = expected_entry * (1 - config._DEFAULTS["STOP_PCT"])

    assert mock_db.execute.call_count == 2  # positions insert + trades insert
    positions_call_args = mock_db.execute.call_args_list[0][0][1]
    assert positions_call_args[3] == pytest.approx(expected_entry)  # entry_px
    assert positions_call_args[5] == pytest.approx(expected_stop)   # stop_px


def test_sell_computes_realized_pct_correctly(mock_db, mock_settings):
    entry_px = 70000.0
    close_px = 77000.0

    paper.sell("005930", "삼성전자", 1, entry_px, close_px, "heat_signal")

    expected_fill = close_px * (1 - config.SLIP_BPS / 10000) * (1 - config.FEE_BPS / 10000 - config.TAX_BPS / 10000)
    expected_pct = expected_fill / entry_px - 1

    assert mock_db.execute.call_count == 3  # update trades + insert sell trade + delete position
    update_args = mock_db.execute.call_args_list[0][0][1]
    assert update_args[0] == "heat_signal"
    assert update_args[1] == pytest.approx(expected_pct)


def test_sell_loss_produces_negative_realized_pct(mock_db, mock_settings):
    paper.sell("005930", "삼성전자", 1, entry_px=70000.0, close_px=60000.0, reason="stop")

    update_args = mock_db.execute.call_args_list[0][0][1]
    realized_pct = update_args[1]
    assert realized_pct < 0
