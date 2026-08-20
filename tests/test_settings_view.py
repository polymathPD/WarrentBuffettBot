"""dashboard 설정 화면 - 어떤 값이 언제부터 듣는지를 화면이 정확히 말해야 한다."""
import config
from dashboard.app import SETTING_SCOPE


def test_every_setting_declares_when_it_takes_effect():
    """설정을 추가하면 적용 시점도 같이 적어야 한다 — 안 그러면 화면에서 빠진다."""
    assert set(SETTING_SCOPE) == set(config._DEFAULTS)


def test_scope_values_are_one_of_the_two_known_kinds():
    for key, (badge, _) in SETTING_SCOPE.items():
        assert badge in ("즉시", "신규 진입분만"), key


def test_thresholds_are_live_and_sizing_is_snapshotted():
    """HEAT_*는 매 판단 때 읽고, 나머지는 진입 시점에 positions로 박제된다.
    이 구분이 뒤집히면 화면이 거짓말을 한다."""
    assert SETTING_SCOPE["HEAT_AVOID"][0] == "즉시"
    assert SETTING_SCOPE["HEAT_SELL"][0] == "즉시"
    for key in ("STOP_PCT", "MAX_HOLD_DAYS", "SLOTS", "CAPITAL"):
        assert SETTING_SCOPE[key][0] == "신규 진입분만", key


def test_exit_rules_read_thresholds_live_but_stops_from_the_position():
    """화면의 주장을 실제 청산 코드와 대조한다."""
    import io
    import os

    src = io.open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "strategy", "contrarian.py"), encoding="utf-8").read()

    assert 'config.get_setting("HEAT_SELL")' in src      # 임계값은 매번 읽고
    assert 'float(r["stop_px"])' in src                  # 손절가는 포지션에서 읽는다
    assert 'r["max_hold_days"]' in src                   # 보유한도도 포지션에서
