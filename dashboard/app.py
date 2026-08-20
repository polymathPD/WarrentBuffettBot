import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from fastapi import FastAPI, Request, Form
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse

import json

import db.connection as db
import config
from recorder.equity import cash_by_key
from agents.base import ERROR_DECISION, ERROR_SEP

app = FastAPI()
templates = Jinja2Templates(
    directory=os.path.join(os.path.dirname(__file__), "templates")
)
app.mount(
    "/static",
    StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")),
    name="static",
)


def agent_alerts() -> list[dict]:
    """최근 24시간의 에이전트 호출 실패를 사유별로 묶는다.

    실패는 '관망'과 섞이지 않도록 decision='오류'로 따로 남는다(agents/base.py 참고).
    크레딧이 떨어지면 하루 수백 건이 쌓이므로 라벨로 묶어 건수만 보여준다.
    base.html이 전 페이지 상단에 띄운다.
    """
    rows = db.fetchall(
        """SELECT rationale, ts FROM agent_decisions
           WHERE decision = %s AND ts > NOW() - INTERVAL '24 hours'
           ORDER BY ts DESC LIMIT 500""",
        (ERROR_DECISION,),
    )

    grouped: dict = {}
    for r in rows:
        label, _, detail = (r["rationale"] or "").partition(ERROR_SEP)
        # rows가 최신순이므로 라벨을 처음 만난 행이 그 사유의 마지막 발생이다.
        g = grouped.setdefault(
            label, {"label": label, "detail": detail, "n": 0, "ts": r["ts"]}
        )
        g["n"] += 1
    return list(grouped.values())


templates.env.globals["agent_alerts"] = agent_alerts


def _line_chart_data(rows, width=720, height=260, pad_x=10, pad_top=18,
                     line_h=150, bar_h=48):
    """선 + 구간 막대 차트의 좌표를 계산한다 (자산곡선·백테스트 누적곡선 공용).

    rows 각 원소: label(x축 표시), value(선), delta(막대, 없으면 None), tip(툴팁).
    x축은 기록이 있는 지점만 등간격으로 잇는다 (달력 간격이 아니다).
    """
    if not rows:
        return None

    values = [r["value"] for r in rows]
    vmin, vmax = min(values), max(values)
    if vmin == vmax:
        vmin, vmax = vmin - abs(vmin) * 0.001 - 0.001, vmax + abs(vmax) * 0.001 + 0.001

    n = len(values)
    span = width - 2 * pad_x
    line_bottom = pad_top + line_h
    bar_zero = line_bottom + 20 + bar_h / 2
    bar_w = max(2.0, min(14.0, span / n * 0.6))

    deltas = [r.get("delta") for r in rows]
    has_bars = any(d is not None for d in deltas)
    max_abs = max((abs(d) for d in deltas if d is not None), default=0.0) or 1.0

    points = []
    for i, v in enumerate(values):
        x = pad_x + (i * span / (n - 1) if n > 1 else span / 2)
        y = pad_top + (vmax - v) / (vmax - vmin) * line_h
        d = deltas[i]
        bar = None
        if d is not None:
            h = abs(d) / max_abs * (bar_h / 2)
            bar = {
                "x": round(x - bar_w / 2, 1),
                "y": round(bar_zero - h if d >= 0 else bar_zero, 1),
                "h": round(max(h, 0.6), 1),
                "up": d >= 0,
            }
        points.append({
            "label": rows[i]["label"],
            "x": round(x, 1), "y": round(y, 1),
            "tip": rows[i]["tip"],
            "bar": bar,
        })

    polyline = " ".join(f"{p['x']},{p['y']}" for p in points)
    area = f"{pad_x},{line_bottom} {polyline} {points[-1]['x']},{line_bottom}"

    return {
        "points": points,
        "polyline": polyline,
        "area": area,
        "last_positive": values[-1] >= values[0],
        "width": width,
        "height": height,
        "line_bottom": round(line_bottom, 1),
        "bar_zero": round(bar_zero, 1),
        "bar_w": round(bar_w, 1),
        "has_bars": has_bars,
        "label_y": height - 6,
    }


