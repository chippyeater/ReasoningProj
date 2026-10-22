import io
from itertools import count
import json
import os
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
from app.quote_units import load_or_build_quote_index

GOLD = REPO / "evals/case_a"
_MODEL_LOCAL_IDS = count()


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


def quote_id_for(case_id, file_id, text):
    case = case_store.load_case(case_id)
    file = next(item for item in case.files if item.file_id == file_id)
    matches = [unit.quote_id for unit in load_or_build_quote_index(case_id, file).units if unit.text == text]
    if len(matches) != 1:
        raise AssertionError(f"Expected one Quote Unit for {text!r}, found {len(matches)}")
    return matches[0]


def model_unit(_file_id, evidence_id, **changes):
    value = dict(
        local_id=f"local-{next(_MODEL_LOCAL_IDS)}", type="event", subtype="reported",
        title="付款陈述", content="甲称已付款，尚未经独立核实。", evidence_ids=[evidence_id],
        speaker="甲", side="付款方", actor_local_id=None, target_local_id=None,
        related_issue="款项是否已支付",
    )
    value.update(changes)
    return value


def model_result(units):
    return json.dumps({
        "core_issue": "款项是否已支付",
        "issue_set": ["款项是否已支付"],
        "info_units": units,
    }, ensure_ascii=False)


class IsolatedCaseTest(unittest.TestCase):
    def setUp(self):
        tracing = patch.dict(os.environ, {"LOGICWEAVER_OBSERVABILITY_ENABLED": "false"})
        tracing.start()
        self.addCleanup(tracing.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data_dir = Path(self.temp.name)
        override = patch.object(case_store, "DATA_DIR", self.data_dir)
        override.start()
        self.addCleanup(override.stop)
        self.client = TestClient(app)

    def upload(self, filename="material.txt", raw="甲方付款😀\n乙方收款".encode("utf-8"), source_kind="document"):
        response = self.client.post("/api/cases", data={"case_title": "测试案件",
                                    "source_kinds": json.dumps({filename: source_kind}, ensure_ascii=False)},
                                    files={"files": (filename, raw)})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["case"]
