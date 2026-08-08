"""
Railway DB 초기화 스크립트.
.env에 DB_URL을 입력한 뒤 실행: python setup_db.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

import config

if not config.DB_URL:
    print("오류: .env 파일에 DB_URL을 입력하세요.")
    print("  railway.app → 새 프로젝트 → PostgreSQL 추가 → Variables 탭 → DATABASE_URL 복사")
    sys.exit(1)

import db.connection as db
import config

db.init_schema()

# settings 초기값 (이미 존재하면 유지)
for key, value in config._DEFAULTS.items():
    db.execute(
        "INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING",
        (key, str(value)),
    )
print("기본 설정값 등록 완료")

# 확인
tables = db.fetchall(
    "SELECT table_name FROM information_schema.tables WHERE table_schema='public' ORDER BY table_name"
)
print("생성된 테이블:")
for t in tables:
    print(f"  {t['table_name']}")