def _equity_chart_data(rows):
    """equity_daily 행(날짜 오름차순) → 자산곡선. 막대는 전일 대비 일별 수익률."""
    if not rows:
        return None

    values = [float(r["total_equity"]) for r in rows]
    base = values[0]
    day_rets = [None] + [values[i] / values[i - 1] - 1 for i in range(1, len(values))]

    points = []
    for i, v in enumerate(values):
        cum_pct = (v / base - 1) * 100
        tip = f"{rows[i]['d']:%m/%d}  {v:,.0f}원  누적 {cum_pct:+.2f}%"
        if day_rets[i] is not None:
            tip += f"  일간 {day_rets[i] * 100:+.2f}%"
        points.append({
            "label": f"{rows[i]['d']:%m/%d}",
            "value": v,
            "delta": day_rets[i],
            "tip": tip,
        })

    chart = _line_chart_data(points)
    chart.update({
        "total_equity": round(values[-1]),
        "cum_pct": round((values[-1] / base - 1) * 100, 2),
        "cash": round(float(rows[-1]["cash"])),
        "positions_value": round(float(rows[-1]["positions_value"])),
    })
    return chart


ALLOC_SLICES = 7   # 범주형 색상 슬롯 수. 초과분은 '기타'로 묶는다.


def _stack_bar(slices, width=720, height=28, gap=2):
    """구성비 스택 바. slices 각 원소: label, value, kind(series|other|cash)."""
    total = sum(s["value"] for s in slices)
    if total <= 0:
        return None

    n = len(slices)
    avail = width - gap * (n - 1)
    x = 0.0
    series_i = 0
    for s in slices:
        w = s["value"] / total * avail
        s["x"] = round(x, 1)
        s["w"] = round(max(w, 1.0), 1)
        s["pct"] = round(s["value"] / total * 100, 1)
        s["amount"] = round(s["value"])
        if s["kind"] == "series":
            series_i += 1
            s["cls"] = f"is-s{series_i}"
        else:
            s["cls"] = f"is-{s['kind']}"
        x += w + gap

    return {"slices": slices, "width": width, "height": height,
            "total": round(total)}


def _fold_tail(slices, limit=ALLOC_SLICES, unit="종목"):
    """색상 슬롯을 넘는 항목은 '기타'로 묶는다 — 색을 새로 만들지 않는다."""
    if len(slices) <= limit:
        return slices
    tail = slices[limit:]
    return slices[:limit] + [{
        "label": f"기타 {len(tail)}{unit}",
        "value": sum(t["value"] for t in tail),
        "kind": "other",
    }]


def _allocation_data(positions, cash):
    """보유 종목별 평가금액 + 현금의 구성비."""
    slices = [{
        "label": p["name"] or p["code"],
        "value": float(p["qty"]) * float(p["current_px"] or p["entry_px"]),
        "kind": "series",
    } for p in positions]
    slices.sort(key=lambda s: -s["value"])
    slices = _fold_tail(slices)

    # 현금이 음수면(자금 초과 집행) 구성비가 성립하지 않으므로 0으로 본다.
    slices.append({"label": "현금", "value": max(cash, 0.0), "kind": "cash"})
    return _stack_bar(slices)


@app.get("/")
def index(request: Request, mode: str = "paper"):
    mode = mode if mode in ("paper", "live") else "paper"
    positions = db.fetchall("""
        SELECT p.code, p.name, p.entry_date, p.entry_px, p.qty, p.stop_px,
               sd.c AS current_px,
               ROUND((sd.c / p.entry_px - 1) * 100, 2) AS pct,
               ROUND((sd.c - p.entry_px) * p.qty, 0) AS unrealized
        FROM positions p
        LEFT JOIN stock_daily sd ON sd.code = p.code
          AND sd.d = (SELECT MAX(d) FROM stock_daily WHERE code = p.code)
        WHERE p.mode = %s
        ORDER BY p.entry_date DESC
    """, (mode,))
    today_trades = db.fetchall("""
        SELECT side, code, name, qty, price, realized_pct, exit_reason, ts
        FROM trades WHERE ts::date = CURRENT_DATE AND mode = %s ORDER BY ts DESC
    """, (mode,))
    total_unrealized = sum(float(p["unrealized"] or 0) for p in positions)

    # 전략이 여럿이면 같은 날 행이 여러 개다 — 모드 전체 자산으로 합산한다.
    equity_rows = db.fetchall("""
        SELECT d, SUM(cash) AS cash,
               SUM(positions_value) AS positions_value,
               SUM(total_equity) AS total_equity
        FROM equity_daily WHERE mode = %s
        GROUP BY d ORDER BY d ASC
    """, (mode,))
    equity_chart = _equity_chart_data(equity_rows)

    cash = sum(v for (m, _), v in cash_by_key().items() if m == mode)
    allocation = _allocation_data(positions, cash)

    return templates.TemplateResponse(request, "index.html", {
        "positions": positions,
        "today_trades": today_trades,
        "total_unrealized": total_unrealized,
        "equity_chart": equity_chart,
        "allocation": allocation,
        "mode": mode,
    })


