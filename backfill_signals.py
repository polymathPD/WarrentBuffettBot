"""
contrarian_signals 백필 스크립트 (백테스트용)

processor/signals.py의 heat_score 계산 로직과 동일하지만,
종목별로 SQL을 반복 호출하지 않고 pandas로 벡터화하여 빠르게 처리한다.
(전체 종목 x 수년치 데이터를 하루 단위로 순회하면 매우 느리기 때문)

- flow_ratio: 개인 순매수 / 과거 30일 |개인 순매수| 평균
- vol_ratio : 거래대금(종가*거래량) / 과거 30일 거래대금 평균
- credit_surge_ratio: 신용잔고 / 과거 30일 신용잔고 평균
  (결제일 기준이라 거래일과 어긋날 수 있어 종목별 ffill 후 사용 — processor/signals.py가
   "d 이하 최신 행"을 쓰는 것과 같은 의미)
- credit_ratio_level: 신용잔고 비율(수준). 기록만 하고 heat_score에는 미반영
- foreign/institution_flow_ratio: 동일 방식으로 계산해 기록만 한다 (heat_score 미반영,
  processor/signals.py와 동일 — 이유는 그쪽 _heat() 주석 참고)
"""
import sys, os, time
sys.path.insert(0, os.path.dirname(__file__))

import numpy as np
import pandas as pd
import db.connection as db
import config

WINDOW = 30
BATCH = 5000


def _load_batch(codes: list[str], load_floor):
    """해당 종목들의 일봉·수급·신용을 하나의 DataFrame으로 합쳐 반환."""
    daily = pd.DataFrame(db.fetchall(
        "SELECT code, d, c, v FROM stock_daily "
        "WHERE code = ANY(%s) AND d >= %s ORDER BY code, d", (codes, load_floor)))
    if daily.empty:
        return daily
    daily["d"] = pd.to_datetime(daily["d"])
    daily["amt"] = daily["c"].astype(float) * daily["v"].astype(float)

    flow = pd.DataFrame(db.fetchall(
        "SELECT code, d, individual_net, foreign_net, institution_net FROM investor_flow "
        "WHERE code = ANY(%s) AND d >= %s ORDER BY code, d", (codes, load_floor)))
    if flow.empty:
        flow = pd.DataFrame(columns=["code", "d", "individual_net",
                                     "foreign_net", "institution_net"])
    else:
        flow["d"] = pd.to_datetime(flow["d"])
        for c in ("individual_net", "foreign_net", "institution_net"):
            flow[c] = flow[c].astype(float)

    credit = pd.DataFrame(db.fetchall(
        "SELECT code, d, credit_amt, credit_ratio FROM credit_balance "
        "WHERE code = ANY(%s) AND d >= %s ORDER BY code, d", (codes, load_floor)))
    if credit.empty:
        credit = pd.DataFrame(columns=["code", "d", "credit_amt", "credit_ratio"])
    else:
        credit["d"] = pd.to_datetime(credit["d"])
        credit["credit_amt"] = credit["credit_amt"].astype(float)
        credit["credit_ratio"] = credit["credit_ratio"].astype(float)

    merged = daily.merge(flow, on=["code", "d"], how="left")
    return merged.merge(credit, on=["code", "d"], how="left")


def backfill(start_date: str, end_date: str, buffer_days: int = 60,
             code_batch: int = 300, start_idx: int = 0):
    """
    종목을 code_batch개씩 끊어 적재→계산→삽입한다.

    전 종목·전 기간을 한 번에 올리면(일봉 350만 + 수급 236만 + 신용 106만 행)
    fetchall이 만드는 파이썬 dict/Decimal 객체만으로 메모리가 터진다. 배치로
    끊으면 사용량이 배치 크기에 비례해 일정하게 유지된다.

    code_batch=300은 전 구간(2022~2026)을 돌릴 때 프로세스가 조용히 죽는 크기다
    (출력도 안 남기고 사라진다). 60으로 낮추면 완주한다.

    start_idx: 종목코드 오름차순 기준 시작 위치. 중간에 끊겼을 때 이어붙인다.
    삽입이 UPSERT라 겹치는 구간을 다시 돌려도 안전하니 넉넉히 앞에서 시작해도 된다.
    """
    end_buffer = pd.Timestamp(end_date) + pd.Timedelta(days=buffer_days)
    # WINDOW(30거래일) 워밍업 확보용 여유. 휴장일을 감안해 넉넉히 잡는다.
    load_floor = (pd.Timestamp(start_date) - pd.Timedelta(days=120)).date()

    all_codes = [r["code"] for r in
                 db.fetchall("SELECT DISTINCT code FROM stock_daily ORDER BY code")]
    total_codes = len(all_codes)
    all_codes = all_codes[start_idx:]
    print(f"대상 {len(all_codes):,}종목"
          + (f" ({start_idx:,}번째부터 재개, 전체 {total_codes:,})" if start_idx else "")
          + f", {code_batch}종목씩 처리 (기준일 {load_floor} 이후)", flush=True)

    t0 = time.time()
    total_out = 0
    for b0 in range(0, len(all_codes), code_batch):
        batch = all_codes[b0:b0 + code_batch]
        merged = _load_batch(batch, load_floor)
        if merged.empty:
            continue
        total_out += _process(merged, start_date, end_buffer)
        print(f"  {start_idx + min(b0 + code_batch, len(all_codes)):,}/{total_codes:,}종목  "
              f"누적 {total_out:,}행  ({time.time() - t0:.0f}s)", flush=True)

    print(f"\n완료: {total_out:,}행, {time.time() - t0:.0f}s")


