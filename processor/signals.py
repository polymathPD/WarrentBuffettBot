"""
역발상 과열 신호 계산 → contrarian_signals
heat_score -10~+10: 높을수록 과열, 낮을수록 소외(개인·신용·거래대금 모두 한산)

외국인/기관 순매수 배율과 신용잔고 비율(수준)도 함께 기록하지만 heat_score에는
반영하지 않는다 — 가중치를 정할 근거(수익률과의 관계)를 아직 측정하지 않았기 때문.
자세한 배경은 _heat() 주석 참고.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import numpy as np
from datetime import date
import db.connection as db
import config

WINDOW = 30  # 배율 계산 기준 기간


def _clamp(val, lo=0.0, hi=10.0):
    return max(lo, min(hi, val))


def _flow_ratio(individual_net: np.ndarray) -> np.ndarray:
    """개인 순매수 배율: 오늘 / 과거 30일 절대값 평균"""
    out = np.full_like(individual_net, np.nan, dtype=float)
    for i in range(WINDOW, len(individual_net)):
        window = individual_net[i - WINDOW : i]
        avg_abs = np.mean(np.abs(window))
        if avg_abs > 0:
            out[i] = individual_net[i] / avg_abs
    return out


def _surge_ratio(arr: np.ndarray) -> np.ndarray:
    """배율: 오늘 / 과거 30일 평균"""
    out = np.full_like(arr, np.nan, dtype=float)
    for i in range(WINDOW, len(arr)):
        avg = np.mean(arr[i - WINDOW : i])
        if avg > 0:
            out[i] = arr[i] / avg
    return out


def _f(x):
    """DB 삽입용 변환: 유한하지 않은 값 -> None, numpy 스칼라 -> 파이썬 float.

    np.float64를 그대로 psycopg2에 넘기면 numpy 2.0의 repr("np.float64(...)")이
    SQL에 박혀 INSERT가 실패하므로 반드시 네이티브 float로 캐스팅한다.

    NaN뿐 아니라 ±inf도 None으로 끊는다. 배율은 '오늘 / 과거 30일 평균'이라
    분모가 0이면 무한대가 나오는데, PostgreSQL의 NUMERIC은 Infinity를 그대로
    저장하고 IS NOT NULL도 통과시킨다. 그러면 오름차순 정렬에서 -Infinity가
    맨 앞을 차지해, 값이 없는 종목이 1순위 후보가 된다.
    백테스트(research/portfolio_backtest.py의 load)는 non-finite를 NaN으로
    바꿔 버리므로, 여기서 끊지 않으면 운용과 검증이 서로 다른 종목을 본다.
    """
    if x is None:
        return None
    return float(x) if np.isfinite(x) else None


def _abs_ratio(arr: np.ndarray) -> float:
    """오늘 값 / 과거 WINDOW일 절대값 평균. 분모가 0이면 NaN."""
    avg_abs = np.mean(np.abs(arr[:-1]))
    return arr[-1] / avg_abs if avg_abs > 0 else np.nan


def contributions(flow_r, credit_r, vol_r):
    """
    세 배율의 기여도. 스칼라와 numpy 배열을 모두 받는다.

    수식은 여기에만 둔다. backfill_signals.py가 같은 계산을 벡터화해서 돌리는데,
    그쪽에 수식을 복제해 두면 한쪽만 고쳤을 때 백필이 운용 신호를 다른 수식으로
    조용히 덮어쓴다 (2026-08-20에 실제로 그럴 뻔했다).

    각 항의 하한은 상한과 대칭이다(0이 아니다). 하한을 0에 두면 세 배율이 모두
    기준선 아래인 종목이 전부 정확히 0.0이 되는데, 그게 이 전략이 매수 대상으로
    삼는 바로 그 구간이다. 2026-08-18 기준 필터 통과 1,324종목 중 426종목이
    heat_score 0.0 동점이었고(전 구간 평균 66~74%), ORDER BY heat_score ASC가
    사실상 DB 행 순서로 종목을 골랐다.

    과열에서 팔고 비과열에서 사려면 자가 양쪽으로 뻗어야 한다. 상한 쪽 의미는
    그대로라 HEAT_AVOID(7.0) · HEAT_SELL(8.5) 임계값은 건드리지 않는다.

    - 개인 순매수 배율: 높으면 개인 쏠림, 낮으면 개인이 손 뗀 상태
    - 신용잔고 급증 배율: 낮을수록 빚내서 사는 사람이 없다
    - 거래대금 급증 배율: 낮을수록 그날 관심이 없었다. 유동성이 낮은 종목이
      상위로 오는 것은 아니다 — 이건 '평소 대비' 상대량이고 절대 유동성은
      strategy/filters.py의 거래대금 하한이 따로 막는다.
    """
    return (np.clip((flow_r - 1.0) * 3.0, -4.0, 4.0),
            np.clip((credit_r - 1.0) * 3.0, -3.0, 3.0),
            np.clip((vol_r - 1.5) * 2.0, -3.0, 3.0))


def _heat(flow_r, credit_r, vol_r) -> tuple[float, str]:
    """
    세 신호를 합산해 heat_score와 signal을 반환.

    외국인/기관 순매수는 여기 들어가지 않는다. 국내 수급은 개인/외국인/기관 합이
    대략 0이라 개인 순매수와 -(외국인+기관)의 상관계수가 0.997로, 별도 축으로
    더하면 같은 정보를 두 번 세게 된다. 다만 외국인 단독(0.76)과 기관 단독(0.59)은
    개인과의 상관이 낮아 독립 정보가 있으므로, compute_for_date()가 두 값을 각각
    기록해 둔다 — decile_analysis.py로 수익률과의 관계를 측정한 뒤 가중치를 정한다.

    신용잔고 비율(credit_ratio_level)도 마찬가지다. credit_surge_ratio가 '변화'라면
    이쪽은 '수준'이라 개념적으로 독립이지만, 아직 표본이 부족해 기록만 한다.
    """
    contrib_flow, contrib_credit, contrib_vol = contributions(flow_r, credit_r, vol_r)

    score = 0.0
    count = 0
    for value, contrib in ((flow_r, contrib_flow),
                           (credit_r, contrib_credit),
                           (vol_r, contrib_vol)):
        if not np.isnan(value):
            score += float(contrib)
            count += 1

    heat = score if count > 0 else np.nan
    if np.isnan(heat):
        signal = "neutral"
    elif heat >= config.HEAT_SELL:
        signal = "sell"
    elif heat >= config.HEAT_AVOID:
        signal = "avoid"
    else:
        signal = "neutral"

    return float(heat) if not np.isnan(heat) else 0.0, signal


def compute_for_date(target_date: str = None):
    """target_date(YYYY-MM-DD) 기준 전 종목 과열 점수 계산 후 저장"""
    d = target_date or date.today().strftime("%Y-%m-%d")

    codes = db.fetchall("SELECT DISTINCT code FROM stock_daily WHERE d = %s::date", (d,))
    codes = [r["code"] for r in codes]
    if not codes:
        print(f"{d}: stock_daily 데이터 없음")
        return

    rows = []
    for code in codes:
        # 일봉 (거래대금 배율용)
        daily = db.fetchall(
            "SELECT d, c, v FROM stock_daily WHERE code=%s AND d <= %s::date "
            "ORDER BY d DESC LIMIT %s",
            (code, d, WINDOW + 1),
        )
        if len(daily) < WINDOW + 1:
            continue
        amounts = np.array([float(r["c"]) * float(r["v"]) for r in reversed(daily)])
        vol_r = amounts[-1] / np.mean(amounts[:-1]) if np.mean(amounts[:-1]) > 0 else np.nan

        # 투자자별 순매수 (개인은 heat_score용, 외국인·기관은 관측용)
        flows = db.fetchall(
            "SELECT individual_net, foreign_net, institution_net FROM investor_flow "
            "WHERE code=%s AND d <= %s::date ORDER BY d DESC LIMIT %s",
            (code, d, WINDOW + 1),
        )
        if len(flows) >= WINDOW + 1:
            ordered = list(reversed(flows))
            flow_r = _abs_ratio(np.array([float(r["individual_net"]) for r in ordered]))
            foreign_r = _abs_ratio(np.array([float(r["foreign_net"]) for r in ordered]))
            inst_r = _abs_ratio(np.array([float(r["institution_net"]) for r in ordered]))
        else:
            flow_r = foreign_r = inst_r = np.nan

        # 신용잔고 (급증 배율은 heat_score용, 잔고 비율 수준은 관측용)
        credits = db.fetchall(
            "SELECT credit_amt, credit_ratio FROM credit_balance "
            "WHERE code=%s AND d <= %s::date ORDER BY d DESC LIMIT %s",
            (code, d, WINDOW + 1),
        )
        credit_lvl = np.nan
        if credits and credits[0]["credit_ratio"] is not None:
            credit_lvl = float(credits[0]["credit_ratio"])  # 결제일 기준이라 며칠 지연됨
        if len(credits) >= WINDOW + 1:
            cred = np.array([float(r["credit_amt"]) for r in reversed(credits)])
            avg_c = np.mean(cred[:-1])
            credit_r = cred[-1] / avg_c if avg_c > 0 else np.nan
        else:
            credit_r = np.nan

        heat, signal = _heat(flow_r, credit_r, vol_r)
        rows.append((code, d, _f(flow_r), _f(credit_r), _f(vol_r),
                     _f(foreign_r), _f(inst_r), _f(credit_lvl),
                     heat, signal))

    if rows:
        db.executemany(
            """INSERT INTO contrarian_signals
               (code, d, individual_flow_ratio, credit_surge_ratio,
                volume_ratio, foreign_flow_ratio, institution_flow_ratio,
                credit_ratio_level, heat_score, signal)
               VALUES %s ON CONFLICT (code, d) DO UPDATE
               SET heat_score = EXCLUDED.heat_score,
                   signal = EXCLUDED.signal,
                   individual_flow_ratio = EXCLUDED.individual_flow_ratio,
                   credit_surge_ratio = EXCLUDED.credit_surge_ratio,
                   volume_ratio = EXCLUDED.volume_ratio,
                   foreign_flow_ratio = EXCLUDED.foreign_flow_ratio,
                   institution_flow_ratio = EXCLUDED.institution_flow_ratio,
                   credit_ratio_level = EXCLUDED.credit_ratio_level""",
            rows,
        )

    # signal은 튜플 마지막 원소다. 고정 인덱스로 두면 컬럼 추가 시 조용히 어긋난다.
    avoid = sum(1 for r in rows if r[-1] == "avoid")
    sell = sum(1 for r in rows if r[-1] == "sell")
    print(f"{d}: {len(rows)}종목 신호 계산 완료  avoid={avoid}  sell={sell}")


if __name__ == "__main__":
    compute_for_date()
