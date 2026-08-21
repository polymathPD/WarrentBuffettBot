"""
모의 실행기: 실제 주문 없이 가상 체결, trades/positions 테이블에 기록
체결 모델: 다음 봉 시가 × (1 + 슬리피지)
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from datetime import date, timedelta
import db.connection as db
import config
from executor.sizing import position_qty
from executor.guard import already_entered

MODE = "paper"


def free_slots(strategy: str, mode: str = MODE) -> int:
    """전략별 남은 슬롯 수. 슬롯은 전략마다 따로 센다."""
    n = db.fetchone(
        "SELECT COUNT(*) AS n FROM positions WHERE mode=%s AND strategy=%s",
        (mode, strategy),
    )["n"]
    return config.get_setting("SLOTS") - int(n)


def _next_open(code: str, after_date: str) -> float | None:
    """after_date 다음 거래일의 시가 반환"""
    row = db.fetchone(
        "SELECT o FROM stock_daily WHERE code=%s AND d > %s::date ORDER BY d LIMIT 1",
        (code, after_date),
    )
    return float(row["o"]) if row else None


def buy(code: str, name: str, signal_date: str, close_px: float,
        heat_score: float, agents_summary: dict, strategy: str,
        fill_px: float | None = None, stop_pct: float | None = None,
        max_hold_days: int | None = None) -> bool:
    """
    signal_date 기준 다음 거래일 시가로 매수.
    슬롯 여유 확인 후 positions에 등록. 슬롯은 전략별로 센다.

    fill_px: 체결가를 직접 넘길 때만 쓴다(장중 실행). 기본은 None이고, 그때는
    stock_daily에서 signal_date 다음 봉의 시가를 읽는다. 일봉은 장 마감 후에야
    들어오므로 장중에 돌리면 그 조회가 None이 되어 매수가 전부 거부된다 —
    2026-08-12에 모의매매가 한 건도 체결되지 않았던 원인이다. 시가 자체는 09:00에
    확정되므로, 장중에 그 값을 따로 받아 여기로 넘기면 체결 규칙은 그대로 지켜진다.
    """
    dup = already_entered(code, strategy, MODE)
    if dup:
        print(f"[모의 매수 거부] {code} - {dup}")
        return False

    # 슬롯 확인
    if free_slots(strategy) <= 0:
        print(f"[모의 매수 거부] {code} — 슬롯 부족")
        return False

    if fill_px is None:
        fill_px = _next_open(code, signal_date)
    if fill_px is None:
        print(f"[모의 매수 거부] {code} — 다음 거래일 데이터 없음")
        return False

    entry_px = fill_px * (1 + config.SLIP_BPS / 10000) * (1 + config.FEE_BPS / 10000)
    # stop_pct=0은 '손절 없음'이다. 퀄리티 전략이 그렇다 — 검증도 손절 없이 했고,
    # 7% 손절은 20거래일 안에 52.2%가 걸려 상승 꼬리를 잘라낸다.
    if stop_pct is None:
        stop_pct = config.get_setting("STOP_PCT")
    # stop_pct=0은 '손절 없음'이다. entry_px * (1 - 0) = entry_px로 두면 손절선이
    # 진입가와 같아져 첫 하락에 즉시 청산된다 — 정반대 동작이다.
    stop_px = entry_px * (1 - stop_pct) if stop_pct > 0 else 0.0

    qty = position_qty(entry_px)
    if qty < 1:
        print(f"[모의 매수 거부] {code} — 1슬롯 금액으로 1주도 살 수 없음")
        return False
    amount = entry_px * qty

    db.execute(
        """INSERT INTO positions (code, strategy, name, entry_date, entry_px, qty,
                                  stop_px, max_hold_days, mode)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (code, strategy) DO NOTHING""",
        (code, strategy, name, date.today(), entry_px, qty,
         stop_px, max_hold_days if max_hold_days is not None
         else config.get_setting("MAX_HOLD_DAYS"), MODE),
    )
    db.execute(
        """INSERT INTO trades (mode, side, code, name, qty, price, amount, strategy, agents)
           VALUES (%s,'buy',%s,%s,%s,%s,%s,%s,%s::jsonb)""",
        (MODE, code, name, qty, entry_px, amount,
         strategy, str(agents_summary).replace("'", '"')),
    )
    print(f"[모의 매수] {code} {name}  진입가={entry_px:,.0f}  손절={stop_px:,.0f}")
    return True


def sell(code: str, name: str, qty: float, entry_px: float,
         close_px: float, reason: str, strategy: str,
         raw_px: float | None = None) -> None:
    """보유 포지션 청산.

    raw_px: 체결 기준가를 직접 넘길 때만 쓴다. 기본은 close_px(당일 종가)인데,
    퀄리티 전략은 백테스트가 리밸런싱일 '시가'로 팔므로 그 값을 넘겨야 한다.
    검증한 규칙과 운용 규칙이 갈라지면 백테스트 수치가 운용을 설명하지 못한다.
    """
    base = raw_px if raw_px is not None else close_px
    fill_px = base * (1 - config.SLIP_BPS / 10000) * (1 - config.FEE_BPS / 10000 - config.TAX_BPS / 10000)
    realized_pct = fill_px / entry_px - 1

    db.execute(
        """UPDATE trades SET exit_reason=%s, realized_pct=%s
           WHERE ctid = (
               SELECT ctid FROM trades
               WHERE code=%s AND side='buy' AND mode=%s AND strategy=%s
                 AND exit_reason IS NULL
               ORDER BY ts DESC LIMIT 1
           )""",
        (reason, realized_pct, code, MODE, strategy),
    )
    db.execute(
        """INSERT INTO trades (mode, side, code, name, qty, price, amount, strategy, exit_reason, realized_pct)
           VALUES (%s,'sell',%s,%s,%s,%s,%s,%s,%s,%s)""",
        (MODE, code, name, qty, fill_px, fill_px * qty, strategy, reason, realized_pct),
    )
    db.execute(
        "DELETE FROM positions WHERE code=%s AND mode=%s AND strategy=%s",
        (code, MODE, strategy),
    )
    pnl = "+" if realized_pct >= 0 else ""
    print(f"[모의 청산] {code} {name}  {pnl}{realized_pct*100:.2f}%  사유={reason}")


def current_equity(strategy: str, prices: dict, mode: str = MODE) -> float:
    """전략의 현재 총자산 = 현금 + 보유 평가액.

    리밸런싱 목표 금액을 여기서 뽑는다. config의 CAPITAL로 계산하면 자산이 늘어도
    투입액이 고정돼 현금 비중이 저절로 커진다(복리로 굴러가지 않는다).
    """
    from recorder.equity import cash_by_key

    cash = cash_by_key().get((mode, strategy))
    if cash is None:
        from recorder.equity import capital_for
        cash = capital_for(strategy)
    held = db.fetchall(
        "SELECT code, qty FROM positions WHERE strategy=%s AND mode=%s",
        (strategy, mode),
    )
    value = sum(float(r["qty"]) * prices[r["code"]]
                for r in held if r["code"] in prices)
    return float(cash) + value


def adjust(code: str, name: str, target_qty: int, fill_px: float,
           strategy: str, agents_summary: dict | None = None,
           mode: str = MODE) -> None:
    """보유 수량을 target_qty로 맞춘다. 차이나는 만큼만 사고판다.

    동일가중 리밸런싱에는 이게 필요하다. 전량 매도 후 재매수로 구현하면 계속
    편입되는 종목까지 매달 왕복 비용을 내는데, 낮은 회전율이 이 전략의 근거다
    (검증도 회전분에만 비용을 매겼다).

    빠진 종목은 target_qty=0으로 부르면 된다.
    """
    pos = db.fetchone(
        "SELECT qty, entry_px FROM positions WHERE code=%s AND strategy=%s AND mode=%s",
        (code, strategy, mode),
    )
    cur = float(pos["qty"]) if pos else 0.0
    entry_px = float(pos["entry_px"]) if pos else 0.0
    delta = target_qty - cur
    if delta == 0:
        return

    if delta > 0:
        px = fill_px * (1 + config.SLIP_BPS / 10000) * (1 + config.FEE_BPS / 10000)
        new_qty = cur + delta
        # 진입가는 가중평균으로 갱신한다. 그래야 부분 청산의 실현손익이 맞는다.
        new_entry = (cur * entry_px + delta * px) / new_qty if cur else px
        if pos:
            db.execute(
                "UPDATE positions SET qty=%s, entry_px=%s WHERE code=%s AND strategy=%s AND mode=%s",
                (new_qty, new_entry, code, strategy, mode))
        else:
            db.execute(
                """INSERT INTO positions (code, strategy, name, entry_date, entry_px,
                                          qty, stop_px, max_hold_days, mode)
                   VALUES (%s,%s,%s,%s,%s,%s,0,99999,%s)
                   ON CONFLICT (code, strategy) DO NOTHING""",
                (code, strategy, name, date.today(), new_entry, new_qty, mode))
        db.execute(
            """INSERT INTO trades (mode, side, code, name, qty, price, amount, strategy, agents)
               VALUES (%s,'buy',%s,%s,%s,%s,%s,%s,%s::jsonb)""",
            (mode, code, name, delta, px, px * delta, strategy,
             str(agents_summary or {}).replace("'", '"')))
        print(f"[리밸런싱 매수] {code} {name}  +{delta:.0f}주 @ {px:,.0f}")
        return

    qty_out = -delta
    px = fill_px * (1 - config.SLIP_BPS / 10000) * (
        1 - config.FEE_BPS / 10000 - config.TAX_BPS / 10000)
    realized = px / entry_px - 1 if entry_px else 0.0
    db.execute(
        """INSERT INTO trades (mode, side, code, name, qty, price, amount, strategy,
                               exit_reason, realized_pct)
           VALUES (%s,'sell',%s,%s,%s,%s,%s,%s,'rebalance',%s)""",
        (mode, code, name, qty_out, px, px * qty_out, strategy, realized))
    if target_qty <= 0:
        db.execute("DELETE FROM positions WHERE code=%s AND strategy=%s AND mode=%s",
                   (code, strategy, mode))
    else:
        db.execute("UPDATE positions SET qty=%s WHERE code=%s AND strategy=%s AND mode=%s",
                   (target_qty, code, strategy, mode))
    sign = "+" if realized >= 0 else ""
    print(f"[리밸런싱 매도] {code} {name}  -{qty_out:.0f}주 @ {px:,.0f}  {sign}{realized*100:.2f}%")
