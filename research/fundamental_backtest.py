"""
펀더멘털 전략(fundamental_v1) 슬롯 제약 백테스트.

후보 선정은 strategy/fundamental.py의 get_entry_candidates()를 그대로 호출한다 —
검증한 규칙과 운용 규칙이 갈라지지 않게 하기 위해서다. 시가총액 필터만 끈다
(FDR은 현재 시총만 주므로 과거에 적용하면 상장폐지 종목이 빠져 성과가 부풀려진다).

비용은 backtester/cost_model.py를 그대로 쓴다 (왕복 약 0.63%).
"""
import os, sys, time
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

import numpy as np
import pandas as pd
import db.connection as db
from backtester.cost_model import buy_price, sell_price
from backtester.store import save_run
from strategy.fundamental import get_entry_candidates, MIN_HOLD_DAYS

START = sys.argv[1] if len(sys.argv) > 1 else "2022-01-01"
END = sys.argv[2] if len(sys.argv) > 2 else "2024-12-31"
# 훈련/검증 결과가 서로 덮어쓰지 않도록 저장 이름에 붙일 꼬리표
SUFFIX = sys.argv[3] if len(sys.argv) > 3 else ""
MAX_HOLD = 20
STOP_PCT = 0.07


def filing_dates() -> list:
    rows = db.fetchall(
        """SELECT DISTINCT d FROM disclosures
           WHERE d BETWEEN %s::date AND %s::date
             AND (report_nm LIKE '분기보고서%%'
               OR report_nm LIKE '반기보고서%%'
               OR report_nm LIKE '사업보고서%%')
           ORDER BY d""",
        (START, END),
    )
    return [r["d"] for r in rows]


def collect_candidates() -> dict:
    """공시일 -> [후보]. 전략 함수를 그대로 호출한다."""
    out = {}
    dates = filing_dates()
    print(f"실적 보고서 공시일 {len(dates)}일 검사 중...")
    for i, d in enumerate(dates, 1):
        cands = get_entry_candidates(d.strftime("%Y-%m-%d"), apply_marcap=False)
        if cands:
            out[d] = cands
        if i % 100 == 0:
            print(f"  {i}/{len(dates)}일  후보 있는 날 {len(out)}일", flush=True)
    total = sum(len(v) for v in out.values())
    print(f"후보 {total:,}건 / {len(out)}일")
    return out


def load_prices(codes: list) -> dict:
    tail = (pd.Timestamp(END) + pd.Timedelta(days=90)).date()
    idx = {}
    for i in range(0, len(codes), 300):
        batch = codes[i:i + 300]
        df = pd.DataFrame(db.fetchall(
            "SELECT code, d, o, h, l, c FROM stock_daily "
            "WHERE code = ANY(%s) AND d >= %s::date AND d <= %s::date ORDER BY code, d",
            (batch, START, tail)))
        if df.empty:
            continue
        for col in ("o", "h", "l", "c"):
            df[col] = df[col].astype(float)
        df["d"] = pd.to_datetime(df["d"])
        for code, g in df.groupby("code", sort=False):
            g = g.sort_values("d")
            idx[code] = {k: g[k].to_numpy() for k in ("d", "o", "h", "l", "c")}
    return idx


