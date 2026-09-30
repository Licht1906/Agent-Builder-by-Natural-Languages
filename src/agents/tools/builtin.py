"""The six MVP tools, each with a real and a simulated implementation.

`http_request` is the deliberate escape hatch: it keeps a closed catalogue able to
cover almost any business process, at the price of declaring — per version — which
domains it may reach. It fails closed: no declared domains means no request.

Some real implementations depend on infrastructure that later tickets stand up
(Postgres for `db_query`, SMTP for `send_email`, a search API for `web_search`).
Those raise `ToolUnavailableError` with a clear message until configured; the code
path is real, only the credentials are missing.
"""

import ast
import operator
import smtplib
from collections.abc import Callable
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field

from src.agents.tools.registry import TOOL_REGISTRY, ToolRisk, ToolSpec
from src.config import get_settings


class ToolUnavailableError(RuntimeError):
    """A real implementation exists but its infrastructure is not configured."""


# --------------------------------------------------------------------------- #
# web_search
# --------------------------------------------------------------------------- #


class WebSearchArgs(BaseModel):
    query: str = Field(..., min_length=1, description="Câu truy vấn tìm kiếm")
    country: str = Field(default="", description="Ưu tiên kết quả từ một quốc gia, ví dụ 'vietnam'")


#: Five results and `basic` depth: one API credit per search rather than two,
#: which is what keeps a demo inside a free tier. `advanced` buys ranking
#: quality on hard research questions — not what a workflow node's single shot
#: is doing, and the snippets an answering node reads are in both.
SEARCH_MAX_RESULTS = 5
SEARCH_DEPTH = "basic"


def _format_search_results(payload: dict[str, Any], query: str) -> str:
    """Render a search response as evidence — what the pages *say*, not that they exist.

    `content` is the whole reason this is not a one-line join of titles and
    URLs. A `Finding` assembled from links alone tells the answering node that
    pages exist without telling it anything they contain, and `_grounded_prompt`
    then leaves that node to either decline or invent the part it cannot see —
    the first is a wasted search, the second is the failure this platform exists
    to prevent. The URL stays attached to the snippet it belongs to instead of
    being listed apart from it, so an answer citing a line cites the source that
    line actually came from.

    The synthesised `answer` goes first when the provider returns one, and
    *alongside* the snippets rather than instead of them: it is a claim, and
    they are the working behind it. A node that needs a figure reads the
    snippets, so dropping them would trade citable sources for one assertion.

    Raw page content is deliberately not requested. It multiplies the tokens a
    downstream node pays for on every search, and a snippet already carries the
    sentence a search was asked to find; a node that genuinely needs a full page
    should draw an `http_request` at the URL this returns.
    """
    answer = str(payload.get("answer") or "").strip()
    blocks: list[str] = [f"Tóm tắt: {answer}"] if answer else []

    for item in payload.get("results") or []:
        title = str(item.get("title") or "").strip()
        url = str(item.get("url") or "").strip()
        content = str(item.get("content") or "").strip()
        headline = " — ".join(part for part in (title, url) if part)
        if headline and content:
            blocks.append(f"- {headline}\n  {content}")
        elif headline or content:
            blocks.append(f"- {headline or content}")

    if not blocks:
        return f"Không tìm thấy kết quả nào cho: {query}"
    return "\n".join(blocks)


def web_search(query: str, country: str = "") -> str:
    """Tìm kiếm trên web qua search API đã cấu hình.

    `country` is omitted from the payload when blank rather than sent empty: an
    unset preference and a preference for nowhere are different requests, and
    only one of them is what a node with the field left alone is asking for.
    """
    settings = get_settings()
    if not settings.search_api_key:
        raise ToolUnavailableError("web_search cần SEARCH_API_KEY trong môi trường để chạy thật")
    payload: dict[str, Any] = {
        "query": query,
        "max_results": SEARCH_MAX_RESULTS,
        "search_depth": SEARCH_DEPTH,
        "include_answer": True,
    }
    if country.strip():
        payload["country"] = country.strip()
    response = httpx.post(
        settings.search_api_url,
        # A bearer header rather than an `api_key` field in the body: the body
        # form is the provider's legacy shape, and the response this parses is
        # already that provider's, so following its current contract costs
        # nothing and stops the day the legacy path is withdrawn.
        headers={"Authorization": f"Bearer {settings.search_api_key}"},
        json=payload,
        timeout=settings.tool_http_timeout_seconds,
    )
    response.raise_for_status()
    return _format_search_results(response.json(), query)


def simulate_web_search(query: str, country: str = "") -> str:
    """Mô phỏng tìm kiếm web: kể lại truy vấn thay vì gọi search API."""
    scope = f" (ưu tiên {country.strip()})" if country.strip() else ""
    return f"[SANDBOX] sẽ tìm kiếm web với truy vấn: {query}{scope}"


# --------------------------------------------------------------------------- #
# db_query — read-only
# --------------------------------------------------------------------------- #


