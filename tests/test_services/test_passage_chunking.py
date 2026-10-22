"""Structure-aware chunking of legal documents — RAG-07 / ADR-0044 §6.

These tests exist because the module they cover had none, and the whole
Knowledge Base retrieval path runs through it. The shape every case below
imitates is the one a real `quy chế đào tạo` PDF actually produces: the
text-layer fast path marks *nothing* `kind="heading"`, and it splits blocks
on blank lines — which a PDF does not put before each `Điều`. So several
articles arrive glued into one `kind="text"` block, and a boundary that is
only looked for at the *start* of a block finds one article where there
were eight.
"""

from src.services.chunking import chunk_document
from src.services.extraction import ExtractedBlock, ExtractionResult

#: Four articles under one chapter, exactly as the text layer hands them
#: over: one block, no blank lines, headings indistinguishable from body by
#: `kind` alone. The wrapped cross-reference in Điều 12 is deliberate — see
#: `test_a_wrapped_cross_reference_is_not_a_boundary`.
_REGULATION_BLOCK = """CHƯƠNG II
ĐÀO TẠO ĐẠI HỌC
Điều 10. Tổ chức lớp học
1. Lớp học được tổ chức theo từng học phần dựa vào đăng ký khối lượng học tập.
Điều 11. Đăng ký học tập
1. Sinh viên đăng ký học phần trước mỗi học kỳ theo kế hoạch của Đại học.
Điều 12. Xếp loại học lực
1. Học lực được xếp loại theo điểm trung bình tích lũy, quy định tại khoản 5
Điều 12 Quy chế này.
Điều 13. Buộc thôi học
1. Sinh viên bị buộc thôi học nếu điểm trung bình tích lũy dưới 1,0."""


def _extraction(*texts: str) -> ExtractionResult:
    """An `ExtractionResult` shaped like the PDF text-layer fast path's output.

    Every block is `kind="text"` and `extraction_confidence="high"`, because
    that is what the fast path emits — it never calls anything a heading, which
    is the precondition all of these tests are about.
    """
    return ExtractionResult(
        blocks=tuple(
            ExtractedBlock(kind="text", text=text, page_number=1, extraction_confidence="high") for text in texts
        ),
        source="text_layer",
    )


def _paths(passages: list) -> list[str | None]:
    return [p.section_path for p in passages]


def test_every_article_in_one_block_becomes_its_own_boundary() -> None:
    """Four `Điều` glued into one block must yield four distinct Section Paths.

    The failure this pins down is not cosmetic. When only the first article is
    recognised, every later article's text is filed under the first one's
    number — so a passage about buộc thôi học is cited as `Điều 10`, and an
    answer that quotes it is confidently, checkably wrong about its source.
    """
    passages = chunk_document(_extraction(_REGULATION_BLOCK), profile="legal")

    paths = " | ".join(str(p) for p in _paths(passages))
    for article in ("Điều 10", "Điều 11", "Điều 12", "Điều 13"):
        assert article in paths, f"{article} không trở thành ranh giới; các path thu được: {paths}"


def test_an_article_heading_keeps_its_title_in_the_section_path() -> None:
    """`Điều 13. Buộc thôi học`, not a bare `Điều 13`.

    The title is the half a reader's question is actually phrased in — nobody
    asks "what does Điều 13 say", they ask about buộc thôi học. A breadcrumb
    that drops it throws away the words that would have matched.
    """
    passages = chunk_document(_extraction(_REGULATION_BLOCK), profile="legal")

    paths = " | ".join(str(p) for p in _paths(passages))
    assert "Buộc thôi học" in paths, f"tiêu đề của Điều 13 bị mất khỏi section_path: {paths}"


