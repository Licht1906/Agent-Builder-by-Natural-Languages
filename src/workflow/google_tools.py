"""The real Gmail and Google Drive calls behind the two built-in connections.

`src.workflow.tools` declares *what* the `gmail` and `google_drive` connections
can do; this module is *how*, over Google's REST APIs, with the agent owner's
own OAuth token (`src.workflow.user_connections`). Split out of `tools.py` so
the declaration module keeps its place in the import graph — this one reaches
into the database for `agents.owner_id` and must therefore only ever be
imported lazily, from inside an invoker.

Every public entry point takes `agent_id`, not a token: whose account a run
borrows is this module's decision to make *consistently* (the agent owner's,
per the platform's settled auth direction), not something each call site
re-derives. Results come back as JSON text because the one consumer is the
workflow runtime, which hands them to the next node's model — a model reads
text.

Output is capped. A mailbox search that returned 400 messages whole would spend
the run's entire token budget on step one; caps here are the difference between
"the agent read your inbox" and "the agent drowned in it".
"""

import base64
import json
import re
from collections.abc import Callable, Mapping
from email.message import EmailMessage
from typing import Any

import httpx
from sqlalchemy import select

from src.config import get_settings
from src.db.base import SessionLocal
from src.db.models import Agent
from src.workflow.faults import TOKEN_REFUSED_MESSAGE, PlatformFault
from src.workflow.user_connections import UserConnectionError, google_access_token, mark_needs_reconnect

GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"
DRIVE_API = "https://www.googleapis.com/drive/v3"
DRIVE_UPLOAD_API = "https://www.googleapis.com/upload/drive/v3/files"

#: How many list entries an action returns when the caller does not say.
DEFAULT_RESULTS = 10
#: And the most it may ask for — the cap that keeps one step from eating the run.
MAX_RESULTS = 25
#: The most characters `read_email` / `fetch` hand to the model, with a note
#: when the document went on — a truncated document the model *knows* is
#: truncated beats a complete one that blew the budget.
MAX_TEXT_CHARS = 50_000

#: Google Docs formats `fetch` can export as text, by mimeType.
_EXPORTABLE = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
}
#: Plain formats `fetch` downloads as they are.
_PLAIN_PREFIXES = ("text/",)
_PLAIN_EXACT = {"application/json", "application/xml"}


class GoogleToolError(RuntimeError):
    """A Gmail/Drive action that did not happen, said so a person can act on it."""


def _owner_of(agent_id: int) -> int:
    """Whose credential this run borrows: the agent's owner."""
    with SessionLocal() as session:
        owner_id = session.execute(select(Agent.owner_id).where(Agent.id == agent_id)).scalar_one_or_none()
    if owner_id is None:
        raise GoogleToolError(f"Không tìm thấy agent id {agent_id} nên không biết dùng tài khoản Google của ai")
    return owner_id


def _authorized(owner_id: int) -> dict[str, str]:
    try:
        token = google_access_token(owner_id)
    except UserConnectionError as exc:
        raise GoogleToolError(str(exc)) from exc
    return {"Authorization": f"Bearer {token}"}


class _TokenRefusedError(GoogleToolError):
    """Google answered 401: the token the run carried is not accepted any more.

    A platform fault (ADR-0061), and the only Google refusal that is one. A 403
    is deliberately **not**: Google returns it both for a scope this platform
    never asked for and for a single file the owner may not read, and a rule
    that cannot tell those apart would excuse a real failure half the time.
    """

    platform_fault = PlatformFault.CREDENTIAL


def _as_owner(agent_id: int, call: Callable[[Mapping[str, str]], str]) -> str:
    """Run `call` with the owner's headers; on a 401, record that on their connection.

    The 401 used to be reported on the run and nowhere else. The row stayed
    `CONNECTED`, both screens kept saying "đang nối", and the next run — or a
    new agent of the same person — hit the same wall with the same sentence.
    Now the row flips to `NEEDS_RECONNECT`, which is what the Connections
    screen and the MCP panel read, and the sentence names the way out.
    """
    owner_id = _owner_of(agent_id)
    try:
        return call(_authorized(owner_id))
    except _TokenRefusedError as exc:
        mark_needs_reconnect(owner_id)
        raise GoogleToolError(str(exc)) from exc


