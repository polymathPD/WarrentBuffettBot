"""
버핏식 퀄리티/가치 전략 백테스트 — 분기 리밸런싱, 손절 없음.

기존 역발상 전략과 기계가 다르다. 그게 요점이다.
  보유    20거래일(실측 11.7일) -> 분기 (약 60거래일)
  손절    7%                    -> 없음
  회전    연 21회               -> 연 4회
  비용    연 13.2%              -> 연 2.5%

역발상은 '단기 역전' 팩터인데, 그건 공개된 팩터 중 거래비용에 가장 취약하다.
연 13%를 비용으로 내고 시작하면 어떤 신호도 못 이긴다. 여기서는 회전을 5분의 1로
줄이고, 대신 수십 년간 공개 검증된 퀄리티/가치 팩터를 얹는다.

AQR(Frazzini/Kabiller/Pedersen)이 버크셔 수익률을 분해했을 때 남은 것은
'싸고 · 우량하고 · 저변동인 기업을 레버리지 없이도 오래 들고 있기'였다.
레버리지(보험 플로트)는 우리가 못 쓰므로 앞의 셋만 구현한다.

미래 정보 차단
  재무는 기간 종료 + 90일이 지나야 쓴다(분기보고서 45일, 사업보고서 90일 법정기한).
  리밸런싱은 2·5·8·11월 첫 거래일이라 어떤 재무든 최소 한 달 묵은 것만 쓴다.
  Q4는 분기가 아니라 연간 누적이다(전 종목 확인: 1,919 중 1,827이 기대비율 1.33).
  따라서 Q4 단독 = 연간 - (Q1+Q2+Q3)로 환원한 뒤 TTM을 4분기 합으로 만든다.

재현: python research/quality_backtest.py
"""
import os, sys, time
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

import numpy as np
import pandas as pd
import db.connection as db
import research.portfolio_backtest as pb
from backtester.cost_model import buy_price, sell_price

# 수식은 strategy/quality.py에만 둔다. 여기에 복제하면 한쪽만 고쳤을 때
# 운용과 백테스트가 조용히 갈라진다 (역발상에서 실제로 그랬다).
from strategy.quality import (FLOW, STOCK, AVAIL_LAG_DAYS, MAX_DEBT_RATIO,
                              load_financials, snapshot, eligible, score)

REBAL_MONTHS = (2, 5, 8, 11)          # 분기
MONTHLY = tuple(range(1, 13))         # 월별


def rebalance_dates(all_dates, start, end, months=REBAL_MONTHS):
    """리밸런싱 월의 첫 거래일.

    월별로 돌려도 미래 정보는 새지 않는다. 재무는 기간종료+90일 규칙으로 걸러지고,
    매달 바뀌는 것은 가격(=시가총액)뿐이라 PBR·PER 순위만 새로 매겨진다.
    분기 리밸런싱은 3.3년에 구간이 8개뿐이라 무엇도 확정할 수 없었다."""
    s = pd.Series(all_dates)
    s = s[(s >= pd.Timestamp(start)) & (s <= pd.Timestamp(end))]
    key = s.dt.year * 100 + s.dt.month
    first = s.groupby(key).min()
    return sorted(d for d in first if d.month in months)