TRADE_DAYS = ("7", "30", "90", "all")
TRADE_LIMIT = 200


@app.get("/trades")
def trades_page(request: Request, mode: str = "paper", days: str = "30",
                side: str = "", reason: str = ""):
    mode = mode if mode in ("paper", "live") else "paper"
    days = days if days in TRADE_DAYS else "30"
    side = side if side in ("buy", "sell") else ""

    # 청산 사유는 전략마다 다르므로 목록을 DB에서 읽는다.
    reasons = [
        r["exit_reason"] for r in db.fetchall(
            "SELECT DISTINCT exit_reason FROM trades "
            "WHERE mode = %s AND exit_reason IS NOT NULL ORDER BY 1", (mode,)
        )
    ]
    reason = reason if reason in reasons else ""

    where = ["mode = %s"]
    params = [mode]
    if days != "all":
        where.append("ts >= NOW() - (%s || ' days')::interval")
        params.append(days)
    if side:
        where.append("side = %s")
        params.append(side)
    if reason:
        where.append("exit_reason = %s")
        params.append(reason)

    trades = db.fetchall(
        f"""SELECT side, code, name, qty, price, amount, strategy,
                   realized_pct, exit_reason, agents, ts
            FROM trades WHERE {' AND '.join(where)}
            ORDER BY ts DESC LIMIT {TRADE_LIMIT}""",
        tuple(params),
    )
    return templates.TemplateResponse(request, "trades.html", {
        "trades": trades,
        "mode": mode,
        "days": days,
        "side": side,
        "reason": reason,
        "reasons": reasons,
        "limit": TRADE_LIMIT,
    })


CURVE_MAX_POINTS = 200
BACKTEST_TRADE_LIMIT = 100

SUMMARY_LABELS = {
    "n": "거래 수", "mean_pct": "평균 수익률(%)", "std_pct": "표준편차(%)",
    "win_rate": "승률(%)", "t_val": "t값", "mdd_pct": "MDD(복리, %)",
    "annualized_pct": "연환산(%)", "turnover_per_slot": "슬롯당 연 회전",
    "avg_held_days": "평균 보유(일)", "bootstrap_positive_pct": "부트스트랩 양수율(%)",
    "random_pct_rank": "무작위 대조군 상위(%)", "split_date": "기간분리 기준일",
    "t1_n": "전반 거래 수", "t2_n": "후반 거래 수",
    "t1_mean_pct": "전반 평균(%)", "t2_mean_pct": "후반 평균(%)",
}


# 백테스트 실행 하나는 "어떤 knob 조합을 어느 구간에 돌렸나"다. 저장 이름만으로는
# 그걸 알 수 없어서(credit_rank_no52w_exit 같은 이름이 무슨 뜻인지 화면에 없었다)
# params JSONB를 읽을 수 있는 문장으로 풀어 준다.
KNOB_ORDER = ["rank_col", "slots", "pos52w_filter", "marcap_filter", "stop_pct",
              "max_hold_days", "min_hold_days", "exit_rank_pct", "exit_cols",
              "pbr_applied", "pbr_limits", "selection", "windows"]
KNOB_LABELS = {
    "rank_col": "랭킹", "slots": "슬롯", "pos52w_filter": "52주 하위 30% 필터",
    "marcap_filter": "시가총액 하한", "stop_pct": "손절", "max_hold_days": "최대 보유",
    "min_hold_days": "최소 보유", "exit_rank_pct": "신호 청산", "exit_cols": "청산 감시 지표",
    "pbr_limits": "PBR 후보", "selection": "창별 선택", "windows": "워크포워드 창",
    "pbr_applied": "PBR 적용",
}
# 목록에서 조합을 한눈에 구분하는 데 실제로 쓰이는 knob (전부 늘어놓으면 못 읽는다)
KNOB_KEY = ["rank_col", "slots", "pos52w_filter", "exit_rank_pct"]


def _knob_value(key, value, params):
    if key == "rank_col":
        direction = "오름차순" if params.get("ascending", True) else "내림차순"
        return f"{value} {direction}"
    if key == "slots":
        return f"{value}개"
    if key in ("pos52w_filter", "marcap_filter", "pbr_applied"):
        return "켬" if value else "끔"
    if key == "stop_pct":
        return f"-{float(value) * 100:.0f}%"
    if key in ("max_hold_days", "min_hold_days"):
        return f"{value}거래일"
    if key == "exit_rank_pct":
        if value is None:
            return "없음"
        return f"상위 {round((1 - float(value)) * 100)}% 진입 시"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value) if value else "없음"
    if isinstance(value, dict):
        return ", ".join(f"{k}={v}" for k, v in value.items()) or "없음"
    return str(value)


