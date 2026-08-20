"""dashboard/app.py - 백테스트 실행을 knob 조합과 판정으로 읽히게 하는 부분.

저장 이름(credit_rank_no52w_exit 등)만으로는 무슨 조합인지, 결론이 뭔지 알 수 없었다.
"""
from datetime import date, datetime

import pytest

from dashboard.app import (KNOB_KEY, _combined_verdict, _fingerprint, _knobs,
                           _overlaps, _verdict)


def _summary(n=100, mean=1.0, t=1.0):
    return {"n": n, "mean_pct": mean, "t_val": t}


def _run(rid, params, start, end, ts, summary=None):
    return {"id": rid, "params": params, "summary": summary or _summary(),
            "start_d": date.fromisoformat(start), "end_d": date.fromisoformat(end),
            "ts": datetime.fromisoformat(ts)}


def test_rank_direction_is_spelled_out():
    """ascending은 그 자체로 의미가 없다 — 랭킹 기준과 붙여야 읽힌다."""
    up = _knobs({"rank_col": "heat_score", "ascending": True})
    down = _knobs({"rank_col": "heat_score", "ascending": False})

    assert up[0]["value"] == "heat_score 오름차순"
    assert down[0]["value"] == "heat_score 내림차순"


def test_boolean_and_none_knobs_read_as_words():
    knobs = {k["label"]: k["value"] for k in _knobs(
        {"pos52w_filter": True, "marcap_filter": False,
         "exit_rank_pct": None, "exit_cols": [], "stop_pct": 0.07})}

    assert knobs["52주 하위 30% 필터"] == "켬"
    assert knobs["시가총액 하한"] == "끔"
    assert knobs["신호 청산"] == "없음"
    assert knobs["청산 감시 지표"] == "없음"
    assert knobs["손절"] == "-7%"


def test_exit_rank_pct_is_shown_as_the_percentile_it_watches():
    knobs = _knobs({"exit_rank_pct": 0.90})
    assert knobs[0]["value"] == "상위 10% 진입 시"


def test_key_knob_summary_keeps_column_order_stable():
    """비교표는 고정 열을 쓰므로 순서가 흔들리면 안 된다."""
    params = {"slots": 5, "rank_col": "heat_score", "ascending": True,
              "pos52w_filter": True, "exit_rank_pct": None, "stop_pct": 0.07}
    labels = [k["label"] for k in _knobs(params, KNOB_KEY)]

    assert labels == ["랭킹", "슬롯", "52주 하위 30% 필터", "신호 청산"]


@pytest.mark.parametrize("mean, t, kind", [
    (1.5, 2.4, "good"),    # 양수 + 유의
    (1.5, 1.1, "warn"),    # 양수지만 0과 구분 안 됨
    (-1.1, -2.2, "bad"),   # 음수 + 유의
    (-0.6, -0.6, "bad"),   # 음수, 유의하지 않음
])
def test_verdict_follows_sign_and_significance(mean, t, kind):
    assert _verdict(_summary(mean=mean, t=t))["kind"] == kind


def test_verdict_is_withheld_without_trades():
    assert _verdict({"n": 0, "mean_pct": None, "t_val": None})["kind"] == "none"


def test_combined_verdict_needs_two_windows():
    assert _combined_verdict([{"summary": _summary()}]) is None


def test_consistent_negative_across_windows_is_the_strongest_reading():
    group = [{"summary": _summary(mean=-1.46)}, {"summary": _summary(mean=-0.60)}]
    assert _combined_verdict(group)["kind"] == "bad"
    assert "전 구간 음수" in _combined_verdict(group)["text"]


def test_sign_flip_across_windows_is_not_evidence():
    group = [{"summary": _summary(mean=+1.28)}, {"summary": _summary(mean=-0.60)}]
    assert _combined_verdict(group)["kind"] == "warn"


def test_runs_differing_only_in_an_unrecorded_knob_are_not_grouped():
    """워크포워드 처리군/대조군이 params가 같으면 한쪽이 이전 측정으로 치워진다.
    pbr_applied가 그 둘을 가르는 유일한 knob이다."""
    treat = {"slots": 5, "windows": 4, "pbr_applied": True}
    control = {"slots": 5, "windows": 4, "pbr_applied": False}

    assert _fingerprint(treat) != _fingerprint(control)


