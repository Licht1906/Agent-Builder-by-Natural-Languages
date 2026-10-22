"""Structure-aware chunking into Passages (RAG-07 / ADR-0044 §6).

`src.workflow.knowledge:72-74` already gave the reason fixed-size windows are
refused: *"a citation that stops mid-sentence reads as the agent misquoting
the source."* This module cuts on boundaries the document's own author put
there — headings RAG-06 extracted, or (when extraction handed back
undifferentiated text) the same markers a reader would recognize by eye —
never a character count.

**A profile is a property of the Document, not of the platform.** There is
no legal node, no legal tool, and no `article_number` column anywhere in this
system. Section Path is the one thing every Passage carries regardless of
profile — `"Chương III > Điều 12 > Khoản 2"` for a document that has
articles, and whatever a `generic` document's own headings say for one that
does not. A document that is not legal text run under `legal` just never
matches the legal patterns and falls back to being read as `generic` would
read it; nothing downstream needs to know which profile ran.

**RAG-06's structure is the primary source; regex is the fallback.** A block
extraction already marked `kind="heading"` is a boundary because RAG-06 said
so. Regex only does work where RAG-06 handed back undifferentiated
`kind="text"` — which is every block from extraction's fast path
(`.md`/`.txt`/`.docx`/text-layer PDF; see `src.services.extraction`, whose
fast path never marks anything a heading) and any VLM page where the model
didn't call something one. `legal` looks for its numbered markers there;
`generic` falls back to a plain heuristic — a short line with no closing
sentence punctuation reads as a title the same way it would to a human
skimming the page.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict

from src.services.extraction import ExtractedBlock, ExtractionResult

Profile = Literal["generic", "legal"]

#: Past this many (approximate) tokens, a Passage is cut further by sentence
#: — never mid-sentence, per the module's own reason for existing.
MAX_PASSAGE_TOKENS = 500

#: Below this many tokens, a Passage (typically an Điều/heading with no body
#: of its own) is not worth being its own retrieval unit and is folded into a
#: neighbor instead.
MIN_PASSAGE_TOKENS = 20

#: A run of at least this many blocks matching a `legal` boundary is what
#: `detect_profile` calls "this is legal text" — one stray "Điều" mentioned
#: in passing (a generic policy citing a law) should not flip the profile.
_LEGAL_DETECTION_MIN_MATCHES = 2

#: A `generic`-profile `kind="text"` block this short, with no closing
#: sentence punctuation, is read as a heading extraction didn't call one —
#: the fallback for the common case (`.md`/`.txt`/`.docx`, or any VLM page)
#: where nothing marked it as such. See `_looks_like_generic_heading`.
_GENERIC_HEADING_MAX_WORDS = 8


class Passage(BaseModel):
    """One retrieval unit: text the chunker decided belongs together, and where it sits.

    Domain vocabulary from `CONTEXT.md`: *"One stretch of a Document handed
    to a model to answer from, carrying enough of where it came from to be
    cited."* This is that stretch, at ingest time, before it has an owning
    Knowledge Base or Document to be cited *from* — RAG-11 (#97) is what
    attaches that and turns this into the `Passage` a retrieval result
    carries.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str
    #: The Section Path to this Passage, e.g. `"Chương III > Điều 12 > Khoản 2"`.
    #: `None` for text that appears before any boundary has been seen yet.
    section_path: str | None
    #: Carried straight from the source block(s); `None` wherever
    #: `ExtractedBlock.page_number` was `None` (`.md`/`.txt`/`.docx`).
    page_number: int | None

    @property
    def embeddable(self) -> str:
        """The text to embed and to index for keyword search — breadcrumb included.

        `src.services.ingestion` builds both the dense and the sparse vector
        from this, not from `text`. Without the breadcrumb no vector in the
        collection contains the words `Điều 13`, so "Điều 13 quy định gì" can
        match on the one term that identifies the answer in neither half of
        hybrid search — the question is answered, if at all, by whatever else
        the body happens to say.

        The breadcrumb is a separate line rather than glued to the first
        sentence, so a model reading the passage can tell the title of the
        section from the start of its body — the retriever this module
        replaced made the same choice, for the same reason.
        """
        if not self.section_path:
            return self.text
        return f"{self.section_path}\n{self.text}"


#: What counts as a word for the token approximation below — the same
#: Unicode-by-default `\w+` `src.workflow.knowledge._TERMS` uses, and for the
#: same reason: it is the only thing that makes Vietnamese text count as
#: anything at all. Not a real tokenizer — a token budget close enough to
#: decide "does this need to be cut" from "does this need to be merged".
_WORDS = re.compile(r"\w+", re.UNICODE)


def _approx_tokens(text: str) -> int:
    return len(_WORDS.findall(text))


_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+")


def _split_by_sentence(text: str, max_tokens: int) -> list[str]:
    """`text` cut into pieces at or under `max_tokens`, breaking only between sentences.

    A single sentence longer than `max_tokens` still comes back as one piece
    — the budget is a target to cut at, not a hard cap that would put a
    fixed-size window's failure back in through the side door.
    """
    sentences = [s for s in _SENTENCE_BOUNDARY.split(text) if s.strip()]
    if not sentences:
        return [text] if text.strip() else []

    pieces: list[str] = []
    buffer: list[str] = []
    buffer_tokens = 0
    for sentence in sentences:
        sentence_tokens = _approx_tokens(sentence)
        if buffer and buffer_tokens + sentence_tokens > max_tokens:
            pieces.append(" ".join(buffer))
            buffer = []
            buffer_tokens = 0
        buffer.append(sentence)
        buffer_tokens += sentence_tokens
    if buffer:
        pieces.append(" ".join(buffer))
    return pieces


# --- legal profile: the boundaries a Vietnamese statute is written in ----- #

#: Ordered shallowest-first. `Mục` sits between `Chương` and `Điều` in the
#: profile's own boundary list even though the ticket's regex examples don't
#: spell it out — the same numbering convention as the others.
_LEGAL_PATTERNS: tuple[tuple[int, re.Pattern[str]], ...] = (
    (1, re.compile(r"Chương\s+[IVXLCDM]+", re.IGNORECASE)),
    (2, re.compile(r"Mục\s+\d+", re.IGNORECASE)),
    (3, re.compile(r"Điều\s+\d+", re.IGNORECASE)),
    (4, re.compile(r"Khoản\s+\d+", re.IGNORECASE)),
    (5, re.compile(r"Điểm\s+[a-z]\)", re.IGNORECASE)),
)


#: The same markers as `_LEGAL_PATTERNS`, but recognised as the opening of a
#: *line* rather than of a whole block — and only the three levels a document
#: actually puts on a line of their own. `Khoản`/`Điểm` are deliberately absent:
#: they are written `1.` and `a)` inside running prose, and cutting there would
#: fragment a single provision into unreadable slivers.
#:
#: **The `[.:]` after the number is the whole point.** A PDF wraps lines
#: wherever its column ends, so a citation lands at the start of a line looking
#: exactly like a heading — `...quy định tại khoản 5` / `Điều 12 Quy chế này`.
#: Real headings in these documents are written `Điều 12.`; citations are not.
#: Measured on the reference document (a 34-page `quy chế đào tạo`): 50 of the
#: 51 lines opening with `Điều N` carry the period and are headings, and the
#: single line that does not is precisely such a wrapped citation.
_LEGAL_LINE_PATTERNS: tuple[tuple[int, re.Pattern[str]], ...] = (
    (1, re.compile(r"^(?:CHƯƠNG|Chương|PHẦN|Phần)\s+[IVXLCDM]+\b", re.IGNORECASE)),
    (2, re.compile(r"^Mục\s+\d+\s*[.:]", re.IGNORECASE)),
    (3, re.compile(r"^Điều\s+\d+\s*[.:]", re.IGNORECASE)),
)


#: A `khoản` (`1.`) and a `điểm` (`a)`) — the levels *below* an article, and
#: the address a citation actually needs. "Thời gian học tập tối đa của cử
#: nhân" is answered by `Điều 3 > 3. > a)` and by nothing else in that article;
#: a citation stopping at `Điều 3` names 1876 characters covering doctoral
#: candidates and transfer students too, and leaves the reader to work out
#: which point applied. The document writes these as bare `1.` and `a)`, never
#: as the words `Khoản`/`Điểm` that `_LEGAL_PATTERNS` spells out.
#:
#: Both are honoured **only inside an article** (see `_raw_passages`): `1.` is
#: also how an ordinary numbered list opens, and outside a `Điều` there is
#: nothing for it to be a subdivision of.
#: A table-of-contents line: a title followed by dot leaders and a page number.
#: They match the heading patterns perfectly and are not headings — the section
#: they name is thirty pages away, and taking one as a boundary opens a section
#: whose "body" is the rest of the table of contents.
_DOT_LEADERS = re.compile(r"\.{5,}")

_KHOAN_LINE = re.compile(r"^(\d{1,2})\.\s+(?=\S)")
_DIEM_LINE = re.compile(r"^([a-zđ])\)\s+(?=\S)")

#: Below this level a heading is a subdivision of an article rather than a
#: section of the document, and is only pushed when an article is open.
_SUBARTICLE_LEVEL = 4

#: The level `Điều` occupies in `_LEGAL_LINE_PATTERNS`.
_ARTICLE_LEVEL = 3


def _legal_heading_of_line(line: str) -> tuple[int, str, str] | None:
    """`(level, label, remainder)` if `line` opens a legal section, else `None`.

    For a `Chương`/`Mục`/`Điều` the label is the *whole* line, title included —
    `"Điều 13. Buộc thôi học"` and not a bare `"Điều 13"`. The title is the half
    a reader's question is actually phrased in: nobody asks what Điều 13 says,
    they ask about buộc thôi học. Since the Section Path travels into the
    embedded text (`Passage.embeddable`), dropping the title would throw away
    the very words that would have matched. Those lines carry no `remainder`.

    A `khoản`/`điểm` is the other way round: `"a) Người học được phép..."` is a
    marker followed by the rule itself, so the label is just `"a)"` and
    everything after it is `remainder` — body text that must not be lost into a
    breadcrumb.
    """
    stripped = line.strip()
    if _DOT_LEADERS.search(stripped):
        return None
    for level, pattern in _LEGAL_LINE_PATTERNS:
        if pattern.match(stripped):
            return level, stripped, ""
    khoan = _KHOAN_LINE.match(stripped)
    if khoan is not None:
        return 4, f"{khoan.group(1)}.", stripped[khoan.end() :]
    diem = _DIEM_LINE.match(stripped)
    if diem is not None:
        return 5, f"{diem.group(1)})", stripped[diem.end() :]
    return None


def _legal_segments(text: str) -> list[tuple[tuple[int, str] | None, str]]:
    """`text` cut at every legal heading line, as `(heading, body)` pairs.

    This is the fix for the failure mode that made this module's output
    unusable on real regulations. The text-layer fast path splits blocks on
    blank lines, and a PDF does not put a blank line before each `Điều` — so
    eight articles arrive glued into one `kind="text"` block. A boundary
    looked for only at the *start* of a block therefore found one article
    where there were eight, and filed all eight under the first one's number.

    A leading `(None, ...)` pair is the text before the first heading, which
    belongs to whatever section was already open.
    """
    segments: list[tuple[tuple[int, str] | None, str]] = []
    heading: tuple[int, str] | None = None
    body: list[str] = []

    for line in text.split("\n"):
        found = _legal_heading_of_line(line)
        if found is None:
            body.append(line)
            continue
        if heading is not None or any(part.strip() for part in body):
            segments.append((heading, "\n".join(body).strip()))
        level, label, remainder = found
        heading, body = (level, label), ([remainder] if remainder else [])

    if heading is not None or any(part.strip() for part in body):
        segments.append((heading, "\n".join(body).strip()))
    return segments


def _match_legal(text: str) -> tuple[int, str] | None:
    """The deepest-matching legal marker anchored at the start of `text`, or `None`.

    Anchored at the start rather than searched for anywhere: a paragraph that
    merely *mentions* "Điều 5" mid-sentence is not the start of Điều 5, and
    treating it as one would plant a false boundary in running prose.
    """
    stripped = text.strip()
    for level, pattern in _LEGAL_PATTERNS:
        matched = pattern.match(stripped)
        if matched:
            return level, matched.group(0)
    return None


def detect_profile(extraction: ExtractionResult) -> Profile:
    """`legal` when enough blocks open with a legal marker; `generic` otherwise.

    Counts headings and text blocks alike — RAG-06's fast path never marks
    anything a heading, so a legal document read through that path would
    never be detected if only headings counted.
    """

    def _is_section_heading(line: str) -> bool:
        # `Chương`/`Mục`/`Điều` only. A `khoản`/`điểm` marker is not evidence
        # that a document is legal text — `1.` opens an ordinary numbered list
        # in any document at all, and counting those would read every `.md`
        # with a list in it as a regulation.
        found = _legal_heading_of_line(line)
        return found is not None and found[0] < _SUBARTICLE_LEVEL

    matches = sum(1 for block in extraction.blocks for line in block.text.split("\n") if _is_section_heading(line))
    # Counted per *line* and not per block, for the same reason `_legal_segments`
    # exists: the fast path hands over several articles in one block, so a
    # document with fifty `Điều` can present as two blocks that happen to open
    # with one — detection would sit right on the threshold by accident.
    if matches < _LEGAL_DETECTION_MIN_MATCHES:
        matches = sum(1 for block in extraction.blocks if _match_legal(block.text) is not None)
    return "legal" if matches >= _LEGAL_DETECTION_MIN_MATCHES else "generic"


_SENTENCE_END = (".", "!", "?", ":")


def _looks_like_generic_heading(text: str) -> bool:
    """A short line with no closing sentence punctuation reads as a title.

    The `generic` fallback for exactly the gap `legal`'s regex fills: RAG-06's
    fast path never marks anything `kind="heading"`, so without this, every
    `.md`/`.txt`/`.docx` document read under `generic` would carry
    `section_path=None` throughout — a document that visibly has section
    titles when a person reads it, producing no Section Path at all.
    """
    stripped = text.strip()
    if not stripped or stripped.endswith(_SENTENCE_END):
        return False
    return _approx_tokens(stripped) <= _GENERIC_HEADING_MAX_WORDS


# --- the running Section Path ----------------------------------------------- #


@dataclass
class _SectionStack:
    """The path to wherever chunking currently is, as a stack of (depth, label).

    Pushing a shallower-or-equal depth pops everything at or below it first —
    a new `Điều` replaces the previous `Điều` (and anything under it) but
    leaves the `Chương` above it standing, which is exactly how a reader's
    own sense of "where am I" in the document works.
    """

    entries: list[tuple[int, str]] = field(default_factory=list)

    def push(self, level: int, label: str) -> None:
        while self.entries and self.entries[-1][0] >= level:
            self.entries.pop()
        self.entries.append((level, label))

    @property
    def path(self) -> str | None:
        if not self.entries:
            return None
        return " > ".join(label for _level, label in self.entries)

    @property
    def inside_article(self) -> bool:
        """True when a `Điều` is currently open.

        What gates `khoản`/`điểm`: `1.` is also how an ordinary numbered list
        opens, and outside an article there is nothing for it to be a
        subdivision of — pushing it would invent a section the document does
        not have.
        """
        return any(level == _ARTICLE_LEVEL for level, _label in self.entries)

    @property
    def next_unclassified_level(self) -> int:
        """One deeper than wherever the stack currently sits — for a heading no pattern recognized."""
        return self.entries[-1][0] + 1 if self.entries else 1


@dataclass(frozen=True)
class _RawPassage:
    text: str
    section_path: str | None
    page_number: int | None

    def merged_with(self, other: "_RawPassage", *, keep: Literal["self", "other"]) -> "_RawPassage":
        """`self` and `other`'s text concatenated in that order; the identity of whichever side `keep` names wins.

        The one place both merge directions in `_merge_short` build a
        combined Passage, so the "which section does the folded-in text end
        up cited under" decision is made in exactly one place.
        """
        receiver = self if keep == "self" else other
        return _RawPassage(
            text=f"{self.text}\n\n{other.text}",
            section_path=receiver.section_path,
            page_number=receiver.page_number if receiver.page_number is not None else self.page_number,
        )


def _raw_passages(blocks: Sequence[ExtractedBlock], profile: Profile) -> list[_RawPassage]:
    stack = _SectionStack()
    raw: list[_RawPassage] = []

    for block in blocks:
        if block.kind == "heading":
            if profile == "legal":
                matched = _match_legal(block.text)
                level, label = matched if matched else (stack.next_unclassified_level, block.text.strip())
            else:
                # `generic` headings are one flat level — RAG-06's `kind`
                # says "this is a heading" but not how deep, and a
                # generic document's own structure gives no second signal.
                level, label = 1, block.text.strip()
            stack.push(level, label)
            continue

        # kind == "text" — the fallback case: extraction didn't call this a
        # heading, so each profile looks for its own kind of boundary inside
        # the plain text instead (see module docstring).
        if profile == "legal":
            segments = _legal_segments(block.text)
            if any(heading is not None for heading, _body in segments):
                for heading, body in segments:
                    if heading is not None:
                        level, label = heading
                        if level >= _SUBARTICLE_LEVEL and not stack.inside_article:
                            # Not a subdivision of anything: put the marker back
                            # where it came from rather than open a section for
                            # it. A bare `1.` outside an article is a list item.
                            body = f"{label} {body}".strip()
                        else:
                            stack.push(level, label)
                    if body:
                        raw.append(_RawPassage(text=body, section_path=stack.path, page_number=block.page_number))
                continue
        elif _looks_like_generic_heading(block.text):
            stack.push(1, block.text.strip())
            continue

        raw.append(_RawPassage(text=block.text, section_path=stack.path, page_number=block.page_number))

    return raw


def _split_long(raw: list[_RawPassage]) -> list[_RawPassage]:
    result: list[_RawPassage] = []
    for passage in raw:
        if _approx_tokens(passage.text) <= MAX_PASSAGE_TOKENS:
            result.append(passage)
            continue
        for piece in _split_by_sentence(passage.text, MAX_PASSAGE_TOKENS):
            result.append(_RawPassage(text=piece, section_path=passage.section_path, page_number=passage.page_number))
    return result


#: A passage this proportion of whose lines are dot-leader entries is a table
#: of contents rather than prose. Half, because a real passage does not cite
#: page numbers line after line, and the table's own heading lines ("MỤC LỤC")
#: dilute the ratio a little.
_TOC_LINE_RATIO = 0.5


def _without_table_of_contents(raw: list["_RawPassage"]) -> list["_RawPassage"]:
    """Drop the table of contents.

    It is not merely low value, it actively competes: it contains the title of
    *every* article in the document, so a question phrased in an article's own
    words ("điều kiện tốt nghiệp") scores against the contents page as strongly
    as against the article, and returns a list of page numbers as its evidence.
    """
    kept: list[_RawPassage] = []
    for passage in raw:
        lines = [line for line in passage.text.splitlines() if line.strip()]
        leaders = sum(1 for line in lines if _DOT_LEADERS.search(line))
        if lines and leaders / len(lines) >= _TOC_LINE_RATIO:
            continue
        kept.append(passage)
    return kept


def _article_of(path: str | None) -> str | None:
    """`path` truncated after its `Điều` component, or `None` if it has none.

    "Which article does this passage belong to" — the question `_merge_short`
    asks before folding one fragment into another, because an article is the
    unit a citation has to be right about and a `khoản` is a refinement of it.
    """
    if not path:
        return None
    parts = path.split(" > ")
    for index, part in enumerate(parts):
        if part.startswith("Điều "):
            return " > ".join(parts[: index + 1])
    return None


def _merge_short(raw: list[_RawPassage]) -> list[_RawPassage]:
    """Fold every under-`MIN_PASSAGE_TOKENS` Passage into a neighbor.

    Merges backward into the Passage immediately before it — reading order,
    so a heading-only `Điều` reads as a continuation of what precedes it
    rather than borrowing the *next* Điều's identity. The one Passage with no
    predecessor (the document's first) merges forward instead. Either way,
    the **receiving** Passage's `section_path` and `page_number` win — the
    folded-in text is retrievable, but a citation naming its own short
    section is a citation this pass deliberately trades away for a Passage
    worth returning at all.
    """
    if not raw:
        return raw

    merged: list[_RawPassage] = [raw[0]]
    for passage in raw[1:]:
        short = _approx_tokens(passage.text) < MIN_PASSAGE_TOKENS
        # **Only within one section.** A short Điều is still a whole Điều, and
        # it is the unit somebody cites — folding it backward would file it
        # under its predecessor's number and produce an answer that is right
        # about the rule and wrong about where the rule lives. That is the
        # failure this module exists to prevent, so a passage is never merged
        # across a Section Path boundary no matter how short it is.
        same_article = _article_of(passage.section_path) is not None and _article_of(
            passage.section_path
        ) == _article_of(merged[-1].section_path)
        # Same Section Path, or two subdivisions of the same article: `b) Đạt
        # chuẩn ngoại ngữ đầu ra.` is six words and cannot be retrieved on its
        # own, but folding it into its sibling keeps the citation inside the
        # article it belongs to — which is the level a reader checks.
        if short and (passage.section_path == merged[-1].section_path or same_article):
            merged[-1] = merged[-1].merged_with(passage, keep="self")
        else:
            merged.append(passage)

    # A second pass for the fragments the rule above deliberately left
    # standing: a chapter's own title line ("ĐÀO TẠO ĐẠI HỌC" under
    # `CHƯƠNG II`) is too short to retrieve on its own and belongs with the
    # first article beneath it. It folds *forward*, keeping the child's
    # identity, because the path it would otherwise carry names a section it
    # is merely the heading of.
    folded: list[_RawPassage] = []
    for index, passage in enumerate(merged):
        following = merged[index + 1] if index + 1 < len(merged) else None
        if (
            _approx_tokens(passage.text) < MIN_PASSAGE_TOKENS
            and following is not None
            and _extends(following.section_path, passage.section_path)
        ):
            merged[index + 1] = passage.merged_with(following, keep="other")
            continue
        folded.append(passage)

    # The document's very first Passage has no predecessor to fold back into,
    # so it folds forward — but only where that does not cross into a section
    # it does not belong to, the same rule both passes above obey. Without the
    # guard, a document opening with a short section files that section's text
    # under the *next* section's Section Path: on the handbook below, "Khách
    # được đổi trả trong vòng 30 ngày" came back cited as `Chính sách hoàn
    # tiền`. Right about the rule, wrong about where the rule lives — the one
    # failure this module says it exists to prevent. A short opening section
    # stays a Passage of its own instead; it is short, but it is citable.
    if (
        len(folded) > 1
        and _approx_tokens(folded[0].text) < MIN_PASSAGE_TOKENS
        and (
            folded[0].section_path == folded[1].section_path
            or _extends(folded[1].section_path, folded[0].section_path)
        )
    ):
        folded[1] = folded[0].merged_with(folded[1], keep="other")
        folded.pop(0)

    return folded


def _extends(child: str | None, parent: str | None) -> bool:
    """True when `child` is `parent` with at least one more level below it.

    `"CHƯƠNG II > Điều 10"` extends `"CHƯƠNG II"`; nothing extends `None`, and
    a path does not extend itself.
    """
    if parent is None or child is None or child == parent:
        return False
    return child.startswith(f"{parent} > ")


def chunk_document(extraction: ExtractionResult, profile: Profile | None = None) -> list[Passage]:
    """`extraction`'s blocks, cut into Passages with a full Section Path each.

    `profile` is the creator's override from upload, per the ticket
    (*"người dùng ghi đè được lúc upload"*) — `None` runs `detect_profile`
    instead, which is the only auto-guess this module makes.
    """
    resolved_profile = profile if profile is not None else detect_profile(extraction)

    raw = _raw_passages(extraction.blocks, resolved_profile)
    raw = _without_table_of_contents(raw)
    raw = _split_long(raw)
    raw = _merge_short(raw)

    return [Passage(text=p.text, section_path=p.section_path, page_number=p.page_number) for p in raw]
