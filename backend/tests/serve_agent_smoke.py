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
    """Offline model decides from exactly the current compact context."""
    from test_reasoning_proposals import suggestion, proposal_call
    await asyncio.sleep(0.15)
    state = json.loads(messages[1]["content"])
    goal, proposals = state["goal"], state["proposals"]
    if "补充合同对已有缺口" in goal:
        units = state["materials"]["info_units"]["items"]
        if not units:
            return replies.tool("search_info_units", {"query": "合同", "limit": 1}, "contract-search")
        uid = units[0]["id"]
        refs = [ref for ref in state["materials"]["source_refs"]["items"] if ref["info_unit_id"] == uid]
        if not refs:
            return replies.tool("get_evidence", {"info_unit_ids": [uid]}, "contract-source")
        ref = refs[0]
        if not proposals:
            return replies.tool("propose_evidence_gap_update", {
                "gap_id": state["reasoning"]["gaps"][0]["id"], "addressing_info_unit_ids": [uid], "status": "open",
                "rationale": "合同只约定起租，实际履行和收款尚待核实，故仅部分回应原缺口。",
                "evidence_refs": [{"info_unit_id": uid, "source_ref_index": ref["source_ref_index"]}]})
        return replies.finish(goal, [uid], refs=[(uid, ref["source_ref_index"])], status="evidence_insufficient",
                              questions=["实际履行情况仍未核实。"], answer="补充合同回应了约定部分，仍需实际履行记录；可审阅对原缺口的更新建议。")
    refs = state["materials"]["source_refs"]["items"]
    ids = {r["info_unit_id"] for r in refs}
    if "失败" in goal and "总结失败" not in goal:
        raise llm_client.LLMFailure("llm_timeout", "Offline model timeout")
    if "删除" in goal:
        return replies.finish(goal, status="unsupported", questions=["当前仅支持只读调查，请使用手动编辑。"])
    if "能力" in goal:
        return replies.finish(goal, status="completed", basis="general", answer="可检索案件材料、核对来源，并提出待确认建议。")
    if "数量" in goal:
        return replies.finish(goal, status="completed", basis="case_metadata", answer=f"当前案件有{state['case_info_directory']['total_count']}个信息节点。")
    arrival = any(w in goal for w in ("入住", "搬进去"))
    analysis = "实际空置损失" in goal or "同轮" in goal
    uid = "U026" if arrival else "U029"
    needed = {"U029", "U036"} if analysis else {uid}
    if not needed <= ids:
        return replies.tool("get_evidence", {"info_unit_ids": sorted(needed)}, "sources")
    candidates = []
    if any(w in goal for w in ("假设", "缺口", "建议")) or analysis:
        first = suggestion("hypothesis")
        if analysis:
            first["content"] = "交房后新租约尚未计租，房东可能少收该期间租金。"
            second = suggestion("hypothesis")
            second["content"] = "计租连续，入住间隔可能没有造成少收租金。"
            gap = suggestion("evidence_gap")
            gap.update(content="核对新旧计租期间和收款记录。", hypothesis_proposal_indices=[0, 1])
            candidates = [first, second, gap]
        else:
            candidates = [first]
            formal = state["reasoning"]["hypotheses"]
            if formal:
                candidates.extend([suggestion("evidence_gap", formal[0]["id"]), suggestion("hypothesis_status", formal[0]["id"])])
    # Explicit protocol scenarios used by reasoning-workspace-smoke.cjs.
    if "同轮" in goal or "总结失败" in goal:
        first, second = suggestion("hypothesis"), suggestion("hypothesis")
        second["content"] = "同轮新假设：入住虽有间隔，但计租连续，租金可能未减少。"
        gaps = []
        for content in ("核对同轮假设的租金流水。", "核对同轮假设的交房记录。"):
            gap = suggestion("evidence_gap")
            gap.update(content=content, hypothesis_proposal_indices=[1])
            gaps.append(gap)
        candidates = [first, second, *gaps]
        if "同轮证据关联" in goal:
            candidates = [first, second, *[
                {**suggestion("evidence_link"), "meaning": meaning, "hypothesis_proposal_indices": [parent]}
                for parent, meaning in ((0, "oppose"), (1, "support"), (0, "related"))]]
            candidates[-1].update(info_unit_ids=["U036"], evidence_refs=[{"info_unit_id": "U036", "source_ref_index": 0}])
    if len(proposals) < len(candidates):
        return proposal_call(candidates[len(proposals)], [p["proposal_id"] for p in proposals], f"proposal-{len(proposals)}")
    if "总结失败" in goal:
        raise llm_client.LLMFailure("llm_timeout", "Offline failure before delivery")
    answer = "据当事人陈述存在相关情况，尚未独立核实。"
    if analysis:
        answer = "实际租金损失尚未证实。已提出竞争性解释与共同缺口，请优先核对计租日与收款记录；完成后可查看、编辑与撤销变更。"
    if "超长总结" in goal:
        answer = "重复的完整调查报告。" * 150
    return replies.finish(goal, [uid], refs=[(uid, 0)], statement="材料记载的是当事人陈述，仍需独立核实。", answer=answer,
        status="evidence_insufficient" if analysis else "completed",
        questions=["尚缺计租及收款记录以核实是否少收租金。"] if analysis else [])


async def extract(config, prompt, model_input, schema):
    """Real extraction pipeline with deterministic local candidates, no provider request."""
    file = model_input["files"][0]
    new = file["filename"] == "new-contract.txt"
    result = {"core_issue": "租金实际损失", "issue_set": ["租金实际损失"], "info_units": [{
        "local_id": "lease-record", "type": "state", "subtype": None,
        "title": "新合同起租约定" if new else "原有租赁陈述",
        "content": "合同约定起租，实际履行尚未核实。" if new else "当事人称存在租赁履行争议。",
        "evidence_ids": [file["quote_units"][0]["quote_id"]], "speaker": file["filename"],
        "side": None, "actor_local_id": None, "target_local_id": None, "related_issue": None}]}
    return llm_client.LLMReply(json.dumps(result, ensure_ascii=False), {"mock": True})


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
                llm_client, "load_config", return_value=replies.CONFIG), patch.object(llm_client, "call_tools", side_effect=model), patch.object(
                llm_client, "call_structured", side_effect=extract):
            uvicorn.run(app, host="127.0.0.1", port=8018)
    finally:
        fixture.doCleanups()
