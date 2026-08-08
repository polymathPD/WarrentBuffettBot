"""
DB의 stock_daily를 읽어 지표를 계산하고 numpy 배열로 반환.
look-ahead 방지: 모든 지표는 t-1까지의 데이터만 사용.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import numpy as np
import db.connection as db


def load_ohlcv(code: str, start: str = None, end: str = None) -> dict:
    """종목 OHLCV를 날짜 오름차순 numpy 배열로 반환"""
    where = "WHERE code = %s"
    params = [code]
    if start:
        where += " AND d >= %s::date"
        params.append(start)
    if end:
        where += " AND d <= %s::date"
        params.append(end)

    rows = db.fetchall(
        f"SELECT d, o, h, l, c, v FROM stock_daily {where} ORDER BY d",
        params,
    )
    if not rows:
        return {}

    dates = np.array([r["d"] for r in rows])
    o = np.array([float(r["o"]) for r in rows])
    h = np.array([float(r["h"]) for r in rows])
    l = np.array([float(r["l"]) for r in rows])
    c = np.array([float(r["c"]) for r in rows])
    v = np.array([float(r["v"]) for r in rows])

    return {"dates": dates, "o": o, "h": h, "l": l, "c": c, "v": v}


def sma(arr: np.ndarray, n: int) -> np.ndarray:
    out = np.full_like(arr, np.nan)
    for i in range(n - 1, len(arr)):
        out[i] = arr[i - n + 1 : i + 1].mean()
    return out


def rolling_std(arr: np.ndarray, n: int) -> np.ndarray:
    out = np.full_like(arr, np.nan)
    for i in range(n - 1, len(arr)):
        out[i] = arr[i - n + 1 : i + 1].std()
    return out


def volume_ratio(v: np.ndarray, n: int = 30) -> np.ndarray:
    """거래량 배율: 오늘 거래량 / 과거 n일 평균 (오늘 제외)"""
    avg = sma(v, n)
    # 오늘 포함 평균을 쓰면 look-ahead — 전날까지 평균 사용
    avg_prev = np.roll(avg, 1)
    avg_prev[0] = np.nan
    return np.where(avg_prev > 0, v / avg_prev, np.nan)


def pos_52w(h: np.ndarray, l: np.ndarray, c: np.ndarray, n: int = 252) -> np.ndarray:
    """52주 위치 (0=바닥, 1=천장). 당일 고가/저가 미포함(look-ahead 방지)"""
    out = np.full_like(c, np.nan)
    for i in range(n, len(c)):
        window_h = h[i - n : i]  # i 포함 안 함
        window_l = l[i - n : i]
        hi = window_h.max()
        lo = window_l.min()
        if hi > lo:
            out[i] = (c[i] - lo) / (hi - lo)
    return out


def returns(c: np.ndarray) -> np.ndarray:
    """일별 수익률"""
    r = np.empty_like(c)
    r[0] = np.nan
    r[1:] = c[1:] / c[:-1] - 1
    return r


def compute_all(code: str, start: str = None, end: str = None) -> dict:
    data = load_ohlcv(code, start, end)
    if not data:
        return {}

    c = data["c"]
    v = data["v"]
    amount = data["c"] * data["v"]  # 거래대금 = 종가 × 거래량 (근사)

    data["ret"] = returns(c)
    data["sma20"] = sma(c, 20)
    data["sma60"] = sma(c, 60)
    data["vol20"] = rolling_std(data["ret"], 20)
    data["vr30"] = volume_ratio(v, 30)
    data["ar30"] = volume_ratio(amount, 30)   # 거래대금 배율
    data["pos52w"] = pos_52w(data["h"], data["l"], c, 252)
    return data
