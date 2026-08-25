"""
저부채 프리미엄이 금리 인상 구간에서 커지는지 확인한다.

financials를 2021Q1~Q4까지 백필해 TTM 가용 시점을 2023-04-03에서 2022-03-31로
당겼다. 그래야 한은 인상기(2022-04 1.50% -> 2023-01 3.50%, +200bp)가 표본에 들어온다.
백필 전에는 인상기에 재무가 한 줄도 없어 이 질문 자체를 못 물었다.

국면
  인상기   2022-04-01 ~ 2023-01-31   기준금리 1.50% -> 3.50%
  동결기   2023-02-01 ~ 2024-09-30   3.50% 유지
  (2024-10 인하 시작. 훈련 구간에 3개월뿐이라 국면으로 세지 않는다.)

검증 구간(2025-01~)은 열지 않는다. research/README.md 참고 — 홀드아웃은 이미
다섯 가설에 소진됐고, 여기서 결과를 보고 규칙을 고르면 점수가 정직하지 않다.

[1] 부채비율 5분위 — eligible() 통과 종목을 부채비율로 5등분하고 구간 수익률을 잰다.
    비용은 매기지 않는다. 분위는 매매 가능한 포트폴리오가 아니고, 회전율이 분위마다
    비슷해 스프레드에서 상쇄된다. 가설이 맞다면 Q1-Q5 스프레드가 인상기에 더 커야 한다.

[2] MAX_DEBT_RATIO 조이기 — 지금 데이터로 '저부채에 점수를 더 준다'를 실제로
    구현할 수 있는 유일한 형태다. 현금성자산은 financials 테이블에 없다
    (revenue/op_income/net_income/assets/liabilities/equity 뿐).
    운용 랭킹(value 10슬롯)은 그대로 두고 필터만 2.0 -> 1.0 -> 0.5로 조인다.

재현: python research/debt_regime.py
"""
import os, sys, time
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

import numpy as np
import pandas as pd
import research.portfolio_backtest as pb
import strategy.quality as q
from strategy.quality import load_financials, snapshot, eligible
from research.quality_backtest import rebalance_dates, simulate, report, MONTHLY

# 훈련 구간만. 검증 구간은 열지 않는다.
REGIMES = [
    ("인상기 1.50->3.50%", "2022-04-01", "2023-01-31"),
    ("동결기 3.50%", "2023-02-01", "2024-09-30"),
    ("훈련 전체", "2022-04-01", "2024-12-31"),
]
TRAIN_E = "2024-12-31"
QUANTILES = 5


def quantile_study(px, fin, start, end, nq=QUANTILES, min_marcap=pb.MIN_MARCAP):
    """부채비율 분위별 구간 수익률. 매수는 리밸런싱 다음 거래일 시가,
    매도는 다음 리밸런싱의 다음 거래일 시가 — simulate()와 같은 체결 가정."""
    px = px.set_index(["code", "d"]).sort_index()
    all_dates = np.sort(px.index.get_level_values("d").unique())
    by_date = {d: g.droplevel("d") for d, g in px.groupby(level="d")}
    date_pos = {d: i for i, d in enumerate(all_dates)}
    rebals = rebalance_dates(all_dates, start, end, MONTHLY)

    def px_on(code, d, col):
        if (code, d) not in px.index:
            return None
        v = float(px.loc[(code, d), col])
        return v if np.isfinite(v) and v > 0 else None

    rows, bounds = [], []
    for step in range(len(rebals) - 1):
        d, nxt = rebals[step], rebals[step + 1]
        i, j = date_pos[d], date_pos[nxt]
        if i + 1 >= len(all_dates) or j + 1 >= len(all_dates):
            continue
        fill, nfill = all_dates[i + 1], all_dates[j + 1]

        s = snapshot(fin, d)
        if s.empty:
            continue
        pool = eligible(s, by_date[d]["marcap"], min_marcap).copy()
        if len(pool) < nq * 10:
            continue
        pool["b"] = pd.qcut(pool["debt_ratio"], nq, labels=False, duplicates="drop")

        for b, g in pool.groupby("b"):
            rets = [px_on(c, nfill, "o") / px_on(c, fill, "o") - 1
                    for c in g["code"]
                    if px_on(c, fill, "o") and px_on(c, nfill, "o")]
            if rets:
                rows.append({"d": d, "b": int(b), "ret": float(np.mean(rets)),
                             "n": len(rets)})
        bd = pool.groupby("b")["debt_ratio"].agg(["min", "max", "count"])
        bd["d"] = d
        bounds.append(bd.reset_index())

    return pd.DataFrame(rows), pd.concat(bounds) if bounds else pd.DataFrame()


