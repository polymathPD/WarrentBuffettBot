"""
미정의 이름(NameError 예비군)이 남아 있지 않은지 정적으로 검사한다.

2026-08-24 09:05 배치가 open_job 안의 `config.KIS_MODE`에서 NameError로 죽었다.
scheduler.py가 config를 임포트하지 않았는데, 파이썬은 전역 이름을 호출 시점에
찾으므로 임포트만으로는 드러나지 않고 테스트 253개도 그 함수를 실행하지 않았다.
같은 날 pyflakes를 돌리자 daily_job의 펀더멘털 진입 블록에서 decide_fundamental,
buy, name_of 세 개가 더 나왔다 — 후보가 하나라도 잡히는 날엔 16:10 배치가 죽는다.

단위 테스트로 모든 분기를 덮는 것보다 이 검사 한 줄이 이 부류를 확실히 막는다.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_no_undefined_names():
    proc = subprocess.run(
        [sys.executable, "-m", "pyflakes", str(ROOT)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    bad = [ln for ln in proc.stdout.splitlines() if "undefined name" in ln]
    assert not bad, "미정의 이름:\n" + "\n".join(bad)
