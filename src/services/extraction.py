"""Document extraction into structured, page-anchored text (RAG-06 / ADR-0044 §5).

`pypdf` already refuses a scanned PDF by name (`src.workflow.knowledge._pdf_text`:
*"có thể là bản scan, cần OCR trước"*) — this module is the answer to that
refusal, for the ingest-time pipeline ADR-0044 moves extraction into. It reads
a document into an `ExtractionResult`: a flat list of `ExtractedBlock`s, each
carrying the page it came from and how much to trust it, so the chunker
(RAG-07) has structure to cut on instead of loose prose, and citations
(RAG-11) have a page number to point at.

**Two paths, chosen by whether a real text layer exists — never by file
extension alone for PDFs.** `.md`/`.txt`/`.docx` and a PDF where every page's
text layer is non-empty are read directly, the same way
`src.workflow.knowledge` reads them today: no model call, `extraction_confidence
= "high"` because there is nothing probabilistic about reading text that is
already text. A PDF with even one page lacking a text layer — the scan/photo
case — goes through the vision model, page image by page image, because a
locally-hosted 4B/Q4 VLM was tried and rejected (ADR-0044 §5): not accurate
enough on Vietnamese scans to be worth indexing, and a bad extraction poisons
every answer downstream while looking exactly like a good one. That is also
why a document whose VLM output comes back mostly `"low"` confidence is
flagged rather than silently indexed — see `ExtractionResult.flagged_for_review`.

**Cached by the file's own SHA-256.** Re-uploading an identical file must not
be charged twice (ADR-0044 §5) — checked before either path runs, so a cache
hit costs nothing, not even a hash of intent to call the VLM.
"""

import base64
import hashlib
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Literal, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from src.config import get_settings

logger = logging.getLogger(__name__)

Confidence = Literal["high", "medium", "low"]

#: The ticket's own bar: past this fraction of low-confidence blocks, a
#: document is not trustworthy enough to auto-index.
LOW_CONFIDENCE_FLAG_THRESHOLD = 0.3

_MAX_VLM_ATTEMPTS = 3
_RETRY_BACKOFF_SECONDS = 1.0


class ExtractionError(RuntimeError):
    """Extraction could not produce a usable structure at all — a failure, not a low-confidence result.

    `usage` is what OpenRouter had already billed *this page* before the
    failure — non-zero when a malformed-reply retry burned real tokens ahead
    of the attempt that finally gave up. `extract_document`'s cost path reads
    it so a page that fails after paying for two bad replies is not billed as
    if it were free (RAG-15 / #101).
    """

    def __init__(self, message: str, *, usage: "VlmPageUsage | None" = None) -> None:
        super().__init__(message)
        self.usage = usage if usage is not None else VlmPageUsage()


@dataclass(frozen=True, slots=True)
class VlmPageUsage:
    """What one OpenRouter call for one page billed, summed across every attempt it took.

    A malformed-JSON retry still generated tokens and OpenRouter still billed
    for them — only a 429/5xx/timeout attempt (no response, or a response with
    nothing on it) contributes zero. Summing across attempts is what makes the
    figure this feeds into (`VlmSpend`, RAG-15 / #101) match what the account
    was actually charged, rather than only the winning attempt.
    """

    input_tokens: int = 0
    output_tokens: int = 0

    def __add__(self, other: "VlmPageUsage") -> "VlmPageUsage":
        return VlmPageUsage(self.input_tokens + other.input_tokens, self.output_tokens + other.output_tokens)


@dataclass(frozen=True, slots=True)
class VlmSpend:
    """What one fresh (non-cached) document extraction cost, aggregated over every page it sent to the VLM.

    "Fresh" is the operative word — see `extract_document`'s `on_vlm_spend`
    parameter. A cache hit never constructs one of these, which is the whole
    mechanism behind RAG-15's acceptance line: *"upload trùng file → không phát
    sinh dòng mới"*.
    """

    model: str
    input_tokens: int
    output_tokens: int
    page_count: int