def tstat(a):
    a = np.asarray(a, dtype=float)
    if len(a) < 2 or a.std(ddof=1) == 0:
        return 0.0
    return float(a.mean() / (a.std(ddof=1) / np.sqrt(len(a))))


def show_quantiles(px, fin, label, start, end):
    print(f"[1] 부채비율 {QUANTILES}분위 — {label}  {start} ~ {end}  (총수익, 비용 제외)")
    r, bd = quantile_study(px, fin, start, end)
    if r.empty:
        print("    구간 없음")
        print()
        return
    if not bd.empty:
        agg = bd.groupby("b").agg(lo=("min", "median"), hi=("max", "median"))
        edges = "  ".join(f"Q{b+1} {row['lo']:.2f}~{row['hi']:.2f}"
                          for b, row in agg.iterrows())
        print(f"    경계(중앙값)  {edges}")
    for b in sorted(r["b"].unique()):
        g = r[r["b"] == b]["ret"].values
        tag = "저부채" if b == 0 else ("고부채" if b == r["b"].max() else "")
        print(f"    Q{b+1} {tag:6} 구간 {len(g):>3}  평균 {g.mean()*100:+6.2f}%  "
              f"t{tstat(g):+5.2f}  승 {(g>0).sum():>2}/{len(g):<2}  "
              f"누적 {(np.prod(1+g)-1)*100:+7.1f}%")
    w = r.pivot(index="d", columns="b", values="ret").dropna()
    hi_b = int(r["b"].max())
    if 0 in w.columns and hi_b in w.columns:
        sp = (w[0] - w[hi_b]).values
        print(f"    >> 스프레드 Q1-Q{hi_b+1}  구간 {len(sp)}  평균 {sp.mean()*100:+6.2f}%p  "
              f"t{tstat(sp):+5.2f}  승 {(sp>0).sum()}/{len(sp)}")
    print()


def show_caps(px, fin, label, start, end):
    print(f"[2] MAX_DEBT_RATIO 조이기 — value 10슬롯, {label}  {start} ~ {end}  (비용 포함)")
    orig = q.MAX_DEBT_RATIO
    try:
        for cap in (2.0, 1.0, 0.5):
            q.MAX_DEBT_RATIO = cap    # eligible()이 호출 시점에 읽는 모듈 전역
            per = simulate(px, fin, "value", 10, start, end, rebal_months=MONTHLY)
            report(f"부채비율 <= {cap}", per, per_year=12)
    finally:
        q.MAX_DEBT_RATIO = orig
    print()


def main():
    t0 = time.time()
    pb.START, pb.END = "2022-01-01", TRAIN_E
    pb.WARMUP = "2020-11-27"
    codes = pb.universe()
    print(f"유니버스 {len(codes):,}종목 — 로딩 중...", flush=True)
    px, _ = pb.load(codes)
    px = pb.add_marcap(px)
    fin = load_financials()
    print(f"  일봉 {len(px):,}행 ({time.time()-t0:.0f}s)", flush=True)
    print(flush=True)

    for label, s_, e_ in REGIMES:
        print("=" * 78)
        print(f"  {label}")
        print("=" * 78)
        show_quantiles(px, fin, label, s_, e_)
        show_caps(px, fin, label, s_, e_)

    print(f"총 {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
