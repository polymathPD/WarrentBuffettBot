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
from backtester.fundamental_sim import load_prices, simulate, MAX_HOLD, STOP_PCT
from backtester.store import save_run
from strategy.fundamental import get_entry_candidates, MIN_HOLD_DAYS

START = sys.argv[1] if len(sys.argv) > 1 else "2022-01-01"
END = sys.argv[2] if len(sys.argv) > 2 else "2024-12-31"
# 훈련/검증 결과가 서로 덮어쓰지 않도록 저장 이름에 붙일 꼬리표
SUFFIX = sys.argv[3] if len(sys.argv) > 3 else ""
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
    px = load_prices(codes, START, END)
    print(f"  {sum(len(v['d']) for v in px.values()):,}행  ({time.time()-t0:.0f}s)\n")

    for slots in (5, 12):
        report(simulate(by_date, px, slots), slots, f"fundamental_v1 {slots}슬롯")
    print(f"\n완료 {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