def _call(
    method: str,
    url: str,
    *,
    headers: Mapping[str, str],
    params: Mapping[str, Any] | None = None,
    json_body: Mapping[str, Any] | None = None,
    content: bytes | None = None,
    extra_headers: Mapping[str, str] | None = None,
) -> httpx.Response:
    merged = {**headers, **(extra_headers or {})}
    try:
        with httpx.Client(timeout=get_settings().tool_http_timeout_seconds) as client:
            reply = client.request(method, url, headers=merged, params=params, json=json_body, content=content)
    except httpx.HTTPError as exc:
        raise GoogleToolError(f"Không gọi được Google API: {exc}") from exc
    if reply.status_code == 401:
        raise _TokenRefusedError(_reason_of(reply))
    if reply.status_code >= 400:
        raise GoogleToolError(_reason_of(reply))
    return reply


def _reason_of(reply: httpx.Response) -> str:
    """Google's refusal, translated into the sentence the run should carry."""
    detail = ""
    try:
        body = reply.json()
        if isinstance(body, dict):
            error = body.get("error")
            if isinstance(error, dict):
                detail = str(error.get("message", ""))
    except ValueError:
        pass
    if reply.status_code == 401:
        return TOKEN_REFUSED_MESSAGE
    if reply.status_code == 403:
        return f"Tài khoản Google đã nối không có quyền cho việc này — kết nối lại để cấp thêm quyền. ({detail})"
    if reply.status_code == 404:
        return f"Google nói không tồn tại: {detail or 'không tìm thấy tài nguyên được yêu cầu'}"
    if reply.status_code == 429:
        return "Google đang giới hạn tần suất — thử lại sau ít phút"
    return f"Google API trả về HTTP {reply.status_code}: {detail or reply.text[:200]}"


def _as_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False)


def _capped_int(value: Any, fallback: int = DEFAULT_RESULTS) -> int:
    try:
        wanted = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(1, min(wanted, MAX_RESULTS))


def _capped_text(text: str) -> str:
    if len(text) <= MAX_TEXT_CHARS:
        return text
    return text[:MAX_TEXT_CHARS] + f"\n\n[… đã cắt bớt: văn bản gốc dài {len(text)} ký tự]"


# --------------------------------------------------------------------------- #
# Gmail
# --------------------------------------------------------------------------- #


def gmail_call(action: str, args: Mapping[str, Any], *, agent_id: int) -> str:
    """One `gmail` action, for real, as the agent owner."""
    return _as_owner(agent_id, lambda headers: _gmail(action, args, headers))


def _gmail(action: str, args: Mapping[str, Any], headers: Mapping[str, str]) -> str:
    if action == "get_profile":
        reply = _call("GET", f"{GMAIL_API}/profile", headers=headers)
        return _as_json(reply.json())
    if action in {"search_emails", "get_recent_emails"}:
        params: dict[str, Any] = {"maxResults": _capped_int(args.get("max_results"))}
        if action == "search_emails":
            params["q"] = str(args.get("query", "")).strip()
        return _as_json(_email_summaries(headers, params))
    if action == "read_email":
        return _as_json(_email_full(headers, str(args.get("message_id", "")).strip()))
    if action == "create_draft":
        raw = _rfc822(args)
        reply = _call("POST", f"{GMAIL_API}/drafts", headers=headers, json_body={"message": {"raw": raw}})
        body = reply.json()
        return _as_json({"draft_id": body.get("id"), "note": "Bản nháp đã lưu trong Gmail, chưa gửi đi đâu"})
    if action == "send_email":
        raw = _rfc822(args)
        reply = _call("POST", f"{GMAIL_API}/messages/send", headers=headers, json_body={"raw": raw})
        body = reply.json()
        return _as_json({"message_id": body.get("id"), "sent": True})
    raise GoogleToolError(f"Kết nối gmail không có action {action!r}")


