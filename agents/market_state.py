"""시장 전체 상태 에이전트 (거부권 보유)"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import numpy as np
import db.connection as db
from agents.base import call

AGENT = "market_state"
MARKET_CODE = "069500"  # KODEX 200 ETF (시장 프록시)
CACHE_SCOPE = "market"  # 캐시는 종목이 아니라 시장 단위

# 시장 판단 규칙은 전략마다 반대 방향을 본다.
# 역발상은 공포에 사는 전략이라 하락이 넓게 퍼진 날을 진입 조건으로 삼는다.
# 펀더멘털은 개별 종목의 실적을 보고 사므로 시장 급락을 회피 조건으로 삼는다.
# 한 프롬프트를 공유하면 둘 중 하나는 반드시 반대로 걸린다.
_RULES = {
    "contrarian_v1": (
        "규칙: 하락 종목 비율 50% 미만이면 반드시 '관망'(거부권 발동).\n"
        "이 전략은 역발상이다 — 하락이 넓게 퍼진 날에 저평가 종목을 사는 것이 목적이므로,\n"
        "시장이 조용하거나 오르는 날은 진입 대상이 아니다.\n"
        "하락 비율이 50% 이상이면 공포 국면으로 보고 신규 매수 여부를 판단하라."
    ),
    "fundamental_v1": (
        "규칙: 하락 종목 비율 70% 이상이면 반드시 '청산'(거부권 발동).\n"
        "그 외는 시장 분위기를 종합해 신규 매수 여부를 판단하라."
    ),
}
DEFAULT_RULE = _RULES["fundamental_v1"]


def analyze(code: str, target_date: str, strategy: str) -> dict:
    # 시장 전체 하락 종목 비율
    total = db.fetchone(
        "SELECT COUNT(*) AS n FROM stock_daily WHERE d=%s::date", (target_date,)
    )
    falling = db.fetchone(
        """SELECT COUNT(*) AS n FROM stock_daily sd
           JOIN (SELECT code, c AS prev_c FROM stock_daily WHERE d = (
                   SELECT MAX(d) FROM stock_daily WHERE d < %s::date
                 )) prev ON prev.code = sd.code
           WHERE sd.d=%s::date AND sd.c < prev.prev_c""",
        (target_date, target_date),
    )

    total_n = int(total["n"]) if total else 0
    fall_n = int(falling["n"]) if falling else 0
    fall_pct = fall_n / total_n * 100 if total_n > 0 else 50.0

    # KODEX200 최근 20일 추세
    mkt = db.fetchall(
        "SELECT c FROM stock_daily WHERE code=%s AND d<=%s::date ORDER BY d DESC LIMIT 20",
        (MARKET_CODE, target_date),
    )
    if len(mkt) >= 20:
        prices = np.array([float(r["c"]) for r in reversed(mkt)])
        trend_pct = (prices[-1] / prices[0] - 1) * 100
    else:
        trend_pct = 0.0

    prompt = f"""너는 시장 전체 상태를 판단하는 전문가다. 거부권을 행사할 수 있다.

[기준일] {target_date}  [전략] {strategy}
[하락 종목 비율] {fall_pct:.1f}%
[KODEX200 20일 추세] {trend_pct:+.1f}%

{_RULES.get(strategy, DEFAULT_RULE)}
[형식] 결정: 매수|관망|청산 / 확신: 0~10 / 이유: 1문장"""

    # 이 프롬프트에는 종목코드가 없다 — 시장 전체 판단이라 종목마다 다시 물을 이유가
    # 없다. 캐시 키를 code로 잡으면 같은 질문을 후보 종목 수만큼 결제하게 된다.
    # 전략은 키에 넣는다 — 규칙이 다르므로 두 전략이 판단을 공유하면 안 된다.
    return call(AGENT, code, prompt, cache_scope=f"{CACHE_SCOPE}:{strategy}")