def _knobs(params, keys=None):
    """params를 [{label, value}] 로 푼다. keys를 주면 그 순서로 추린다."""
    params = params or {}
    order = keys or [k for k in KNOB_ORDER if k in params]
    out = []
    for k in order:
        if k not in params:
            continue
        out.append({"label": KNOB_LABELS.get(k, k),
                    "value": _knob_value(k, params[k], params)})
    return out


def _verdict(summary):
    """
    한 실행의 판정. 기준은 research/README.md의 방법론 그대로다 —
    비용 반영 후 거래당 평균의 부호와 |t| >= 2.
    한 구간만으로는 채택 근거가 못 되므로 문구를 단정적으로 쓰지 않는다.
    """
    summary = summary or {}
    mean, t, n = summary.get("mean_pct"), summary.get("t_val"), summary.get("n")
    if not n or mean is None or t is None:
        return {"kind": "none", "text": "판정 불가", "note": "요약 값이 없습니다"}
    if mean >= 0 and t >= 2:
        return {"kind": "good", "text": "유의하게 양수",
                "note": "이 구간에서는 우연으로 보기 어렵습니다. 다른 구간에서도 같은 부호여야 채택 근거가 됩니다."}
    if mean >= 0:
        return {"kind": "warn", "text": "양수, 유의하지 않음",
                "note": "|t| < 2 라 0과 구분되지 않습니다. 표본이 작을수록 우연히 양수가 나오기 쉽습니다."}
    if t <= -2:
        return {"kind": "bad", "text": "유의하게 음수 — 해로움",
                "note": "비용을 넘기기는커녕 규칙 자체가 손해를 냅니다."}
    return {"kind": "bad", "text": "음수, 유의하지 않음",
            "note": "손실 쪽이지만 0과 구분되지는 않습니다."}


def _fingerprint(params):
    """knob 조합의 동일성 판정용 키. 같은 규칙을 다른 구간에 돌린 실행을 묶는다."""
    return json.dumps(params or {}, sort_keys=True, default=str)


def _overlaps(a, b):
    return a["start_d"] <= b["end_d"] and b["start_d"] <= a["end_d"]


def _combined_verdict(group):
    """같은 규칙을 여러 구간에 돌린 결과를 합쳐 읽는다."""
    means = [g["summary"].get("mean_pct") for g in group
             if g["summary"] and g["summary"].get("mean_pct") is not None]
    if len(means) < 2:
        return None
    if all(m < 0 for m in means):
        return {"kind": "bad", "text": "해로움 (전 구간 음수)",
                "note": "구간을 바꿔도 부호가 유지됩니다. 이 저장소에서 가장 신뢰할 만한 형태의 결론입니다."}
    if all(m >= 0 for m in means):
        return {"kind": "good", "text": "전 구간 양수",
                "note": "부호가 일관합니다. 다만 유의성과 다중비교를 따로 확인해야 합니다."}
    return {"kind": "warn", "text": "부호 불안정",
            "note": "구간에 따라 방향이 뒤집힙니다. 어느 쪽도 채택 근거가 아닙니다."}


# ── 팩터 귀속 ────────────────────────────────────────────────────────────
# knob 하나만 다른 두 실행을 같은 구간에서 비교하면, 성과 차이를 그 knob에 돌릴
# 수 있다. 여러 knob이 동시에 바뀐 쌍은 무엇 때문인지 가릴 수 없으므로 뺀다.
#
# exit_rank_pct와 exit_cols처럼 항상 같이 움직이는 값은 논리적으로 한 knob이다.
# 따로 세면 "두 개가 바뀐 쌍"이 되어 비교에서 통째로 빠진다.
KNOB_GROUPS = {
    "exit_rank_pct": "신호 청산", "exit_cols": "신호 청산",
    "pbr_applied": "PBR 적용", "selection": "PBR 적용",
}
# 랭킹 팩터는 쌍대 차이로 보지 않는다. 팩터가 N개면 쌍이 N(N-1)/2개로 불어나는데,
# "A 대신 B로 바꾸면 얼마 오르나"는 답할 질문이 아니다. 알고 싶은 것은 각 팩터가
# 그 자체로 구간을 넘어 작동하느냐이므로, _rank_factor_table()에서 절대 성과로 본다.
RANK_KEYS = {"rank_col", "ascending"}
# 비교 자체가 성립하지 않는 knob (구간·자본은 규칙이 아니다)
KNOB_SKIP = {"marcap_filter", "windows", "pbr_limits"}


