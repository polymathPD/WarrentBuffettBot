"""processor/signals.py의 _heat() - 순수 함수, mock 불필요.
config.HEAT_AVOID/HEAT_SELL 모듈 상수를 직접 참조하므로 config.get_setting()과 무관."""
import numpy as np
import pytest

from processor.signals import _heat


def test_all_nan_returns_neutral_zero():
    heat, signal = _heat(np.nan, np.nan, np.nan)
    assert heat == 0.0
    assert signal == "neutral"


def test_flow_ratio_contribution_clamped_to_4():
    # (flow_r - 1.0) * 3.0, flow_r=3.0 -> 6.0이지만 4.0으로 클램프
    heat, signal = _heat(3.0, np.nan, np.nan)
    assert heat == pytest.approx(4.0)
    assert signal == "neutral"  # HEAT_AVOID(7.0) 미만


def test_credit_ratio_contribution_clamped_to_3():
    heat, signal = _heat(np.nan, 5.0, np.nan)
    assert heat == pytest.approx(3.0)


def test_below_threshold_scores_negative_not_zero():
    """기준선 아래는 0이 아니라 음수여야 한다.

    하한을 0에 두면 세 배율이 모두 낮은 종목이 전부 0.0으로 동점이 되는데,
    그게 이 전략이 사려는 구간이다. 2026-08-18에 필터 통과 1,324종목 중
    426종목이 0.0 동점이라 랭킹이 DB 행 순서로 결정됐다."""
    heat, signal = _heat(np.nan, np.nan, 1.0)   # (1.0-1.5)*2 = -1.0
    assert heat == pytest.approx(-1.0)
    assert signal == "neutral"


def test_quiet_stocks_are_ranked_apart_from_each_other():
    """소외 정도가 다르면 점수도 달라야 한다 — 동점이 생기면 안 된다."""
    mild = _heat(0.95, 0.98, 1.40)[0]
    deep = _heat(0.30, 0.55, 0.60)[0]
    assert deep < mild < 0


def test_heat_never_goes_below_minus_10():
    heat, signal = _heat(0.0, 0.0, 0.0)
    assert heat >= -10.0
    assert signal == "neutral"


def test_combined_score_reaches_avoid_threshold():
    # flow_r=3.0 -> clamp((3-1)*3,0,4)=4.0
    # credit_r=3.0 -> clamp((3-1)*3,0,3)=3.0
    # vol_r=2.0   -> clamp((2-1.5)*2,0,3)=1.0
    # 합계 8.0 -> 7.0<=8.0<8.5 이므로 avoid
    heat, signal = _heat(3.0, 3.0, 2.0)
    assert heat == pytest.approx(8.0)
    assert signal == "avoid"


def test_signal_classification_boundaries():
    # heat < 7.0 -> neutral
    heat, signal = _heat(1.5, np.nan, np.nan)  # (1.5-1)*3=1.5
    assert signal == "neutral"

    # 7.0 <= heat < 8.5 -> avoid (개별 최대치가 4/3/3이므로 여러 요소를 합쳐야 도달)
    heat, signal = _heat(3.5, 2.0, np.nan)  # flow: clamp((3.5-1)*3,0,4)=4.0, credit: clamp((2-1)*3,0,3)=3.0 -> 7.0
    assert heat == pytest.approx(7.0)
    assert signal == "avoid"

    # heat >= 8.5 -> sell
    heat, signal = _heat(3.5, 3.0, 3.0)  # flow 4.0 + credit 3.0 + vol clamp((3-1.5)*2,0,3)=3.0 -> 10.0
    assert heat == pytest.approx(10.0)
    assert signal == "sell"


def test_heat_never_exceeds_10():
    heat, signal = _heat(100.0, 100.0, 100.0)
    assert heat == pytest.approx(10.0)


# --- 관측용 지표 (외국인/기관/신용잔고 수준) --------------------------------

def test_abs_ratio_uses_prior_window_only():
    from processor.signals import _abs_ratio

    arr = np.array([100.0] * 30 + [300.0])
    assert _abs_ratio(arr) == pytest.approx(3.0)


def test_abs_ratio_returns_nan_when_denominator_is_zero():
    from processor.signals import _abs_ratio

    assert np.isnan(_abs_ratio(np.array([0.0] * 30 + [100.0])))


def test_f_converts_nan_to_none_and_numpy_to_float():
    """np.float64를 그대로 psycopg2에 넘기면 INSERT가 깨진다."""
    from processor.signals import _f

    assert _f(np.nan) is None
    assert _f(None) is None
    out = _f(np.float64(1.5))
    assert out == 1.5 and type(out) is float


def _rows(latest, rest, n=30):
    return [latest] + [rest] * n


