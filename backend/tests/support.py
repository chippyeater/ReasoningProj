import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend"))

from docx import Document
from fastapi.testclient import TestClient
from app import case_store
from app.main import app

GOLD = REPO / "evals/case_a"


def gold_lines(name):
    return [json.loads(line) for line in (GOLD / "gold" / name).read_text(encoding="utf-8").splitlines()
            if line.strip()]


def docx_bytes(*paragraphs):
    document = Document()
    for text in paragraphs:
        document.add_paragraph(text)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


class IsolatedCaseTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data_dir = Path(self.temp.name)
        override = patch.object(case_store, "DATA_DIR", self.data_dir)
        override.start()
        self.addCleanup(override.stop)
        self.client = TestClient(app)

    def upload(self, filename="material.txt", raw="甲方付款😀\n乙方收款".encode("utf-8")):
        response = self.client.post("/api/cases", data={"case_title": "测试案件"},
                                    files={"files": (filename, raw)})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["case"]
