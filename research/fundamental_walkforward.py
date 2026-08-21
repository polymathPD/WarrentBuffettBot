"""
펀더멘털 전략 워크포워드 검증 (fundamental_v2)

fundamental_v1은 2025-2026 홀드아웃에서 기각됐다. 거기에 PBR을 얹어 같은 구간에서
다시 재면 그 구간이 훈련 데이터가 되므로, 규칙 선택 자체를 자동화한 워크포워드로 간다.

  2022        -> 2023 시험
  2022~2023   -> 2024 시험
  2022~2024   -> 2025 시험
  2022~2025   -> 2026 시험

각 시험 구간의 규칙은 그 앞 구간만 보고 정해진다. 사람이 결과를 보고 고르는 순간
오염되므로, 선택은 '학습 구간에서 거래당 평균이 가장 높은 변형'으로 고정한다.

변형은 PBR 상한 하나뿐이다. 후보를 늘릴수록 우연히 좋아 보이는 것이 생긴다.
시가총액 필터는 이제 shares 기반 시점 정합 값이라 학습·시험 양쪽에 항상 켠다
(실전 경로와 같은 규칙으로 돌리기 위해서다).
"""
import os, sys, time
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

import numpy as np
import pandas as pd
import db.connection as db
from backtester.fundamental_sim import load_prices, simulate, stats
from backtester.store import save_run
from strategy.fundamental import get_entry_candidates
from processor.valuation import market_caps, pbr

MIN_MARCAP = 300_000_000_000
PBR_LIMITS = [None, 1.0, 2.0, 3.0]     # 선택 대상 변형
MIN_TRAIN_TRADES = 10                   # 이보다 적으면 학습 결과를 믿지 않고 기본값
SLOTS = 5

WINDOWS = [
    ("2022-01-01", "2022-12-31", "2023-01-01", "2023-12-31"),
    ("2022-01-01", "2023-12-31", "2024-01-01", "2024-12-31"),
    ("2022-01-01", "2024-12-31", "2025-01-01", "2025-12-31"),
    ("2022-01-01", "2025-12-31", "2026-01-01", "2026-08-09"),
]
SPAN_START, SPAN_END = "2022-01-01", "2026-08-09"


def collect_candidates() -> dict:
    """공시일 -> [후보]. 시총 필터와 PBR을 그 시점 값으로 붙인다."""
    dates = [r["d"] for r in db.fetchall(
        """SELECT DISTINCT d FROM disclosures
           WHERE d BETWEEN %s::date AND %s::date
             AND (report_nm LIKE '분기보고서%%' OR report_nm LIKE '반기보고서%%'
               OR report_nm LIKE '사업보고서%%')
           ORDER BY d""", (SPAN_START, SPAN_END))]

    out = {}
    print(f"공시일 {len(dates)}일 검사 중...", flush=True)
    for i, d in enumerate(dates, 1):
        ds = d.strftime("%Y-%m-%d")
        cands = get_entry_candidates(ds, apply_marcap=False)
        if not cands:
            continue
        codes = [c["code"] for c in cands]
        caps, ratios = market_caps(codes, ds), pbr(codes, ds)
        kept = []
        for c in cands:
            cap = caps.get(c["code"])
            if cap is None or cap < MIN_MARCAP:      # 시총 필터 (시점 정합)
                continue
            c["pbr"] = ratios.get(c["code"])
            kept.append(c)
        if kept:
            out[d] = kept
        if i % 100 == 0:
            print(f"  {i}/{len(dates)}일  후보 있는 날 {len(out)}일", flush=True)
    print(f"후보 {sum(len(v) for v in out.values()):,}건 / {len(out)}일")
    return out


def apply_pbr(by_date: dict, limit) -> dict:
    """PBR 상한을 적용한 후보 집합. limit이 None이면 그대로."""
    if limit is None:
        return by_date
    out = {}
    for d, cands in by_date.items():
        kept = [c for c in cands if c["pbr"] is not None and c["pbr"] <= limit]
        if kept:
            out[d] = kept
    return out


