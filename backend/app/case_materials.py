"""Read-only HTML projection of the saved Case and every current Issue workspace."""
from base64 import b64encode
from collections.abc import Callable
from datetime import datetime, timezone
from hashlib import sha256
from html import escape
from pathlib import Path
from urllib.parse import quote

from app.issue_workspaces import IssueWorkspaces, issue_keys
from app.schemas import CaseData, CaseFile, InfoUnit

SECTIONS = (
    ("overview", "案件概况"), ("timeline", "事实时间线"),
    ("issues", "争议问题与证据对应表"), ("files", "证据目录"),
    ("gaps", "待补充事项"), ("preparation", "协商准备摘要"),
)
JUDGMENTS = {"verified": "已证实命题", "unverified": "未证实命题", "refuted": "已否定命题"}
MEANINGS = {"support": "支持", "oppose": "反对", "related": "相关"}
STATUSES = {"pending": "待调查", "investigating": "调查中", "closed": "已关闭"}


def text(value: object) -> str:
    return escape(str(value), quote=True)


def empty(message: str) -> str:
    return f'<p class="empty">{text(message)}</p>'


def classification(unit: InfoUnit) -> tuple[str, str]:
    # A file reference or a supporting EvidenceLink never upgrades a statement.
    if unit.type == "event" and unit.subtype == "verified":
        return "verified", "已核实事件 · 按现有标记"
    if unit.type in {"state", "position"} or unit.subtype == "reported":
        return "unverified", "当事人 / 来源陈述 · 未证实"
    if unit.subtype == "documented":
        return "unverified", "有材料记录 · 仍待核实"
    return "unverified", "待核实事项"


