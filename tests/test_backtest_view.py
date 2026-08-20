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
