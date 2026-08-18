"""
시가총액·PBR 계산 (shares + stock_daily + financials)

시점 정합성: 어떤 날짜 d에 알 수 있는 발행주식수는 그때까지 공시된 사업보고서
값이다. Y년 사업보고서는 Y+1년 3월에 나오므로, d가 4월 이후면 (d.year-1)Q4를,
1~3월이면 (d.year-2)Q4를 쓴다.

신뢰도: DART 주식의총수는 제출사가 단위를 틀리게 적는 경우가 있다(10·1,000·
1,000,000배). 감자·병합과 구분이 안 되는 경우가 많아 값을 고치지 않고, 같은 종목의
다른 기간 중앙값에서 크게 벗어난 기간은 '모름'으로 버린다. 잘못 고쳐 조용히 틀린
값을 쓰는 것보다 안 쓰는 편이 낫다.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from datetime import date
import db.connection as db

OUTLIER_RATIO = 5.0    # 같은 종목 중앙값 대비 이 배수를 넘으면 버린다
PBR_SANITY_MAX = 50.0  # 자본총계 대비 시총이 이보다 크면 주식수를 못 믿는다


def available_period(d: str | date) -> str:
    """날짜 d 시점에 이미 공시된 최신 사업보고서 기간."""
    day = date.fromisoformat(d) if isinstance(d, str) else d
    year = day.year - 1 if day.month >= 4 else day.year - 2
    return f"{year}Q4"


def _median(values: list[float]) -> float:
    s = sorted(values)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2


def reliable_shares(codes: list[str], period: str) -> dict:
    """code -> 발행주식수. 같은 종목의 기간별 중앙값에서 크게 벗어나면 뺀다."""
    if not codes:
        return {}

    rows = db.fetchall(
        "SELECT code, period, issued FROM shares WHERE code = ANY(%s)", (list(codes),)
    )
    by_code: dict = {}
    for r in rows:
        by_code.setdefault(r["code"], {})[r["period"]] = float(r["issued"])

    out = {}
    for code, periods in by_code.items():
        issued = periods.get(period)
        if not issued or issued <= 0:
            continue
        med = _median(list(periods.values()))
        if med > 0 and (issued / med > OUTLIER_RATIO or med / issued > OUTLIER_RATIO):
            continue    # 단위 오류로 의심 — 이 종목은 밸류에이션을 쓰지 않는다
        out[code] = issued
    return out


def _equity(codes: list[str], period: str) -> dict:
    rows = db.fetchall(
        "SELECT code, equity FROM financials WHERE code = ANY(%s) AND period = %s",
        (list(codes), period),
    )
    return {r["code"]: float(r["equity"]) for r in rows if r["equity"] is not None}


def market_caps(codes: list[str], d: str) -> dict:
    """날짜 d의 종가 × 그 시점에 공시돼 있던 발행주식수.

    자본총계 대비 시총이 터무니없으면 뺀다. 중앙값 검사는 같은 종목의 여러 기간이
    함께 틀린 경우를 못 잡는데(조선내화는 3개 기간 중 2개가 1,000배였다),
    자본총계는 별도 API에서 온 값이라 그 오류와 같이 틀리지 않는다.
    """
    period = available_period(d)
    shares = reliable_shares(codes, period)
    if not shares:
        return {}

    prices = db.fetchall(
        "SELECT code, c FROM stock_daily WHERE d = %s::date AND code = ANY(%s)",
        (d, list(shares)),
    )
    caps = {r["code"]: float(r["c"]) * shares[r["code"]] for r in prices}

    equity = _equity(list(caps), period)
    return {
        code: cap for code, cap in caps.items()
        if code not in equity or equity[code] <= 0
        or cap / equity[code] <= PBR_SANITY_MAX
    }


def pbr(codes: list[str], d: str) -> dict:
    """code -> PBR (시가총액 / 자본총계). 자본잠식이면 뺀다."""
    caps = market_caps(codes, d)
    if not caps:
        return {}

    equity = _equity(list(caps), available_period(d))
    return {code: caps[code] / eq for code, eq in equity.items()
            if eq > 0 and code in caps}
