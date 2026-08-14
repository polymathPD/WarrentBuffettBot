"""
시총 3,000억 이상 유니버스로 제한한 분위수 분석.

앞선 전 종목 분석에서 모든 분위가 음수였는데, 잡주가 평균을 끌어내린 것인지
매수 전용 설계 자체의 문제인지 구분한다. 보유기간을 1~20일로 넓혀 잡아
매도 시점(MAX_HOLD_DAYS) 판단 근거도 함께 만든다.
"""
import os, sys, time
sys.stdout.reconfigure(encoding="utf-8")  # cp949 콘솔에서 특수문자 출력 실패 방지
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

import numpy as np
import pandas as pd
import FinanceDataReader as fdr
import db.connection as db

START, END = "2022-01-01", "2024-12-31"
TAIL_DAYS = 90
HORIZONS = [1, 3, 5, 10, 20]
CODE_BATCH = 300
MIN_MARCAP = 300_000_000_000
METRICS = ["heat_score", "individual_flow_ratio", "credit_surge_ratio", "volume_ratio",
           "foreign_flow_ratio", "institution_flow_ratio", "credit_ratio_level"]


def universe():
    lst = fdr.StockListing("KRX")
    big = {c for c, m in zip(lst["Code"], lst["Marcap"]) if m and m >= MIN_MARCAP}
    have = {r["code"] for r in
            db.fetchall("SELECT DISTINCT code FROM contrarian_signals")}
    return sorted(big & have)


def load_batch(codes, price_end):
    sig = pd.DataFrame(db.fetchall(
        f"""SELECT code, d, {', '.join(METRICS)} FROM contrarian_signals
            WHERE code = ANY(%s) AND d >= %s AND d <= %s ORDER BY code, d""",
        (codes, START, END)))
    if sig.empty:
        return None
    sig["d"] = pd.to_datetime(sig["d"])
    for m in METRICS:
        sig[m] = pd.to_numeric(sig[m], errors="coerce")
        sig.loc[~np.isfinite(sig[m]), m] = np.nan  # inf 제거 (분모 0 근처)

    px = pd.DataFrame(db.fetchall(
        """SELECT code, d, o, c FROM stock_daily
           WHERE code = ANY(%s) AND d >= %s AND d <= %s ORDER BY code, d""",
        (codes, START, price_end)))
    if px.empty:
        return None
    px["d"] = pd.to_datetime(px["d"])
    px["o"] = px["o"].astype(float)
    px["c"] = px["c"].astype(float)
    return sig, px


def forward_returns(px):
    parts = []
    for code, g in px.groupby("code", sort=False):
        g = g.sort_values("d").reset_index(drop=True)
        o, c = g["o"].to_numpy(), g["c"].to_numpy()
        n = len(g)
        cols = {"code": code, "d": g["d"].to_numpy()}
        for h in HORIZONS:
            ret = np.full(n, np.nan)
            si = np.arange(0, n - 1 - h)
            ent, ext = si + 1, si + 1 + h
            with np.errstate(divide="ignore", invalid="ignore"):
                ret[si] = np.where(o[ent] > 0, c[ext] / o[ent] - 1, np.nan)
            cols[f"fwd_{h}"] = ret
        parts.append(pd.DataFrame(cols))
    return pd.concat(parts, ignore_index=True) if parts else None


def main():
    price_end = (pd.Timestamp(END) + pd.Timedelta(days=TAIL_DAYS)).date()
    codes = universe()
    print(f"유니버스 {len(codes):,}종목 (시총 3,000억 이상)")

    parts, t0 = [], time.time()
    for b0 in range(0, len(codes), CODE_BATCH):
        loaded = load_batch(codes[b0:b0 + CODE_BATCH], price_end)
        if loaded is None:
            continue
        sig, px = loaded
        fwd = forward_returns(px)
        if fwd is not None:
            parts.append(sig.merge(fwd, on=["code", "d"], how="inner"))
        print(f"  {min(b0+CODE_BATCH, len(codes)):,}/{len(codes):,}  ({time.time()-t0:.0f}s)")

    full = pd.concat(parts, ignore_index=True)
    print(f"\n분석 대상: {len(full):,}행\n")

    # 기준선: 유니버스 전체 평균 (시장 드리프트)
    print("=" * 76)
    print("기준선 — 유니버스 평균 수익률 (보유기간별)")
    print("=" * 76)
    for h in HORIZONS:
        s = full[f"fwd_{h}"].dropna()
        print(f"  {h:>2}거래일: 평균 {s.mean()*100:>6.2f}%   중앙값 {s.median()*100:>6.2f}%   n={len(s):,}")

    print("\n" + "=" * 76)
    print(f"지표별 분위수  ({START} ~ {END}, 비용 미반영)")
    print("=" * 76)
    print(f"{'지표':<24}{'기간':>4}{'표본':>9}{'D1':>9}{'D10':>9}{'D10-D1':>9}{'단조성':>8}")
    for m in METRICS:
        for h in HORIZONS:
            sub = full[[m, f"fwd_{h}"]].dropna()
            if len(sub) < 3000:
                continue
            try:
                q = pd.qcut(sub[m], 10, labels=False, duplicates="drop")
            except ValueError:
                continue
            means = sub.groupby(q)[f"fwd_{h}"].mean() * 100
            if len(means) < 3:
                print(f"{m:<24}{h:>4}{len(sub):>9}   분위 {len(means)}개로 뭉개짐(분포 편중)")
                continue
            mono = np.corrcoef(np.arange(len(means)), means.to_numpy())[0, 1]
            print(f"{m:<24}{h:>4}{len(sub):>9}{means.iloc[0]:>8.2f}%{means.iloc[-1]:>8.2f}%"
                  f"{means.iloc[-1]-means.iloc[0]:>8.2f}%{mono:>8.2f}")

    # 진입 조건(52주 하위 30% 제외)에 해당하는 부분집합에서 heat 분위
    print("\n" + "=" * 76)
    print("heat_score 전체 분위 (유니버스 한정)")
    print("=" * 76)
    for h in (5, 20):
        sub = full[["heat_score", f"fwd_{h}"]].dropna()
        q = pd.qcut(sub["heat_score"], 10, labels=False, duplicates="drop")
        g = sub.groupby(q)[f"fwd_{h}"].agg(["mean", "count"])
        print(f"\n[{h}거래일]")
        for i, row in g.iterrows():
            print(f"  D{i+1:<3} {row['mean']*100:>7.2f}%   n={int(row['count']):>7,}")


if __name__ == "__main__":
    main()
