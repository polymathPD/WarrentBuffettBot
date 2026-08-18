"""
펀더멘털 전략 시뮬레이션 코어.

research/fundamental_backtest.py(단일 구간)와 research/fundamental_walkforward.py
(워크포워드)가 같은 코드를 쓰도록 여기 둔다. 규칙이 두 곳에서 갈라지면 검증이
무의미해진다.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import numpy as np
import pandas as pd
import db.connection as db
from backtester.cost_model import buy_price, sell_price
from strategy.fundamental import MIN_HOLD_DAYS

MAX_HOLD = 20
STOP_PCT = 0.07


def load_prices(codes: list[str], start: str, end: str, tail_days: int = 90) -> dict:
    """code -> {d, o, h, l, c} ndarray. 청산이 end 이후에 나므로 뒤를 여유 있게 받는다."""
    stop = (pd.Timestamp(end) + pd.Timedelta(days=tail_days)).date()
    idx = {}
    for i in range(0, len(codes), 300):
        df = pd.DataFrame(db.fetchall(
            "SELECT code, d, o, h, l, c FROM stock_daily "
            "WHERE code = ANY(%s) AND d >= %s::date AND d <= %s::date ORDER BY code, d",
            (codes[i:i + 300], start, stop)))
        if df.empty:
            continue
        for col in ("o", "h", "l", "c"):
            df[col] = df[col].astype(float)
        df["d"] = pd.to_datetime(df["d"])
        for code, g in df.groupby("code", sort=False):
            g = g.sort_values("d")
            idx[code] = {k: g[k].to_numpy() for k in ("d", "o", "h", "l", "c")}
    return idx


def simulate(by_date: dict, px: dict, slots: int,
             max_hold: int = MAX_HOLD, stop_pct: float = STOP_PCT,
             start: str = None, end: str = None) -> list[dict]:
    """
    매일 슬롯 한도 안에서만 진입. 진입은 공시일 다음 거래일 시가,
    청산은 손절(비관적 체결) 또는 만기 종가. 최소 보유기간은 손절에만 예외.

    start/end를 주면 그 구간에 공시된 후보만 진입시킨다(청산은 그 뒤까지 이어진다).
    """
    all_dates = np.unique(np.concatenate([v["d"] for v in px.values()]))
    lo = pd.Timestamp(start) if start else None
    hi = pd.Timestamp(end) if end else None

    positions, trades = {}, []

    for i, day in enumerate(all_dates):
        for code in list(positions):
            p = positions[code]
            arr = px[code]
            j = np.searchsorted(arr["d"], day)
            if j >= len(arr["d"]) or arr["d"][j] != day:
                continue
            held = i - p["entry_i"]
            reason = None
            if arr["l"][j] <= p["stop_px"]:
                fill = float(sell_price(min(p["stop_px"], float(arr["o"][j]))))
                reason = "stop"
            elif held >= MIN_HOLD_DAYS and held >= max_hold:
                fill, reason = float(sell_price(float(arr["c"][j]))), "expiry"
            if reason:
                # numpy 스칼라를 그대로 넘기면 psycopg2가 SQL에 repr을 박는다
                trades.append({"code": code, "entry_d": p["entry_d"], "exit_d": day,
                               "entry_px": float(p["entry_px"]), "exit_px": float(fill),
                               "ret": float(fill / p["entry_px"] - 1),
                               "reason": reason, "held": int(held)})
                del positions[code]

        ts = pd.Timestamp(day)
        if (lo is not None and ts < lo) or (hi is not None and ts > hi):
            continue
        cands = by_date.get(ts.date())
        if not cands or i + 1 >= len(all_dates):
            continue

        nxt = all_dates[i + 1]
        for c in cands:
            if len(positions) >= slots:
                break
            code = c["code"]
            if code in positions or code not in px:
                continue
            arr = px[code]
            j = np.searchsorted(arr["d"], nxt)
            if j >= len(arr["d"]) or arr["d"][j] != nxt or arr["o"][j] <= 0:
                continue
            entry = float(buy_price(float(arr["o"][j])))
            positions[code] = {"entry_i": i + 1, "entry_px": entry,
                               "entry_d": nxt, "stop_px": entry * (1 - stop_pct)}
    return trades


def stats(trades: list[dict]) -> dict:
    if not trades:
        return {"n": 0}
    rets = np.array([t["ret"] for t in trades])
    n = len(rets)
    return {
        "n": n,
        "mean_pct": float(rets.mean() * 100),
        "std_pct": float(rets.std() * 100),
        "win_rate": float((rets > 0).mean() * 100),
        "t_val": float(rets.mean() / (rets.std() / np.sqrt(n))) if rets.std() > 0 else 0.0,
        "avg_held_days": float(np.mean([t["held"] for t in trades])),
        "reasons": pd.Series([t["reason"] for t in trades]).value_counts().to_dict(),
    }