def main():
    t0 = time.time()
    by_date = collect_candidates()
    if not by_date:
        print("후보 없음")
        return
    codes = sorted({c["code"] for v in by_date.values() for c in v})
    px = load_prices(codes, SPAN_START, SPAN_END)
    print(f"가격 {len(px):,}종목 로딩 ({time.time()-t0:.0f}s)\n")

    variants = {lim: apply_pbr(by_date, lim) for lim in PBR_LIMITS}
    picked, oos, baseline = [], [], []

    for tr_s, tr_e, te_s, te_e in WINDOWS:
        scores = {}
        for lim in PBR_LIMITS:
            st = stats(simulate(variants[lim], px, SLOTS, start=tr_s, end=tr_e))
            scores[lim] = st
        # 자동 선택: 학습 구간 거래당 평균 최대. 표본이 적으면 기본값(PBR 미적용).
        usable = {k: v for k, v in scores.items() if v["n"] >= MIN_TRAIN_TRADES}
        best = max(usable, key=lambda k: usable[k]["mean_pct"]) if usable else None

        test = simulate(variants[best], px, SLOTS, start=te_s, end=te_e)
        base = simulate(variants[None], px, SLOTS, start=te_s, end=te_e)
        oos += test
        baseline += base
        picked.append((te_s[:4], best, scores, stats(test), stats(base)))

        label = f"PBR<={best}" if best else "PBR 미적용"
        print(f"[{tr_s[:4]}~{tr_e[:4]} 학습 -> {te_s[:4]} 시험]  선택: {label}")
        for lim in PBR_LIMITS:
            s = scores[lim]
            mark = " <-" if lim == best else ""
            print(f"    학습 PBR<={str(lim):5s} {s['n']:4d}건 {s.get('mean_pct', 0):+7.2f}%{mark}")
        t, b = stats(test), stats(base)
        print(f"    시험 결과   {t['n']:4d}건 {t.get('mean_pct',0):+7.2f}% (t {t.get('t_val',0):+.2f})"
              f"   |  PBR 미적용 대조군 {b['n']:4d}건 {b.get('mean_pct',0):+7.2f}%\n", flush=True)

    print("=" * 70)
    for name, trades in (("fundamental_v2 (워크포워드 선택)", oos),
                         ("대조군 (PBR 미적용 고정)", baseline)):
        s = stats(trades)
        if not s["n"]:
            print(f"{name}: 거래 없음"); continue
        print(f"{name}")
        print(f"  시험구간 합계 {s['n']:,}건  평균 {s['mean_pct']:+.2f}%  "
              f"승률 {s['win_rate']:.1f}%  t값 {s['t_val']:+.2f}")
        print(f"  평균 보유 {s['avg_held_days']:.1f}거래일  청산사유 {s['reasons']}")

    # pbr_applied가 이 둘을 가르는 유일한 knob이다. 이게 빠져 있으면 두 실행의
    # params가 완전히 같아져서, 대시보드가 "같은 규칙을 같은 구간에 두 번 잰 것"으로
    # 읽고 한쪽을 이전 측정으로 치워 버린다.
    for name, trades, strategy, pbr_applied in (
        ("v2", oos, "fundamental_v2_walkforward", True),
        ("base", baseline, "fundamental_v1_walkforward_base", False),
    ):
        s = stats(trades)
        if s["n"]:
            save_run(strategy, WINDOWS[0][2], WINDOWS[-1][3],
                     {"slots": SLOTS, "pbr_applied": pbr_applied,
                      "pbr_limits": [str(x) for x in PBR_LIMITS],
                      # 시총 하한은 끄고 돌린다(get_entry_candidates(apply_marcap=False)). FDR은 현재
                      # 시총만 주므로 과거 시점에 적용하면 상장폐지 종목이 통째로 빠진다.
                      "marcap_filter": False, "windows": len(WINDOWS),
                      "selection": "학습 구간 거래당 평균 최대" if pbr_applied else "PBR 미적용 고정"},
                     s,
                     [{"code": t["code"],
                       "entry_d": pd.Timestamp(t["entry_d"]).date(),
                       "exit_d": pd.Timestamp(t["exit_d"]).date(),
                       "entry_px": t["entry_px"], "exit_px": t["exit_px"],
                       "ret_pct": t["ret"], "exit_reason": t["reason"]} for t in trades])

    print(f"\n완료 {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
