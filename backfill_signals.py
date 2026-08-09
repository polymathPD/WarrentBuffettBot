"""
contrarian_signals 백필 스크립트 (백테스트용)

processor/signals.py의 heat_score 계산 로직과 동일하지만,
종목별로 SQL을 반복 호출하지 않고 pandas로 벡터화하여 빠르게 처리한다.
(전체 종목 x 수년치 데이터를 하루 단위로 순회하면 매우 느리기 때문)

- flow_ratio: 개인 순매수 / 과거 30일 |개인 순매수| 평균
- vol_ratio : 거래대금(종가*거래량) / 과거 30일 거래대금 평균
- credit_ratio: credit_balance 데이터 없음 -> 항상 0 기여 (운영 코드와 동일한 동작)
"""
import sys, os, time
sys.path.insert(0, os.path.dirname(__file__))

import numpy as np
import pandas as pd
import db.connection as db
import config

WINDOW = 30
BATCH = 5000


def backfill(start_date: str, end_date: str, buffer_days: int = 60):
    end_buffer = pd.Timestamp(end_date) + pd.Timedelta(days=buffer_days)

    print("stock_daily 로딩 중...")
    daily = pd.DataFrame(db.fetchall("SELECT code, d, c, v FROM stock_daily ORDER BY code, d"))
    daily["d"] = pd.to_datetime(daily["d"])
    daily["amt"] = daily["c"].astype(float) * daily["v"].astype(float)
    print(f"  {len(daily):,}행")

    print("investor_flow 로딩 중...")
    flow = pd.DataFrame(db.fetchall("SELECT code, d, individual_net FROM investor_flow ORDER BY code, d"))
    flow["d"] = pd.to_datetime(flow["d"])
    flow["individual_net"] = flow["individual_net"].astype(float)
    print(f"  {len(flow):,}행")

    merged = daily.merge(flow[["code", "d", "individual_net"]], on=["code", "d"], how="left")

    out_rows = []
    t0 = time.time()
    codes = merged["code"].unique()
    print(f"\n{len(codes):,}개 종목 벡터화 계산 중...")

    for i, (code, g) in enumerate(merged.groupby("code", sort=False)):
        if len(g) < WINDOW + 1:
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

        score = np.where(valid_fr, contrib_fr, 0) + np.where(valid_vr, contrib_vr, 0)
        has_any = valid_fr | valid_vr
        heat = np.where(has_any, score, 0.0)

        signal = np.full(len(g), "neutral", dtype=object)
        signal[heat >= config.HEAT_SELL] = "sell"
        signal[(heat >= config.HEAT_AVOID) & (heat < config.HEAT_SELL)] = "avoid"

        dates = g["d"].to_numpy()
        in_range = (dates >= np.datetime64(start_date)) & (dates <= np.datetime64(end_buffer))
        idxs = np.where(in_range)[0]
        idxs = idxs[idxs >= WINDOW]  # 초기 WINDOW구간은 계산 불가

        for idx in idxs:
            out_rows.append((
                code,
                g.loc[idx, "d"].date(),
                float(fr[idx]) if valid_fr[idx] else None,
                None,
                float(vr[idx]) if valid_vr[idx] else None,
                float(heat[idx]),
                str(signal[idx]),
            ))

        if (i + 1) % 500 == 0:
            print(f"  {i+1:,}/{len(codes):,}종목 처리, 누적 {len(out_rows):,}행 ({time.time()-t0:.0f}s)")

    print(f"\n계산 완료: {len(out_rows):,}행, {time.time()-t0:.0f}s")

    print("DB 삽입 중...")
    t1 = time.time()
    for i in range(0, len(out_rows), BATCH):
        chunk = out_rows[i:i + BATCH]
        db.executemany(
            """INSERT INTO contrarian_signals
               (code, d, individual_flow_ratio, credit_surge_ratio,
                volume_ratio, heat_score, signal)
               VALUES %s ON CONFLICT (code, d) DO UPDATE
               SET heat_score = EXCLUDED.heat_score,
                   signal = EXCLUDED.signal,
                   individual_flow_ratio = EXCLUDED.individual_flow_ratio,
                   volume_ratio = EXCLUDED.volume_ratio""",
            chunk,
        )
        print(f"  {min(i+BATCH, len(out_rows)):,}/{len(out_rows):,}", end="\r")

    print(f"\n삽입 완료: {len(out_rows):,}행 ({time.time()-t1:.0f}s)")


if __name__ == "__main__":
    s = sys.argv[1] if len(sys.argv) > 1 else "2022-01-01"
    e = sys.argv[2] if len(sys.argv) > 2 else "2024-12-31"
    backfill(s, e)
