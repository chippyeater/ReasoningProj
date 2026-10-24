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
    names = [a["tool"] for a in initial["knowledge_state"]["activities"]["items"]]
    returned = [{**p, "pending": True} for p in initial["knowledge_state"]["proposals"]["items"]]
    # New delivery/recovery scenarios reuse actual initial sources and real tools.
    if any(word in goal for word in ("首轮来源直用", "遗漏来源交付", "恢复参数", "探索无直接证据")):
        from test_reasoning_proposals import suggestion, proposal_call
        sources = initial["knowledge_state"]["source_refs"]["items"]
        uid = sources[0]["info_unit_id"] if sources else "U026"
        index = sources[0]["source_ref_index"] if sources else 0
        proof = {"info_unit_ids": [uid], "evidence_refs": [{"info_unit_id": uid, "source_ref_index": index}]}
        reviewed = bool(initial.get("recovery") and initial["recovery"]["code"] == "finish_required")
        if "恢复参数" in goal and not any(r.get("pending") for r in returned):
            if not names:
                return proposal_call({**suggestion("hypothesis"), **proof,
                    "evidence_refs": [{"info_unit_id": uid, "source_ref_index": 999}]}, [], "bad-proposal")
            if not reviewed:
                return replies.final("该建议暂未成功提交。")
            return proposal_call({**suggestion("hypothesis"), **proof}, [], "corrected-proposal")
        if "探索无直接证据" in goal:
            ids = [r["proposal_id"] for r in returned if r.get("pending")]
            if not ids:
                return proposal_call({**suggestion("hypothesis"), "info_unit_ids": [], "evidence_refs": []}, [], "exploratory-h")
            if len(ids) == 1:
                return proposal_call({**suggestion("evidence_gap"), "info_unit_ids": [], "evidence_refs": [],
                    "hypothesis_proposal_ids": ids}, ids, "exploratory-gap")
            return replies.finish(goal, questions=["能否补充新旧租约计租日和收款记录？"], answer="已提出待检验解释与关键缺口，尚无直接证明材料；可逐项采纳或补充计租和收款记录。")
        answer = "据张某陈述，替租人于 2026 年 3 月 8 日搬入，尚未独立核实。"
        if "遗漏来源交付" in goal and not names and not reviewed:
            return replies.final(answer)
        return replies.finish(goal, [uid], refs=[(uid, index)], statement=answer, status="completed", answer=answer if "恢复参数" not in goal else "相关陈述尚未独立核实。已提出一条待检验假设，可在画布中查看、采纳或忽略。")
    if "失败" in goal and "总结失败" not in goal:
        raise llm_client.LLMFailure("llm_timeout", "离线模拟调用超时")
    if "删除" in goal:
        return replies.finish(
            goal, status="unsupported", questions=["当前仅支持只读调查；请使用手动编辑功能。"])
    arrival = any(word in goal for word in ("入住", "搬进去"))
    analysis = "房东是否存在实际空置损失" in goal
    link_analysis = "同轮证据关联" in goal
    if not names:
        if link_analysis:
            return replies.tool("get_evidence", {"info_unit_ids": ["U029", "U036"]})
        return replies.tool("search_info_units", {"query": "空置" if analysis else "入住"}) if arrival or analysis else replies.tool("get_evidence", {"info_unit_ids": ["U029"]})
    if analysis and "get_evidence" not in names:
        return replies.tool("get_evidence", {"info_unit_ids": ["U029", "U036"]}, "both-sides")
    if analysis and names.count("search_info_units") == 1:
        return replies.tool("search_info_units", {"query": "计租"}, "rent")
    if arrival and "get_evidence" not in names:
        return replies.tool("get_evidence", {"info_unit_ids": ["U026"]}, "verify")
    proposals = []
    if any(word in goal for word in ("假设", "缺口", "建议")) or analysis or link_analysis:
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
            if link_analysis:
                def link(index, meaning, uid="U029"):
                    return {**suggestion("evidence_link"), "meaning": meaning, "hypothesis_proposal_indices": [index],
                            "info_unit_ids": [uid], "evidence_refs": [{"info_unit_id": uid, "source_ref_index": 0}]}
                proposals = [first, target, link(0, "oppose"), link(1, "support"), link(0, "verify", "U036")]
        else:
            proposals = [suggestion("hypothesis")]
            if hypotheses:
                proposals.extend([suggestion("evidence_gap", hypotheses[0]["id"]), suggestion("hypothesis_status", hypotheses[0]["id"])])
        attempts = sum(name.startswith("propose_") for name in names)
        if attempts < len(proposals):
            ids = [r["proposal_id"] for r in returned if r.get("pending")]
            return proposal_call(proposals[attempts], ids, f"proposal-{attempts}")
    if "总结失败" in goal:
        raise llm_client.LLMFailure("llm_timeout", "离线模拟结束前超时")
    if "超长总结" in goal:
        # Invalid answer rejected inside the finish tool; no Findings have been accepted.
        return replies.finish(goal, ["U029"], refs=[("U029", 0)], answer="重复的完整调查报告。" * 40, call_id=f"bad-finish-{len(names)}")
    if "安全格式检查" in goal:
        answer = '实际损失**尚未证实**。\n\n<img src=x onerror="window.summaryInjected=true">[查看](javascript:alert(1))'
    elif analysis:
        answer = "实际租金损失**尚未证实**。\n\n已提出两条假设和两项证据缺口，优先核实计租日及收款记录，以判断是否少收租金。可在画布中查看、采纳或忽略建议。"
    elif link_analysis:
        answer = "实际租金损失尚未证实。已提出两条假设和三项证据关联，建议核对计租日和收款记录；可逐项查看、采纳或忽略。"
    else:
        answer = "据张某陈述，替租人于 2026 年 3 月 8 日搬入，尚未独立核实。" if arrival else "现有陈述尚不足以确认实际租金损失。关键是计租起点和收款记录，仍需核实。"
        if proposals:
            answer += "已提出待确认建议，可在画布查看。"
    uid = "U026" if arrival else "U029"
    return replies.finish(goal, [uid], refs=[(uid, 0)], status="evidence_insufficient" if analysis else "completed",
        statement="张某在笔录中提出相关说法，尚未独立核实。", answer=answer)


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