def test_overlapping_windows_are_detected():
    a = _run(1, {}, "2025-01-01", "2026-08-18", "2026-08-19T10:00")
    b = _run(2, {}, "2025-01-01", "2026-08-19", "2026-08-20T10:00")
    c = _run(3, {}, "2022-01-01", "2024-12-31", "2026-08-20T10:00")

    assert _overlaps(a, b) is True
    assert _overlaps(a, c) is False


# ── 팩터 귀속 ────────────────────────────────────────────────────────────
from dashboard.app import (_current_runs, _factor_table, _rank_factor_table,  # noqa: E402
                           _multiple_comparison_pct, _pair_delta)


def _fr(rid, params, start="2022-01-01", end="2024-12-31",
        ts="2026-08-20T10:00", mean=0.0, std=15.0, n=250, strategy="s"):
    r = _run(rid, params, start, end, ts,
             {"n": n, "mean_pct": mean, "std_pct": std, "t_val": 0.0})
    r["strategy"] = strategy
    return r


def test_only_pairs_differing_in_one_knob_are_compared():
    """두 knob이 동시에 바뀌면 무엇 때문인지 가릴 수 없다."""
    runs = [
        _fr(1, {"slots": 5, "rank_col": "heat_score", "ascending": True}),
        _fr(2, {"slots": 12, "rank_col": "heat_score", "ascending": True}),
        _fr(3, {"slots": 12, "rank_col": "volume_ratio", "ascending": True}),
    ]
    knobs = {g["knob"] for g in _factor_table(runs)}

    assert knobs == {"슬롯"}   # 1-2는 슬롯. 1-3은 두 개가 바뀌어 제외


def test_pairs_from_different_windows_are_not_compared():
    runs = [
        _fr(1, {"slots": 5}, start="2022-01-01", end="2024-12-31"),
        _fr(2, {"slots": 12}, start="2025-01-01", end="2026-08-19"),
    ]
    assert _factor_table(runs) == []


def test_always_paired_knobs_count_as_one_change():
    """exit_rank_pct와 exit_cols는 늘 같이 움직인다 — 따로 세면 비교에서 빠진다."""
    runs = [
        _fr(1, {"exit_rank_pct": None, "exit_cols": []}),
        _fr(2, {"exit_rank_pct": 0.9, "exit_cols": ["credit_surge_ratio"]}),
    ]
    table = _factor_table(runs)

    assert len(table) == 1
    assert table[0]["knob"] == "신호 청산"


def test_sign_flip_across_comparisons_is_reported_as_unexplained():
    runs = [
        _fr(1, {"slots": 5}, mean=-1.5),
        _fr(2, {"slots": 12}, mean=-0.5),                       # +1.0%p
        _fr(3, {"slots": 5}, start="2025-01-01", end="2026-01-01", mean=+1.0),
        _fr(4, {"slots": 12}, start="2025-01-01", end="2026-01-01", mean=-1.0),  # -2.0%p
    ]
    assert _factor_table(runs)[0]["verdict"]["kind"] == "bad"


def test_consistent_direction_with_a_large_gap_is_the_strongest_reading():
    runs = [
        _fr(1, {"slots": 5}, mean=-3.0, std=8.0, n=400),
        _fr(2, {"slots": 12}, mean=+1.0, std=8.0, n=400),
        _fr(3, {"slots": 5}, start="2025-01-01", end="2026-01-01", mean=-2.0, std=8.0, n=400),
        _fr(4, {"slots": 12}, start="2025-01-01", end="2026-01-01", mean=+1.5, std=8.0, n=400),
    ]
    g = _factor_table(runs)[0]

    assert g["verdict"]["kind"] == "good"
    assert g["max_abs_z"] >= 2


def test_consistent_direction_within_noise_is_not_called_significant():
    runs = [
        _fr(1, {"slots": 5}, mean=-1.0, std=25.0, n=120),
        _fr(2, {"slots": 12}, mean=-0.8, std=25.0, n=120),
        _fr(3, {"slots": 5}, start="2025-01-01", end="2026-01-01", mean=-1.0, std=25.0, n=120),
        _fr(4, {"slots": 12}, start="2025-01-01", end="2026-01-01", mean=-0.9, std=25.0, n=120),
    ]
    assert _factor_table(runs)[0]["verdict"]["kind"] == "warn"


def test_delta_standard_error_uses_both_samples():
    a = {"summary": {"mean_pct": 0.0, "std_pct": 10.0, "n": 100}}
    b = {"summary": {"mean_pct": 2.0, "std_pct": 10.0, "n": 100}}

    d = _pair_delta(a, b)
    assert d["delta"] == pytest.approx(2.0)
    assert d["se"] == pytest.approx((1.0 + 1.0) ** 0.5)


