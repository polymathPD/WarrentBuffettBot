"""
중복 진입 방지. 모의/실전 실행기가 공용으로 쓴다.

positions는 (code, strategy) 충돌 시 무시되지만 trades에는 그대로 한 줄이 더 들어간다.
그러면 매수금액이 두 번 잡혀 현금·자산 스냅샷이 틀어지므로 주문 전에 막는다.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import db.connection as db


def already_entered(code: str, strategy: str, mode: str) -> str | None:
    """이미 진입한 종목이면 거부 사유, 아니면 None."""
    if db.fetchone(
        "SELECT 1 AS x FROM positions WHERE code=%s AND strategy=%s AND mode=%s",
        (code, strategy, mode),
    ):
        return "이미 보유 중"

    # 같은 날 진입 후 청산된 뒤 스케줄러가 다시 도는 경우까지 막는다.
    if db.fetchone(
        """SELECT 1 AS x FROM trades
           WHERE code=%s AND strategy=%s AND mode=%s AND side='buy'
             AND ts::date = CURRENT_DATE""",
        (code, strategy, mode),
    ):
        return "오늘 이미 매수함"

    return None
