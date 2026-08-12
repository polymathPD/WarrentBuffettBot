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