def _logical(key):
    return KNOB_GROUPS.get(key, KNOB_LABELS.get(key, key))


def _current_runs(runs):
    """같은 규칙을 같은 구간에 다시 잰 실행이 있으면 최신만 남긴다."""
    kept = []
    for r in sorted(runs, key=lambda x: x["ts"], reverse=True):
        fp = _fingerprint(r["params"])
        if not any(fp == _fingerprint(k["params"]) and _overlaps(r, k) for k in kept):
            kept.append(r)
    return kept


def _describe_change(group, a, b):
    """a → b 로 무엇이 바뀌었는지 한 문장."""
    pa, pb_ = a["params"] or {}, b["params"] or {}
    keys = [k for k, v in KNOB_GROUPS.items() if v == group] or [
        k for k in set(pa) | set(pb_) if _logical(k) == group]
    parts = []
    for k in keys:
        if k not in pa and k not in pb_:
            continue
        if k == "ascending" and "rank_col" in keys:
            continue
        before = _knob_value(k, pa.get(k), pa) if k in pa else "-"
        after = _knob_value(k, pb_.get(k), pb_) if k in pb_ else "-"
        if before != after:
            parts.append(f"{before} → {after}")
    return " / ".join(parts) or "변경 없음"


def _pair_delta(a, b):
    """
    b가 a보다 거래당 평균이 얼마나 높은지와, 그 차이가 잡음보다 큰지.

    표준오차는 두 표본이 독립이라고 보고 더한다. 실제로는 같은 구간·같은
    유니버스라 양의 상관이 있어 진짜 오차는 이보다 작다 — 즉 이 z는 보수적이다.
    """
    sa, sb = a["summary"] or {}, b["summary"] or {}
    for k in ("mean_pct", "std_pct", "n"):
        if sa.get(k) is None or sb.get(k) is None:
            return None
    if not sa["n"] or not sb["n"]:
        return None
    delta = sb["mean_pct"] - sa["mean_pct"]
    se = (sa["std_pct"] ** 2 / sa["n"] + sb["std_pct"] ** 2 / sb["n"]) ** 0.5
    return {"delta": delta, "z": delta / se if se else 0.0, "se": se}


def _factor_table(runs):
    """
    knob 하나만 다른 쌍을 모아 knob별로 묶는다.

    한 knob의 판정은 비교가 둘 이상이고 부호가 일관할 때만 준다. 구간마다
    방향이 뒤집히는 것은 이 저장소가 반복해서 겪은 실패 형태다.
    """
    import itertools

    runs = _current_runs(runs)
    groups = {}
    for a, b in itertools.combinations(sorted(runs, key=lambda r: r["id"]), 2):
        if (a["start_d"], a["end_d"]) != (b["start_d"], b["end_d"]):
            continue
        pa, pb_ = a["params"] or {}, b["params"] or {}
        raw_changed = {k for k in set(pa) | set(pb_)
                       if k not in KNOB_SKIP and pa.get(k) != pb_.get(k)}
        if raw_changed & RANK_KEYS:
            continue
        changed = {_logical(k) for k in raw_changed}
        if len(changed) != 1:
            continue
        d = _pair_delta(a, b)
        if d is None:
            continue
        knob = changed.pop()
        groups.setdefault(knob, []).append({
            "knob": knob, "from": a, "to": b,
            "change": _describe_change(knob, a, b),
            "window": f"{a['start_d']} ~ {a['end_d']}",
            **d,
        })

    out = []
    for knob, comps in groups.items():
        deltas = [c["delta"] for c in comps]
        strong = [c for c in comps if abs(c["z"]) >= 2]
        if len(comps) < 2:
            verdict = {"kind": "none", "text": "비교 1건 — 근거 부족",
                       "note": "다른 구간에서도 같은 방향인지 확인해야 합니다."}
        elif all(d > 0 for d in deltas) or all(d < 0 for d in deltas):
            if strong:
                verdict = {"kind": "good", "text": "방향 일관 · 크기 유의",
                           "note": "모든 비교에서 같은 방향이고, 적어도 한 비교는 잡음보다 큽니다."}
            else:
                verdict = {"kind": "warn", "text": "방향은 일관, 크기는 잡음 수준",
                           "note": "부호는 유지되지만 |z| < 2 라 표본 변동으로도 나올 수 있습니다."}
        else:
            verdict = {"kind": "bad", "text": "방향 불일치",
                       "note": "구간·전략에 따라 부호가 뒤집힙니다. 이 knob으로는 성과를 설명할 수 없습니다."}
        out.append({
            "knob": knob, "comparisons": sorted(comps, key=lambda c: c["window"]),
            "n_comp": len(comps),
            "mean_delta": sum(deltas) / len(deltas),
            "max_abs_z": max(abs(c["z"]) for c in comps),
            "verdict": verdict,
        })
    out.sort(key=lambda g: (-g["max_abs_z"], g["knob"]))
    return out