def test_a_wrapped_cross_reference_is_not_a_boundary() -> None:
    """`...quy định tại khoản 5\nĐiều 12 Quy chế này` starts no new article.

    A PDF wraps lines wherever the column ends, so a citation can land at the
    start of a line looking exactly like a heading. Real headings in these
    documents are written `Điều 12.` — number then a period — which is the
    signal that separates the two. Getting this wrong plants a false boundary
    in the middle of a sentence, which is worse than the bug being fixed: it
    cuts a provision in half and files the halves under different articles.
    """
    passages = chunk_document(_extraction(_REGULATION_BLOCK), profile="legal")

    # `Điều 12` appears once as a heading and once as a wrapped citation. The
    # citation must stay inside its passage's body, not become a second one.
    starts = [
        p for p in passages if (p.section_path or "").startswith("Điều 12") or "Điều 12" in (p.section_path or "")
    ]
    bodies = " ".join(p.text for p in starts)
    assert "Quy chế này" in bodies, "câu bị ngắt dòng đã bị cắt rời khỏi Điều 12"


def test_the_chapter_stays_above_the_articles_it_contains() -> None:
    """`CHƯƠNG II > Điều 11`: pushing an article must not pop its chapter."""
    passages = chunk_document(_extraction(_REGULATION_BLOCK), profile="legal")

    nested = [p for p in _paths(passages) if p and "CHƯƠNG" in p and "Điều" in p]
    assert nested, f"không path nào giữ được cả Chương lẫn Điều: {_paths(passages)}"


def test_the_section_path_travels_into_the_text_that_gets_embedded() -> None:
    """A Passage's embeddable form carries its breadcrumb.

    `src.services.ingestion` embeds one string per passage. If that string is
    the bare body, then no vector in the collection contains the words
    `Điều 13`, and "Điều 13 quy định gì" cannot match on the one term that
    identifies the answer — in either the dense or the sparse half. The
    The retriever this module replaced made the same choice for the same
    reason, before there was a Section Path to put in front.
    """
    passages = chunk_document(_extraction(_REGULATION_BLOCK), profile="legal")

    for passage in passages:
        if passage.section_path:
            assert passage.section_path in passage.embeddable, "breadcrumb không có trong văn bản đem đi embed"
            assert passage.text in passage.embeddable, "phần thân bị mất khỏi văn bản đem đi embed"


#: `Điều 3` of the reference `quy chế`, abridged to the two points that matter:
#: `a)` scopes its rule to **cử nhân**, `b)` scopes a *different* rule to
#: **liên thông** students. Verbatim, because the bug this pins down was a
#: reader — human or model — losing track of which point it was reading.
_ARTICLE_WITH_POINTS = """Điều 3. Thời gian và kế hoạch học tập
3. Căn cứ kế hoạch học tập chuẩn toàn khóa của CTĐT, người học tự xây dựng kế hoạch học tập theo từng học kỳ:
a) Người học được phép tốt nghiệp sớm hơn khi đã đáp ứng toàn bộ điều kiện tốt nghiệp của CTĐT hoặc tốt nghiệp chậm hơn so với thời gian theo kế hoạch học tập chuẩn, nhưng thời gian chậm tiến độ không được vượt quá 5 học kỳ chính đối với các chương trình cấp bằng cử nhân, không quá 2 học kỳ chính đối với các chương trình cấp bằng kỹ sư và thạc sĩ.
b) Đối với sinh viên học liên thông đã được miễn trừ khối lượng tín chỉ tích lũy, thời gian thiết kế để hoàn thành khóa học được xác định trên cơ sở thời gian theo kế hoạch học tập chuẩn toàn khoá giảm tương ứng với khối lượng tín chỉ được công nhận và miễn trừ cho người học. Thời gian chậm tiến độ không được vượt quá 3 học kỳ chính và không vượt quá 2 lần thời gian thiết kế để hoàn thành khóa học.
c) Thời gian đào tạo chuẩn của trình độ tiến sĩ từ 3 năm đến 4 năm."""


