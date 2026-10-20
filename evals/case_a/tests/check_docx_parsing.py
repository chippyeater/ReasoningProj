"""v0.2 actual upload/cache checks against unchanged Gold parse cases.

Run from the repository root using backend/.venv/Scripts/python.exe.
This is not a historical baseline or a model evaluation.
"""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parents[1]
sys.path.insert(0, str(REPO / "backend"))

from fastapi.testclient import TestClient
from app import case_store, file_parsers
from app.main import app


class TestV02GoldParsing(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.override = patch.object(case_store, "DATA_DIR", Path(self.temp.name))
        self.override.start()
        self.addCleanup(self.override.stop)
        self.client = TestClient(app)
        self.cases = [json.loads(line) for line in
                      (ROOT / "gold/gold_parse_cases.jsonl").read_text(encoding="utf-8").splitlines()
                      if line.strip()]
        self.source_kinds = {r["filename"]: r["source_kind"] for r in json.loads(
            (ROOT / "fixtures/file_manifest.json").read_text(encoding="utf-8"))["files"]}

    def test_five_actual_docx_uploads_match_fixed_gold_and_cache(self):
        cases = [c for c in self.cases if c["id"] in {"P001", "P002", "P003", "P004", "P005"}]
        self.assertEqual(len(cases), 5, "Must actually run all five Gold DOCX cases")
        for case in cases:
            with self.subTest(case=case["id"]):
                raw = (ROOT / case["input"]["raw_file_path"]).read_bytes()
                filename = case["input"]["filename"]
                response = self.client.post("/api/cases", data={"case_title": "案件 A", "source_kinds": json.dumps({filename: self.source_kinds[filename]})},
                                            files={"files": (case["input"]["filename"], raw)})
                self.assertEqual(response.status_code, 201, response.text)
                saved = response.json()["case"]
                file = saved["files"][0]
                self.assertEqual(file["parse_status"], "parsed")
                directory = Path(self.temp.name) / saved["case_id"]
                self.assertEqual((directory / file["original_path"]).read_bytes(), raw)
                cached = (directory / file["parsed_text_path"]).read_bytes()
                self.assertEqual(hashlib.sha256(cached).hexdigest(), case["expected"]["text_sha256"])
                self.assertEqual(len(cached.decode("utf-8")), case["expected"]["text_length"])
                with patch.object(file_parsers, "parse_text", side_effect=AssertionError("must use TXT")):
                    for _ in range(2):
                        context = self.client.get(f"/api/cases/{saved['case_id']}/extraction-input")
                        self.assertEqual(context.status_code, 200, context.text)
                        self.assertEqual(context.json()["files"][0]["text"], cached.decode("utf-8"))

    def test_empty_and_image_match_gold_failure_contract(self):
        for cid, filename, raw in [("P006", "empty.txt", b""), ("P007", "unreadable.png", b"image")]:
            expected = next(c["expected"] for c in self.cases if c["id"] == cid)
            with self.subTest(case=cid):
                response = self.client.post("/api/cases", data={"case_title": "异常输入", "source_kinds": json.dumps({filename: "other"})},
                                            files={"files": (filename, raw)})
                self.assertEqual(response.status_code, 201, response.text)
                saved = response.json()["case"]
                file = saved["files"][0]
                self.assertEqual(file["parse_status"], expected["parse_status"])
                self.assertIsNone(file["parsed_text_path"])
                self.assertTrue(file["error_message"])
                response = self.client.get(f"/api/cases/{saved['case_id']}/extraction-input")
                self.assertEqual(response.status_code, 422, response.text)


if __name__ == "__main__":
    unittest.main()