FACTOR_NAMES = {
    "heat_score": "과열 점수", "individual_flow_ratio": "개인 순매수 배율",
    "credit_surge_ratio": "신용잔고 급증 배율", "volume_ratio": "거래대금 배율",
    "foreign_flow_ratio": "외국인 순매수 배율", "institution_flow_ratio": "기관 순매수 배율",
}


def _factor_label(params):
    p = params or {}
    name = FACTOR_NAMES.get(p.get("rank_col"), p.get("rank_col"))
    direction = "낮은 순" if p.get("ascending", True) else "높은 순"
    extra = []
    if p.get("pos52w_filter") is False:
        extra.append("52주 필터 끔")
    if p.get("slots") not in (None, 5):
        extra.append(f"슬롯 {p['slots']}")
    if p.get("exit_rank_pct") is not None:
        extra.append("신호 청산 켬")
    return f"{name} {direction}" + (f" · {' · '.join(extra)}" if extra else "")


def _rank_factor_table(runs):
    """
    랭킹 팩터별 절대 성과를 구간별로 나란히 놓는다.

    "유의미한 팩터가 있나"는 곧 "훈련과 검증에서 같은 방향이고, 그 크기가 잡음을
    넘느냐"다. 한 구간만 있는 팩터는 답할 수 없으므로 판정을 주지 않는다.
    """
    groups = {}
    for r in _current_runs(runs):
        if "rank_col" not in (r["params"] or {}):
            continue
        groups.setdefault(_fingerprint(r["params"]), []).append(r)

    out = []
    for g in groups.values():
        g.sort(key=lambda r: r["start_d"])
        windows = [{"run": w, "label": f"{w['start_d'].year}~{w['end_d'].year}",
                    "mean_pct": (w["summary"] or {}).get("mean_pct"),
                    "t_val": (w["summary"] or {}).get("t_val"),
                    "n": (w["summary"] or {}).get("n")} for w in g]
        means = [w["mean_pct"] for w in windows if w["mean_pct"] is not None]
        ts = [abs(w["t_val"]) for w in windows if w["t_val"] is not None]
        if not means:
            continue
        if len(means) < 2:
            verdict = {"kind": "none", "text": "구간 1개 — 판정 불가",
                       "note": "다른 구간에서도 재야 방향이 유지되는지 알 수 있습니다."}
        elif all(m > 0 for m in means):
            verdict = ({"kind": "good", "text": "전 구간 양수 · 유의",
                        "note": "두 구간 모두 양수이고 |t| ≥ 2 입니다. 이 저장소에서 가장 강한 형태의 관찰입니다."}
                       if any(t >= 2 for t in ts) else
                       {"kind": "warn", "text": "전 구간 양수, 유의하지 않음",
                        "note": "부호는 유지되지만 |t| < 2 라 0과 구분되지 않습니다."})
        elif all(m < 0 for m in means):
            verdict = ({"kind": "bad", "text": "전 구간 음수 · 유의",
                        "note": "구간을 바꿔도 손해입니다. 이 팩터로 랭킹하면 안 됩니다."}
                       if any(t >= 2 for t in ts) else
                       {"kind": "bad", "text": "전 구간 음수",
                        "note": "부호가 일관되게 음수입니다."})
        else:
            verdict = {"kind": "bad", "text": "구간마다 부호가 뒤집힘",
                       "note": "방향이 유지되지 않습니다. 이 팩터로는 성과를 설명할 수 없습니다."}
        out.append({
            "label": _factor_label(g[0]["params"]),
            "windows": windows,
            "best_mean": max(means),
            "verdict": verdict,
        })
    order = {"good": 0, "warn": 1, "none": 2, "bad": 3}
    out.sort(key=lambda f: (order[f["verdict"]["kind"]], -f["best_mean"]))
    return out


def _multiple_comparison_pct(k):
    """변형 k개를 재면 그중 하나가 우연히 양수로 나올 확률."""
    return round((1 - 0.95 ** k) * 100, 1)


