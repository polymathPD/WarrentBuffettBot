"""strategy/quality.py + executor.paper.adjust - 퀄리티 전략과 동일가중 리밸런싱."""
import numpy as np
import pandas as pd
import pytest

from strategy import quality
from executor import paper


# ── TTM 계산 ────────────────────────────────────────────────────────────
def _fin_rows(code="005930"):
    """Q1~Q3는 단독 분기, Q4는 연간 누적. DART가 이렇게 준다."""
    q = [("2024Q1", 100), ("2024Q2", 110), ("2024Q3", 120), ("2024Q4", 500),
         ("2025Q1", 130), ("2025Q2", 140), ("2025Q3", 150), ("2025Q4", 600)]
    return [{"code": code, "period": p, "fs_div": "CFS",
             "revenue": v * 10, "op_income": v, "net_income": v,
             "assets": 3000, "liabilities": 1000, "equity": 2000} for p, v in q]


def test_q4_is_annual_not_quarterly(mock_db):
    """Q4를 그대로 분기로 쓰면 한 해가 7분기치로 부풀려진다."""
    mock_db.fetchall.return_value = _fin_rows()

    f = quality.load_financials().set_index("period")

    # 2024Q4 단독 = 500 - (100+110+120) = 170
    assert f.loc["2024Q4", "net_income_q"] == pytest.approx(170)
    # TTM(2024Q4) = 연간 그대로 = 500
    assert f.loc["2024Q4", "net_income_ttm"] == pytest.approx(500)
    # TTM(2025Q2) = 170 + 130 + 140 = ... 직전 4개 단독분기(2024Q3,Q4,2025Q1,Q2)
    assert f.loc["2025Q2", "net_income_ttm"] == pytest.approx(120 + 170 + 130 + 140)


def test_financials_are_not_used_before_they_are_published(mock_db):
    """기간종료 + 90일 전에는 쓰면 안 된다. 미래 정보가 샌다."""
    mock_db.fetchall.return_value = _fin_rows()
    f = quality.load_financials()

    # 2024Q4는 2024-12-31 종료 -> 2025-03-31에야 쓸 수 있다
    before = quality.snapshot(f, pd.Timestamp("2025-03-30"))
    after = quality.snapshot(f, pd.Timestamp("2025-04-01"))

    assert before.iloc[-1]["period"] != "2024Q4"
    assert after.iloc[-1]["period"] == "2024Q4"


def test_consolidated_statements_win_over_separate(mock_db):
    rows = _fin_rows()
    rows.append(dict(rows[0], fs_div="OFS", net_income=99999))
    mock_db.fetchall.return_value = rows

    f = quality.load_financials()

    assert 99999 not in set(f["net_income"])


# ── 후보 필터 ───────────────────────────────────────────────────────────
def _pool():
    return pd.DataFrame([
        {"code": "A", "equity": 1000, "liabilities": 1000, "net_income_ttm": 100,
         "revenue_ttm": 1000, "op_income_ttm": 200},
        {"code": "B", "equity": 1000, "liabilities": 5000, "net_income_ttm": 100,
         "revenue_ttm": 1000, "op_income_ttm": 200},   # 부채비율 500%
        {"code": "C", "equity": 1000, "liabilities": 500, "net_income_ttm": -50,
         "revenue_ttm": 1000, "op_income_ttm": -10},   # 적자
        {"code": "D", "equity": -100, "liabilities": 500, "net_income_ttm": 50,
         "revenue_ttm": 1000, "op_income_ttm": 100},   # 자본잠식
    ])


def test_eligible_drops_leveraged_lossmaking_and_impaired():
    mcap = pd.Series({"A": 5e11, "B": 5e11, "C": 5e11, "D": 5e11})

    out = quality.eligible(_pool(), mcap)

    assert list(out["code"]) == ["A"]


def test_eligible_drops_small_caps():
    mcap = pd.Series({"A": 1e10, "B": 5e11, "C": 5e11, "D": 5e11})

    assert quality.eligible(_pool(), mcap).empty


