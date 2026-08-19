"""
역발상 전략 규칙
- 진입 조건: heat_score < HEAT_AVOID 이고, 52주 위치가 하위 30% 이하 (저평가 영역)
             (단, heat_score의 3개 입력 지표가 모두 있는 종목만 — 결측을 저과열로
              오인하지 않기 위함. get_entry_candidates() 주석 참고)
             보통주·거래대금·시가총액 필터는 strategy/filters.py 참고
- 청산 조건: heat_score >= HEAT_SELL 또는 보유 기간 초과 또는 손절
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from datetime import date
import db.connection as db
import config
from strategy.filters import tradable

STRATEGY = "contrarian_v1"
POS52W_ENTRY = 0.30   # 52주 위치 30% 이하 (바닥권)
CANDIDATE_LIMIT = 50


def get_entry_candidates(target_date: str = None,
                         apply_marcap: bool = True) -> list[dict]:
    """
    당일 진입 후보 종목 반환.
    조건: heat_score < HEAT_AVOID AND 52주 위치 <= 30% AND 포지션 없음
          AND heat_score를 구성하는 3개 지표가 모두 존재
          AND 보통주 AND 거래대금·시가총액 하한 통과
    """
    d = target_date or date.today().strftime("%Y-%m-%d")

    # 이미 보유 중인 종목 제외 (이 전략의 포지션만)
    held = {
        r["code"]
        for r in db.fetchall("SELECT code FROM positions WHERE strategy=%s", (STRATEGY,))
    }

    # 3개 지표 NOT NULL 조건이 필요한 이유:
    # _heat()는 결측 지표를 0점으로 취급하고 합계만 반환하므로, 수급/신용 데이터가
    # 없는 종목일수록 heat_score가 낮아진다. 아래 ORDER BY heat_score ASC와 만나면
    # '데이터가 없는 종목'이 '가장 안 과열된 종목'으로 둔갑해 후보 최상위를 차지한다.
    # 지표 개수가 다른 점수끼리는 애초에 비교가 성립하지 않으므로 셋 다 있는 종목만 본다.
    rows = db.fetchall(
        """SELECT cs.code, cs.heat_score, cs.signal,
                  sd.c AS close_price
           FROM contrarian_signals cs
           JOIN stock_daily sd ON sd.code = cs.code AND sd.d = cs.d::date
           WHERE cs.d = %s::date
             AND cs.heat_score < %s
             AND cs.signal = 'neutral'
             AND cs.individual_flow_ratio IS NOT NULL
             AND cs.credit_surge_ratio IS NOT NULL
             AND cs.volume_ratio IS NOT NULL
           ORDER BY cs.heat_score ASC""",
        (d, config.get_setting("HEAT_AVOID")),
    )

    # 일봉은 전 종목을 받으므로 여기서 잡주·비보통주를 거른다.
    # heat_score가 0 근처에 몰려 정렬이 사실상 동점 처리라, 이 필터가 없으면
    # 종목코드 순으로 신주인수권증서까지 딸려 온다.
    #
    # 필터를 SQL의 LIMIT 뒤에 두면 안 된다: 동점 정렬이라 코드 앞쪽 50개만 뽑아
    # 거르게 되고, 유니버스의 나머지는 영영 후보에 오르지 못한다. 여기서는 전부
    # 받아 거른 뒤, 52주 위치까지 통과한 것만 CANDIDATE_LIMIT개 모으고 끊는다.
    allowed = tradable([r["code"] for r in rows], d, apply_marcap=apply_marcap)

    candidates = []
    for r in rows:
        if len(candidates) >= CANDIDATE_LIMIT:
            break
        if r["code"] in held or r["code"] not in allowed:
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

    보유기간은 거래일 기준으로 센다(달력일이 아니라 stock_daily 봉 수).
    백테스트(research/portfolio_backtest.py)가 거래일 인덱스 차이로 세므로,
    달력일로 세면 MAX_HOLD_DAYS=20이 검증에서는 20거래일(약 28달력일)인데
    운용에서는 20달력일(약 14거래일)이 되어 30% 일찍 팔린다. 검증한 규칙과
    운용 규칙이 갈라지면 백테스트 수치가 운용을 설명하지 못한다.
    """
    d = target_date or date.today().strftime("%Y-%m-%d")

    rows = db.fetchall(
        """SELECT p.code, p.name, p.entry_date, p.entry_px, p.qty, p.stop_px,
                  p.max_hold_days, p.mode,
                  sd.c AS close_price,
                  COALESCE(cs.heat_score, 0) AS heat_score,
                  COALESCE(cs.signal, 'neutral') AS signal,
                  (SELECT COUNT(*) FROM stock_daily h
                    WHERE h.code = p.code AND h.d > p.entry_date AND h.d <= %s::date)
                    AS held_days
           FROM positions p
           JOIN stock_daily sd ON sd.code = p.code AND sd.d = %s::date
           LEFT JOIN contrarian_signals cs ON cs.code = p.code AND cs.d = %s::date
           WHERE p.strategy = %s""",
        (d, d, d, STRATEGY),
    )

    exits = []
    for r in rows:
        reason = None
        close = float(r["close_price"])
        held_days = int(r["held_days"])

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
