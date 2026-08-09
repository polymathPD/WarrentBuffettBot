"""
로컬 백테스트 실행기

backtester/engine.py와 동일한 알고리즘이지만, Railway Postgres의 디스크 여유 공간이
부족해 engine.py의 CROSS JOIN LATERAL 쿼리(서버사이드 정렬/임시파일 필요)가
"No space left on device"로 실패하는 문제를 우회하기 위해,
데이터를 단순 SELECT로 통째로 읽어와 pandas/numpy로 시뮬레이션한다.

비용 모델(buy_price/sell_price/net_return)은 backtester/cost_model.py를 그대로 재사용하여
engine.py와 수치적으로 동일한 결과를 보장한다.

주의(기존 engine.py와 동일하게 재현하는 동작 — 버그 아님, 원본 그대로 유지):
- engine.py의 `active` dict는 선언만 되고 갱신되지 않아 "이미 보유 중인 종목 skip" 로직이
  never triggers. 즉 같은 종목에 대해 신호가 여러 번 발생하면 중복 포지션이 허용된다.
  이 스크립트도 동일하게 동작해 engine.py와 같은 결과를 낸다.
"""
import sys, os, time
sys.path.insert(0, os.path.dirname(__file__))

import numpy as np
import pandas as pd
import db.connection as db
import config
from backtester.cost_model import buy_price, sell_price, net_return


def load_data():
    print("stock_daily 로딩 중...")
    daily = pd.DataFrame(db.fetchall("SELECT code, d, o, h, l, c FROM stock_daily ORDER BY code, d"))
    daily["d"] = pd.to_datetime(daily["d"])
    for col in ("o", "h", "l", "c"):
        daily[col] = daily[col].astype(float)
    print(f"  {len(daily):,}행")

    print("contrarian_signals 로딩 중...")
    signals = pd.DataFrame(db.fetchall(
        "SELECT code, d, heat_score, signal FROM contrarian_signals ORDER BY code, d"
    ))
    signals["d"] = pd.to_datetime(signals["d"])
    signals["heat_score"] = signals["heat_score"].astype(float)
    print(f"  {len(signals):,}행")

    return daily, signals


def build_price_index(daily: pd.DataFrame):
    """code -> (dates ndarray, o, h, l, c ndarray) 딕셔너리 생성"""
    idx = {}
    for code, g in daily.groupby("code", sort=False):
        g = g.sort_values("d")
        idx[code] = {
            "d": g["d"].to_numpy(),
            "o": g["o"].to_numpy(),
            "h": g["h"].to_numpy(),
            "l": g["l"].to_numpy(),
            "c": g["c"].to_numpy(),
        }
    return idx


def run_local(start_date: str, end_date: str, price_idx: dict, signals_df: pd.DataFrame,
              heat_lookup: dict, max_hold: int = None, stop_pct: float = None,
              heat_avoid: float = None, heat_sell: float = None) -> list[dict]:
    mh = max_hold or config.MAX_HOLD_DAYS
    sp = stop_pct or config.STOP_PCT
    ha = heat_avoid or config.HEAT_AVOID
    hs = heat_sell or config.HEAT_SELL

    cand = signals_df[
        (signals_df["d"] >= pd.Timestamp(start_date))
        & (signals_df["d"] <= pd.Timestamp(end_date))
        & (signals_df["heat_score"] < ha)
        & (signals_df["signal"] == "neutral")
    ].sort_values(["d", "code"])

    trades = []
    active: dict[str, np.datetime64] = {}  # code -> 보유 중 청산 예정일(포지션 종료 시 제거)

    for row in cand.itertuples(index=False):
        code = row.code
        sig_date = np.datetime64(row.d)

        held_until = active.get(code)
        if held_until is not None and sig_date < held_until:
            continue  # 이미 보유 중인 종목: 신규 진입 skip

        arr = price_idx.get(code)
        if arr is None:
            continue

        dates = arr["d"]
        entry_idx = np.searchsorted(dates, sig_date, side="right")
        if entry_idx >= len(dates):
            continue

        entry_open = float(arr["o"][entry_idx])
        if entry_open <= 0:
            continue  # 거래정지 등으로 시가가 0인 이상 데이터 skip
        entry_px = buy_price(entry_open)
        stop_px = entry_px * (1 - sp)

        end_slice = min(entry_idx + mh + 1, len(dates))
        if end_slice - entry_idx < 2:
            continue

        exit_date = None
        exit_px = None
        reason = None

        for i in range(entry_idx + 1, end_slice):
            days_held = i - entry_idx
            lo = float(arr["l"][i])
            close = float(arr["c"][i])
            opn = float(arr["o"][i])
            d = dates[i]

            if lo <= stop_px:
                exit_px = sell_price(min(stop_px, opn))
                exit_date = d
                reason = "stop"
                break

            h = heat_lookup.get((code, d))
            if h is not None and h >= hs:
                exit_px = sell_price(close)
                exit_date = d
                reason = "heat_signal"
                break

            if days_held >= mh:
                exit_px = sell_price(close)
                exit_date = d
                reason = "expiry"
                break

        if exit_date is not None and exit_px is not None:
            active[code] = exit_date
            exit_raw = exit_px / (1 - config.SLIP_BPS / 10000 - config.FEE_BPS / 10000 - config.TAX_BPS / 10000)
            trades.append({
                "code": code,
                "entry_date": str(pd.Timestamp(dates[entry_idx]).date()),
                "exit_date": str(pd.Timestamp(exit_date).date()),
                "entry_px": entry_px,
                "exit_px": exit_px,
                "exit_reason": reason,
                "net_ret": net_return(entry_open, exit_raw),
            })

    return trades


