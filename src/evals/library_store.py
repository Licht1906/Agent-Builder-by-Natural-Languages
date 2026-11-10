"""Reading and editing the template library from the admin screen — ADR-0093.

`src.evals.library` reads the library to build suites, and refuses one that does
not validate. This module is the other side: the admin's view of every line,
with the file it sits in, and the three edits (add, replace, delete) that keep
the files the source of truth.

**The files stay the library.** An edit rewrites one `eval/library/*.jsonl` file
in place — every other line byte for byte — so what the admin changed is a
`git diff` someone can review and commit, as ADR-0069 §1 wants. Nothing here is
a second copy in the database.

**Nothing invalid is written.** A template is checked the way `load_library`
checks it (shape, slots, a rule that grades once filled) and against every other
id before its file is touched; a refused edit leaves the file as it was.

**Ids do not change.** A filled case remembers `extra.template_id` and the suite
builder dedups on it (`src.evals.suite_builder`), so a renamed template would
land a second time in every suite next built. Replace keeps the id; a new id is
a new template.
"""

import json
import os
import tempfile
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from src.evals.library import LIBRARY_DIR, LibraryTemplate, Problem, template_problems, validate_text

#: Where a new template goes when the admin does not pick a file: its first tag's theme.
FILE_BY_TAG: Mapping[str, str] = {
    "injection": "injection.jsonl",
    "leak": "leak.jsonl",
    "cross_tenant": "cross_tenant.jsonl",
    "out_of_scope_refusal": "out_of_scope.jsonl",
    "asks_back_missing_param": "asks_back.jsonl",
    "approval_required": "approval.jsonl",
    "prohibition": "prohibition.jsonl",
    "knowledge_grounding": "knowledge.jsonl",
}

#: One writer at a time: two admins saving at once would each rewrite the file from what they read.
_WRITE_LOCK = threading.Lock()


class LibraryEditError(Exception):
    """An edit the library refuses — the reason is shown to the admin as is."""


class TemplateNotFoundError(LibraryEditError):
    pass


@dataclass(frozen=True, slots=True)
class StoredTemplate:
    file: str
    template: LibraryTemplate


@dataclass(frozen=True, slots=True)
class LibraryListing:
    templates: list[StoredTemplate]
    #: Lines that are not templates. Normally empty — the tests and `load_library` keep it so.
    problems: list[Problem]
    files: list[str]


def read_library(directory: Path = LIBRARY_DIR) -> LibraryListing:
    """Every template with the file it sits in, and every line that is not one — never raises on a bad line."""
    templates: list[StoredTemplate] = []
    problems: list[Problem] = []
    files: list[str] = []
    for path in sorted(directory.glob("*.jsonl")):
        files.append(path.name)
        found, wrong = validate_text(path.read_text(encoding="utf-8"), source=path.name)
        templates.extend(StoredTemplate(path.name, template) for template in found)
        problems.extend(wrong)
    return LibraryListing(templates, problems, files)


def checked(raw: Mapping[str, Any]) -> LibraryTemplate:
    """`raw` as a template, or `LibraryEditError` with every reason it is not one."""
    try:
        template = LibraryTemplate.model_validate(dict(raw))
    except ValidationError as exc:
        reasons = [
            f"{'.'.join(str(part) for part in error['loc']) or 'template'}: {error['msg']}" for error in exc.errors()
        ]
        raise LibraryEditError("; ".join(reasons)) from None
    problems = template_problems(template)
    if problems:
        raise LibraryEditError("; ".join(problems))
    return template


def add_template(raw: Mapping[str, Any], *, file: str | None = None, directory: Path = LIBRARY_DIR) -> StoredTemplate:
    template = checked(raw)
    target = _file_name(file or FILE_BY_TAG.get(template.tags[0], f"{template.tags[0]}.jsonl"))
    with _WRITE_LOCK:
        if template.id in _ids(directory):
            raise LibraryEditError(f"id '{template.id}' đã có trong thư viện")
        path = directory / target
        lines = _lines(path)
        lines.append(_line(template))
        _write(path, lines)
    return StoredTemplate(target, template)


def replace_template(template_id: str, raw: Mapping[str, Any], *, directory: Path = LIBRARY_DIR) -> StoredTemplate:
    template = checked(raw)
    if template.id != template_id:
        raise LibraryEditError(
            "không đổi id được — case đã dựng nhớ id này; muốn đổi thì thêm template mới rồi xoá template cũ"
        )
    with _WRITE_LOCK:
        path, number = _locate(template_id, directory)
        lines = _lines(path)
        lines[number] = _line(template)
        _write(path, lines)
    return StoredTemplate(path.name, template)


def delete_template(template_id: str, *, directory: Path = LIBRARY_DIR) -> None:
    with _WRITE_LOCK:
        path, number = _locate(template_id, directory)
        lines = _lines(path)
        del lines[number]
        _write(path, lines)


def _locate(template_id: str, directory: Path) -> tuple[Path, int]:
    """The file and the 0-based line index holding `template_id`."""
    for path in sorted(directory.glob("*.jsonl")):
        for number, line in enumerate(_lines(path)):
            if not line.strip():
                continue
            try:
                found = json.loads(line).get("id")
            except (json.JSONDecodeError, AttributeError):
                continue
            if found == template_id:
                return path, number
    raise TemplateNotFoundError(f"không có template '{template_id}'")


def _ids(directory: Path) -> set[str]:
    ids: set[str] = set()
    for path in directory.glob("*.jsonl"):
        for line in _lines(path):
            try:
                found = json.loads(line).get("id") if line.strip() else None
            except (json.JSONDecodeError, AttributeError):
                continue
            if isinstance(found, str):
                ids.add(found)
    return ids


def _file_name(name: str) -> str:
    """A bare `*.jsonl` name inside the library — never a path out of it, never into `drafts/`."""
    if Path(name).name != name or not name.endswith(".jsonl") or name.startswith("."):
        raise LibraryEditError(f"tên file không hợp lệ: {name!r} (chỉ tên *.jsonl, không có thư mục)")
    return name


def _line(template: LibraryTemplate) -> str:
    """A template written the way the reviewed files are: field order, no nulls, Vietnamese as is."""
    return json.dumps(template.model_dump(mode="json", exclude_none=True), ensure_ascii=False)


def _lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def _write(path: Path, lines: list[str]) -> None:
    """Replace `path` whole, so a reader never sees half a file."""
    text = "".join(f"{line}\n" for line in lines)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as out:
            out.write(text)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
