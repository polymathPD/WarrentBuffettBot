import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import config


def buy_price(px: float) -> float:
    """매수 실효 단가 (슬리피지 + 수수료)"""
    return px * (1 + config.SLIP_BPS / 10000) * (1 + config.FEE_BPS / 10000)


def sell_price(px: float) -> float:
    """매도 실효 수취 (슬리피지 + 수수료 + 거래세)"""
    return px * (1 - config.SLIP_BPS / 10000) * (1 - config.FEE_BPS / 10000 - config.TAX_BPS / 10000)


def net_return(entry_px: float, exit_px: float) -> float:
    """비용 반영 순수익률"""
    return sell_price(exit_px) / buy_price(entry_px) - 1
