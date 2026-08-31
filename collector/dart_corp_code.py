"""
DART 고유번호(corp_code) 매핑 → instruments.dart_corp_code

DART 조회 API는 종목코드가 아니라 8자리 고유번호를 받는다. 전체 목록이
corpCode.xml 한 파일(zip)로 제공되므로 받아서 상장사만 골라 붙인다.
종목이 새로 상장되면 다시 실행한다.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from dotenv import load_dotenv
load_dotenv()

import io
import zipfile
import xml.etree.ElementTree as ET
import requests
import db.connection as db
import config
from collector.base import Collector


class DartCorpCodeCollector(Collector):
    SOURCE = "dart_corp_code"
    API_URL = "https://opendart.fss.or.kr/api/corpCode.xml"

    def fetch_xml(self) -> bytes:
        """corpCode.xml(zip)을 받아 XML 바이트를 반환."""
        if not config.DART_API_KEY:
            raise RuntimeError(
                "DART_API_KEY 미설정: opendart.fss.or.kr에서 인증키를 발급받아 .env에 넣으세요"
            )

        resp = requests.get(self.API_URL,
                            params={"crtfc_key": config.DART_API_KEY}, timeout=60)
        resp.raise_for_status()

        # 키가 틀리거나 한도를 넘으면 zip이 아니라 status/message XML이 그대로 온다.
        if resp.content[:2] != b"PK":
            raise RuntimeError(f"zip 응답이 아님: {resp.content[:200]!r}")

        with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
            return z.read(z.namelist()[0])

    def parse(self, xml_bytes: bytes) -> dict:
        """stock_code -> corp_code. 종목코드가 있는 상장사만 남긴다."""
        root = ET.fromstring(xml_bytes)
        mapping = {}
        for el in root.iter("list"):
            stock = (el.findtext("stock_code") or "").strip()
            corp = (el.findtext("corp_code") or "").strip()
            if len(stock) == 6 and corp:
                mapping[stock] = corp
        return mapping

    def run(self) -> int:
        """매핑을 받아 instruments에 반영하고 갱신된 종목 수를 반환."""
        mapping = self.parse(self.fetch_xml())
        print(f"DART 상장사 {len(mapping):,}종목 수신")

        known = {r["code"] for r in db.fetchall("SELECT code FROM instruments")}
        rows = [(corp, code) for code, corp in mapping.items() if code in known]
        if rows:
            db.executemany(
                """UPDATE instruments SET dart_corp_code = v.corp
                   FROM (VALUES %s) AS v(corp, code)
                   WHERE instruments.code = v.code""",
                rows,
            )

        filled = db.fetchone(
            "SELECT COUNT(*) AS n FROM instruments WHERE dart_corp_code IS NOT NULL"
        )["n"]
        print(f"매핑 완료: {filled:,}/{len(known):,}종목 "
              f"(미매핑 {len(known) - filled:,}종목: 상장폐지·비상장 등)")
        return len(rows)
