"""
포지션 수량 계산. 모의/실전 실행기가 같은 규칙을 쓴다.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import config


def position_qty(px: float) -> int:
    """1슬롯 = CAPITAL / SLOTS. 그 금액으로 살 수 있는 주식 수(내림)."""
    slot_amount = float(config.get_setting("CAPITAL")) / config.get_setting("SLOTS")
    return int(slot_amount // px)
