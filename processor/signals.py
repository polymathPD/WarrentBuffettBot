"""
역발상 과열 신호 계산 → contrarian_signals
heat_score 0~10: 높을수록 과열(개인 쏠림, 신용 급증, 거래대금 급증)
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


def _heat(flow_r, credit_r, vol_r) -> tuple[float, str]:
    """세 신호를 합산해 heat_score와 signal을 반환"""
    score = 0.0
    count = 0

    # 개인 순매수 배율: 2.0 이상이면 과열 의심
    if not np.isnan(flow_r):
        score += _clamp((flow_r - 1.0) * 3.0, 0, 4.0)
        count += 1

    # 신용잔고 급증 배율
    if not np.isnan(credit_r):
        score += _clamp((credit_r - 1.0) * 3.0, 0, 3.0)
        count += 1

    # 거래대금 급증 배율
    if not np.isnan(vol_r):
        score += _clamp((vol_r - 1.5) * 2.0, 0, 3.0)
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

        # 개인 순매수
        flows = db.fetchall(
            "SELECT individual_net FROM investor_flow WHERE code=%s AND d <= %s::date "
            "ORDER BY d DESC LIMIT %s",
            (code, d, WINDOW + 1),
        )
        if len(flows) >= WINDOW + 1:
            ind = np.array([float(r["individual_net"]) for r in reversed(flows)])
            avg_abs = np.mean(np.abs(ind[:-1]))
            flow_r = ind[-1] / avg_abs if avg_abs > 0 else np.nan
        else:
            flow_r = np.nan

        # 신용잔고
        credits = db.fetchall(
            "SELECT credit_amt FROM credit_balance WHERE code=%s AND d <= %s::date "
            "ORDER BY d DESC LIMIT %s",
            (code, d, WINDOW + 1),
        )
        if len(credits) >= WINDOW + 1:
            cred = np.array([float(r["credit_amt"]) for r in reversed(credits)])
            avg_c = np.mean(cred[:-1])
            credit_r = cred[-1] / avg_c if avg_c > 0 else np.nan
        else:
            credit_r = np.nan

        heat, signal = _heat(flow_r, credit_r, vol_r)
        rows.append((code, d, flow_r if not np.isnan(flow_r) else None,
                     credit_r if not np.isnan(credit_r) else None,
                     vol_r if not np.isnan(vol_r) else None,
                     heat, signal))

    if rows:
        db.executemany(
            """INSERT INTO contrarian_signals
               (code, d, individual_flow_ratio, credit_surge_ratio,
                volume_ratio, heat_score, signal)
               VALUES %s ON CONFLICT (code, d) DO UPDATE
               SET heat_score = EXCLUDED.heat_score,
                   signal = EXCLUDED.signal""",
            rows,
        )

    avoid = sum(1 for r in rows if r[6] == "avoid")
    sell = sum(1 for r in rows if r[6] == "sell")
    print(f"{d}: {len(rows)}종목 신호 계산 완료  avoid={avoid}  sell={sell}")


if __name__ == "__main__":
    compute_for_date()
