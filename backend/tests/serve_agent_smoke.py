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


async def model(config, messages, definitions, schema=None, **kwargs):
    await asyncio.sleep(0.15)
    initial = json.loads(messages[1]["content"])
    goal = initial["goal"]
    names = [m["tool_calls"][0]["function"]["name"] for m in messages if m["role"] == "assistant" and m.get("tool_calls")]
    returned = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
    if "失败" in goal and "总结失败" not in goal:
        raise llm_client.LLMFailure("llm_timeout", "离线模拟调用超时")
    if "删除" in goal:
        return replies.final("当前仅支持只读调查；请使用手动编辑功能。") if "present_findings" in names else replies.present(
            goal, status="unsupported", questions=["当前仅支持只读调查；请使用手动编辑功能。"])
    arrival = any(word in goal for word in ("入住", "搬进去"))
    analysis = "房东是否存在实际空置损失" in goal
    if not names:
        return replies.tool("search_info_units", {"query": "空置" if analysis else "入住"}) if arrival or analysis else replies.tool("get_evidence", {"info_unit_ids": ["U029"]})
    if analysis and "get_evidence" not in names:
        return replies.tool("get_evidence", {"info_unit_ids": ["U029", "U036"]}, "both-sides")
    if analysis and names.count("search_info_units") == 1:
        return replies.tool("search_info_units", {"query": "计租"}, "rent")
    if arrival and "get_evidence" not in names:
        return replies.tool("get_evidence", {"info_unit_ids": ["U026"]}, "verify")
    proposals = []
    if any(word in goal for word in ("假设", "缺口", "建议")) or analysis:
        from test_reasoning_proposals import suggestion, proposal_call
        hypotheses = initial.get("work_context", {}).get("reasoning", {}).get("hypotheses", [])
        if "同轮" in goal or analysis:
            first, target = suggestion("hypothesis"), suggestion("hypothesis")
            first["content"] = "交房后新租约尚未计租，房东可能损失该时段租金。"
            target["content"] = "同轮新假设：入住虽有间隔，但计租连续，租金可能未减少。"
            first_gap, second_gap = suggestion("evidence_gap"), suggestion("evidence_gap")
            first_gap.update(content="核对同轮假设的租金流水。", hypothesis_proposal_indices=[1])
            second_gap.update(content="核对同轮假设的交房记录。", hypothesis_proposal_indices=[1])
            proposals = [first, target, first_gap, second_gap]
        else:
            proposals = [suggestion("hypothesis")]
            if hypotheses:
                proposals.extend([suggestion("evidence_gap", hypotheses[0]["id"]), suggestion("hypothesis_status", hypotheses[0]["id"])])
        attempts = sum(name.startswith("propose_") for name in names)
        if attempts < len(proposals):
            ids = [r["proposal_id"] for r in returned if r.get("pending")]
            return proposal_call(proposals[attempts], ids, f"proposal-{attempts}")
    if "present_findings" not in names:
        uid = "U026" if arrival else "U029"
        return replies.present(goal, [uid], refs=[(uid, 0)], status="evidence_insufficient" if analysis else "completed",
            statement="张某在笔录中提出相关说法，尚未独立核实。")
    if "总结失败" in goal:
        raise llm_client.LLMFailure("llm_timeout", "离线模拟总结超时")
    answer = "据张某陈述，替租人于 2026 年 3 月 8 日搬入，尚未独立核实。" if arrival else "现有陈述尚不足以确认实际租金损失。关键是计租起点和收款记录，仍需核实。"
    if proposals:
        answer += "已提出待确认建议，可在画布查看。"
    return replies.final(answer)


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