def render_case_materials(case: CaseData, workspaces: IssueWorkspaces, *,
                          source_url: Callable[[CaseFile], str] | None = None) -> str:
    """No writes, model calls, source parsing, or conclusions beyond saved labels.

    IDs in HTML attributes are generated locally; business IDs are escaped text.
    The only file authority is CaseFile.file_id via InfoUnit.source_refs.file_id.
    """
    generated = datetime.now(timezone.utc).isoformat(timespec="seconds")
    keys = issue_keys(case)
    units = {u.id: u for u in case.info_units}
    files = {f.file_id: f for f in case.files}
    unit_targets = {uid: f"source-{i}" for i, uid in enumerate(units)}
    file_targets = {fid: f"file-{i}" for i, fid in enumerate(files)}
    unit_issues = {uid: set() for uid in units}
    links_by_unit = {uid: [] for uid in units}
    for unit in units.values():
        if unit.related_issue in keys:
            unit_issues[unit.id].add(unit.related_issue)
    for issue in keys:
        workspace = workspaces.workspaces[issue]
        hypotheses = {h.id: h for h in workspace.reasoning.hypotheses}
        referenced = set(workspace.canvas.visible_node_ids)
        for link in workspace.reasoning.links:
            referenced.add(link.info_unit_id)
            if link.info_unit_id in units:
                links_by_unit[link.info_unit_id].append((issue, link, hypotheses[link.hypothesis_id]))
        for gap in workspace.reasoning.gaps:
            referenced.update(gap.basis_info_unit_ids + gap.addressing_info_unit_ids)
        for uid in referenced & units.keys():
            unit_issues[uid].add(issue)

    def badge(kind: str, label: str) -> str:
        return f'<span class="badge {text(kind)}">{text(label)}</span>'

    def source_button(uid: str) -> str:
        return f'<button type="button" class="source-button" data-source="{unit_targets[uid]}">查看来源与关联 ↗</button>'

    def original_link(file: CaseFile) -> str:
        url = source_url(file) if source_url else (
            f'/api/cases/{quote(case.case_id, safe="")}/files/{quote(file.file_id, safe="")}/original')
        return (f'<a class="original-link" href="{text(url)}" target="_blank" rel="noopener noreferrer">'
                '打开原始文件 ↗</a>')

    def source_labels(unit: InfoUnit) -> str:
        labels = []
        for fid in dict.fromkeys(ref.file_id for ref in unit.source_refs):
            if fid in files:
                labels.append(f'<a href="#{file_targets[fid]}">{text(files[fid].filename)}</a>')
            else:
                labels.append('来源未关联')
        return '<p class="meta">来源文件：' + ('；'.join(labels) or '来源未关联') + '</p>'

    def unit_card(uid: str) -> str:
        unit = units.get(uid)
        if unit is None:
            return empty(f"信息节点 {uid} 未关联到当前案件；来源未关联。")
        kind, label = classification(unit)
        speaker = f'<p class="meta">陈述者：{text(unit.speaker)} · 立场：{text(unit.side or "未记录")}</p>' if unit.speaker else ""
        return (f'<details class="unit"><summary>{text(unit.title)} {badge(kind, label)}</summary>'
                f'<p>{text(unit.content)}</p>{speaker}{source_labels(unit)}<p class="meta">InfoUnit ID：<code>{text(uid)}</code></p>'
                f'{source_button(uid)}</details>')

    def unit_list(items: list[InfoUnit], missing: str) -> str:
        return "".join(unit_card(u.id) for u in items) or empty(missing)

    def hypothesis_card(h, issue: str) -> str:
        retired = ' · 已停用' if h.work_status == "deprecated" else ""
        return (f'<article class="record">{badge(h.judgment, JUDGMENTS[h.judgment])}'
                f'<p>{text(h.content)}</p><p class="meta">{text(issue)}{retired} · '
                f'Hypothesis ID：<code>{text(h.id)}</code></p></article>')

    positions = [u for u in units.values() if u.type == "position"]
    statements = [u for u in units.values() if u.type == "state"]
    events = [u for u in units.values() if u.type == "event"]
    people = [u for u in units.values() if u.type == "entity" and u.subtype in {"person", "organization"}]
    all_hypotheses = [(issue, h) for issue in keys for h in workspaces.workspaces[issue].reasoning.hypotheses]
    open_gaps = [(issue, g) for issue in keys for g in workspaces.workspaces[issue].reasoning.gaps if g.status == "open"]
    resolved_count = sum(g.status == "resolved" for issue in keys for g in workspaces.workspaces[issue].reasoning.gaps)
    progress = "".join(
        f'<li><span>{text(issue)}</span>{badge(workspaces.workspaces[issue].status, STATUSES[workspaces.workspaces[issue].status])}</li>'
        for issue in keys)
    overview = (f'<p class="lead">{text(case.case_title)}</p>'
        f'<p class="note">这是一份已保存调查结果的材料快照。来源关联不等于内容已证实，判断标签沿用 Workspace。</p>'
        '<h3>人物与组织</h3>' + empty('以下为材料中记录的人物与组织；现有数据未单独标定当事人角色。')
        + unit_list(people, '尚无人物或组织记录。')
        + '<h3>争议背景</h3>' + empty('未保存独立的争议背景摘要；以下保留已有事件与状态陈述。')
        + unit_list(events + statements, '尚无事件或状态记录。')
        + '<h3>用户诉求与各方立场</h3>' + empty('现有数据未标定用户身份，不能自动认定哪一方立场属于用户诉求。')
        + unit_list(positions, '尚无已记录的诉求或立场。')
        + f'<h3>核心争议问题</h3><p>{text(case.core_issue or "尚未记录核心争议问题")}</p>'
        + f'<h3>当前调查进度</h3><p>{len(keys)} 个争议问题 · {len(all_hypotheses)} 个命题 · '
        f'{len(open_gaps)} 个未解决缺口 · {resolved_count} 个已解决缺口</p>'
        + (f'<ul class="progress">{progress}</ul>' if progress else empty('尚无 Issue 调查状态。')))

    # Reuse saved semantic timeline labels. InfoUnit.created_at is extraction time.
    times = {uid: [] for uid in units}
    for view in case.reasoning_views:
        if view.view_type == "timeline":
            for event in view.payload.events:
                for uid in event.related_info_unit_ids:
                    times[uid].append(event)

    def event_key(unit):
        dates = [e.sort_date for e in times[unit.id] if e.sort_date]
        return (not bool(dates), min(dates) if dates else "", unit.id)

    timeline_rows = []
    for unit in sorted(events, key=event_key):
        labels = list(dict.fromkeys(e.time_label for e in times[unit.id]))
        time_label = " / ".join(labels) if labels else "事件时间未记录"
        kind, label = classification(unit)
        linked = "".join(hypothesis_card(h, issue) for issue, _, h in links_by_unit[unit.id])
        timeline_rows.append(f'<article class="timeline-event"><div class="time-label">{text(time_label)}</div>'
            f'<h3>{text(unit.title)}</h3>{badge(kind, label)}<p>{text(unit.content)}</p>'
            f'<p class="meta">陈述者：{text(unit.speaker or "未记录")} · InfoUnit ID：<code>{text(unit.id)}</code></p>'
            f'{source_labels(unit)}{source_button(unit.id)}<details><summary>关联命题及其判断</summary>{linked or empty("尚无关联命题。")}</details></article>')
    timeline = ('<p class="note">事件时间沿用已保存时间线；没有时间记录时不推测。命题判断独立展示，不反向改变来源陈述的性质。</p>'
                + ("".join(timeline_rows) or empty('尚无事件类 InfoUnit，暂无可整理的时间线。')))

    issue_rows = []
    gap_rows = []
    for index, issue in enumerate(keys):
        reasoning = workspaces.workspaces[issue].reasoning
        hlookup = {h.id: h for h in reasoning.hypotheses}
        hypotheses_html = []
        for h in reasoning.hypotheses:
            evidence = []
            for link in reasoning.links:
                if link.hypothesis_id == h.id:
                    evidence.append(f'<article class="evidence-link">{badge(link.meaning, MEANINGS[link.meaning])}'
                        f'<p>{text(link.rationale)}</p><p class="meta">EvidenceLink ID：<code>{text(link.id)}</code></p>'
                        f'{unit_card(link.info_unit_id)}</article>')
            hypotheses_html.append(hypothesis_card(h, issue) + '<details><summary>证据关联 · '
                f'{len(evidence)} 项</summary>{"".join(evidence) or empty("尚无 EvidenceLink；来源未关联。")}</details>')
        related_units = [u for u in units.values() if issue in unit_issues[u.id]]
        issue_rows.append(f'<details class="issue-group" id="issue-{index}" open><summary>{text(issue)} '
            f'{badge(workspaces.workspaces[issue].status, STATUSES[workspaces.workspaces[issue].status])}</summary>'
            + ("".join(hypotheses_html) or empty('尚无 Hypothesis。'))
            + '<details><summary>本议题关联材料</summary>' + unit_list(related_units, '尚无关联材料。') + '</details></details>')
        for gap in reasoning.gaps:
            if gap.status != "open":
                continue
            targets = "".join(hypothesis_card(hlookup[hid], issue) for hid in gap.hypothesis_ids)
            gap_rows.append(f'<article class="record gap">{badge("open", "未解决 · open")}'
                f'<h3>{text(gap.content)}</h3><p class="meta">Issue：{text(issue)} · Gap ID：<code>{text(gap.id)}</code></p>'
                f'<details><summary>关联命题 · {len(gap.hypothesis_ids)} 项</summary>{targets}</details>'
                '<details><summary>缺口形成依据</summary>'
                + ("".join(unit_card(uid) for uid in gap.basis_info_unit_ids) or empty('尚无形成依据；来源未关联。'))
                + '</details><details><summary>已有回应材料（不等于缺口已解决）</summary>'
                + ("".join(unit_card(uid) for uid in gap.addressing_info_unit_ids) or empty('尚无回应材料。')) + '</details></article>')
    issue_nav = ''.join(f'<a href="#issue-{i}">{text(issue)}</a>' for i, issue in enumerate(keys))
    issues_html = f'<nav class="issue-index" aria-label="议题分组">{issue_nav}</nav>' + (''.join(issue_rows) or empty('尚无争议问题。'))

    file_rows = []
    for fid, file in files.items():
        associated = [u for u in units.values() if any(ref.file_id == fid for ref in u.source_refs)]
        related_issues = [issue for issue in keys if any(issue in unit_issues[u.id] for u in associated)]
        file_rows.append(f'<details class="file-record" id="{file_targets[fid]}" data-file-id="{text(fid)}">'
            f'<summary>{text(file.filename)} <span class="badge">{text(file.file_type.value)}</span></summary>'
            f'<p>原始文件 ID：<code>{text(fid)}</code></p><p class="meta">解析状态：{text(file.parse_status.value)}</p>'
            f'{original_link(file)}'
            f'<p>相关 Issue：{text("；".join(related_issues) or "尚未关联 Issue")}</p>'
            '<h4>关联信息节点</h4>' + unit_list(associated, '尚无关联 InfoUnit。') + '</details>')
    files_html = '<p class="note">按原始文件 ID 去重，包含本 Case 的全部上传文件；同名文件仍按不同 ID 保留。</p>' + (''.join(file_rows) or empty('尚无原始上传文件。'))
    gaps_html = f'<p class="note">当前未解决 {len(open_gaps)} 项，已解决 {resolved_count} 项；已解决缺口不列为待补充事项。</p>' + (''.join(gap_rows) or empty('当前没有已记录的未解决缺口；这不表示材料已经充分。'))

    confirmed = [u for u in events if u.subtype == "verified"]
    verified_h = ''.join(hypothesis_card(h, issue) for issue, h in all_hypotheses if h.judgment == 'verified' and h.work_status == 'active')
    disputed_h = ''.join(hypothesis_card(h, issue) for issue, h in all_hypotheses if h.judgment != 'verified' and h.work_status == 'active')
    communications = [u for u in units.values() if any(ref.source_kind == 'communication' for ref in u.source_refs)]
    questions = ''.join(f'<li><strong>{text(issue)}</strong><p>{text(g.content)}</p><code>{text(g.id)}</code></li>' for issue, g in open_gaps)
    preparation = ('<h3>当前已核实的事件</h3>' + unit_list(confirmed, '尚无标记为 verified 的事件。')
        + '<h3>当前已证实的命题</h3>' + empty('以下仅复述 Workspace 判断，仍需结合支持、反对及来源陈述阅读。')
        + (verified_h or empty('尚无有效的已证实命题。'))
        + '<h3>尚有争议或已否定的命题</h3>' + (disputed_h or empty('尚无有效的未证实或已否定命题记录。'))
        + '<h3>仍待核实的材料陈述</h3>' + unit_list([u for u in units.values() if u.type in {'event', 'state'} and u.subtype != 'verified'], '尚无待核实陈述。')
        + '<h3>用户诉求与各方立场</h3>' + empty('用户身份未单独标定，以下保留各方原有立场。')
        + unit_list(positions, '尚无已记录的诉求。')
        + '<h3>已有沟通线索</h3>' + empty('以下仅按 communication 来源分类列出，是否涉及协商需结合内容确认。')
        + unit_list(communications, '尚无 communication 类来源的沟通线索。')
        + '<h3>后续沟通仍需确认</h3>' + (f'<ul class="questions">{questions}</ul>' if questions else empty('尚无已记录的未解决问题；需另行确认用户身份、诉求及沟通目标。')))

    source_panels = []
    for uid, unit in units.items():
        refs = []
        for fid in dict.fromkeys(ref.file_id for ref in unit.source_refs):
            if fid in files:
                refs.append(f'<li><a href="#{file_targets[fid]}">{text(files[fid].filename)}</a>'
                            f'<p class="meta">原始文件 ID：<code>{text(fid)}</code></p>{original_link(files[fid])}</li>')
            else:
                refs.append('<li>来源未关联：引用的文件不在当前 Case 文件目录中。</li>')
        connected = ''.join(f'<li>{badge(link.meaning, MEANINGS[link.meaning])}<p>{text(h.content)}</p>'
            f'{badge(h.judgment, JUDGMENTS[h.judgment])}<p class="meta">{text(issue)} · '
            f'Hypothesis ID：<code>{text(h.id)}</code></p></li>' for issue, link, h in links_by_unit[uid])
        kind, label = classification(unit)
        source_panels.append(f'<article id="{unit_targets[uid]}" class="source-panel" hidden><h3>{text(unit.title)}</h3>{badge(kind, label)}'
            f'<p>{text(unit.content)}</p><p class="meta">InfoUnit ID：<code>{text(uid)}</code></p>'
            f'<p class="meta">陈述者：{text(unit.speaker or "未记录")} · 立场：{text(unit.side or "未记录")}</p>'
            f'<h4>原始文件</h4><ul>{"".join(refs) or "<li>来源未关联</li>"}</ul><h4>关联命题</h4>'
            f'<ul>{connected}</ul>{empty("尚无关联命题。") if not connected else ""}</article>')

    bodies = [overview, timeline, issues_html, files_html, gaps_html, preparation]
    sections = ''.join(f'<section id="{sid}" class="chapter" aria-labelledby="heading-{sid}"><header>'
        f'<p class="eyebrow">案件材料 / {text(case.case_title)}</p><h2 id="heading-{sid}" tabindex="-1">{title}</h2></header>{body}</section>'
        for (sid, title), body in zip(SECTIONS, bodies))
    nav = ''.join(f'<a href="#{sid}">{title}</a>' for sid, title in SECTIONS)
    asset_dir = Path(__file__).with_name('templates')
    css = (asset_dir / 'case_materials.css').read_text(encoding='utf-8')
    script = (asset_dir / 'case_materials.js').read_text(encoding='utf-8')
    digest = b64encode(sha256(script.encode('utf-8')).digest()).decode('ascii')
    csp = f"default-src 'none'; style-src 'unsafe-inline'; script-src 'sha256-{digest}'; base-uri 'none'; form-action 'none'"
    return (f'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        f'<meta http-equiv="Content-Security-Policy" content="{text(csp)}">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>{text(case.case_title)} · 案件材料</title><style>{css}</style></head><body>'
        '<a class="skip-link" href="#overview">跳到正文</a><div class="reader">'
        f'<nav class="contents" aria-label="材料包目录"><p class="brand">LogicWeaver</p><h1>案件材料</h1>'
        f'<p class="case-name">{text(case.case_title)}</p>{nav}<div class="snapshot">'
        f'<p>Case ID<br><code>{text(case.case_id)}</code></p><p>生成时间<br><time datetime="{generated}">{generated}</time></p>'
        f'<p>Case 保存时间<br>{text(case.updated_at)}</p><p>覆盖全部 {len(keys)} 个 Issue</p></div></nav>'
        f'<main>{sections}</main><aside class="sources" aria-label="相关证据及文件来源">'
        '<div class="source-heading"><h2>证据与来源</h2><button type="button" id="close-sources" aria-label="收起证据与来源">×</button></div>'
        f'<p class="source-placeholder">选择「查看来源与关联」，在这里对照信息节点、原始文件和命题判断。</p>{"".join(source_panels)}</aside></div>'
        f'<script>{script}</script></body></html>')