def test_a_point_inside_an_article_is_cited_down_to_the_point() -> None:
    """`Điều 3 > 3 > a)`, because that is the address of the rule.

    "Thời gian học tập tối đa của cử nhân" is answered by point a) and by
    nothing else in the article. A citation that stops at `Điều 3` names 1876
    characters covering four unrelated subjects — doctoral candidates, transfer
    students, priority admissions — and leaves the reader to find which one
    applied.
    """
    passages = chunk_document(_extraction(_ARTICLE_WITH_POINTS), profile="legal")

    owning = [p for p in passages if "5 học kỳ chính" in p.text]
    assert owning, "không passage nào chứa quy định của điểm a)"
    path = owning[0].section_path or ""
    assert "Điều 3" in path, f"mất số điều khỏi trích dẫn: {path}"
    assert path.rstrip().endswith("a)"), f"trích dẫn không xuống tới điểm a): {path}"


def test_a_rule_never_arrives_without_the_point_that_scopes_it() -> None:
    """b)'s "2 lần thời gian thiết kế" must not be readable as a) or c)'s rule.

    This is the failure that produced a confident, arithmetic, wrong answer:
    a chunk boundary landed inside b), the fragment lost its opening "Đối với
    sinh viên học liên thông", and "không vượt quá 2 lần thời gian thiết kế"
    was then read as the general rule for cử nhân — 4 + 2×4 = 12 years, from a
    provision that was never about cử nhân at all.
    """
    passages = chunk_document(_extraction(_ARTICLE_WITH_POINTS), profile="legal")

    for passage in passages:
        if "2 lần thời gian thiết kế" in passage.text:
            scoped = "liên thông" in passage.text or "b)" in (passage.section_path or "")
            assert scoped, f"quy định của điểm b) mất phạm vi áp dụng: {passage.section_path} :: {passage.text[:120]}"


def test_the_table_of_contents_is_not_retrievable() -> None:
    """A contents page competes with every article it lists, so it is dropped.

    It names the title of *every* article in the document. Left in, a question
    phrased in an article's own words scores against the contents page as
    strongly as against the article itself — and the evidence it returns is a
    column of page numbers.
    """
    contents = (
        "MỤC LỤC\n"
        "CHƯƠNG I NHỮNG QUY ĐỊNH CHUNG ...................................... 1\n"
        "Điều 1. Phạm vi điều chỉnh và đối tượng áp dụng ................... 1\n"
        "Điều 3. Thời gian và kế hoạch học tập ............................. 3\n"
    )
    body = "Điều 3. Thời gian và kế hoạch học tập\n1. ĐHBK Hà Nội tổ chức đào tạo theo năm học và học kỳ."

    passages = chunk_document(_extraction(contents, body), profile="legal")

    assert not any("....." in p.text for p in passages), "mục lục vẫn còn trong kết quả"
    assert any("năm học và học kỳ" in p.text for p in passages), "phần thân thật bị xoá nhầm"


def test_a_short_opening_section_is_not_filed_under_the_next_ones_heading() -> None:
    """The document's first Passage has no predecessor to fold back into, so it
    folds *forward* — and folding forward across a heading files one section's
    text under the next section's name.

    Found while consolidating the retrieval stacks, on a three-section
    handbook: "Khách được đổi trả trong vòng 30 ngày" came back cited as
    `Chính sách hoàn tiền`. Right about the rule, wrong about where the rule
    lives, which is the one failure this module says it exists to prevent. The
    passage is short — that is why it was a candidate to be folded at all — and
    it stays short and on its own, because a short section is still a section
    somebody cites.
    """
    passages = chunk_document(
        _extraction(
            "Chính sách đổi trả",
            "Khách được đổi trả trong vòng 30 ngày.",
            "Chính sách hoàn tiền",
            "Hoàn tiền được xử lý trong 7 ngày làm việc sau khi nhận lại hàng đã kiểm tra xong.",
        )
    )

    filed = {passage.section_path: passage.text for passage in passages}
    assert "30 ngày" in filed["Chính sách đổi trả"]
    assert "30 ngày" not in filed.get("Chính sách hoàn tiền", "")