def _email_summaries(headers: Mapping[str, str], params: Mapping[str, Any]) -> dict[str, Any]:
    listed = _call("GET", f"{GMAIL_API}/messages", headers=headers, params=params).json()
    found = listed.get("messages") or []
    summaries = []
    for entry in found:
        message = _call(
            "GET",
            f"{GMAIL_API}/messages/{entry['id']}",
            headers=headers,
            params={"format": "metadata", "metadataHeaders": ["From", "To", "Subject", "Date"]},
        ).json()
        wanted = {header["name"]: header["value"] for header in message.get("payload", {}).get("headers", [])}
        summaries.append(
            {
                "message_id": message.get("id"),
                "from": wanted.get("From", ""),
                "subject": wanted.get("Subject", ""),
                "date": wanted.get("Date", ""),
                "snippet": message.get("snippet", ""),
            }
        )
    return {"messages": summaries, "estimate_total": listed.get("resultSizeEstimate")}


def _email_full(headers: Mapping[str, str], message_id: str) -> dict[str, Any]:
    if not message_id:
        raise GoogleToolError("read_email cần message_id — lấy từ kết quả search_emails/get_recent_emails")
    message = _call("GET", f"{GMAIL_API}/messages/{message_id}", headers=headers, params={"format": "full"}).json()
    wanted = {header["name"]: header["value"] for header in message.get("payload", {}).get("headers", [])}
    return {
        "message_id": message.get("id"),
        "from": wanted.get("From", ""),
        "to": wanted.get("To", ""),
        "subject": wanted.get("Subject", ""),
        "date": wanted.get("Date", ""),
        "body": _capped_text(_body_text(message.get("payload", {})) or message.get("snippet", "")),
    }


def _body_text(payload: Mapping[str, Any]) -> str:
    """The first `text/plain` part, walking multiparts in order — how a person's
    mail client picks what to show, minus the HTML fallback a model has no use for."""
    if payload.get("mimeType", "").startswith("text/plain"):
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data + "===").decode("utf-8", errors="replace")
    for part in payload.get("parts", []) or []:
        text = _body_text(part)
        if text:
            return text
    return ""