def simulate(px, fin, kind, slots, start, end, min_marcap=pb.MIN_MARCAP,
             rebal_months=REBAL_MONTHS):
    """분기 리밸런싱. 손절 없음. 구간별 동일가중 수익률과 자산곡선을 낸다.

    거래당 평균으로는 이 전략을 못 잰다. 편입이 유지되는 종목은 보유가 몇 분기씩
    이어지고 교체되는 종목은 한 분기라, 거래마다 기간이 달라 평균이 의미를 잃는다.
    구간(리밸런싱 사이)마다 동일가중 수익률을 내고 그것을 복리로 쌓는다.

    비용은 회전분에만 매긴다. 계속 들고 가는 종목은 사고팔지 않으므로 비용이 없다 —
    이 전략의 요점이 낮은 회전율이라 그걸 비용에 반영하지 않으면 측정이 무의미하다.
    """
    px = px.set_index(["code", "d"]).sort_index()
    all_dates = np.sort(px.index.get_level_values("d").unique())
    by_date = {d: g.droplevel("d") for d, g in px.groupby(level="d")}
    date_pos = {d: i for i, d in enumerate(all_dates)}

    buy_cost = buy_price(1.0) - 1.0
    sell_cost = 1.0 - sell_price(1.0)

    rebals = rebalance_dates(all_dates, start, end, rebal_months)
    last_day = max(d for d in all_dates if d <= pd.Timestamp(end))
    marks = {}          # code -> 직전 리밸런싱 체결가 (총액 기준, 비용 제외)
    periods = []

    def target_set(d):
        snap_px = by_date[d]
        s = snapshot(fin, d)
        if s.empty:
            return None
        pool = eligible(s, snap_px["marcap"], min_marcap)
        if pool.empty:
            return None
        if kind == "all":
            return set(snap_px.index[snap_px["marcap"] >= min_marcap])
        if kind == "filtered":
            return set(pool["code"])
        return set(pool.assign(sc=score(pool, kind)).nlargest(slots, "sc")["code"])

    def px_on(code, d, col):
        if (code, d) not in px.index:
            return None
        v = float(px.loc[(code, d), col])
        return v if np.isfinite(v) and v > 0 else None

    for step, d in enumerate(rebals):
        i = date_pos[d]
        if i + 1 >= len(all_dates):
            break
        fill = all_dates[i + 1]          # 체결일: 다음 거래일 시가

        # (1) 직전 구간 수익률 — 보유 종목의 동일가중 평균
        if marks:
            rets = []
            for code, m in marks.items():
                o = px_on(code, fill, "o")
                if o is not None:
                    rets.append(o / m - 1)
            if rets:
                periods.append({"d": d, "n": len(rets), "gross": float(np.mean(rets))})

        # (2) 목표 편입 결정
        tgt = target_set(d)
        if tgt is None:
            continue
        tgt = {c for c in tgt if px_on(c, fill, "o") is not None}
        if not tgt:
            continue

        # (3) 회전 비용 — 판 비중 x 매도비용 + 산 비중 x 매수비용
        old = set(marks)
        sold = len(old - tgt) / len(old) if old else 0.0
        bought = len(tgt - old) / len(tgt)
        if periods:
            periods[-1]["cost"] = sold * sell_cost + bought * buy_cost
        else:
            periods.append({"d": d, "n": 0, "gross": 0.0,
                            "cost": bought * buy_cost})

        marks = {c: px_on(c, fill, "o") for c in tgt}

    # 마지막 구간 — 종가로 정리한다. 안 그러면 이 구간이 통째로 빠진다.
    if marks:
        rets = [px_on(c, last_day, "c") / m for c, m in marks.items()
                if px_on(c, last_day, "c") is not None]
        if rets:
            periods.append({"d": rebals[-1] if rebals else last_day,
                            "n": len(rets), "gross": float(np.mean(rets)) - 1,
                            "cost": sell_cost})

    for p in periods:
        p.setdefault("cost", 0.0)
        p["net"] = (1 + p["gross"]) * (1 - p["cost"]) - 1
    return periods


def report(label, periods, per_year=4):
    if not periods:
        print(f"  {label:24} 구간 없음")
        return None
    r = np.array([p["net"] for p in periods])
    comp = float(np.prod(1 + r) - 1)
    yrs = len(r) / per_year
    ann = (1 + comp) ** (1 / yrs) - 1 if comp > -1 else float("nan")
    tv = r.mean() / (r.std(ddof=1) / np.sqrt(len(r))) if len(r) > 1 and r.std(ddof=1) > 0 else 0.0
    avg_n = float(np.mean([p["n"] for p in periods]))
    print(f"  {label:24} 구간 {len(r):>3}  구간평균 {r.mean()*100:+6.2f}%  "
          f"승 {(r>0).sum():>3}/{len(r):<3} t{tv:+5.2f}  "
          f"누적 {comp*100:+7.1f}%  연환산 {ann*100:+6.1f}%  보유 {avg_n:.0f}종목")
    return {"n": len(r), "mean_pct": float(r.mean()*100), "t_val": float(tv),
            "compound_pct": comp*100, "annual_pct": float(ann*100),
            "win_rate": float((r > 0).mean()*100), "avg_holdings": avg_n}


def main():
    t0 = time.time()
    # 재무는 2022Q1부터라 TTM 4분기가 채워지는 건 2022Q4(공시 2023-03-31)부터다.
    pb.START, pb.END = "2022-01-01", "2026-08-19"
    pb.WARMUP = "2020-11-27"
    codes = pb.universe()
    print(f"유니버스 {len(codes):,}종목 — 로딩 중...", flush=True)
    px, _ = pb.load(codes)
    px = pb.add_marcap(px)
    fin = load_financials()
    print(f"  일봉 {len(px):,}행 / 재무 TTM {fin['net_income_ttm'].notna().sum():,}행 "
          f"({time.time()-t0:.0f}s)", flush=True)
    print(flush=True)

    from backtester.store import save_run
    for wname, ws, we in [("훈련", "2023-04-01", "2024-12-31"),
                          ("검증", "2025-01-01", "2026-08-19")]:
        print(f"[{wname} {ws} ~ {we}]")
        for rname, months, per_year in [("분기", REBAL_MONTHS, 4),
                                        ("월별", MONTHLY, 12)]:
            print(f" [{rname} 리밸런싱]")
            for kind, slots in [("all", None), ("filtered", None),
                                ("quality", 20), ("value", 20), ("combo", 20)]:
                per = simulate(px, fin, kind, slots or 99999, ws, we,
                               rebal_months=months)
                lab = kind if slots is None else f"{kind} {slots}슬롯"
                st = report(lab, per, per_year)
                if st:
                    save_run(f"q3_{rname}_{kind}_{slots or 'all'}_{wname}", ws, we,
                             {"kind": kind, "slots": slots, "rebalance": rname,
                              "stop_pct": None, "point_in_time_universe": True,
                              "max_debt_ratio": MAX_DEBT_RATIO}, st, [])
        print()
    print(f"총 {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
