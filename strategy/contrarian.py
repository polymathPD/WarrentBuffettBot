"""
역발상 전략 규칙
- 진입 조건: heat_score < HEAT_AVOID 이고, 52주 위치가 하위 30% 이하 (저평가 영역)
- 청산 조건: heat_score >= HEAT_SELL 또는 보유 기간 초과 또는 손절
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from datetime import date
import db.connection as db
import config

STRATEGY = "contrarian_v1"
POS52W_ENTRY = 0.30   # 52주 위치 30% 이하 (바닥권)


def get_entry_candidates(target_date: str = None) -> list[dict]:
    """
    당일 진입 후보 종목 반환.
    조건: heat_score < HEAT_AVOID AND 52주 위치 <= 30% AND 포지션 없음
    """
    d = target_date or date.today().strftime("%Y-%m-%d")

    # 이미 보유 중인 종목 제외
    held = {r["code"] for r in db.fetchall("SELECT code FROM positions")}

    rows = db.fetchall(
        """SELECT cs.code, cs.heat_score, cs.signal,
                  sd.c AS close_price
           FROM contrarian_signals cs
           JOIN stock_daily sd ON sd.code = cs.code AND sd.d = cs.d::date
           WHERE cs.d = %s::date
             AND cs.heat_score < %s
             AND cs.signal = 'neutral'
           ORDER BY cs.heat_score ASC
           LIMIT 50""",
        (d, config.get_setting("HEAT_AVOID")),
    )

    candidates = []
    for r in rows:
        if r["code"] in held:
            continue
        # 52주 위치 확인
        pos = db.fetchone(
            """SELECT (c - MIN(l) OVER (ORDER BY d ROWS BETWEEN 251 PRECEDING AND 1 PRECEDING))
                    / NULLIF(MAX(h) OVER (ORDER BY d ROWS BETWEEN 251 PRECEDING AND 1 PRECEDING)
                           - MIN(l) OVER (ORDER BY d ROWS BETWEEN 251 PRECEDING AND 1 PRECEDING), 0) AS pos52w
               FROM stock_daily
               WHERE code = %s AND d <= %s::date
               ORDER BY d DESC LIMIT 1""",
            (r["code"], d),
        )
        if pos and pos["pos52w"] is not None and float(pos["pos52w"]) <= POS52W_ENTRY:
            candidates.append({
                "code": r["code"],
                "close": float(r["close_price"]),
                "heat_score": float(r["heat_score"]),
                "pos52w": float(pos["pos52w"]),
            })

    return candidates


def get_exit_candidates(target_date: str = None) -> list[dict]:
    """
    청산 후보: 과열 신호 발생 또는 보유 기간 초과 포지션
    """
    d = target_date or date.today().strftime("%Y-%m-%d")

    rows = db.fetchall(
        """SELECT p.code, p.name, p.entry_date, p.entry_px, p.qty, p.stop_px,
                  p.max_hold_days, p.mode,
                  sd.c AS close_price,
                  COALESCE(cs.heat_score, 0) AS heat_score,
                  COALESCE(cs.signal, 'neutral') AS signal
           FROM positions p
           JOIN stock_daily sd ON sd.code = p.code AND sd.d = %s::date
           LEFT JOIN contrarian_signals cs ON cs.code = p.code AND cs.d = %s::date""",
        (d, d),
    )

    exits = []
    today = date.fromisoformat(d)
    for r in rows:
        reason = None
        close = float(r["close_price"])
        held_days = (today - r["entry_date"]).days

        if close <= float(r["stop_px"]):
            reason = "stop"
        elif held_days >= r["max_hold_days"]:
            reason = "expiry"
        elif float(r["heat_score"]) >= config.get_setting("HEAT_SELL"):
            reason = "heat_signal"

        if reason:
            exits.append({
                "code": r["code"],
                "name": r["name"],
                "close": close,
                "entry_px": float(r["entry_px"]),
                "qty": float(r["qty"]),
                "reason": reason,
                "mode": r["mode"],
            })

    return exits