class DbQueryArgs(BaseModel):
    sql: str = Field(..., min_length=1, description="Một câu SELECT duy nhất")


_WRITE_KEYWORDS = (
    "insert",
    "update",
    "delete",
    "drop",
    "alter",
    "create",
    "truncate",
    "grant",
    "revoke",
    "copy",
)


def _as_read_only_statement(sql: str) -> str:
    """Chuẩn hoá và kiểm tra một câu SQL, trả về câu lệnh nếu nó chỉ đọc."""
    statement = sql.strip().rstrip(";").strip()
    if ";" in statement:
        raise ValueError("db_query chỉ nhận một câu lệnh duy nhất")
    lowered = statement.lower()
    if not (lowered.startswith("select") or lowered.startswith("with")):
        raise ValueError("db_query chỉ được phép SELECT")
    if any(f" {keyword} " in f" {lowered} " for keyword in _WRITE_KEYWORDS):
        raise ValueError("db_query phát hiện từ khoá ghi dữ liệu — bị từ chối")
    return statement


def db_query(sql: str) -> str:
    """Truy vấn dữ liệu chỉ-đọc. Chỉ nhận một câu SELECT."""
    statement = _as_read_only_statement(sql)
    try:
        # Optional until ticket #3 enables SQLAlchemy in requirements.txt.
        from sqlalchemy import create_engine, text  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - depends on ticket #3
        raise ToolUnavailableError("db_query cần SQLAlchemy — bật trong requirements.txt (ticket #3)") from exc

    engine = create_engine(get_settings().database_url)
    with engine.connect() as connection:
        rows = connection.execute(text(statement)).mappings().all()
    if not rows:
        return "Truy vấn không trả về dòng nào."
    return "\n".join(str(dict(row)) for row in rows)


def simulate_db_query(sql: str) -> str:
    """Mô phỏng truy vấn chỉ-đọc: kể lại câu SQL thay vì chạm vào Postgres."""
    return f"[SANDBOX] sẽ chạy truy vấn chỉ-đọc: {sql}"


# --------------------------------------------------------------------------- #
# read_file
# --------------------------------------------------------------------------- #


class ReadFileArgs(BaseModel):
    path: str = Field(..., min_length=1, description="Đường dẫn tương đối trong workspace")


def read_file(path: str) -> str:
    """Đọc một file văn bản trong workspace đã cấu hình."""
    base = Path(get_settings().tool_file_base_dir).resolve()
    target = (base / path).resolve()
    if base != target and base not in target.parents:
        raise ValueError(f"read_file chỉ được đọc trong {base}")
    if not target.is_file():
        raise ValueError(f"Không tìm thấy file: {path}")
    return target.read_text(encoding="utf-8")


def simulate_read_file(path: str) -> str:
    """Mô phỏng đọc file: kể lại đường dẫn thay vì đọc đĩa."""
    return f"[SANDBOX] sẽ đọc file: {path}"


# --------------------------------------------------------------------------- #
# calculate
# --------------------------------------------------------------------------- #


class CalculateArgs(BaseModel):
    expression: str = Field(..., min_length=1, description='Ví dụ: "2 + 3 * 4"')


