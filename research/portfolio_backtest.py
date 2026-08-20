"""
슬롯 제약이 있는 포트폴리오 백테스트.

기존 backtester/engine.py는 신호가 뜨는 족족 진입시켜 3년간 14만 건을 만든다.
12슬롯으로는 물리적으로 불가능한 수치라 "신호 품질"은 재도 "포트폴리오 수익"은
재지 못한다. 여기서는 매일 슬롯 한도 안에서만 진입시켜 실현 가능한 수익을 낸다.

비용은 backtester/cost_model.py를 그대로 재사용한다 (왕복 약 0.63%).
"""
import os, sys, time
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

import numpy as np
import pandas as pd
import FinanceDataReader as fdr
import db.connection as db
from backtester.cost_model import buy_price, sell_price
from backtester.store import save_run

START = sys.argv[1] if len(sys.argv) > 1 else "2022-01-01"
END   = sys.argv[2] if len(sys.argv) > 2 else "2024-12-31"
WARMUP = (pd.Timestamp(START) - pd.Timedelta(days=400)).strftime("%Y-%m-%d")          # pos52w(252일) 워밍업
MIN_MARCAP = 300_000_000_000
CODE_BATCH = 300
METRICS = ["heat_score", "individual_flow_ratio", "credit_surge_ratio",
           "volume_ratio", "foreign_flow_ratio", "institution_flow_ratio"]


def universe():
    lst = fdr.StockListing("KRX")
    big = {c for c, m in zip(lst["Code"], lst["Marcap"]) if m and m >= MIN_MARCAP}
    have = {r["code"] for r in db.fetchall("SELECT DISTINCT code FROM contrarian_signals")}
    return sorted(big & have)


def load(codes):
    px_parts, sig_parts = [], []
    for b0 in range(0, len(codes), CODE_BATCH):
        b = codes[b0:b0 + CODE_BATCH]
        p = pd.DataFrame(db.fetchall(
            "SELECT code, d, o, h, l, c FROM stock_daily "
            "WHERE code = ANY(%s) AND d >= %s AND d <= %s ORDER BY code, d",
            (b, WARMUP, END)))
        if not p.empty:
            for col in ("o", "h", "l", "c"):
                p[col] = p[col].astype(float)
            px_parts.append(p)
        s = pd.DataFrame(db.fetchall(
            f"SELECT code, d, {', '.join(METRICS)} FROM contrarian_signals "
            "WHERE code = ANY(%s) AND d >= %s AND d <= %s ORDER BY code, d",
            (b, START, END)))
        if not s.empty:
            for m in METRICS:
                s[m] = pd.to_numeric(s[m], errors="coerce")
                s.loc[~np.isfinite(s[m]), m] = np.nan
            sig_parts.append(s)
    px = pd.concat(px_parts, ignore_index=True)
    sig = pd.concat(sig_parts, ignore_index=True)
    px["d"] = pd.to_datetime(px["d"])
    sig["d"] = pd.to_datetime(sig["d"])
    return px, sig


def add_pos52w(px):
    """52주 위치: (종가 - 252일 최저) / (252일 최고 - 252일 최저), 당일 제외"""
    out = []
    for code, g in px.groupby("code", sort=False):
        g = g.sort_values("d").reset_index(drop=True)
        lo = g["l"].shift(1).rolling(252, min_periods=60).min()
        hi = g["h"].shift(1).rolling(252, min_periods=60).max()
        rng = (hi - lo).replace(0, np.nan)
        g["pos52w"] = (g["c"] - lo) / rng
        out.append(g)
    return pd.concat(out, ignore_index=True)


