"""
분위수(decile) 분석: heat_score 및 하위 지표(개인수급비율/거래대금비율)가
실제로 향후 수익률과 관계가 있는지 문턱값(threshold) 없이 확인한다.

기존 백테스트는 heat<7 / stop -7% / 20일 만기 같은 "문턱값"을 미리 정해두고
평가했기 때문에 그 문턱값 자체가 잘못됐을 가능성을 배제할 수 없었다.
이 스크립트는 문턱값을 전혀 사용하지 않고, 지표 값으로 종목을 10분위로 나눈 뒤
각 분위의 향후 N일 수익률을 그대로 관찰한다 (과최적화 위험이 거의 없는 방식).

추가로 같은 기간 KOSPI 지수 buy&hold 수익률과 비교해 시장 베타 효과를 분리한다.
(코스피 데이터는 참조 DB의 benchmark 테이블 사용 - 월간 종가)
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

import numpy as np
import pandas as pd
import psycopg2
from psycopg2.extras import RealDictCursor
import db.connection as db

WINDOW = 30
HORIZONS = [5, 10, 20]  # 진입 다음날 시가 기준 향후 N거래일 종가까지
REF_DB_URL = "postgresql://postgres:OOcMdAMhSvFryGWkYqiuDxJMQBajxYgu@hayabusa.proxy.rlwy.net:46481/railway"


def load_kospi_benchmark():
    conn = psycopg2.connect(REF_DB_URL)
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT ym, close FROM benchmark ORDER BY ym")
    rows = cur.fetchall()
    conn.close()
    df = pd.DataFrame(rows)
    df["close"] = df["close"].astype(float)
    return df


def load_data(load_floor="2021-09-01"):
    """분석에 필요한 범위만 적재한다 (전 기간을 통째로 올리면 메모리가 터진다).
    WINDOW 워밍업과 향후수익률 계산 꼬리를 감안해 시작일보다 넉넉히 앞에서 자른다."""
    print(f"stock_daily 로딩 중... ({load_floor} 이후)")
    daily = pd.DataFrame(db.fetchall(
        "SELECT code, d, o, c, v FROM stock_daily WHERE d >= %s ORDER BY code, d",
        (load_floor,)))
    daily["d"] = pd.to_datetime(daily["d"])
    for col in ("o", "c", "v"):
        daily[col] = daily[col].astype(float)
    daily["amt"] = daily["c"] * daily["v"]
    print(f"  {len(daily):,}행")

    print("investor_flow 로딩 중...")
    flow = pd.DataFrame(db.fetchall(
        "SELECT code, d, individual_net FROM investor_flow WHERE d >= %s ORDER BY code, d",
        (load_floor,)))
    flow["d"] = pd.to_datetime(flow["d"])
    flow["individual_net"] = flow["individual_net"].astype(float)
    print(f"  {len(flow):,}행")

    return daily.merge(flow, on=["code", "d"], how="left")


def compute_metrics_and_forward_returns(merged: pd.DataFrame) -> pd.DataFrame:
    """종목별 heat_score/flow_r/vol_r 및 향후 N일 수익률(비용 미반영, 순수 신호검증용)을 계산"""
    out = []
    codes = merged["code"].unique()
    print(f"\n{len(codes):,}개 종목 벡터화 계산 중...")

    for i, (code, g) in enumerate(merged.groupby("code", sort=False)):
        if len(g) < WINDOW + max(HORIZONS) + 2:
            continue
        g = g.sort_values("d").reset_index(drop=True)

        amt_roll = g["amt"].shift(1).rolling(WINDOW).mean()
        vol_r = g["amt"] / amt_roll

        ind_abs_roll = g["individual_net"].abs().shift(1).rolling(WINDOW).mean()
        flow_r = g["individual_net"] / ind_abs_roll

        fr = flow_r.to_numpy()
        vr = vol_r.to_numpy()
        valid_fr = ~np.isnan(fr) & (ind_abs_roll.to_numpy() > 0)
        valid_vr = ~np.isnan(vr) & (amt_roll.to_numpy() > 0)

        contrib_fr = np.clip((fr - 1.0) * 3.0, 0, 4.0)
        contrib_vr = np.clip((vr - 1.5) * 2.0, 0, 3.0)
        heat = np.where(valid_fr, contrib_fr, 0) + np.where(valid_vr, contrib_vr, 0)

        o = g["o"].to_numpy()
        c = g["c"].to_numpy()
        n = len(g)

        # 진입: t+1 시가, 청산: t+1+N 종가 (원시 수익률, 비용 미반영)
        entry_idx = np.arange(1, n)  # signal day t -> entry at t+1
        fwd = {}
        for h in HORIZONS:
            ret = np.full(n, np.nan)
            exit_idx = entry_idx + h
            valid = exit_idx < n
            ei = entry_idx[valid]
            xi = exit_idx[valid]
            entry_open = o[ei]
            exit_close = c[xi]
            with np.errstate(divide="ignore", invalid="ignore"):
                r = np.where(entry_open > 0, exit_close / entry_open - 1, np.nan)
            ret[ei - 1] = r  # signal day index = entry_idx-1
            fwd[h] = ret

        d_arr = g["d"].to_numpy()
        for h in HORIZONS:
            pass  # placeholder (per-horizon arrays already in fwd)

        chunk = pd.DataFrame({
            "code": code,
            "d": d_arr,
            "heat": heat,
            "flow_r": np.where(valid_fr, fr, np.nan),
            "vol_r": np.where(valid_vr, vr, np.nan),
            **{f"fwd_{h}": fwd[h] for h in HORIZONS},
        })
        out.append(chunk)

        if (i + 1) % 500 == 0:
            print(f"  {i+1:,}/{len(codes):,}종목 처리")

    result = pd.concat(out, ignore_index=True)
    print(f"계산 완료: {len(result):,}행")
    return result


def decile_report(df: pd.DataFrame, metric: str, horizon: int, label: str):
    sub = df[["d", metric, f"fwd_{horizon}"]].dropna()
    if len(sub) < 1000:
        print(f"  [{label}] {metric} h={horizon}: 표본 부족 ({len(sub)}행)")
        return

    sub = sub.copy()
    try:
        sub["decile"] = pd.qcut(sub[metric], 10, labels=False, duplicates="drop")
    except ValueError:
        print(f"  [{label}] {metric} h={horizon}: 분위 생성 실패 (값 다양성 부족)")
        return

    grp = sub.groupby("decile")[f"fwd_{horizon}"].agg(["mean", "std", "count"])
    baseline = sub[f"fwd_{horizon}"].mean()

    print(f"\n  --- {label} | 지표={metric} | 향후 {horizon}거래일 수익률 (전체 평균 {baseline*100:+.2f}%) ---")
    print(f"  {'분위':>4} {'구간(하한~상한)':>24} {'평균수익률':>10} {'표본수':>10}")
    for dec, row in grp.iterrows():
        lo = sub[sub["decile"] == dec][metric].min()
        hi = sub[sub["decile"] == dec][metric].max()
        mean_r = row["mean"] * 100
        n = int(row["count"])
        marker = " <=신규진입 임계값 부근" if metric == "heat" and lo <= 7.0 <= hi else ""
        print(f"  {int(dec):>4} {lo:>10.2f}~{hi:>10.2f} {mean_r:>+9.2f}% {n:>10,}{marker}")

    # 최하위 분위 vs 최상위 분위 스프레드
    d0 = grp.loc[grp.index.min(), "mean"] * 100
    d9 = grp.loc[grp.index.max(), "mean"] * 100
    print(f"  분위0(최저) {d0:+.2f}%  vs  분위9(최고) {d9:+.2f}%  스프레드 {d0-d9:+.2f}%p")


def benchmark_report(start: str, end: str):
    kospi = load_kospi_benchmark()
    kospi["ym_dt"] = pd.to_datetime(kospi["ym"] + "-01")
    start_ym = pd.Timestamp(start).replace(day=1)
    end_ym = pd.Timestamp(end).replace(day=1)

    before = kospi[kospi["ym_dt"] <= start_ym].sort_values("ym_dt")
    after = kospi[kospi["ym_dt"] <= end_ym].sort_values("ym_dt")
    if before.empty or after.empty:
        print("  KOSPI 벤치마크 데이터 부족")
        return

    start_close = before.iloc[-1]["close"]
    end_close = after.iloc[-1]["close"]
    total_ret = end_close / start_close - 1
    months = (end_ym.year - start_ym.year) * 12 + (end_ym.month - start_ym.month)
    monthly_avg = (1 + total_ret) ** (1 / months) - 1 if months > 0 else 0

    print(f"\n[KOSPI 벤치마크] {start} ~ {end}")
    print(f"  누적 수익률: {total_ret*100:+.2f}%  (지수 {start_close:.1f} -> {end_close:.1f})")
    print(f"  월평균 수익률: {monthly_avg*100:+.2f}%  (참고: 전략의 20거래일=~1개월 보유와 비교)")


def main(start_date: str, end_date: str):
    merged = load_data()
    full = compute_metrics_and_forward_returns(merged)

    period = full[(full["d"] >= pd.Timestamp(start_date)) & (full["d"] <= pd.Timestamp(end_date))]
    print(f"\n{'='*70}")
    print(f"분위수 분석: {start_date} ~ {end_date} (백테스트 기간과 동일)")
    print(f"{'='*70}")
    for metric in ["heat", "flow_r", "vol_r"]:
        for h in HORIZONS:
            decile_report(period, metric, h, f"{start_date}~{end_date}")

    print(f"\n{'='*70}")
    print("분위수 분석: 전체 가용 기간 (레짐 의존성 확인용)")
    print(f"{'='*70}")
    full_start = str(full["d"].min().date())
    full_end = str(full["d"].max().date())
    print(f"  기간: {full_start} ~ {full_end}")
    for metric in ["heat", "flow_r", "vol_r"]:
        decile_report(full, metric, 20, f"{full_start}~{full_end}")

    benchmark_report(start_date, end_date)
    benchmark_report(full_start, full_end)


if __name__ == "__main__":
    s = sys.argv[1] if len(sys.argv) > 1 else "2022-01-01"
    e = sys.argv[2] if len(sys.argv) > 2 else "2024-12-31"
    main(s, e)
