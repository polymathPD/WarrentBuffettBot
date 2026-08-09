"""
이벤트 드리븐 백테스트 엔진
입력: 신호 테이블(contrarian_signals) + 일봉(stock_daily)
출력: 매매 기록 리스트
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import numpy as np
from datetime import date
import db.connection as db
from backtester.cost_model import buy_price, sell_price, net_return
import config


def run(start_date: str, end_date: str, max_hold: int = None,
        stop_pct: float = None, heat_avoid: float = None,
        heat_sell: float = None) -> list[dict]:
    """
    start_date ~ end_date 구간에서 역발상 전략 백테스트.
    반환: [{"code", "entry_date", "exit_date", "entry_px", "exit_px",
             "exit_reason", "net_ret"}, ...]
    """
    mh = max_hold or config.MAX_HOLD_DAYS
    sp = stop_pct or config.STOP_PCT
    ha = heat_avoid or config.HEAT_AVOID
    hs = heat_sell or config.HEAT_SELL

    # 신호 + 일봉 JOIN (entry_date = 신호일 다음 거래일의 시가)
    signals = db.fetchall(
        """SELECT cs.code, cs.d AS signal_date, cs.heat_score, cs.signal,
                  next_day.d AS entry_date, next_day.o AS entry_open
           FROM contrarian_signals cs
           CROSS JOIN LATERAL (
               SELECT d, o FROM stock_daily
               WHERE code = cs.code AND d > cs.d
               ORDER BY d LIMIT 1
           ) next_day
           WHERE cs.d >= %s::date AND cs.d <= %s::date
             AND cs.heat_score < %s
             AND cs.signal = 'neutral'
           ORDER BY cs.d, cs.code""",
        (start_date, end_date, ha),
    )

    trades = []
    active: dict[str, object] = {}  # code → 보유 중 포지션의 청산일

    for sig in signals:
        code = sig["code"]
        entry_date = sig["entry_date"]
        entry_open = float(sig["entry_open"])
        entry_px = buy_price(entry_open)
        stop_px = entry_px * (1 - sp)

        if code in active and sig["signal_date"] <= active[code]:
            continue  # 이미 보유 중

        # 진입 이후 봉을 순서대로 확인
        future = db.fetchall(
            "SELECT d, o, h, l, c FROM stock_daily WHERE code=%s AND d >= %s ORDER BY d LIMIT %s",
            (code, entry_date, mh + 1),
        )
        if len(future) < 2:
            continue

        # 진입 봉 (d == entry_date) 은 future[0]
        exit_date = None
        exit_px = None
        reason = None

        for i, row in enumerate(future[1:], 1):  # 진입 다음 봉부터 감시
            lo = float(row["l"])
            hi = float(row["h"])
            close = float(row["c"])
            days_held = i

            # 손절 확인 (비관적: 저가가 손절선 아래면 min(손절가, 시가)에 체결)
            if lo <= stop_px:
                exit_px = sell_price(min(stop_px, float(row["o"])))
                exit_date = row["d"]
                reason = "stop"
                break

            # 과열 신호 발생 시 청산
            sig_row = db.fetchone(
                "SELECT heat_score FROM contrarian_signals WHERE code=%s AND d=%s",
                (code, row["d"]),
            )
            if sig_row and float(sig_row["heat_score"]) >= hs:
                exit_px = sell_price(close)
                exit_date = row["d"]
                reason = "heat_signal"
                break

            # 보유 기한 도달
            if days_held >= mh:
                exit_px = sell_price(close)
                exit_date = row["d"]
                reason = "expiry"
                break

        if exit_date and exit_px:
            active[code] = exit_date
            trades.append({
                "code": code,
                "entry_date": str(future[0]["d"]),
                "exit_date": str(exit_date),
                "entry_px": entry_px,
                "exit_px": exit_px,
                "exit_reason": reason,
                "net_ret": net_return(entry_open, exit_px / (1 - config.SLIP_BPS / 10000 - config.FEE_BPS / 10000 - config.TAX_BPS / 10000)),
            })

    return trades


def print_summary(trades: list[dict]):
    if not trades:
        print("매매 없음")
        return

    rets = np.array([t["net_ret"] for t in trades])
    n = len(rets)
    mean_r = rets.mean() * 100
    std_r = rets.std() * 100
    t_val = (rets.mean() / (rets.std() / np.sqrt(n))) if rets.std() > 0 else 0
    win_rate = (rets > 0).mean() * 100

    print(f"백테스트 결과: {n}건")
    print(f"평균 수익률: {mean_r:+.2f}%  승률: {win_rate:.1f}%  t값: {t_val:.2f}")
    print(f"표준편차: {std_r:.2f}%")

    reasons = {}
    for t in trades:
        reasons[t["exit_reason"]] = reasons.get(t["exit_reason"], 0) + 1
    print("청산 사유:", reasons)


if __name__ == "__main__":
    import sys
    s = sys.argv[1] if len(sys.argv) > 1 else "2022-01-01"
    e = sys.argv[2] if len(sys.argv) > 2 else "2024-12-31"
    trades = run(s, e)
    print_summary(trades)
