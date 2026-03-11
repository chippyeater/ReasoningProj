"""Mock data used when LLM is unavailable or fails to produce valid JSON."""

from app.schemas import ReasonResponse


DEMO_CASE_TEXT = """证词A：A 在 22:00 出现在仓库
证词B：A 在 22:00 在公司加班
监控：A 在 21:45 进入仓库
银行记录：A 在 22:10 收到转账"""


def get_mock_reasoning() -> ReasonResponse:
    """Return deterministic structured reasoning for local demo."""

    return ReasonResponse(
        entities=[
            {"id": "person_a", "name": "A", "type": "person"},
            {"id": "warehouse", "name": "仓库", "type": "location"},
            {"id": "company", "name": "公司", "type": "location"},
        ],
        events=[
            {"id": "e1", "time": "21:45", "description": "监控显示 A 进入仓库", "source": "监控"},
            {"id": "e2", "time": "22:00", "description": "证词A称 A 在仓库", "source": "证词A"},
            {"id": "e3", "time": "22:00", "description": "证词B称 A 在公司加班", "source": "证词B"},
            {"id": "e4", "time": "22:10", "description": "银行记录显示 A 收到转账", "source": "银行记录"},
        ],
        claims=[
            {"id": "c1", "speaker": "证词A", "content": "A 在 22:00 出现在仓库", "supports": ["e2"]},
            {"id": "c2", "speaker": "证词B", "content": "A 在 22:00 在公司加班", "supports": ["e3"]},
        ],
        conflicts=[
            {
                "id": "cf1",
                "left_claim": "A 在 22:00 在仓库",
                "right_claim": "A 在 22:00 在公司",
                "reason": "同一时间不能同时出现在两个地点",
            }
        ],
        evidence_paths=[
            {
                "id": "p1",
                "path": ["监控 21:45 进入仓库", "22:00 仓库出现", "22:10 收到转账"],
                "conclusion": "A 在案发时段与仓库及资金流动存在关联",
            }
        ],
        recommended_view="conflict_compare",
        summary="关键矛盾在于 22:00 的位置冲突，监控和银行记录可作为辅助证据链。",
    )
