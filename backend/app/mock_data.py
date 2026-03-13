"""Mock data used when LLM is unavailable or fails to produce valid JSON."""

from app.schemas import Claim, Entity, Event, EvidenceItem, ParsedEvidence, ReasonResponse, Relation


DEMO_CASE_TEXT = """证词A：A 在 22:00 出现在仓库
证词B：A 在 22:00 在公司加班
监控：A 在 21:45 进入仓库
银行记录：A 在 22:10 收到转账"""


def get_mock_reasoning() -> ReasonResponse:
    """Return deterministic structured reasoning for local demo."""

    return ReasonResponse(
        parsed_evidences=[
            ParsedEvidence(
                id="pe-1",
                type="text",
                name="证词A",
                parser_tool="parse_text_evidence",
                normalized_text="证词A：A 在 22:00 出现在仓库",
                metadata={"parse_status": "success", "parser_detail": "Plain text content is available."},
            ),
            ParsedEvidence(
                id="pe-2",
                type="text",
                name="证词B",
                parser_tool="parse_text_evidence",
                normalized_text="证词B：A 在 22:00 在公司加班",
                metadata={"parse_status": "success", "parser_detail": "Plain text content is available."},
            ),
        ],
        evidence_items=[
            EvidenceItem(
                id="evd-1",
                type="text",
                original_content="证词A：A 在 22:00 出现在仓库",
                source_file="manual",
                page_or_paragraph="paragraph-1",
                time="22:00",
                producer_or_speaker="证词A",
                is_original_evidence=True,
                notes="直接证词",
            ),
            EvidenceItem(
                id="evd-2",
                type="text",
                original_content="证词B：A 在 22:00 在公司加班",
                source_file="manual",
                page_or_paragraph="paragraph-2",
                time="22:00",
                producer_or_speaker="证词B",
                is_original_evidence=True,
                notes="直接证词",
            ),
            EvidenceItem(
                id="evd-3",
                type="text",
                original_content="监控：A 在 21:45 进入仓库",
                source_file="manual",
                page_or_paragraph="paragraph-3",
                time="21:45",
                producer_or_speaker="监控",
                is_original_evidence=True,
                notes="客观记录",
            ),
            EvidenceItem(
                id="evd-4",
                type="text",
                original_content="银行记录：A 在 22:10 收到转账",
                source_file="manual",
                page_or_paragraph="paragraph-4",
                time="22:10",
                producer_or_speaker="银行记录",
                is_original_evidence=True,
                notes="客观记录",
            ),
        ],
        entities=[
            Entity(id="ent-1", name="A", type="person", aliases=[], source_evidence_ids=["evd-1", "evd-2", "evd-3", "evd-4"]),
            Entity(id="ent-2", name="仓库", type="location", aliases=[], source_evidence_ids=["evd-1", "evd-3"]),
            Entity(id="ent-3", name="公司", type="location", aliases=[], source_evidence_ids=["evd-2"]),
        ],
        relations=[
            Relation(
                id="rel-1",
                subject_entity="ent-1",
                object_entity="ent-2",
                relation_type="entered",
                time="21:45",
                evidence_sources=["evd-3"],
                confidence_status="high",
            ),
            Relation(
                id="rel-2",
                subject_entity="ent-1",
                object_entity="ent-3",
                relation_type="worked_overtime_at",
                time="22:00",
                evidence_sources=["evd-2"],
                confidence_status="medium",
            ),
        ],
        events=[
            Event(
                id="evt-1",
                event_type="warehouse_entry",
                participant_entities=["ent-1", "ent-2"],
                time="21:45",
                location="仓库",
                description="监控显示 A 于 21:45 进入仓库。",
                source_evidence_ids=["evd-3"],
            ),
            Event(
                id="evt-2",
                event_type="fund_transfer",
                participant_entities=["ent-1"],
                time="22:10",
                location="",
                description="银行记录显示 A 在 22:10 收到转账。",
                source_evidence_ids=["evd-4"],
            ),
        ],
        claims=[
            Claim(
                id="clm-1",
                content="A 在 22:00 出现在仓库。",
                source="证词A",
                target_ids=["ent-1", "ent-2", "evt-1"],
                stance="support",
                credibility_status="medium",
                quote="证词A：A 在 22:00 出现在仓库",
            ),
            Claim(
                id="clm-2",
                content="A 在 22:00 在公司加班。",
                source="证词B",
                target_ids=["ent-1", "ent-3"],
                stance="support",
                credibility_status="medium",
                quote="证词B：A 在 22:00 在公司加班",
            ),
        ],
        conflicts=[
            {
                "id": "cf-1",
                "left_claim": "A 在 22:00 在仓库",
                "right_claim": "A 在 22:00 在公司",
                "reason": "同一时间不能同时出现在两个地点",
            }
        ],
        evidence_paths=[
            {
                "id": "path-1",
                "path": ["evd-3", "evd-1", "evd-4"],
                "conclusion": "A 在案发时段与仓库及资金流动存在关联。",
            }
        ],
        recommended_view="conflict_compare",
        summary="关键矛盾在于 22:00 的位置冲突，监控与银行记录构成辅助证据链。",
    )