def run_sim(px, sig, rank_col, ascending, slots, max_hold,
            stop_pct=0.07, use_pos52w=True, exit_rank_pct=None,
            exit_cols=(), start=None, end=None):
    """
    매일: (1) 보유 종목 청산 판정  (2) 빈 슬롯을 랭킹 상위 후보로 채움
    진입은 신호일 다음 거래일 시가, 청산은 당일 종가(손절은 비관적 체결).

    매매 목록만 돌려준다 — 출력도 저장도 하지 않는다. 워크포워드는 학습 구간마다
    변형 수만큼 이 함수를 부르므로(한 번 돌 때 수십 회) 그때마다 결과를 저장하면
    안 된다. simulate()가 이 위에서 통계·출력·저장을 얹는다.

    start/end: 진입 판정을 이 구간의 신호로만 한다(청산은 그 뒤로 이어진다).
    워크포워드가 같은 데이터로 학습 구간과 시험 구간을 나눠 돌리기 위한 것이다.
    """
    px = px.set_index(["code", "d"]).sort_index()
    dates = np.sort(sig["d"].unique())

    # 날짜별 횡단면 백분위 (청산 판정용)
    for c in exit_cols:
        sig[f"{c}_pct"] = sig.groupby("d")[c].rank(pct=True)

    sig_by_date = {d: g for d, g in sig.groupby("d")}
    all_dates = np.sort(px.index.get_level_values("d").unique())
    date_pos = {d: i for i, d in enumerate(all_dates)}

    entry_from = pd.Timestamp(start) if start else None
    entry_to = pd.Timestamp(end) if end else None

    positions = {}   # code -> dict(entry_i, entry_px, stop_px)
    trades = []

    for d in dates:
        i = date_pos.get(d)
        if i is None or i + 1 >= len(all_dates):
            continue
        nxt = all_dates[i + 1]

        # (1) 청산 판정 (당일 종가 기준)
        today = sig_by_date.get(d)
        pct_lookup = {}
        if today is not None:
            for c in exit_cols:
                pct_lookup[c] = dict(zip(today["code"], today[f"{c}_pct"]))

        for code in list(positions):
            if (code, d) not in px.index:
                continue
            row = px.loc[(code, d)]
            pos = positions[code]
            held = i - pos["entry_i"]
            reason = None
            if row["l"] <= pos["stop_px"]:
                fill = sell_price(min(pos["stop_px"], row["o"]))
                reason = "stop"
            elif held >= max_hold:
                fill, reason = sell_price(row["c"]), "expiry"
            elif exit_rank_pct is not None and any(
                    pct_lookup.get(c, {}).get(code, 0) >= exit_rank_pct for c in exit_cols):
                fill, reason = sell_price(row["c"]), "signal"
            if reason:
                trades.append({"code": code, "ret": fill / pos["entry_px"] - 1,
                               "reason": reason, "held": held, "exit_d": d,
                               "entry_d": all_dates[pos["entry_i"]],
                               "entry_px": pos["entry_px"], "exit_px": fill})
                del positions[code]

        # (2) 빈 슬롯 채우기
        free = slots - len(positions)
        if free <= 0 or today is None:
            continue
        if (entry_from is not None and d < entry_from) or            (entry_to is not None and d > entry_to):
            continue
        cand = today.dropna(subset=[rank_col])
        if use_pos52w:
            p = px.loc[px.index.get_level_values("d") == d, "pos52w"]
            p = p.reset_index().set_index("code")["pos52w"]
            cand = cand[cand["code"].map(p).le(0.30)]
        cand = cand[~cand["code"].isin(positions)]
        cand = cand.sort_values(rank_col, ascending=ascending).head(free)

        for code in cand["code"]:
            if (code, nxt) not in px.index:
                continue
            o = float(px.loc[(code, nxt), "o"])
            if not np.isfinite(o) or o <= 0:   # 거래정지 등으로 시가가 없는 종목
                continue
            entry = buy_price(o)
            positions[code] = {"entry_i": i + 1, "entry_px": entry,
                               "stop_px": entry * (1 - stop_pct)}

    return trades


