"""Local UI smoke fixture: real API/loop, isolated Case A, no external services.

Run from repo root with backend/.venv/Scripts/python.exe backend/tests/serve_agent_smoke.py.
Only the model boundary is mocked; existing CRUD and workspace routes stay real.
"""
import asyncio
import json
import os
from unittest.mock import patch

import test_investigation_tools as fixtures
import test_investigation_agent as replies
from app import case_store, llm_client
from app.main import app
import uvicorn


async def model(config, messages, definitions, schema, **kwargs):
    await asyncio.sleep(0.4)
    goal = json.loads(messages[1]["content"])["goal"]
    if "失败" in goal:
        raise llm_client.LLMFailure("llm_timeout", "离线模拟调用超时")
    if "删除" in goal:
        reply = replies.final(goal, status="unsupported")
        data = json.loads(reply.content)
        data["open_questions"] = ["当前仅支持只读调查；请使用手动编辑功能。"]
        reply.content = json.dumps(data, ensure_ascii=False)
        return reply
    calls = [message for message in messages if message["role"] == "tool"]
    if not calls:
        if "入住" in goal:
            return replies.tool("search_info_units", {"query": "入住"})
        return replies.tool("get_evidence", {"info_unit_ids": ["U029"]})
    if "入住" in goal and len(calls) == 1:
        uid = json.loads(calls[-1]["content"])["items"][0]["id"]
        return replies.tool("get_evidence", {"info_unit_ids": [uid]}, "verify")
    uid = "U026" if "入住" in goal else "U029"
    return replies.final(goal, [uid], refs=[(uid, 0)], status="completed",
                         statement="张某在材料中提出相关说法，仍需独立证据核实。")


if __name__ == "__main__":
    fixture = fixtures.TestInvestigationTools()
    fixture.setUp()
    try:
        for file in fixture.case.files:
            target = fixture.data_dir / fixture.case.case_id / file.original_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((fixtures.GOLD / "fixtures/raw" / file.filename).read_bytes())
        case_store.save_case(fixture.case)
        from app.case_service import create_empty_case
        case_store.save_case(create_empty_case("空白测试案件"), set_current=False)
        with patch.dict(os.environ, {"LOGICWEAVER_OBSERVABILITY_ENABLED": "false"}), patch.object(
                llm_client, "load_config", return_value=replies.CONFIG), patch.object(llm_client, "call_tools", side_effect=model):
            uvicorn.run(app, host="127.0.0.1", port=8018)
    finally:
        fixture.doCleanups()
