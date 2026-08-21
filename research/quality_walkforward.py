"""
퀄리티/가치 전략 워크포워드 검증.

훈련/검증 고정 분할로는 더 못 간다. 2025~2026은 역발상 가설들에 이미 소진됐고,
퀄리티 전략의 훈련/검증 결과(quality_backtest.py)를 보고 랭킹을 고르면 그 순간
정직한 점수가 아니다. 그래서 규칙 선택 자체를 알고리즘에 맡긴다.

  선택 규칙: '학습 구간 구간평균 수익률 최대' — 고정. 사람이 결과를 보고 고르지 않는다.
  후보: quality / value / combo 셋뿐이다. 늘리면 우연히 좋아 보이는 것이 생긴다.
  대조군: filtered(흑자·자본·부채비율 필터만, 랭킹 없음) 고정.
          선택이 값어치를 하는지 보려면 '고르지 않은 것'과 비교해야 한다.

월별 리밸런싱을 쓴다. 분기로는 3.3년에 구간이 8개뿐이라 창을 나누면 창당 3구간이
되어 선택이 사실상 무작위가 된다.

시작이 2023-04인 이유: 재무가 2022Q1부터라 TTM 4분기가 채워지는 첫 시점이
2022Q4이고, 그 공시가능일이 2023-03-31이다.

재현: python research/quality_walkforward.py
"""
import os, sys, time
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

import numpy as np
import pandas as pd
import research.portfolio_backtest as pb
import research.quality_backtest as qb
from backtester.store import save_run

# argv를 쓰면 안 된다 — portfolio_backtest가 import 시점에 sys.argv[1]을
# 백테스트 시작일로 읽는다. 슬롯 수를 넘기면 날짜 파싱에서 터진다.
SLOTS = int(os.environ.get("WF_SLOTS", "20"))
KINDS = ["quality", "value", "combo"]
CONTROL = "filtered"
MIN_TRAIN_PERIODS = 8      # 이보다 적으면 학습을 믿지 않고 기본값을 쓴다
DEFAULT_KIND = "quality"

WINDOWS = [
    ("2023-04-01", "2024-03-31", "2024-04-01", "2024-09-30"),
    ("2023-04-01", "2024-09-30", "2024-10-01", "2025-03-31"),
    ("2023-04-01", "2025-03-31", "2025-04-01", "2025-09-30"),
    ("2023-04-01", "2025-09-30", "2025-10-01", "2026-03-31"),
    ("2023-04-01", "2026-03-31", "2026-04-01", "2026-08-19"),
]
SPAN_START, SPAN_END = "2022-01-01", "2026-08-19"


def stat(periods):
    if not periods:
        return None
    r = np.array([p["net"] for p in periods])
    comp = float(np.prod(1 + r) - 1)
    tv = (r.mean() / (r.std(ddof=1) / np.sqrt(len(r)))
          if len(r) > 1 and r.std(ddof=1) > 0 else 0.0)
    return {"n": len(r), "mean_pct": float(r.mean() * 100), "t_val": float(tv),
            "compound_pct": comp * 100,
            "win_rate": float((r > 0).mean() * 100)}


def main():
    t0 = time.time()
    pb.START, pb.END = SPAN_START, SPAN_END
    pb.WARMUP = "2020-11-27"
    codes = pb.universe()
    print(f"유니버스 {len(codes):,}종목 — 로딩 중...", flush=True)
    px, _ = pb.load(codes)
    px = pb.add_marcap(px)
    fin = qb.load_financials()
    print(f"  일봉 {len(px):,}행 ({time.time()-t0:.0f}s)\n", flush=True)

    def sim(kind, s, e):
        return qb.simulate(px, fin, kind, SLOTS, s, e, rebal_months=qb.MONTHLY)

    picked, oos, base = [], [], []
    for tr_s, tr_e, te_s, te_e in WINDOWS:
        print(f"[{tr_s[:7]}~{tr_e[:7]} 학습 → {te_s[:7]}~{te_e[:7]} 시험]", flush=True)
        best, best_v = None, None
        for k in KINDS:
            st = stat(sim(k, tr_s, tr_e))
            if st is None or st["n"] < MIN_TRAIN_PERIODS:
                print(f"    학습 {k:10} 구간 부족")
                continue
            print(f"    학습 {k:10} {st['n']:>3}구간  {st['mean_pct']:+6.2f}%")
            if best_v is None or st["mean_pct"] > best_v:
                best, best_v = k, st["mean_pct"]
        choice = best or DEFAULT_KIND
        print(f"  선택: {choice}")

        te = sim(choice, te_s, te_e)
        bs = sim(CONTROL, te_s, te_e)
        oos += te
        base += bs
        picked.append(choice)
        a, b = stat(te), stat(bs)
        print(f"    시험 {a['n']:>3}구간 {a['mean_pct']:+6.2f}% (t{a['t_val']:+.2f})"
              f"  |  대조군 {b['n']:>3}구간 {b['mean_pct']:+6.2f}%\n", flush=True)

    print("=" * 70)
    for label, ps, key in [("워크포워드 선택", oos, "quality_walkforward"),
                           ("고정 대조군(필터만)", base, "quality_walkforward_base")]:
        s = stat(ps)
        yrs = s["n"] / 12
        ann = (1 + s["compound_pct"] / 100) ** (1 / yrs) - 1
        print(f"{label}")
        print(f"  시험구간 {s['n']}개  구간평균 {s['mean_pct']:+.2f}%  "
              f"승률 {s['win_rate']:.0f}%  t값 {s['t_val']:+.2f}")
        print(f"  누적 {s['compound_pct']:+.1f}%  연환산 {ann*100:+.1f}%")
        save_run(f"{key}_s{SLOTS}", WINDOWS[0][2], WINDOWS[-1][3],
                 {"slots": SLOTS, "rebalance": "월별", "stop_pct": None,
                  "walkforward": True, "point_in_time_universe": True,
                  "windows": len(WINDOWS), "kind_pool": KINDS,
                  "selection": "학습 구간평균 최대" if ps is oos else "고정(filtered)",
                  "picked": picked if ps is oos else []},
                 dict(s, annual_pct=ann * 100), [])
    print(f"\n총 {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
