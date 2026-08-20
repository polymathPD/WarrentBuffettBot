"""
역발상 전략 규칙
- 진입 조건: 52주 위치가 하위 30% 이하(저평가 영역)인 종목을
             RANK_COL 오름차순으로 정렬해 슬롯만큼 진입
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

# 정렬 기준. contrarian_signals의 컬럼명이어야 한다.
#
# heat_score에서 institution_flow_ratio로 바꿨다(2026-08-20). heat 랭킹은 훈련·검증
# 양쪽에서 음수였고, 방향을 뒤집어도 같았다(-1.46% / -1.45%) — 정렬 기준으로서 정보가
# 없다는 뜻이다. 반면 institution_flow_ratio 오름차순은 워크포워드 네 창에서 모두
# 선택됐고 네 창 모두 고정 대조군을 이겼다(합계 +1.77% vs -0.45%).
#
# 다만 t +1.82로 유의 기준(2)에 못 미치고, 후보 팩터를 이미 훑어본 뒤에 설계한
# 실험이다. 채택은 "유망하지만 미검증"이라는 전제 위에 있고, 진짜 시험은
# 2026-08-20 이후 데이터다. research/README.md 참고.
#
# 오름차순 = 기관 순매수 배율이 낮은(크게 순매도한) 종목부터.
RANK_COL = "institution_flow_ratio"


def get_entry_candidates(target_date: str = None,
                         apply_marcap: bool = True) -> list[dict]:
    """
    당일 진입 후보 종목 반환.
    조건: 52주 위치 <= 30% AND 포지션 없음 AND RANK_COL 값 존재
          AND 보통주 AND 거래대금·시가총액 하한 통과
          AND heat_score < HEAT_AVOID (과열 종목 안전장치)
    """
    d = target_date or date.today().strftime("%Y-%m-%d")

    # 이미 보유 중인 종목 제외 (이 전략의 포지션만)
    held = {
        r["code"]
        for r in db.fetchall("SELECT code FROM positions WHERE strategy=%s", (STRATEGY,))
    }

    # 유한값 조건은 NULL과 ±Infinity를 한 번에 끊는다. 배율은 분모가 0이면
    # 무한대가 되는데 NUMERIC은 그걸 그대로 담고 IS NOT NULL도 통과시켜서,
    # 오름차순 정렬의 1순위가 '데이터가 없는 종목'이 된다(전 구간 1,482행).
    # NaN도 'NaN < Infinity'가 거짓이라 함께 걸러진다.
    # 검증 경로는 non-finite를 NaN으로 바꿔 제외하므로 이렇게 맞춘다.
    #
    # NOT NULL은 정렬에 쓰는 컬럼에만 건다. 결측 종목이 섞이면 정렬 결과에
    # '값이 없어서 앞에 온 종목'이 생긴다. 반대로 랭킹에 쓰지도 않는 지표까지
    # 요구하면 후보가 근거 없이 줄어든다 — 2026-08-19 기준 3개를 모두 요구하면
    # 2,649종목이 1,943종목으로 깎였고, 검증(research/portfolio_backtest.py)은
    # 정렬 컬럼 하나만 요구한다. 검증한 규칙과 운용 규칙을 맞춘다.
    #
    # heat_score < HEAT_AVOID는 검증 경로에는 없는 안전장치다. 과열 종목을 사지
    # 않겠다는 뜻이고 실제로 거의 걸리지 않는다(2026-08-19 기준 27종목, 1%).
    rows = db.fetchall(
        f"""SELECT cs.code, cs.heat_score, cs.signal, cs.{RANK_COL} AS rank_value,
                   sd.c AS close_price
            FROM contrarian_signals cs
            JOIN stock_daily sd ON sd.code = cs.code AND sd.d = cs.d::date
            WHERE cs.d = %s::date
              AND cs.heat_score < %s
              AND cs.signal = 'neutral'
              AND cs.{RANK_COL} > '-Infinity'::numeric
              AND cs.{RANK_COL} < 'Infinity'::numeric
            ORDER BY cs.{RANK_COL} ASC""",
        (d, config.get_setting("HEAT_AVOID")),
    )

    # 일봉은 전 종목을 받으므로 여기서 잡주·비보통주를 거른다.
    #
    # 필터를 SQL의 LIMIT 뒤에 두면 안 된다: 앞쪽 50개만 뽑아 거르게 되고
    # 유니버스의 나머지는 영영 후보에 오르지 못한다. 여기서는 전부 받아 거른 뒤,
    # 52주 위치까지 통과한 것만 CANDIDATE_LIMIT개 모으고 끊는다.
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
                "rank_value": float(r["rank_value"]),
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
