"""backtester/cost_model.py - 순수 함수, mock 불필요"""
import pytest

from backtester.cost_model import buy_price, sell_price, net_return
import config


def test_buy_price_applies_slippage_and_fee():
    px = 10_000
    expected = px * (1 + config.SLIP_BPS / 10000) * (1 + config.FEE_BPS / 10000)
    assert buy_price(px) == pytest.approx(expected)
    assert buy_price(px) > px  # 매수는 항상 시장가보다 비싸게 체결


def test_sell_price_applies_slippage_fee_and_tax():
    px = 10_000
    expected = px * (1 - config.SLIP_BPS / 10000) * (1 - config.FEE_BPS / 10000 - config.TAX_BPS / 10000)
    assert sell_price(px) == pytest.approx(expected)
    assert sell_price(px) < px  # 매도는 항상 시장가보다 싸게 체결


def test_net_return_same_price_is_negative_due_to_costs():
    # 진입가와 청산가가 동일해도 슬리피지+수수료+세금 때문에 손실이어야 함
    assert net_return(10_000, 10_000) < 0


def test_net_return_zero_price_is_full_loss():
    assert net_return(10_000, 0) == pytest.approx(-1.0)


def test_net_return_matches_manual_formula():
    entry, exit_px = 50_000, 55_000
    expected = sell_price(exit_px) / buy_price(entry) - 1
    assert net_return(entry, exit_px) == pytest.approx(expected)