_SAFE_OPERATORS: dict[type[ast.AST], Callable[..., Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def _eval_node(node: ast.AST) -> float:
    """Evaluate an arithmetic AST node. No `eval`, no names, no calls."""
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return node.value
        raise ValueError(f"Hằng số không được hỗ trợ: {node.value!r}")
    if isinstance(node, ast.UnaryOp):
        op_func = _SAFE_OPERATORS.get(type(node.op))
        if op_func is None:
            raise ValueError(f"Toán tử không được hỗ trợ: {type(node.op).__name__}")
        return op_func(_eval_node(node.operand))
    if isinstance(node, ast.BinOp):
        op_func = _SAFE_OPERATORS.get(type(node.op))
        if op_func is None:
            raise ValueError(f"Toán tử không được hỗ trợ: {type(node.op).__name__}")
        return op_func(_eval_node(node.left), _eval_node(node.right))
    raise ValueError(f"Biểu thức không được hỗ trợ: {type(node).__name__}")


def calculate(expression: str) -> str:
    """Tính biểu thức toán học an toàn (không dùng `eval`)."""
    return str(_eval_node(ast.parse(expression, mode="eval").body))


def simulate_calculate(expression: str) -> str:
    """Mô phỏng tính toán: kể lại biểu thức thay vì tính."""
    return f"[SANDBOX] sẽ tính biểu thức: {expression}"


# --------------------------------------------------------------------------- #
# send_email — side effect
# --------------------------------------------------------------------------- #


class SendEmailArgs(BaseModel):
    to: str = Field(..., min_length=1, description="Địa chỉ người nhận")
    subject: str = Field(..., min_length=1)
    body: str = Field(..., min_length=1)


def send_email(to: str, subject: str, body: str) -> str:
    """Gửi email thật qua SMTP đã cấu hình."""
    settings = get_settings()
    if not settings.smtp_host:
        raise ToolUnavailableError("send_email cần SMTP_HOST trong môi trường để gửi thật")
    message = EmailMessage()
    message["From"] = settings.smtp_from
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as smtp:
        if settings.smtp_username:
            smtp.starttls()
            smtp.login(settings.smtp_username, settings.smtp_password)
        smtp.send_message(message)
    return f"Đã gửi email tới {to}."


def simulate_send_email(to: str, subject: str, body: str) -> str:
    """Mô phỏng gửi email: ghi vào transcript thay vì gửi thật cho khách."""
    return f"[SANDBOX] sẽ gửi email tới {to} với tiêu đề {subject!r} và nội dung: {body}"


# --------------------------------------------------------------------------- #
# http_request — side effect, domain-gated
# --------------------------------------------------------------------------- #


class HttpRequestArgs(BaseModel):
    url: str = Field(..., min_length=1)
    method: str = Field(default="GET", description="GET hoặc POST")
    body: dict[str, Any] | None = Field(default=None, description="Thân JSON cho POST")


def _require_allowed_domain(url: str, allowed_domains: tuple[str, ...] | None) -> str:
    """Fail closed: an undeclared domain — or no declaration at all — is refused."""
    host = (urlparse(url).hostname or "").lower()
    if not host:
        raise ValueError(f"URL không hợp lệ: {url!r}")
    allowed = tuple(domain.lower() for domain in (allowed_domains or ()))
    if not allowed:
        raise ValueError("http_request bị từ chối: version chưa khai domain nào được phép gọi")
    if host not in allowed:
        raise ValueError(f"http_request bị từ chối: domain {host!r} không nằm trong danh sách đã khai {list(allowed)}")
    return host


def http_request(
    args: HttpRequestArgs,
    allowed_domains: tuple[str, ...] | None = None,
) -> str:
    """Gọi HTTP tới một domain đã khai trong version.

    `args` là phần LLM chọn; `allowed_domains` do runtime truyền vào từ version.
    Hai thứ này tách nhau có chủ ý — LLM không được tự nới danh sách domain.
    Không đi theo redirect: một redirect sẽ đưa request ra ngoài domain đã duyệt.
    """
    _require_allowed_domain(args.url, allowed_domains)
    response = httpx.request(
        args.method.upper(),
        args.url,
        json=args.body,
        timeout=get_settings().tool_http_timeout_seconds,
        follow_redirects=False,
    )
    response.raise_for_status()
    return response.text


def simulate_http_request(
    args: HttpRequestArgs,
    allowed_domains: tuple[str, ...] | None = None,
) -> str:
    """Mô phỏng gọi HTTP: vẫn kiểm tra domain, nhưng không phát request thật."""
    _require_allowed_domain(args.url, allowed_domains)
    return f"[SANDBOX] sẽ gọi {args.method.upper()} tới {args.url} với thân: {args.body!r}"


# --------------------------------------------------------------------------- #
# Registration — the closed catalogue
# --------------------------------------------------------------------------- #

TOOL_REGISTRY.register(
    ToolSpec(
        name="web_search",
        description="Tìm kiếm thông tin công khai trên web.",
        args_schema=WebSearchArgs,
        risk=ToolRisk.LOW,
        has_side_effects=False,
        run=web_search,
        simulate=simulate_web_search,
    )
)

TOOL_REGISTRY.register(
    ToolSpec(
        name="db_query",
        description="Truy vấn dữ liệu nội bộ, chỉ đọc (một câu SELECT).",
        args_schema=DbQueryArgs,
        risk=ToolRisk.LOW,
        has_side_effects=False,
        run=db_query,
        simulate=simulate_db_query,
    )
)

TOOL_REGISTRY.register(
    ToolSpec(
        name="read_file",
        description="Đọc một file văn bản trong workspace.",
        args_schema=ReadFileArgs,
        risk=ToolRisk.LOW,
        has_side_effects=False,
        run=read_file,
        simulate=simulate_read_file,
    )
)

TOOL_REGISTRY.register(
    ToolSpec(
        name="calculate",
        description="Tính toán biểu thức số học.",
        args_schema=CalculateArgs,
        risk=ToolRisk.LOW,
        has_side_effects=False,
        run=calculate,
        simulate=simulate_calculate,
    )
)

TOOL_REGISTRY.register(
    ToolSpec(
        name="send_email",
        description="Gửi email tới một địa chỉ.",
        args_schema=SendEmailArgs,
        risk=ToolRisk.HIGH,
        has_side_effects=True,
        run=send_email,
        simulate=simulate_send_email,
    )
)

TOOL_REGISTRY.register(
    ToolSpec(
        name="http_request",
        description="Gọi HTTP tới một domain đã được khai và duyệt cho version này.",
        args_schema=HttpRequestArgs,
        risk=ToolRisk.HIGH,
        has_side_effects=True,
        requires_declared_domains=True,
        run=http_request,
        simulate=simulate_http_request,
    )
)