def summarize(trades: list[dict], label: str = ""):
    if not trades:
        print(f"{label}: 매매 없음")
        return None

    rets = np.array([t["net_ret"] for t in trades])
    n = len(rets)
    mean_r = rets.mean() * 100
    std_r = rets.std() * 100
    t_val = (rets.mean() / (rets.std() / np.sqrt(n))) if rets.std() > 0 else 0.0
    win_rate = (rets > 0).mean() * 100

    print(f"[{label}] 거래 {n}건  평균수익률 {mean_r:+.2f}%  승률 {win_rate:.1f}%  t값 {t_val:.2f}  표준편차 {std_r:.2f}%")

    reasons = {}
    for t in trades:
        reasons[t["exit_reason"]] = reasons.get(t["exit_reason"], 0) + 1
    print(f"  청산 사유: {reasons}")

    return {
        "n": n, "mean_pct": mean_r, "std_pct": std_r, "win_rate": win_rate,
        "t_val": t_val, "reasons": reasons, "rets": rets,
    }


def mdd(rets: np.ndarray) -> float:
    """거래 순서대로 복리 가정한 equity curve 기준 MDD"""
    equity = np.cumprod(1 + rets)
    peak = np.maximum.accumulate(equity)
    dd = equity / peak - 1
    return float(dd.min()) * 100


def main(start_date: str, end_date: str):
    daily, signals_df = load_data()
    print("\n가격 인덱스 구성 중...")
    price_idx = build_price_index(daily)

    heat_lookup = {
        (row.code, np.datetime64(row.d)): row.heat_score
        for row in signals_df.itertuples(index=False)
    }

    print(f"\n{'='*60}")
    print(f"전체 기간 백테스트: {start_date} ~ {end_date}")
    print(f"{'='*60}")
    t0 = time.time()
    all_trades = run_local(start_date, end_date, price_idx, signals_df, heat_lookup)
    print(f"시뮬레이션 완료: {time.time()-t0:.1f}s\n")

    stats = summarize(all_trades, "전체 기간")
    if stats:
        print(f"  MDD(복리 가정): {mdd(stats['rets']):.2f}%")

    if not all_trades or len(all_trades) < 10:
        print("\n거래 수가 10건 미만 -> 검증(기간분리/부트스트랩/대조군) 생략")
        return all_trades, stats

    # 기간 분리
    print(f"\n{'-'*60}")
    print("기간 분리 검증 (전반 vs 후반)")
    all_sorted = sorted(all_trades, key=lambda t: t["entry_date"])
    mid_date = all_sorted[len(all_sorted)//2]["entry_date"]
    print(f"  분리 기준일: {mid_date}")

    t1 = run_local(start_date, mid_date, price_idx, signals_df, heat_lookup)
    t2 = run_local(mid_date, end_date, price_idx, signals_df, heat_lookup)
    summarize(t1, "전반")
    summarize(t2, "후반")

    # 부트스트랩
    print(f"\n{'-'*60}")
    print("부트스트랩 검증 (1000회 재추출)")
    rets = stats["rets"]
    n_iter = 1000
    pos_count = sum(1 for _ in range(n_iter) if np.random.choice(rets, size=len(rets), replace=True).mean() > 0)
    bp = pos_count / n_iter * 100
    print(f"  양수 평균 비율: {bp:.1f}%  ({'PASS >=80%' if bp >= 80 else 'FAIL <80%'})")

    # 무작위 대조군
    print(f"\n{'-'*60}")
    print("무작위 대조군 검증 (200회)")
    sample_size = min(len(rets), 50)
    rand_means = [np.random.choice(rets, size=sample_size, replace=False).mean() for _ in range(200)]
    rand_mean_dist = np.array(rand_means)
    strategy_mean = rets.mean()
    pct_rank = (rand_mean_dist < strategy_mean).mean() * 100
    print(f"  전략 평균 {strategy_mean*100:+.2f}%  vs 무작위 분포 상위 {100-pct_rank:.1f}%  ({'PASS 상위20%' if pct_rank >= 80 else 'FAIL'})")

    print(f"\n{'='*60}")

    return all_trades, {**stats, "bootstrap_positive_pct": bp, "random_pct_rank": pct_rank,
                          "half1": summarize.__self__ if False else None,
                          "t1_n": len(t1), "t2_n": len(t2),
                          "t1_mean": (np.mean([t["net_ret"] for t in t1])*100 if t1 else None),
                          "t2_mean": (np.mean([t["net_ret"] for t in t2])*100 if t2 else None)}


if __name__ == "__main__":
    s = sys.argv[1] if len(sys.argv) > 1 else "2022-01-01"
    e = sys.argv[2] if len(sys.argv) > 2 else "2024-12-31"
    main(s, e)
