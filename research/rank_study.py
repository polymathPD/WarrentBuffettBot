"""
랭킹에 신호가 있는가? — 표본을 넓혀 랭킹별 수익률 곡선을 본다.

슬롯 5개로 좁게 돌리면 연 60건뿐이라 통계가 서지 않는다. 여기서는 매일 상위 K개를
전부 독립 거래로 시뮬레이션해 표본을 수천 건으로 늘리고, 진입 시점의 랭킹별로
수익률을 집계한다. 랭킹 1~2위가 하위보다 꾸준히 좋다면 상위만 거래하는 것이
정당화된다 (실전은 슬롯 5개로 운용하되, 검증은 넓은 표본으로).

청산 규칙은 실전과 동일: 손절 -7% 또는 20거래일 만기.
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

START = sys.argv[1] if len(sys.argv) > 1 else "2022-01-01"
END   = sys.argv[2] if len(sys.argv) > 2 else "2024-12-31"
WARMUP = (pd.Timestamp(START) - pd.Timedelta(days=400)).strftime("%Y-%m-%d")
TOP_K = 20          # 매일 상위 20개까지 기록
MAX_HOLD, STOP_PCT = 20, 0.07
MIN_MARCAP = 300_000_000_000
CODE_BATCH = 300
METRICS = ["heat_score", "individual_flow_ratio", "credit_surge_ratio",
           "institution_flow_ratio"]


def universe():
    lst = fdr.StockListing("KRX")
    big = {c for c, m in zip(lst["Code"], lst["Marcap"]) if m and m >= MIN_MARCAP}
    have = {r["code"] for r in db.fetchall("SELECT DISTINCT code FROM contrarian_signals")}
    return sorted(big & have)


def load(codes):
    pp, sp = [], []
    for b0 in range(0, len(codes), CODE_BATCH):
        b = codes[b0:b0 + CODE_BATCH]
        p = pd.DataFrame(db.fetchall(
            "SELECT code, d, o, h, l, c FROM stock_daily "
            "WHERE code = ANY(%s) AND d >= %s AND d <= %s ORDER BY code, d",
            (b, WARMUP, END)))
        if not p.empty:
            for col in ("o", "h", "l", "c"):
                p[col] = p[col].astype(float)
            pp.append(p)
        s = pd.DataFrame(db.fetchall(
            f"SELECT code, d, {', '.join(METRICS)} FROM contrarian_signals "
            "WHERE code = ANY(%s) AND d >= %s AND d <= %s ORDER BY code, d",
            (b, START, END)))
        if not s.empty:
            for m in METRICS:
                s[m] = pd.to_numeric(s[m], errors="coerce")
                s.loc[~np.isfinite(s[m]), m] = np.nan
            sp.append(s)
    px, sig = pd.concat(pp, ignore_index=True), pd.concat(sp, ignore_index=True)
    px["d"], sig["d"] = pd.to_datetime(px["d"]), pd.to_datetime(sig["d"])
    return px, sig


def add_pos52w(px):
    out = []
    for code, g in px.groupby("code", sort=False):
        g = g.sort_values("d").reset_index(drop=True)
        lo = g["l"].shift(1).rolling(252, min_periods=60).min()
        hi = g["h"].shift(1).rolling(252, min_periods=60).max()
        g["pos52w"] = (g["c"] - lo) / (hi - lo).replace(0, np.nan)
        out.append(g)
    return pd.concat(out, ignore_index=True)


def rank_study(px, sig, rank_col, ascending, use_pos52w, label):
    arr = {}   # code -> (dates, o,h,l,c) 배열
    for code, g in px.groupby("code", sort=False):
        g = g.sort_values("d")
        arr[code] = (g["d"].to_numpy(), g["o"].to_numpy(),
                     g["h"].to_numpy(), g["l"].to_numpy(), g["c"].to_numpy())
    pos = px.set_index(["code", "d"])["pos52w"]

    rows = []
    for d, today in sig.groupby("d"):
        cand = today.dropna(subset=[rank_col])
        if use_pos52w:
            pv = cand["code"].map(lambda c: pos.get((c, d), np.nan)).to_numpy(dtype=float)
            cand = cand[np.nan_to_num(pv, nan=9.9) <= 0.30]
        if cand.empty:
            continue
        cand = cand.sort_values(rank_col, ascending=ascending).head(TOP_K)

        for rank, code in enumerate(cand["code"], 1):
            dates, o, h, l, c = arr[code]
            i = np.searchsorted(dates, np.datetime64(d))
            if i + 1 >= len(dates):
                continue
            e = i + 1
            if not np.isfinite(o[e]) or o[e] <= 0:
                continue
            entry = buy_price(o[e])
            stop = entry * (1 - STOP_PCT)
            ret = None
            for j in range(e + 1, min(e + 1 + MAX_HOLD, len(dates))):
                if l[j] <= stop:
                    ret = sell_price(min(stop, o[j])) / entry - 1
                    break
                if j - e >= MAX_HOLD:
                    ret = sell_price(c[j]) / entry - 1
                    break
            if ret is not None:
                rows.append((rank, ret, pd.Timestamp(d)))

    df = pd.DataFrame(rows, columns=["rank", "ret", "d"])
    if df.empty:
        print(f"[{label}] 거래 없음")
        return
    print(f"\n[{label}]  총 {len(df):,}건")
    print(f"  {'랭킹':<10}{'건수':>8}{'평균':>9}{'승률':>8}{'t값':>8}")
    buckets = [(1, 2), (3, 5), (6, 10), (11, 20)]
    means = []
    for lo, hi in buckets:
        s = df[(df["rank"] >= lo) & (df["rank"] <= hi)]["ret"]
        if len(s) < 30:
            continue
        t = s.mean() / (s.std() / np.sqrt(len(s)))
        means.append(s.mean())
        print(f"  {f'{lo}-{hi}위':<10}{len(s):>8,}{s.mean()*100:>8.2f}%"
              f"{(s > 0).mean()*100:>7.1f}%{t:>8.2f}")
    if len(means) >= 3:
        mono = np.corrcoef(np.arange(len(means)), means)[0, 1]
        print(f"  랭킹 단조성(상위->하위): {mono:+.2f}")
    return df


def monthly(df, top_n, label):
    s = df[df["rank"] <= top_n].copy()
    s["m"] = s["d"].dt.to_period("Q")
    g = s.groupby("m")["ret"].agg(["mean", "count"])
    print(f"\n[{label}] 상위 {top_n}위, 분기별")
    pos = 0
    for m, row in g.iterrows():
        mark = "훈련" if m.year <= 2024 else "검증"
        if row["mean"] > 0:
            pos += 1
        print(f"  {m}  {mark}  평균 {row['mean']*100:+7.2f}%  n={int(row['count']):>4}")
    print(f"  양의 분기: {pos}/{len(g)}")
    tr = s[s["d"] < "2025-01-01"]["ret"]
    te = s[s["d"] >= "2025-01-01"]["ret"]
    for nm, x in (("훈련 22-24", tr), ("검증 25-26", te)):
        if len(x) > 30:
            t = x.mean() / (x.std() / np.sqrt(len(x)))
            print(f"  {nm}: n={len(x):,}  평균 {x.mean()*100:+.2f}%  t값 {t:+.2f}")


def main():
    codes = universe()
    print(f"유니버스 {len(codes):,}종목 / 기간 {START} ~ {END}\n로딩 중...")
    px, sig = load(codes)
    px = add_pos52w(px)
    print(f"  일봉 {len(px):,}행 / 신호 {len(sig):,}행")

    df = rank_study(px, sig, "heat_score", True, True, "heat 오름차순 + 52주 하위 30%")
    for n in (2, 3, 5):
        monthly(df, n, "heat 랭킹")


if __name__ == "__main__":
    main()