def _process(merged, start_date, end_buffer) -> int:
    out_rows = []
    for code, g in merged.groupby("code", sort=False):
        if len(g) < WINDOW + 1:
            continue
        g = g.sort_values("d").reset_index(drop=True)

        amt_roll = g["amt"].shift(1).rolling(WINDOW).mean()
        vol_r = g["amt"] / amt_roll

        ind_abs_roll = g["individual_net"].abs().shift(1).rolling(WINDOW).mean()
        flow_r = g["individual_net"] / ind_abs_roll

        # 관측용 (heat_score 미반영)
        frgn_abs_roll = g["foreign_net"].abs().shift(1).rolling(WINDOW).mean()
        frgn_r = (g["foreign_net"] / frgn_abs_roll).to_numpy()
        orgn_abs_roll = g["institution_net"].abs().shift(1).rolling(WINDOW).mean()
        orgn_r = (g["institution_net"] / orgn_abs_roll).to_numpy()

        # 신용잔고는 결제일 기준이라 거래일과 어긋날 수 있어 ffill로 최신값을 끌어온다
        cred = g["credit_amt"].ffill()
        cred_roll = cred.shift(1).rolling(WINDOW).mean()
        cred_lvl = g["credit_ratio"].ffill().to_numpy()

        fr = flow_r.to_numpy()
        vr = vol_r.to_numpy()
        cr = (cred / cred_roll).to_numpy()
        valid_fr = ~np.isnan(fr) & (ind_abs_roll.to_numpy() > 0)
        valid_vr = ~np.isnan(vr) & (amt_roll.to_numpy() > 0)
        valid_cr = ~np.isnan(cr) & (cred_roll.to_numpy() > 0)

        contrib_fr = np.clip((fr - 1.0) * 3.0, 0, 4.0)
        contrib_vr = np.clip((vr - 1.5) * 2.0, 0, 3.0)
        contrib_cr = np.clip((cr - 1.0) * 3.0, 0, 3.0)

        score = (np.where(valid_fr, contrib_fr, 0)
                 + np.where(valid_vr, contrib_vr, 0)
                 + np.where(valid_cr, contrib_cr, 0))
        has_any = valid_fr | valid_vr | valid_cr
        heat = np.where(has_any, score, 0.0)

        signal = np.full(len(g), "neutral", dtype=object)
        signal[heat >= config.HEAT_SELL] = "sell"
        signal[(heat >= config.HEAT_AVOID) & (heat < config.HEAT_SELL)] = "avoid"

        dates = g["d"].to_numpy()
        in_range = (dates >= np.datetime64(start_date)) & (dates <= np.datetime64(end_buffer))
        idxs = np.where(in_range)[0]
        idxs = idxs[idxs >= WINDOW]  # 초기 WINDOW구간은 계산 불가

        def _f(v):
            return None if v is None or np.isnan(v) else float(v)

        for idx in idxs:
            out_rows.append((
                code,
                g.loc[idx, "d"].date(),
                float(fr[idx]) if valid_fr[idx] else None,
                float(cr[idx]) if valid_cr[idx] else None,
                float(vr[idx]) if valid_vr[idx] else None,
                _f(frgn_r[idx]),
                _f(orgn_r[idx]),
                _f(cred_lvl[idx]),
                float(heat[idx]),
                str(signal[idx]),
            ))

    for i in range(0, len(out_rows), BATCH):
        chunk = out_rows[i:i + BATCH]
        db.executemany(
            """INSERT INTO contrarian_signals
               (code, d, individual_flow_ratio, credit_surge_ratio,
                volume_ratio, foreign_flow_ratio, institution_flow_ratio,
                credit_ratio_level, heat_score, signal)
               VALUES %s ON CONFLICT (code, d) DO UPDATE
               SET heat_score = EXCLUDED.heat_score,
                   signal = EXCLUDED.signal,
                   individual_flow_ratio = EXCLUDED.individual_flow_ratio,
                   credit_surge_ratio = EXCLUDED.credit_surge_ratio,
                   volume_ratio = EXCLUDED.volume_ratio,
                   foreign_flow_ratio = EXCLUDED.foreign_flow_ratio,
                   institution_flow_ratio = EXCLUDED.institution_flow_ratio,
                   credit_ratio_level = EXCLUDED.credit_ratio_level""",
            chunk,
        )

    return len(out_rows)


if __name__ == "__main__":
    s = sys.argv[1] if len(sys.argv) > 1 else "2022-01-01"
    e = sys.argv[2] if len(sys.argv) > 2 else "2024-12-31"
    batch = int(sys.argv[3]) if len(sys.argv) > 3 else 60
    idx = int(sys.argv[4]) if len(sys.argv) > 4 else 0
    backfill(s, e, code_batch=batch, start_idx=idx)