def _backtest_curve(rows):
    """청산일별 수익률 합을 누적한 곡선.

    거래가 시간상 겹치므로 일별 자산을 복원할 수는 없다. 여기서는 '한 거래에
    같은 금액을 넣었다고 볼 때의 누적 수익률(단리 합)'을 그린다 — 복리로 접으면
    슬롯 제약이 없는 실행에서 값이 0으로 붕괴해 형태를 못 본다.
    """
    if not rows:
        return None, None

    step = max(1, len(rows) // CURVE_MAX_POINTS)
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    points = []
    for i, r in enumerate(rows):
        day_pct = float(r["ret_sum"]) * 100
        cum += day_pct
        peak = max(peak, cum)
        max_dd = min(max_dd, cum - peak)
        if i % step and i != len(rows) - 1:
            continue
        points.append({
            "label": f"{r['exit_d']:%y/%m}",
            "value": round(cum, 2),
            "delta": day_pct,
            "tip": f"{r['exit_d']:%Y-%m-%d}  누적 {cum:+.1f}%p  "
                   f"당일 청산 {r['n']}건 {day_pct:+.2f}%p",
        })

    chart = _line_chart_data(points)
    chart["cum_pct"] = round(cum, 1)
    return chart, round(max_dd, 1)


@app.get("/backtest")
def backtest_page(request: Request, run: int = 0, reason: str = "", worst: str = ""):
    runs = [dict(r) for r in db.fetchall("""
        SELECT id, ts, strategy, start_d, end_d, params, summary
        FROM backtest_runs ORDER BY strategy
    """)]
    for r in runs:
        r["knobs_key"] = _knobs(r["params"], KNOB_KEY)
        r["verdict"] = _verdict(r["summary"])

    selected = next((r for r in runs if r["id"] == run), None)
    if selected is None and runs:
        selected = runs[0]

    # 같은 knob 조합을 여러 구간에 돌린 실행을 묶어 훈련/검증 대조로 읽는다.
    #
    # 구간이 겹치는 실행은 대조가 아니라 '같은 구간 재측정'이다. 이 저장소에서는
    # 지표 정의가 바뀔 때마다 그런 쌍이 생긴다(2026-08-20 heat_score 대칭화 전후).
    # 옛 측정을 함께 세면 실제로는 전 구간 음수인 규칙이 '부호 불안정'으로 읽히므로,
    # 겹치는 것끼리는 최신 측정만 남긴다.
    siblings, superseded = [], []
    combined = None
    is_superseded = False
    if selected:
        fp = _fingerprint(selected["params"])
        group = [r for r in runs if _fingerprint(r["params"]) == fp]
        kept = []
        for r in sorted(group, key=lambda x: x["ts"], reverse=True):
            if not any(_overlaps(r, k) for k in kept):
                kept.append(r)
        kept_ids = {r["id"] for r in kept}
        superseded = [r for r in group if r["id"] not in kept_ids]
        is_superseded = selected["id"] not in kept_ids
        siblings = [r for r in kept if r["id"] != selected["id"]]
        if not is_superseded and len(kept) >= 2:
            combined = _combined_verdict(kept)

    curve = mdd = reasons = quarters = None
    trades = []
    if selected:
        curve, mdd = _backtest_curve(db.fetchall("""
            SELECT exit_d, COUNT(*) AS n, SUM(ret_pct) AS ret_sum
            FROM backtest_trades WHERE run_id = %s
            GROUP BY exit_d ORDER BY exit_d
        """, (selected["id"],)))

        reason_rows = db.fetchall("""
            SELECT exit_reason, COUNT(*) AS n FROM backtest_trades
            WHERE run_id = %s GROUP BY exit_reason ORDER BY n DESC
        """, (selected["id"],))
        reasons = _stack_bar(_fold_tail([
            {"label": r["exit_reason"] or "-", "value": float(r["n"]), "kind": "series"}
            for r in reason_rows
        ], unit="가지"))

        quarter_rows = db.fetchall("""
            SELECT to_char(exit_d, 'YYYY"Q"Q') AS q, COUNT(*) AS n,
                   AVG(ret_pct) * 100 AS avg_pct
            FROM backtest_trades WHERE run_id = %s
            GROUP BY q ORDER BY q
        """, (selected["id"],))
        peak_abs = max((abs(float(q["avg_pct"])) for q in quarter_rows), default=1.0) or 1.0
        quarters = [{
            "q": q["q"], "n": q["n"], "avg_pct": round(float(q["avg_pct"]), 2),
            "bar_pct": round(abs(float(q["avg_pct"])) / peak_abs * 100, 1),
        } for q in quarter_rows]

        # 개별 매매. 기본은 최근 청산순, worst=1이면 손실 큰 순으로 본다.
        where = ["run_id = %s"]
        params = [selected["id"]]
        if reason:
            where.append("exit_reason = %s")
            params.append(reason)
        order = "ret_pct ASC" if worst else "exit_d DESC"
        trades = db.fetchall(
            f"""SELECT code, entry_d, exit_d, entry_px, exit_px, ret_pct, exit_reason
                FROM backtest_trades WHERE {' AND '.join(where)}
                ORDER BY {order} LIMIT {BACKTEST_TRADE_LIMIT}""",
            tuple(params),
        )

    return templates.TemplateResponse(request, "backtest.html", {
        "runs": runs,
        "selected": selected,
        "trades": trades,
        "reason": reason,
        "worst": bool(worst),
        "limit": BACKTEST_TRADE_LIMIT,
        "curve": curve,
        "mdd": mdd,
        "reasons": reasons,
        "quarters": quarters,
        "labels": SUMMARY_LABELS,
        "knobs": _knobs(selected["params"]) if selected else [],
        "verdict": _verdict(selected["summary"]) if selected else None,
        "siblings": siblings,
        "superseded": superseded,
        "combined": combined,
        "is_superseded": is_superseded,
        "factors": _factor_table(runs),
        "rank_factors": _rank_factor_table(runs),
        "multiple_pct": _multiple_comparison_pct(len(runs)),
    })


# 설정이 언제부터 듣는지가 항목마다 다르다. HEAT_*는 매 판단 때 읽으므로 보유
# 중인 포지션의 청산 판정까지 바로 바뀌지만, 나머지 넷은 진입 시점에 positions
# 행으로 박제된다(max_hold_days 컬럼, stop_px 계산값, 슬롯 수·수량). 화면에
# 적어두지 않으면 "손절을 5%로 바꿨는데 왜 그대로냐"를 알 길이 없다.
SETTING_SCOPE = {
    "HEAT_AVOID": ("즉시", "다음 판단부터 신규 매수 필터에 적용됩니다."),
    "HEAT_SELL": ("즉시", "보유 중인 포지션의 청산 판정에도 바로 적용됩니다."),
    "STOP_PCT": ("신규 진입분만", "손절가는 진입 시점에 계산해 positions.stop_px에 저장됩니다. 지금 보유 중인 종목의 손절가는 바뀌지 않습니다."),
    "MAX_HOLD_DAYS": ("신규 진입분만", "보유기간 한도는 진입 시점에 positions.max_hold_days로 박제됩니다."),
    "SLOTS": ("신규 진입분만", "보유 수가 이 값을 넘어도 강제 청산하지 않습니다 — 자리가 빌 때까지 신규 매수만 멈춥니다."),
    "CAPITAL": ("신규 진입분만", "1슬롯 = CAPITAL / SLOTS로 수량을 정합니다. 이미 산 종목의 수량은 그대로입니다."),
}


@app.get("/settings")
def settings_get(request: Request, saved: str = ""):
    updated = {r["key"]: r["updated_at"]
               for r in db.fetchall("SELECT key, updated_at FROM settings")}
    settings = {k: config.get_setting(k) for k in config._DEFAULTS}
    return templates.TemplateResponse(request, "settings.html", {
        "settings": settings,
        "defaults": config._DEFAULTS,
        "scope": SETTING_SCOPE,
        "updated": updated,
        "cache_ttl": config._CACHE_TTL,
        "saved": saved == "1",
    })


@app.post("/settings")
def settings_post(
    HEAT_AVOID: float = Form(...),
    HEAT_SELL: float = Form(...),
    STOP_PCT: float = Form(...),
    MAX_HOLD_DAYS: int = Form(...),
    SLOTS: int = Form(...),
    CAPITAL: int = Form(...),
):
    for key, value in [
        ("HEAT_AVOID", HEAT_AVOID),
        ("HEAT_SELL", HEAT_SELL),
        ("STOP_PCT", STOP_PCT),
        ("MAX_HOLD_DAYS", MAX_HOLD_DAYS),
        ("SLOTS", SLOTS),
        ("CAPITAL", CAPITAL),
    ]:
        db.execute(
            """INSERT INTO settings (key, value, updated_at)
               VALUES (%s, %s, NOW())
               ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()""",
            (key, str(value)),
        )
    config.invalidate_settings_cache()
    return RedirectResponse("/settings?saved=1", status_code=303)
