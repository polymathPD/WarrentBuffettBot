"""
두 전략이 공유하는 종목 필터 — "잡주는 사지 않는다"를 코드로 옮긴 것.

수집 단계(collector/universe.py)는 수급·신용을 받을 대상만 좁힌다. 일봉은 전 종목을
받으므로 진입 후보에는 소형주와 신주인수권증서 같은 비보통주가 그대로 올라온다.
그래서 진입 시점에 한 번 더 거른다.

셋 다 수익률과 무관하게 정해진 기준이다:
- 보통주만: 신주인수권증서·전환우선주 등은 애초에 매매 대상이 아니다
- 거래대금: 편도 0.2% 슬리피지 가정이 성립하는지를 직접 재는 값
- 시가총액: collector/universe.py와 같은 3,000억 기준
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import db.connection as db
from collector.universe import large_caps

MIN_TURNOVER = 1_000_000_000   # 최근 20거래일 평균 거래대금 10억원
TURNOVER_WINDOW = 20


def ordinary(codes) -> list:
    """보통주만. 신주인수권증서·전환우선주 등은 종목코드에 영문자가 섞인다."""
    return [c for c in codes if c.isdigit()]


def liquid(codes: list, d: str) -> set:
    """최근 TURNOVER_WINDOW 거래일 평균 거래대금이 기준 이상인 종목."""
    if not codes:
        return set()
    return {
        r["code"] for r in db.fetchall(
            """SELECT code FROM (
                   SELECT code, c * v AS turnover,
                          ROW_NUMBER() OVER (PARTITION BY code ORDER BY d DESC) AS rn
                   FROM stock_daily WHERE code = ANY(%s) AND d <= %s::date
               ) t
               WHERE rn <= %s
               GROUP BY code HAVING AVG(turnover) >= %s""",
            (list(codes), d, TURNOVER_WINDOW, MIN_TURNOVER),
        )
    }


def tradable(codes: list, d: str, apply_marcap: bool = True) -> set:
    """세 필터를 모두 통과한 종목.

    apply_marcap: 시가총액 하한 적용 여부. FDR은 현재 시총만 주므로 과거 시점에
    적용하면 상장폐지 종목이 통째로 빠진다 — 백테스트에서는 꺼야 한다.
    """
    codes = ordinary(codes)
    ok = liquid(codes, d)
    if apply_marcap:
        ok &= large_caps()
    return ok
