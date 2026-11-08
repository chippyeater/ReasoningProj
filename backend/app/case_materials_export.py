"""Original-file access and ZIP assembly; no parsed caches or business writes."""
from io import BytesIO
import json
from pathlib import Path, PurePosixPath
import re
from urllib.parse import quote
from zipfile import ZIP_DEFLATED, ZipFile

from app.case_materials import render_case_materials
from app.case_store import artifact_path
from app.errors import AppError
from app.issue_workspaces import IssueWorkspaces
from app.schemas import CaseData, CaseFile

_DEVICE = re.compile(r'^(CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³]) *(?:\.|$)', re.I)
# Only inert, browser-readable formats are served inline. Active HTML/SVG and
# unknown formats are attachments, never executable documents on the API origin.
_INLINE_TYPES = {
    '.pdf': 'application/pdf', '.txt': 'text/plain', '.md': 'text/plain', '.markdown': 'text/plain',
    '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.gif': 'image/gif',
    '.webp': 'image/webp', '.bmp': 'image/bmp', '.avif': 'image/avif',
    '.mp3': 'audio/mpeg', '.wav': 'audio/wav', '.ogg': 'audio/ogg',
    '.mp4': 'video/mp4', '.webm': 'video/webm',
}


def validate_file_id(file_id: str) -> None:
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', file_id) or _DEVICE.match(file_id):
        raise AppError('invalid_file_id', '原始文件 ID 无效。')


def safe_original_filename(filename: str) -> str:
    """One portable path component; keep the exact original name in manifest."""
    name = re.sub(r'[\x00-\x1f\x7f<>:"/\\|?*]', '_', filename).strip(' .') or 'original-file'
    if _DEVICE.match(name):
        name = '_' + name
    # Bound the UTF-8 component length as well as Windows' character limit.
    suffix = PurePosixPath(name).suffix
    if len(suffix.encode('utf-8')) > 32:
        suffix = ''
    stem = name[:-len(suffix)] if suffix else name
    stem = stem.encode('utf-8')[:200 - len(suffix.encode('utf-8'))].decode('utf-8', errors='ignore')
    return (stem + suffix).rstrip(' .') or 'original-file'


def original_file(case: CaseData, file_id: str) -> tuple[CaseFile, Path]:
    validate_file_id(file_id)
    file = next((item for item in case.files if item.file_id == file_id), None)
    if file is None:
        raise AppError('unknown_file_id', '当前案件中找不到该原始文件 ID。', 404)
    path = artifact_path(case.case_id, file.original_path)
    if not path.is_file():
        raise AppError('original_file_missing', f'原始文件缺失：{file_id}', 409)
    return file, path


def original_response_type(file: CaseFile) -> tuple[str, str]:
    media_type = _INLINE_TYPES.get(PurePosixPath(file.filename).suffix.lower())
    return (media_type, 'inline') if media_type else ('application/octet-stream', 'attachment')


def build_case_materials_zip(case: CaseData, workspaces: IssueWorkspaces) -> bytes:
    """Caller holds the existing Workspace lock through snapshot and assembly.

    Explicit allowlist: index, manifest and CaseFile originals only. Paths never
    come from request parameters and an incomplete archive is never returned.
    """
    sources = []
    directories = set()
    for file in case.files:
        _, path = original_file(case, file.file_id)
        if file.file_id.casefold() in directories:
            raise AppError('unsafe_zip_path', '原始文件 ID 在大小写不敏感系统中存在目录冲突。', 409)
        directories.add(file.file_id.casefold())
        relative = f'sources/{file.file_id}/{safe_original_filename(file.filename)}'
        sources.append((file, path, relative))
    relative_paths = {file.file_id: relative for file, _, relative in sources}
    html = render_case_materials(case, workspaces,
        source_url=lambda file: quote(relative_paths[file.file_id], safe='/'))
    manifest = {
        'case_id': case.case_id, 'case_title': case.case_title,
        'case_created_at': case.created_at, 'case_updated_at': case.updated_at,
        'files': [{'file_id': file.file_id, 'original_filename': file.filename,
                   'zip_path': relative, 'file_type': file.file_type.value,
                   'source_kind': file.source_kind.value, 'parse_status': file.parse_status.value}
                  for file, _, relative in sources],
    }
    buffer = BytesIO()
    with ZipFile(buffer, 'w', compression=ZIP_DEFLATED) as archive:
        archive.writestr('index.html', html.encode('utf-8'))
        archive.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2).encode('utf-8'))
        for file, path, relative in sources:
            try:
                archive.write(path, arcname=relative)
            except FileNotFoundError as exc:
                raise AppError('original_file_missing', f'原始文件缺失：{file.file_id}', 409) from exc
    return buffer.getvalue()