def test_remeasured_runs_are_dropped_before_comparing():
    """수정 전 측정이 남아 있으면 팩터 차이가 데이터 변경분까지 떠안는다."""
    runs = [
        _fr(1, {"slots": 5}, ts="2026-08-19T10:00", mean=+1.2, strategy="old"),
        _fr(2, {"slots": 5}, ts="2026-08-20T10:00", mean=-1.5, strategy="new"),
        _fr(3, {"slots": 12}, ts="2026-08-20T10:00", mean=-1.1, strategy="new12"),
    ]
    kept = {r["id"] for r in _current_runs(runs)}
    assert kept == {2, 3}

    comps = _factor_table(runs)[0]["comparisons"]
    assert len(comps) == 1
    assert comps[0]["from"]["strategy"] == "new"


def test_multiple_comparison_probability_grows_with_run_count():
    assert _multiple_comparison_pct(1) == pytest.approx(5.0)
    assert _multiple_comparison_pct(20) > 60


def test_ranking_factors_are_not_compared_pairwise():
    """팩터가 N개면 쌍이 N(N-1)/2개로 불어나고, "A 대신 B" 는 답할 질문이 아니다.
    랭킹은 쌍대 차이가 아니라 팩터별 절대 성과로 본다."""
    runs = [
        _fr(1, {"slots": 5, "rank_col": "heat_score", "ascending": True}),
        _fr(2, {"slots": 5, "rank_col": "volume_ratio", "ascending": True}),
        _fr(3, {"slots": 5, "rank_col": "heat_score", "ascending": False}),
    ]
    assert _factor_table(runs) == []


def test_a_factor_measured_in_one_window_gets_no_verdict():
    runs = [_fr(1, {"slots": 5, "rank_col": "heat_score", "ascending": True}, mean=2.0)]
    assert _rank_factor_table(runs)[0]["verdict"]["kind"] == "none"


def test_factor_positive_in_both_windows_but_weak_is_not_called_significant():
    """이 저장소가 반복해서 속은 자리다 — 부호만 보고 채택하면 안 된다."""
    runs = [
        _fr(1, {"rank_col": "institution_flow_ratio", "ascending": True},
            mean=+0.93, n=234),
        _fr(2, {"rank_col": "institution_flow_ratio", "ascending": True},
            start="2025-01-01", end="2026-08-19", mean=+1.51, n=133),
    ]
    for r in runs:
        r["summary"]["t_val"] = 1.0

    f = _rank_factor_table(runs)[0]
    assert f["verdict"]["kind"] == "warn"
    assert "유의하지 않음" in f["verdict"]["text"]


def test_factor_flipping_sign_between_windows_is_rejected():
    runs = [
        _fr(1, {"rank_col": "volume_ratio", "ascending": True}, mean=-0.50),
        _fr(2, {"rank_col": "volume_ratio", "ascending": True},
            start="2025-01-01", end="2026-08-19", mean=+0.50),
    ]
    assert _rank_factor_table(runs)[0]["verdict"]["kind"] == "bad"


def test_factor_label_spells_out_direction_and_deviations():
    from dashboard.app import _factor_label

    assert _factor_label({"rank_col": "heat_score", "ascending": True}) == "과열 점수 낮은 순"
    assert "52주 필터 끔" in _factor_label(
        {"rank_col": "heat_score", "ascending": True, "pos52w_filter": False})
    assert "신호 청산 켬" in _factor_label(
        {"rank_col": "credit_surge_ratio", "ascending": True, "exit_rank_pct": 0.9})


def test_factor_rows_put_the_survivors_first():
    """전 구간 음수인 팩터가 위에 오면 표를 읽는 순서가 뒤집힌다."""
    runs = [
        _fr(1, {"rank_col": "heat_score", "ascending": True}, mean=-1.46),
        _fr(2, {"rank_col": "heat_score", "ascending": True},
            start="2025-01-01", end="2026-08-19", mean=-0.60),
        _fr(3, {"rank_col": "institution_flow_ratio", "ascending": True}, mean=+0.93),
        _fr(4, {"rank_col": "institution_flow_ratio", "ascending": True},
            start="2025-01-01", end="2026-08-19", mean=+1.51),
    ]
    labels = [f["label"] for f in _rank_factor_table(runs)]
    assert labels[0].startswith("기관 순매수 배율")