def test_value_ranks_cheap_first_quality_ranks_profitable_first():
    s = pd.DataFrame([
        {"code": "cheap", "equity": 1000, "debt_ratio": 0.5, "net_income_ttm": 100,
         "revenue_ttm": 1000, "op_income_ttm": 100, "marcap": 200},
        {"code": "good", "equity": 100, "debt_ratio": 0.1, "net_income_ttm": 90,
         "revenue_ttm": 100, "op_income_ttm": 90, "marcap": 5000},
    ])

    assert s.loc[quality.score(s, "value").idxmax(), "code"] == "cheap"
    assert s.loc[quality.score(s, "quality").idxmax(), "code"] == "good"


def test_score_rejects_unknown_kind():
    s = pd.DataFrame([{"equity": 1, "debt_ratio": 1, "net_income_ttm": 1,
                       "revenue_ttm": 1, "op_income_ttm": 1, "marcap": 1}])
    with pytest.raises(ValueError):
        quality.score(s, "momentum")


# ── 동일가중 리밸런싱 ───────────────────────────────────────────────────
def test_adjust_buys_only_the_shortfall(mock_db, mock_settings):
    """계속 편입되는 종목을 전량 매도 후 재매수하면 매달 왕복 비용을 낸다.
    낮은 회전율이 이 전략의 근거이므로 차이만 사고팔아야 한다."""
    mock_db.fetchone.return_value = {"qty": 80, "entry_px": 10000}

    paper.adjust("005930", "삼성전자", 100, 10000, "quality_v1")

    buys = [c for c in mock_db.execute.call_args_list if "'buy'" in str(c)]
    assert len(buys) == 1
    assert buys[0].args[1][3] == 20      # 100 - 80


def test_adjust_sells_down_without_closing_the_position(mock_db, mock_settings):
    mock_db.fetchone.return_value = {"qty": 100, "entry_px": 10000}

    paper.adjust("005930", "삼성전자", 60, 11000, "quality_v1")

    sql = " ".join(str(c) for c in mock_db.execute.call_args_list)
    assert "DELETE FROM positions" not in sql
    assert "UPDATE positions SET qty" in sql


def test_adjust_to_zero_closes_the_position(mock_db, mock_settings):
    mock_db.fetchone.return_value = {"qty": 100, "entry_px": 10000}

    paper.adjust("005930", "삼성전자", 0, 11000, "quality_v1")

    sql = " ".join(str(c) for c in mock_db.execute.call_args_list)
    assert "DELETE FROM positions" in sql


def test_adjust_does_nothing_when_already_at_target(mock_db, mock_settings):
    mock_db.fetchone.return_value = {"qty": 100, "entry_px": 10000}

    paper.adjust("005930", "삼성전자", 100, 12000, "quality_v1")

    mock_db.execute.assert_not_called()


def test_adding_to_a_position_averages_the_entry_price(mock_db, mock_settings):
    """진입가를 갱신하지 않으면 부분 청산의 실현손익이 틀어진다."""
    mock_db.fetchone.return_value = {"qty": 100, "entry_px": 10000}

    paper.adjust("005930", "삼성전자", 200, 20000, "quality_v1")

    upd = [c for c in mock_db.execute.call_args_list if "UPDATE positions" in str(c)][0]
    new_entry = upd.args[1][1]
    assert 10000 < new_entry < 20000


def test_no_stop_means_no_stop_price(mock_db, mock_settings):
    """stop_pct=0을 entry*(1-0)으로 두면 손절선이 진입가와 같아져 즉시 청산된다."""
    mock_db.fetchone.side_effect = [None, None, {"n": 0}]

    paper.buy("005930", "삼성전자", "2026-08-20", 10000, 0.0, {}, "quality_v1",
              fill_px=10000, stop_pct=0.0, max_hold_days=99999)

    ins = [c for c in mock_db.execute.call_args_list if "INSERT INTO positions" in str(c)][0]
    assert ins.args[1][6] == 0.0        # stop_px