#: Markdown emphasis, as a model writes it into prose it believes will be
#: rendered: `**dam**`, `*nghieng*`, `` `ma` ``, and `#` at the head of a line.
#: `_gach_duoi_` is left alone on purpose — see `_plain_text`.
_EMPHASIS = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*|\*(?=\S)([^*\n]+?)(?<=\S)\*|`([^`\n]+)`")
_HEADING = re.compile(r"^#{1,6}[ \t]+", re.MULTILINE)


def _plain_text(body: str) -> str:
    """Strip Markdown emphasis from a body that is about to be sent as `text/plain`.

    `EmailMessage.set_content` writes exactly what it is given, and Gmail
    delivers it as plain text — so a model that wrote `- **Thời gian:** 19h`
    because it assumed a renderer produced an email in which the recipient
    reads the asterisks. Nobody on either end wanted them there: the creator
    never typed them, the model meant them as formatting, and the person who
    got the invitation sees punctuation where emphasis was intended.

    Only the markers come off; the words, the line breaks and the `- ` bullets
    stay, because all three read correctly as plain text already.

    `_gạch_dưới_` is deliberately not treated as emphasis. Underscores turn up
    in ordinary content — file names, identifiers, addresses — and a rule that
    ate them would quietly corrupt a body that had nothing wrong with it.

    Not an HTML alternative part instead, which would keep the formatting: that
    is a different email and a bigger decision than fixing the one being sent.
    """
    without_headings = _HEADING.sub("", body)
    return _EMPHASIS.sub(lambda found: found.group(1) or found.group(2) or found.group(3), without_headings)


def _rfc822(args: Mapping[str, Any]) -> str:
    message = email_message(args)
    return base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")


def email_message(args: Mapping[str, Any]) -> EmailMessage:
    """`send_email`/`create_draft`'s `{to, subject, body}` as one plain-text message — no `From`.

    Shared with the exported package's mail channel (DS-16), so a mail sent
    over SMTP there reads exactly as one sent through Gmail here.
    """
    to = str(args.get("to", "")).strip()
    subject = str(args.get("subject", "")).strip()
    body = _plain_text(str(args.get("body", "")))
    if not to:
        raise GoogleToolError("Thiếu người nhận (to) — không soạn được email")
    message = EmailMessage()
    message["To"] = to
    message["Subject"] = _plain_text(subject).strip()
    message.set_content(body)
    return message


# --------------------------------------------------------------------------- #
# Google Drive
# --------------------------------------------------------------------------- #

_FILE_FIELDS = "id,name,mimeType,modifiedTime,webViewLink,size"


def drive_call(action: str, args: Mapping[str, Any], *, agent_id: int) -> str:
    """One `google_drive` action, for real, as the agent owner."""
    return _as_owner(agent_id, lambda headers: _drive(action, args, headers))


def _drive(action: str, args: Mapping[str, Any], headers: Mapping[str, str]) -> str:
    if action == "get_profile":
        reply = _call("GET", f"{DRIVE_API}/about", headers=headers, params={"fields": "user"})
        return _as_json(reply.json())
    if action == "search":
        needle = str(args.get("query", "")).strip().replace("\\", "\\\\").replace("'", "\\'")
        if not needle:
            raise GoogleToolError("search cần query — từ khoá muốn tìm trong Drive")
        return _as_json(
            _files(
                headers,
                {
                    "q": f"(fullText contains '{needle}' or name contains '{needle}') and trashed = false",
                    "pageSize": _capped_int(args.get("max_results")),
                    "fields": f"files({_FILE_FIELDS})",
                },
            )
        )
    if action == "recent_documents":
        return _as_json(
            _files(
                headers,
                {
                    "q": "trashed = false",
                    "orderBy": "modifiedTime desc",
                    "pageSize": _capped_int(args.get("max_results")),
                    "fields": f"files({_FILE_FIELDS})",
                },
            )
        )
    if action == "fetch":
        return _as_json(_fetch(headers, str(args.get("file_id", "")).strip()))
    if action == "create_doc":
        return _as_json(_create_doc(headers, args))
    raise GoogleToolError(f"Kết nối google_drive không có action {action!r}")


def _files(headers: Mapping[str, str], params: Mapping[str, Any]) -> dict[str, Any]:
    reply = _call("GET", f"{DRIVE_API}/files", headers=headers, params=params).json()
    return {"files": reply.get("files", [])}


def _fetch(headers: Mapping[str, str], file_id: str) -> dict[str, Any]:
    if not file_id:
        raise GoogleToolError("fetch cần file_id — lấy từ kết quả search/recent_documents")
    meta = _call(
        "GET", f"{DRIVE_API}/files/{file_id}", headers=headers, params={"fields": "id,name,mimeType,size"}
    ).json()
    mime = str(meta.get("mimeType", ""))
    if mime in _EXPORTABLE:
        reply = _call(
            "GET", f"{DRIVE_API}/files/{file_id}/export", headers=headers, params={"mimeType": _EXPORTABLE[mime]}
        )
        text = reply.text
    elif mime.startswith(_PLAIN_PREFIXES) or mime in _PLAIN_EXACT:
        reply = _call("GET", f"{DRIVE_API}/files/{file_id}", headers=headers, params={"alt": "media"})
        text = reply.text
    else:
        raise GoogleToolError(
            f"Chưa đọc được nội dung định dạng {mime!r} — mở file bằng Google Docs/Sheets rồi thử lại, "
            "hoặc dùng bản đã chuyển sang Google Docs"
        )
    return {"file_id": meta.get("id"), "name": meta.get("name"), "mime_type": mime, "content": _capped_text(text)}


def _create_doc(headers: Mapping[str, str], args: Mapping[str, Any]) -> dict[str, Any]:
    title = str(args.get("title", "")).strip()
    content = str(args.get("content", ""))
    if not title:
        raise GoogleToolError("create_doc cần title — tên tài liệu sẽ tạo")
    boundary = "agent-builder-doc-boundary"
    metadata = _as_json({"name": title, "mimeType": "application/vnd.google-apps.document"})
    # Multipart by hand, `uploadType=multipart`'s own shape: metadata part, then
    # the body as text/markdown — Drive converts markdown into a real Google Doc,
    # headings and tables included, which is what a person expects to open.
    payload = (
        f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{metadata}\r\n"
        f"--{boundary}\r\nContent-Type: text/markdown; charset=UTF-8\r\n\r\n{content}\r\n"
        f"--{boundary}--"
    ).encode()
    reply = _call(
        "POST",
        DRIVE_UPLOAD_API,
        headers=headers,
        params={"uploadType": "multipart", "fields": "id,name,webViewLink"},
        content=payload,
        extra_headers={"Content-Type": f"multipart/related; boundary={boundary}"},
    ).json()
    return {"file_id": reply.get("id"), "name": reply.get("name"), "link": reply.get("webViewLink")}