class ExtractedBlock(BaseModel):
    """One structural unit — a heading or a paragraph of body text — and where it came from."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["heading", "text"]
    text: str
    #: `None` for formats with no page concept (`.md`, `.txt`, `.docx`); a
    #: 1-based page number for anything that came from a PDF.
    page_number: int | None
    extraction_confidence: Confidence


class ExtractionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    blocks: tuple[ExtractedBlock, ...]
    source: Literal["text_layer", "vlm"]

    @property
    def low_confidence_ratio(self) -> float:
        if not self.blocks:
            return 0.0
        low = sum(1 for block in self.blocks if block.extraction_confidence == "low")
        return low / len(self.blocks)

    @property
    def flagged_for_review(self) -> bool:
        """True when this document must not be auto-indexed — see module docstring."""
        return self.low_confidence_ratio > LOW_CONFIDENCE_FLAG_THRESHOLD


# --- cache: one JSON blob per file content hash --------------------------- #


class ExtractionCache(Protocol):
    def get(self, digest: str) -> ExtractionResult | None: ...
    def set(self, digest: str, result: ExtractionResult) -> None: ...


@dataclass(frozen=True, slots=True)
class FileExtractionCache:
    """One file per SHA-256 digest under `cache_dir`.

    A digest, not a filename or a Document id, is the key — deliberately: two
    creators uploading byte-identical files (a shared handbook, a form
    template) share the one extraction rather than paying for it twice, and a
    creator re-uploading the *same* file under a new name still hits cache.
    """

    cache_dir: Path

    def _path_for(self, digest: str) -> Path:
        return self.cache_dir / f"{digest}.json"

    def get(self, digest: str) -> ExtractionResult | None:
        path = self._path_for(digest)
        if not path.is_file():
            return None
        return ExtractionResult.model_validate_json(path.read_text(encoding="utf-8"))

    def set(self, digest: str, result: ExtractionResult) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._path_for(digest).write_text(result.model_dump_json(), encoding="utf-8")


def get_extraction_cache() -> ExtractionCache:
    return FileExtractionCache(cache_dir=Path(get_settings().extraction_cache_dir))


# --- the VLM seam: one page image in, validated blocks out ---------------- #

#: (page image bytes, 1-based page number) -> the blocks the model read off it.
VlmPageClient = Callable[[bytes, int], list[ExtractedBlock]]

_SCHEMA_PROMPT = """Bạn đọc một trang tài liệu tiếng Việt từ ảnh và trả về CHỈ MỘT đối tượng JSON, \
không kèm giải thích, đúng schema sau:

{"blocks": [{"kind": "heading" | "text", "text": "...", "extraction_confidence": "high" | "medium" | "low"}]}

