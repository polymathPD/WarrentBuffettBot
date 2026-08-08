"""
모의 실행기: 실제 주문 없이 가상 체결, trades/positions 테이블에 기록
체결 모델: 다음 봉 시가 × (1 + 슬리피지)
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from datetime import date, timedelta
import db.connection as db
import config

MODE = "paper"


def _next_open(code: str, after_date: str) -> float | None:
    """after_date 다음 거래일의 시가 반환"""
    row = db.fetchone(
        "SELECT o FROM stock_daily WHERE code=%s AND d > %s::date ORDER BY d LIMIT 1",
        (code, after_date),
    )
    return float(row["o"]) if row else None


def buy(code: str, name: str, signal_date: str, close_px: float,
        heat_score: float, agents_summary: dict) -> bool:
    """
    signal_date 기준 다음 거래일 시가로 매수.
    슬롯 여유 확인 후 positions에 등록.
    """
    # 슬롯 확인
    held = db.fetchone("SELECT COUNT(*) AS n FROM positions")
    if int(held["n"]) >= config.get_setting("SLOTS"):
        print(f"[모의 매수 거부] {code} — 슬롯 부족")
        return False

    fill_px = _next_open(code, signal_date)
    if fill_px is None:
        print(f"[모의 매수 거부] {code} — 다음 거래일 데이터 없음")
        return False

    entry_px = fill_px * (1 + config.SLIP_BPS / 10000) * (1 + config.FEE_BPS / 10000)
    stop_px = entry_px * (1 - config.get_setting("STOP_PCT"))

    # 1슬롯 = 전체 자금 / SLOTS (여기선 수량 1주로 고정, 금액은 기록용)
    qty = 1
    amount = entry_px * qty

    db.execute(
        """INSERT INTO positions (code, name, entry_date, entry_px, qty, stop_px, max_hold_days, mode)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (code) DO NOTHING""",
        (code, name, date.today(), entry_px, qty,
         stop_px, config.get_setting("MAX_HOLD_DAYS"), MODE),
    )
    db.execute(
        """INSERT INTO trades (mode, side, code, name, qty, price, amount, strategy, agents)
           VALUES (%s,'buy',%s,%s,%s,%s,%s,%s,%s::jsonb)""",
        (MODE, code, name, qty, entry_px, amount,
         "contrarian_v1", str(agents_summary).replace("'", '"')),
    )
    print(f"[모의 매수] {code} {name}  진입가={entry_px:,.0f}  손절={stop_px:,.0f}")
    return True


def sell(code: str, name: str, qty: float, entry_px: float,
         close_px: float, reason: str) -> None:
    """보유 포지션 청산"""
    fill_px = close_px * (1 - config.SLIP_BPS / 10000) * (1 - config.FEE_BPS / 10000 - config.TAX_BPS / 10000)
    realized_pct = fill_px / entry_px - 1

    db.execute(
        """UPDATE trades SET exit_reason=%s, realized_pct=%s
           WHERE code=%s AND side='buy' AND exit_reason IS NULL
           ORDER BY ts DESC LIMIT 1""",
        (reason, realized_pct, code),
    )
    db.execute(
        """INSERT INTO trades (mode, side, code, name, qty, price, amount, strategy, exit_reason, realized_pct)
           VALUES (%s,'sell',%s,%s,%s,%s,%s,'contrarian_v1',%s,%s)""",
        (MODE, code, name, qty, fill_px, fill_px * qty, reason, realized_pct),
    )
    db.execute("DELETE FROM positions WHERE code=%s", (code,))
    pnl = "+" if realized_pct >= 0 else ""
    print(f"[모의 청산] {code} {name}  {pnl}{realized_pct*100:.2f}%  사유={reason}")