def simulate(by_date: dict, px: dict, slots: int) -> list[dict]:
    """매일 슬롯 한도 안에서만 진입. 진입은 공시일 다음 거래일 시가."""
    all_dates = np.unique(np.concatenate([v["d"] for v in px.values()]))
    pos_at = {d: i for i, d in enumerate(all_dates)}

    positions = {}   # code -> dict
    trades = []

    for i, day in enumerate(all_dates):
        # (1) 청산 판정
        for code in list(positions):
            p = positions[code]
            arr = px[code]
            j = np.searchsorted(arr["d"], day)
            if j >= len(arr["d"]) or arr["d"][j] != day:
                continue
            held = i - p["entry_i"]
            reason = None
            if arr["l"][j] <= p["stop_px"]:
                fill, reason = float(sell_price(min(p["stop_px"], float(arr["o"][j])))), "stop"
            elif held >= MIN_HOLD_DAYS and held >= MAX_HOLD:
                fill, reason = float(sell_price(float(arr["c"][j]))), "expiry"
            if reason:
                # numpy 스칼라를 그대로 넘기면 psycopg2가 'np.float64(...)'를 SQL에 박는다
                # (processor/signals.py:_f() 주석 참고). 여기서 네이티브 float로 끊는다.
                trades.append({"code": code, "entry_d": p["entry_d"], "exit_d": day,
                               "entry_px": float(p["entry_px"]), "exit_px": float(fill),
                               "ret": float(fill / p["entry_px"] - 1), "reason": reason,
                               "held": int(held)})
                del positions[code]

        # (2) 그날 공시된 후보로 빈 슬롯 채우기 (다음 거래일 시가 체결)
        cands = by_date.get(pd.Timestamp(day).date())
        if not cands or i + 1 >= len(all_dates):
            continue
        nxt = all_dates[i + 1]
        for c in cands:
            if len(positions) >= slots:
                break
            code = c["code"]
            if code in positions or code not in px:
                continue
            arr = px[code]
            j = np.searchsorted(arr["d"], nxt)
            if j >= len(arr["d"]) or arr["d"][j] != nxt or arr["o"][j] <= 0:
                continue
            entry = float(buy_price(float(arr["o"][j])))
            positions[code] = {"entry_i": i + 1, "entry_px": entry,
                               "entry_d": nxt, "stop_px": entry * (1 - STOP_PCT)}
    return trades


def report(trades: list[dict], slots: int, label: str):
    if not trades:
        print(f"[{label}] 거래 없음")
        return
    rets = np.array([t["ret"] for t in trades])
    n = len(rets)
    t_val = rets.mean() / (rets.std() / np.sqrt(n)) if rets.std() > 0 else 0.0
    years = (pd.Timestamp(END) - pd.Timestamp(START)).days / 365.25
    ann = rets.mean() * (n / slots) / years
    reasons = pd.Series([t["reason"] for t in trades]).value_counts().to_dict()
    held = np.mean([t["held"] for t in trades])

    print(f"[{label}]")
    print(f"  거래 {n:,}건  평균 {rets.mean()*100:+.2f}%  승률 {(rets>0).mean()*100:.1f}%  t값 {t_val:+.2f}")
    print(f"  슬롯당 연 회전 {n/slots/years:.1f}회  ->  연환산 {ann*100:+.1f}%")
    print(f"  평균 보유 {held:.1f}거래일  청산사유 {reasons}")

    save_run(
        f"fundamental_v1_slot{slots}{SUFFIX}", START, END,
        {"slots": slots, "max_hold_days": MAX_HOLD, "stop_pct": STOP_PCT,
         "min_hold_days": MIN_HOLD_DAYS, "pos52w_filter": False, "marcap_filter": False},
        {"n": n, "mean_pct": float(rets.mean()*100), "std_pct": float(rets.std()*100),
         "win_rate": float((rets>0).mean()*100), "t_val": float(t_val),
         "annualized_pct": float(ann*100), "turnover_per_slot": float(n/slots/years),
         "avg_held_days": float(held), "reasons": reasons},
        [{"code": t["code"], "entry_d": pd.Timestamp(t["entry_d"]).date(),
          "exit_d": pd.Timestamp(t["exit_d"]).date(), "entry_px": t["entry_px"],
          "exit_px": t["exit_px"], "ret_pct": t["ret"], "exit_reason": t["reason"]}
         for t in trades],
    )


def main():
    t0 = time.time()
    by_date = collect_candidates()
    if not by_date:
        print("후보 없음")
        return
    codes = sorted({c["code"] for v in by_date.values() for c in v})
    print(f"가격 로딩: {len(codes):,}종목")
    px = load_prices(codes)
    print(f"  {sum(len(v['d']) for v in px.values()):,}행  ({time.time()-t0:.0f}s)\n")

    for slots in (5, 12):
        report(simulate(by_date, px, slots), slots, f"fundamental_v1 {slots}슬롯")
    print(f"\n완료 {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