- "heading": một tiêu đề/đề mục (chương, điều, khoản, mục, tiêu đề phần...).
- "text": một đoạn văn bản thân bài.
- "extraction_confidence": "high" nếu chữ rõ ràng và bạn chắc chắn đọc đúng; \
"medium" nếu có vài chữ không chắc; "low" nếu mờ, viết tay khó đọc, hoặc bạn \
phải đoán phần lớn nội dung.
- Giữ đúng thứ tự đọc tự nhiên của trang (trên xuống dưới).
- Không thêm trường nào ngoài schema trên."""


class _VlmBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["heading", "text"]
    text: str
    extraction_confidence: Confidence


class _VlmPageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    blocks: list[_VlmBlock]


def _usage_of(response: httpx.Response) -> VlmPageUsage:
    """The token usage OpenRouter billed for `response`.

    Zero when the response carries no `usage` object at all — logged rather
    than silently treated as free: a 200 with real content and no usage means
    OpenRouter answered without saying what it cost, and a cost figure that
    turns that into `$0` without a trace is the exact "silently free" outcome
    `src.agents.costs.UnpricedModelError`'s docstring warns against.
    """
    body = response.json()
    if "usage" not in body:
        logger.warning("OpenRouter trả về 200 không kèm `usage` — chi phí trang này ghi nhận bằng 0")
    usage = body.get("usage") or {}
    return VlmPageUsage(int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0)))


def _call_openrouter_page_with_usage(image_bytes: bytes, page_number: int) -> tuple[list[ExtractedBlock], VlmPageUsage]:
    """`_call_openrouter_page`'s real body — blocks, plus what every attempt at this page billed.

    Split out so the ingest cost path (`extract_document`'s `on_vlm_spend`)
    reads the same usage the request was actually charged for, rather than a
    second guess computed beside it. `_call_openrouter_page` below is the
    public, usage-blind wrapper every existing caller (and every test that
    predates RAG-15) still gets.
    """
    settings = get_settings()
    image_b64 = base64.b64encode(image_bytes).decode("ascii")
    messages: list[dict[str, object]] = [
        {"role": "system", "content": _SCHEMA_PROMPT},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": f"Trang {page_number}."},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}},
            ],
        },
    ]

    last_error: Exception | None = None
    usage = VlmPageUsage()
    for attempt in range(_MAX_VLM_ATTEMPTS):
        last_attempt = attempt == _MAX_VLM_ATTEMPTS - 1
        try:
            response = httpx.post(
                settings.openrouter_url,
                headers={"Authorization": f"Bearer {settings.openrouter_api_key}"},
                json={
                    "model": settings.extraction_vlm_model,
                    "messages": messages,
                    "response_format": {"type": "json_object"},
                },
                timeout=settings.tool_http_timeout_seconds,
            )
            if response.status_code == 429 or response.status_code >= 500:
                last_error = RuntimeError(f"OpenRouter trả về {response.status_code}")
                if not last_attempt:
                    time.sleep(_RETRY_BACKOFF_SECONDS * (attempt + 1))
                continue
            response.raise_for_status()
            body = response.json()
            content = body["choices"][0]["message"]["content"]
            parsed = _VlmPageResponse.model_validate_json(content)
            usage += _usage_of(response)
            return [
                ExtractedBlock(
                    kind=block.kind,
                    text=block.text,
                    page_number=page_number,
                    extraction_confidence=block.extraction_confidence,
                )
                for block in parsed.blocks
            ], usage
        except httpx.HTTPStatusError as exc:
            # A non-429/5xx status here is a 4xx that will not fix itself on
            # retry (bad/missing API key, malformed request) — fail loud and
            # immediately, wrapped like every other failure path, rather than
            # let a raw httpx error escape this module's exception contract.
            raise ExtractionError(f"Trang {page_number}: OpenRouter từ chối yêu cầu ({exc})", usage=usage) from exc
        except (json.JSONDecodeError, ValidationError, KeyError, IndexError) as exc:
            # IndexError covers an empty `choices` list — the same "not the
            # shape we asked for" failure as a missing key, just one level up.
            # The model still generated tokens for this malformed reply and
            # OpenRouter still billed for them, so they count even though the
            # attempt itself did not win.
            last_error = exc
            try:
                usage += _usage_of(response)
            except (json.JSONDecodeError, KeyError):
                pass
            if not last_attempt:
                # A reminder in-conversation, not a fresh request — the model
                # sees its own bad output and what was wrong with it, the same
                # thing a human reviewer would say back to it.
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"Kết quả trước không đúng định dạng JSON schema yêu cầu ({exc}). "
                            "Hãy trả lại CHỈ một đối tượng JSON hợp lệ đúng schema đã cho, không kèm gì khác."
                        ),
                    }
                )
        except httpx.TimeoutException as exc:
            last_error = exc
            if not last_attempt:
                time.sleep(_RETRY_BACKOFF_SECONDS * (attempt + 1))

    raise ExtractionError(
        f"Trang {page_number}: không trích xuất được sau {_MAX_VLM_ATTEMPTS} lần thử ({last_error})", usage=usage
    )


def _call_openrouter_page(image_bytes: bytes, page_number: int) -> list[ExtractedBlock]:
    blocks, _usage = _call_openrouter_page_with_usage(image_bytes, page_number)
    return blocks


def get_vlm_page_client() -> VlmPageClient:
    """FastAPI dependency: the only function in the system allowed to call the extraction VLM."""
    return _call_openrouter_page


# --- fast path: text already exists, no model needed ---------------------- #

#: A block boundary: one or more blank lines — the same convention
#: `src.workflow.knowledge` reads paragraphs by, for the same reason (a
#: paragraph is a unit somebody wrote on purpose).
_PARAGRAPHS = re.compile(r"\n\s*\n")


def _paragraph_blocks(text: str, *, page_number: int | None) -> list[ExtractedBlock]:
    return [
        ExtractedBlock(kind="text", text=block.strip(), page_number=page_number, extraction_confidence="high")
        for block in _PARAGRAPHS.split(text)
        if block.strip()
    ]


def _docx_text(content: bytes) -> str:
    from docx import Document as DocxDocument

    document = DocxDocument(BytesIO(content))
    parts = [paragraph.text for paragraph in document.paragraphs if paragraph.text.strip()]
    for table in document.tables:
        for row in table.rows:
            parts.append(" | ".join(cell.text.strip() for cell in row.cells))
    return "\n\n".join(parts)


def _pdf_text_layer_blocks(content: bytes) -> list[ExtractedBlock] | None:
    """`None` when any page lacks a text layer — the signal to fall through to the VLM path."""
    from pypdf import PdfReader

    pages = PdfReader(BytesIO(content)).pages
    blocks: list[ExtractedBlock] = []
    for page_number, page in enumerate(pages, start=1):
        text = (page.extract_text() or "").strip()
        if not text:
            return None
        blocks.extend(_paragraph_blocks(text, page_number=page_number))
    return blocks


def _fast_path_blocks(content: bytes, suffix: str) -> list[ExtractedBlock] | None:
    """Blocks for a document that never needs the VLM, or `None` for a PDF that does."""
    if suffix in (".md", ".txt"):
        return _paragraph_blocks(content.decode("utf-8"), page_number=None)
    if suffix == ".docx":
        return _paragraph_blocks(_docx_text(content), page_number=None)
    if suffix == ".pdf":
        return _pdf_text_layer_blocks(content)
    raise ExtractionError(f"Định dạng {suffix!r} không được hỗ trợ")


# --- VLM path: render pages, then read each one -------------------------- #


def _vlm_blocks(content: bytes, vlm_client: VlmPageClient) -> list[ExtractedBlock]:
    import fitz  # type: ignore[import-untyped]  # PyMuPDF ships no stubs / py.typed marker

    blocks: list[ExtractedBlock] = []
    with fitz.open(stream=content, filetype="pdf") as document:
        for page_index in range(document.page_count):
            image_bytes = document[page_index].get_pixmap().tobytes("png")
            blocks.extend(vlm_client(image_bytes, page_index + 1))
    return blocks


# --- entrypoint ------------------------------------------------------------ #


def extract_document(
    content: bytes,
    filename: str,
    *,
    cache: ExtractionCache | None = None,
    vlm_client: VlmPageClient | None = None,
    on_vlm_spend: Callable[[VlmSpend], None] | None = None,
) -> ExtractionResult:
    """The one function ingest calls: `content` in, a validated structure out.

    Cache-checked before any extraction work — including before deciding which
    path applies, since that decision itself costs a PDF parse. That check
    alone is what RAG-15's *"upload trùng file → không phát sinh dòng mới"* is
    built on: `on_vlm_spend` is never even reachable on a cache hit.

    `on_vlm_spend`, when given, is called at most once — after every page of a
    freshly-extracted (never cached, never text-native) document has gone
    through the VLM — with the total this extraction actually cost. It fires
    only when the *real* OpenRouter client answered, whether that client
    arrived as the default or was resolved by the caller (`document_routes`
    resolves it through FastAPI's own DI and always passes it in explicitly,
    so identity — not "was `vlm_client` omitted" — is the only reliable test).
    A caller's test double never matches, which is what keeps the test suite's
    fake clients from ever looking like billed spend.
    """
    cache = cache if cache is not None else get_extraction_cache()
    digest = hashlib.sha256(content).hexdigest()

    cached = cache.get(digest)
    if cached is not None:
        return cached

    suffix = Path(filename).suffix.lower()
    fast_blocks = _fast_path_blocks(content, suffix)
    if fast_blocks is not None:
        result = ExtractionResult(blocks=tuple(fast_blocks), source="text_layer")
    else:
        client = vlm_client if vlm_client is not None else get_vlm_page_client()
        if client is _call_openrouter_page:
            page_usages: list[VlmPageUsage] = []

            def _tracked(image_bytes: bytes, page_number: int) -> list[ExtractedBlock]:
                try:
                    page_blocks, usage = _call_openrouter_page_with_usage(image_bytes, page_number)
                except ExtractionError as exc:
                    # This page never produced blocks, but its failed attempts
                    # may still have billed real tokens (a malformed-JSON retry
                    # before the final giving-up) — recorded below in `finally`
                    # even though this page contributes nothing to `blocks`.
                    if exc.usage.input_tokens or exc.usage.output_tokens:
                        page_usages.append(exc.usage)
                    raise
                page_usages.append(usage)
                return page_blocks

            try:
                blocks = _vlm_blocks(content, _tracked)
            finally:
                # A later page failing must not erase what earlier pages in
                # this same document already cost — `on_vlm_spend` fires here,
                # in `finally`, so a partial failure still bills for the pages
                # that did go through (RAG-15 / #101's "chi phí ghi lại khớp
                # pricing OpenRouter thực tế" holds for the pages OpenRouter
                # actually charged, not only for a document that finished).
                if on_vlm_spend is not None and page_usages:
                    total = sum(page_usages, VlmPageUsage())
                    on_vlm_spend(
                        VlmSpend(
                            model=get_settings().extraction_vlm_model,
                            input_tokens=total.input_tokens,
                            output_tokens=total.output_tokens,
                            page_count=len(page_usages),
                        )
                    )
        else:
            blocks = _vlm_blocks(content, client)
        result = ExtractionResult(blocks=tuple(blocks), source="vlm")

    if result.flagged_for_review:
        logger.warning(
            "Extraction of %r flagged for manual review: %.0f%% of %d blocks are low-confidence",
            filename,
            result.low_confidence_ratio * 100,
            len(result.blocks),
        )
        for block in result.blocks:
            if block.extraction_confidence == "low":
                logger.warning(
                    "Low-confidence extraction — %r page=%s: %r",
                    filename,
                    block.page_number,
                    block.text[:120],
                )

    cache.set(digest, result)
    return result
