"""
검증 배터리: 기간 분리, 부트스트랩, 무작위 대조군
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import numpy as np
from backtester.engine import run, print_summary


def run_validation(start: str, end: str):
    print(f"\n{'='*50}")
    print(f"전체 기간 백테스트: {start} ~ {end}")
    all_trades = run(start, end)
    print_summary(all_trades)

    if len(all_trades) < 10:
        print("매매 수 부족, 검증 생략")
        return

    rets = np.array([t["net_ret"] for t in all_trades])

    # 1) 기간 분리
    n = len(all_trades)
    mid_idx = n // 2
    mid_date = all_trades[mid_idx]["entry_date"]

    print(f"\n[기간 분리] 전반: {start}~{mid_date} / 후반: {mid_date}~{end}")
    t1 = run(start, mid_date)
    t2 = run(mid_date, end)

    for label, trades in [("전반", t1), ("후반", t2)]:
        if not trades:
            print(f"  {label}: 매매 없음")
            continue
        r = np.array([t["net_ret"] for t in trades])
        print(f"  {label}: {len(r)}건  평균={r.mean()*100:+.2f}%  {'✓ 양수' if r.mean() > 0 else '✗ 음수'}")

    # 2) 부트스트랩
    n_iter = 1000
    pos_count = 0
    for _ in range(n_iter):
        sample = np.random.choice(rets, size=len(rets), replace=True)
        if sample.mean() > 0:
            pos_count += 1
    bp = pos_count / n_iter * 100
    print(f"\n[부트스트랩] {n_iter}회 재추출 양수 비율: {bp:.1f}%  {'✓' if bp >= 80 else '✗ 80% 미달'}")

    # 3) 무작위 대조군
    print(f"\n[무작위 대조군] 전략과 동일 거래 횟수·날짜로 무작위 선택")
    rand_means = []
    for _ in range(200):
        rand_mean = np.random.choice(rets, size=min(len(rets), 50), replace=False).mean()
        rand_means.append(rand_mean)
    rand_mean_dist = np.array(rand_means)
    strategy_mean = rets.mean()
    pct_rank = (rand_mean_dist < strategy_mean).mean() * 100
    print(f"  전략 평균 {strategy_mean*100:+.2f}% vs 무작위 분포 상위 {100-pct_rank:.1f}%")
    print(f"  {'✓ 대조군 상위 20%' if pct_rank >= 80 else '✗ 대조군 우위 없음'}")

    print(f"\n{'='*50}")


if __name__ == "__main__":
    import sys
    s = sys.argv[1] if len(sys.argv) > 1 else "2022-01-01"
    e = sys.argv[2] if len(sys.argv) > 2 else "2024-12-31"
    run_validation(s, e)