def simulate(px, sig, rank_col, ascending, slots, max_hold,
             stop_pct=0.07, use_pos52w=True, exit_rank_pct=None,
             exit_cols=(), label="", strategy=""):
    """run_sim()을 돌리고 통계를 내 출력하고 backtest_runs에 저장한다.

    label은 콘솔 표시용, strategy는 저장 키다. 규칙 변형마다 다른 strategy를
    줘야 서로 덮어쓰지 않는다.
    """
    trades = run_sim(px, sig, rank_col, ascending, slots, max_hold,
                     stop_pct=stop_pct, use_pos52w=use_pos52w,
                     exit_rank_pct=exit_rank_pct, exit_cols=exit_cols)

    rets = np.array([t["ret"] for t in trades])
    if len(rets) == 0:
        print(f"[{label}] 거래 없음")
        return None
    reasons = pd.Series([t["reason"] for t in trades]).value_counts().to_dict()
    n_years = (pd.Timestamp(END) - pd.Timestamp(START)).days / 365.25
    # 슬롯 하나당 연간 회전수 * 회당 수익 (동일가중 근사)
    ann = rets.mean() * (len(rets) / slots) / n_years
    t_val = rets.mean() / (rets.std() / np.sqrt(len(rets)))
    avg_held = float(np.mean([t["held"] for t in trades]))
    print(f"[{label}]")
    print(f"  거래 {len(rets):,}건  평균 {rets.mean()*100:+.2f}%  승률 {(rets>0).mean()*100:.1f}%  "
          f"t값 {t_val:+.2f}")
    print(f"  슬롯당 연 회전 {len(rets)/slots/n_years:.1f}회  →  연환산 {ann*100:+.1f}%")
    print(f"  평균 보유 {avg_held:.1f}일  청산사유 {reasons}")

    save_run(
        strategy, START, END,
        {"rank_col": rank_col, "ascending": ascending, "slots": slots,
         "max_hold_days": max_hold, "stop_pct": stop_pct,
         "pos52w_filter": use_pos52w, "exit_rank_pct": exit_rank_pct,
         "exit_cols": list(exit_cols)},
        {"n": len(rets), "mean_pct": float(rets.mean() * 100),
         "std_pct": float(rets.std() * 100),
         "win_rate": float((rets > 0).mean() * 100), "t_val": float(t_val),
         "annualized_pct": float(ann * 100),
         "turnover_per_slot": float(len(rets) / slots / n_years),
         "avg_held_days": avg_held, "reasons": reasons},
        [{"code": t["code"],
          "entry_d": pd.Timestamp(t["entry_d"]).date(),
          "exit_d": pd.Timestamp(t["exit_d"]).date(),
          "entry_px": float(t["entry_px"]), "exit_px": float(t["exit_px"]),
          "ret_pct": float(t["ret"]), "exit_reason": t["reason"]}
         for t in trades],
    )
    return rets


def main():
    codes = universe()
    print(f"유니버스 {len(codes):,}종목\n로딩 중...")
    px, sig = load(codes)
    px = add_pos52w(px)
    print(f"  일봉 {len(px):,}행 / 신호 {len(sig):,}행\n")

    print("=" * 74)
    print("A. 현행 규칙 (heat 랭킹, 52주 하위 30%, 슬롯 12, 20일)")
    print("=" * 74)
    simulate(px, sig.copy(), "heat_score", True, 12, 20,
             label="현행 12슬롯", strategy="contrarian_v1_slot12")
    simulate(px, sig.copy(), "heat_score", True, 5, 20,
             label="현행 5슬롯", strategy="contrarian_v1_slot5")

    print("\n" + "=" * 74)
    print("B. 신용급증 랭킹으로 교체 (52주 필터 유지)")
    print("=" * 74)
    simulate(px, sig.copy(), "credit_surge_ratio", True, 5, 20,
             label="신용랭킹 5슬롯", strategy="credit_rank_slot5")

    print("\n" + "=" * 74)
    print("C. 52주 필터 제거")
    print("=" * 74)
    simulate(px, sig.copy(), "credit_surge_ratio", True, 5, 20, use_pos52w=False,
             label="신용랭킹 5슬롯 (52주 필터 없음)", strategy="credit_rank_no52w")

    print("\n" + "=" * 74)
    print("D. C + 청산신호 추가 (신용급증/기관순매수 상위 10% 진입 시 청산)")
    print("=" * 74)
    simulate(px, sig.copy(), "credit_surge_ratio", True, 5, 20, use_pos52w=False,
             exit_rank_pct=0.90, exit_cols=("credit_surge_ratio", "institution_flow_ratio"),
             label="C + 신호청산", strategy="credit_rank_no52w_exit")


if __name__ == "__main__":
    main()
