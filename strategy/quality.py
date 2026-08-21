"""
퀄리티/가치 전략 — 분기 재무 기반, 월별 리밸런싱, 손절 없음.

역발상과 기계가 다르다. 그게 요점이다.
  보유    11.7거래일 -> 리밸런싱까지 (평균 수개월)
  손절    7%         -> 없음
  회전    연 21회    -> 연 12회 이하 (편입 유지 종목은 거래하지 않는다)
  근거    수급·과열   -> ROE·부채비율·PBR·PER

수식은 이 파일에만 둔다. research/quality_backtest.py가 여기서 가져다 쓴다.
검증 경로에 수식을 복제하면 한쪽만 고쳤을 때 운용과 백테스트가 조용히 갈라진다
(역발상에서 손절 판정이 실제로 갈라져 있었다: 백테스트는 장중 저가, 운용은 종가).

미래 정보 차단
  재무는 기간종료 + AVAIL_LAG_DAYS(90일)가 지나야 쓴다. 분기보고서 45일,
  사업보고서 90일이 법정기한이므로 그보다 보수적이다.
  Q4는 분기가 아니라 연간 누적이다(전 종목 확인: 1,919 중 1,827이 기대비율 1.33).
  Q4 단독 = 연간 - (Q1+Q2+Q3)로 환원한 뒤 TTM을 4분기 합으로 만든다.
  시가총액도 그 시점에 공시돼 있던 발행주식수를 쓴다(연도 - 1의 Q4).
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import numpy as np
import pandas as pd
import db.connection as db
from strategy.filters import ordinary, liquid

STRATEGY = "quality_v1"

FLOW = ["revenue", "op_income", "net_income"]      # 손익 (기간 흐름)
STOCK = ["assets", "liabilities", "equity"]        # 재무상태 (시점)
AVAIL_LAG_DAYS = 90
MAX_DEBT_RATIO = 2.0
MIN_MARCAP = 300_000_000_000
RANK_KIND = "value"      # 워크포워드가 5개 창 중 4번 고른 값. research/README.md 참고


def load_financials() -> pd.DataFrame:
    """(code, 분기) 단위 TTM 손익 + 시점 재무상태. 연결 우선, 없으면 별도."""
    f = pd.DataFrame(db.fetchall(
        "SELECT code, period, fs_div, revenue, op_income, net_income, "
        "assets, liabilities, equity FROM financials"))
    for c in FLOW + STOCK:
        f[c] = pd.to_numeric(f[c], errors="coerce")

    # 연결/별도가 다 있으면 연결만 쓴다. 섞으면 규모가 종목마다 들쭉날쭉해진다.
    pref = f.assign(rank=f["fs_div"].map({"CFS": 0, "OFS": 1}))
    f = (pref.sort_values("rank").drop_duplicates(["code", "period"])
             .drop(columns=["rank", "fs_div"]))

    f["y"] = f["period"].str.slice(0, 4).astype(int)
    f["q"] = f["period"].str.slice(5, 6).astype(int)
    f = f.sort_values(["code", "y", "q"])

    # Q4는 연간 누적이므로 단독 분기로 환원한다: Q4단독 = 연간 - (Q1+Q2+Q3)
    cum3 = (f[f["q"] <= 3].groupby(["code", "y"])[FLOW].sum()
              .rename(columns={c: c + "_cum3" for c in FLOW}).reset_index())
    f = f.merge(cum3, on=["code", "y"], how="left")
    for c in FLOW:
        f[c + "_q"] = np.where(f["q"] <= 3, f[c], f[c] - f[c + "_cum3"])

    # TTM = 직전 4개 단독 분기 합 (4개가 다 있을 때만)
    f = f.sort_values(["code", "y", "q"])
    for c in FLOW:
        f[c + "_ttm"] = (f.groupby("code")[c + "_q"]
                          .rolling(4, min_periods=4).sum()
                          .reset_index(level=0, drop=True))

    f["avail"] = (pd.PeriodIndex(f["y"].astype(str) + "Q" + f["q"].astype(str),
                                 freq="Q").end_time.normalize()
                  + pd.Timedelta(days=AVAIL_LAG_DAYS))
    return f


def snapshot(fin: pd.DataFrame, d: pd.Timestamp) -> pd.DataFrame:
    """d 시점에 공시돼 있던 가장 최근 재무."""
    ok = fin[fin["avail"] <= d]
    if ok.empty:
        return ok
    return ok.sort_values(["y", "q"]).drop_duplicates("code", keep="last")


def eligible(snap: pd.DataFrame, mcap: pd.Series,
             min_marcap: float = MIN_MARCAP) -> pd.DataFrame:
    """살 수 있는 상태인가 — 규모 · 흑자 · 자본 건전성.

    이 세 조건만으로도 시장을 이겼다(월별 21구간, 하락장 +5.1%p / 상승장 +4.9%p).
    다만 워크포워드 시험 구간에서는 그 초과분이 사라졌다 — 필터만으로는 부족하고
    랭킹이 필요하다.
    """
    s = snap.copy()
    s["marcap"] = s["code"].map(mcap)
    s = s.dropna(subset=["marcap", "equity", "net_income_ttm", "revenue_ttm"])
    s = s[(s["marcap"] >= min_marcap) & (s["equity"] > 0)
          & (s["net_income_ttm"] > 0) & (s["revenue_ttm"] > 0)]
    s["debt_ratio"] = s["liabilities"] / s["equity"]
    return s[s["debt_ratio"] <= MAX_DEBT_RATIO]


def score(s: pd.DataFrame, kind: str) -> pd.Series:
    """백분위 순위의 평균. 값의 단위가 제각각이라 순위로 맞춘다.

    combo(퀄리티+가치 반반)는 훈련·검증·양쪽 리밸런싱 주기에서 일관되게
    각각보다 나빴다. 두 축을 섞으면 어중간한 종목이 뽑히는 것으로 보인다.
    """
    roe = (s["net_income_ttm"] / s["equity"]).rank(pct=True)
    margin = (s["op_income_ttm"] / s["revenue_ttm"]).rank(pct=True)
    debt = (-s["debt_ratio"]).rank(pct=True)
    earn_yield = (s["net_income_ttm"] / s["marcap"]).rank(pct=True)   # 1/PER
    book_yield = (s["equity"] / s["marcap"]).rank(pct=True)           # 1/PBR
    if kind == "quality":
        return (roe + margin + debt) / 3
    if kind == "value":
        return (earn_yield + book_yield) / 2
    if kind == "combo":
        return (roe + margin + debt) / 3 * 0.5 + (earn_yield + book_yield) / 2 * 0.5
    raise ValueError(kind)


def marcap_on(d: str) -> pd.Series:
    """d일의 시점별 시가총액 = 그때 공시돼 있던 발행주식수 x 그날 종가.

    발행주식수는 연도 - 1의 Q4를 쓴다. 2022Q4 주식수는 2023년 3월경 공시되므로
    2023년 날짜에 써야 미래 정보가 새지 않는다.
    """
    cutoff = f"{pd.Timestamp(d).year - 1}Q4"
    rows = db.fetchall(
        """SELECT DISTINCT ON (sd.code) sd.code, sd.c * s.issued AS marcap
             FROM stock_daily sd
             JOIN shares s ON s.code = sd.code AND s.period <= %s
            WHERE sd.d = %s::date AND s.issued > 0
            ORDER BY sd.code, s.period DESC""",
        (cutoff, d),
    )
    return pd.Series({r["code"]: float(r["marcap"]) for r in rows})


def get_targets(target_date: str, slots: int, kind: str = RANK_KIND) -> list[dict]:
    """리밸런싱 목표 편입 종목. 점수 높은 순 slots개.

    백테스트(research/quality_backtest.py)와 같은 함수를 쓴다. 다만 보통주·거래대금
    필터가 운용에만 있다 — 백테스트 유니버스는 시총 하한만 걸었다. 시총 3,000억이면
    거의 다 통과하므로 실질 차이는 없지만, 편도 0.2% 슬리피지 가정이 성립하는지는
    운용에서 직접 확인해야 한다.
    """
    fin = load_financials()
    snap = snapshot(fin, pd.Timestamp(target_date))
    if snap.empty:
        return []
    pool = eligible(snap, marcap_on(target_date))
    if pool.empty:
        return []

    codes = ordinary(list(pool["code"]))
    ok = liquid(codes, target_date)
    pool = pool[pool["code"].isin(ok)]
    if pool.empty:
        return []

    pool = pool.assign(sc=score(pool, kind)).nlargest(slots, "sc")
    px = {r["code"]: float(r["c"]) for r in db.fetchall(
        "SELECT code, c FROM stock_daily WHERE code = ANY(%s) AND d = %s::date",
        (list(pool["code"]), target_date))}

    out = []
    for _, r in pool.iterrows():
        if r["code"] not in px:
            continue
        out.append({
            "code": r["code"],
            "close": px[r["code"]],
            "score": float(r["sc"]),
            "roe": float(r["net_income_ttm"] / r["equity"]),
            "debt_ratio": float(r["debt_ratio"]),
            "pbr": float(r["marcap"] / r["equity"]),
            "per": float(r["marcap"] / r["net_income_ttm"]),
            "period": r["period"],
        })
    return out