def test_compute_for_date_records_observation_only_factors(mock_db):
    """외국인·기관 배율과 신용잔고 비율(수준)이 기록되고,
    heat_score는 기존 3축(개인/신용급증/거래대금)으로만 계산되어야 한다."""
    from processor.signals import compute_for_date

    mock_db.fetchall.side_effect = [
        [{"code": "005930"}],
        _rows({"d": None, "c": 300, "v": 100}, {"d": None, "c": 100, "v": 100}),
        _rows({"individual_net": 300, "foreign_net": -200, "institution_net": 100},
              {"individual_net": 100, "foreign_net": 100, "institution_net": 50}),
        _rows({"credit_amt": 2000, "credit_ratio": 1.25},
              {"credit_amt": 1000, "credit_ratio": 0.5}),
    ]

    compute_for_date("2026-08-12")

    (row,) = mock_db.executemany.call_args[0][1]
    code, d, flow_r, credit_r, vol_r, foreign_r, inst_r, credit_lvl, heat, signal = row

    assert (flow_r, credit_r, vol_r) == pytest.approx((3.0, 2.0, 3.0))
    assert (foreign_r, inst_r, credit_lvl) == pytest.approx((-2.0, 2.0, 1.25))
    # 4.0(개인) + 3.0(신용급증) + 3.0(거래대금) — 외국인/기관은 미반영
    assert heat == pytest.approx(10.0)
    assert signal == "sell"


def test_summary_counts_match_the_stored_signal(mock_db, capsys):
    """요약 출력이 튜플의 고정 인덱스를 참조하면 컬럼 추가 시 조용히 어긋난다.
    (실제로 avoid가 있는데 avoid=0으로 찍히던 회귀)"""
    from processor.signals import compute_for_date

    mock_db.fetchall.side_effect = [
        [{"code": "005930"}],
        _rows({"d": None, "c": 300, "v": 100}, {"d": None, "c": 100, "v": 100}),
        _rows({"individual_net": 300, "foreign_net": -200, "institution_net": 100},
              {"individual_net": 100, "foreign_net": 100, "institution_net": 50}),
        _rows({"credit_amt": 2000, "credit_ratio": 1.25},
              {"credit_amt": 1000, "credit_ratio": 0.5}),
    ]

    compute_for_date("2026-08-12")

    (row,) = mock_db.executemany.call_args[0][1]
    assert row[-1] == "sell"
    assert "sell=1" in capsys.readouterr().out


def test_foreign_and_institution_do_not_change_heat(mock_db):
    """외국인+기관은 개인과 상관 0.997이라 점수에 더하면 이중계산이 된다.
    값이 어떻든 heat_score는 동일해야 한다."""
    from processor.signals import compute_for_date

    def run(frgn, orgn):
        mock_db.executemany.reset_mock()
        mock_db.fetchall.side_effect = [
            [{"code": "005930"}],
            _rows({"d": None, "c": 300, "v": 100}, {"d": None, "c": 100, "v": 100}),
            _rows({"individual_net": 300, "foreign_net": frgn, "institution_net": orgn},
                  {"individual_net": 100, "foreign_net": 100, "institution_net": 50}),
            _rows({"credit_amt": 1000, "credit_ratio": 0.5},
                  {"credit_amt": 1000, "credit_ratio": 0.5}),
        ]
        compute_for_date("2026-08-12")
        return mock_db.executemany.call_args[0][1][0][8]  # heat_score

    assert run(-200, 100) == run(9999, -9999)


def test_backfill_uses_the_same_formula_as_the_live_path():
    """수식이 두 곳에 복제되면 백필이 운용 신호를 조용히 다른 값으로 덮는다.

    backfill_signals.py는 processor/signals.py의 contributions()를 import해야 하고,
    자기 버전의 clip 상수를 들고 있으면 안 된다."""
    import io
    import os

    src = io.open(
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "backfill_signals.py"),
        encoding="utf-8").read()

    assert "from processor.signals import contributions" in src
    assert "np.clip((fr" not in src
    assert "np.clip((vr" not in src
    assert "np.clip((cr" not in src


def test_contributions_accepts_arrays_and_scalars_alike():
    """백필은 배열로, 운용은 스칼라로 같은 함수를 부른다."""
    from processor.signals import contributions

    scalar = contributions(0.30, 0.55, 0.60)
    arrays = contributions(np.array([0.30]), np.array([0.55]), np.array([0.60]))

    for s_val, a_val in zip(scalar, arrays):
        assert float(a_val[0]) == pytest.approx(float(s_val))


def test_infinite_ratios_are_stored_as_null():
    """배율은 분모가 0이면 무한대가 된다. NUMERIC은 Infinity를 그대로 담고
    IS NOT NULL도 통과시키므로, 오름차순 정렬의 1순위가 '데이터가 없는 종목'이 된다.
    백테스트는 non-finite를 제외하므로 여기서 끊지 않으면 운용과 검증이 갈라진다."""
    from processor.signals import _f

    assert _f(float("inf")) is None
    assert _f(float("-inf")) is None
    assert _f(float("nan")) is None
    assert _f(np.float64(2.5)) == pytest.approx(2.5)
    assert isinstance(_f(np.float64(2.5)), float)
