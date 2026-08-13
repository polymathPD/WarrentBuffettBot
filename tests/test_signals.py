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


def test_volume_ratio_below_threshold_contributes_zero():
    # (vol_r - 1.5) * 2.0, vol_r=1.0 -> 음수이므로 0으로 클램프
    heat, signal = _heat(np.nan, np.nan, 1.0)
    assert heat == pytest.approx(0.0)


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
