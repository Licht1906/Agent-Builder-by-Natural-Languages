"""Does this action change something in the world that cannot be taken back?

ADR-0023 §3 assigns a tool action's `risk_level` from its effect — reads are
`low`, unrecoverable writes and sends are `high` — and flagged the rule as "a
heuristic worth a follow-up pass to make explicit." ADR-0025's Consequences
noted that the follow-up now has two call sites rather than one: assigning risk
to a registered tool, and deciding at template-selection time whether a spec
needs a `HumanApproval` node in front of its `MCP` node. This module is that one
place, so the two call sites cannot come to different conclusions about the same
sentence.

It reads the creator's own words, from `AgentSolutionSpecification.actions` —
business language, not a tool name, because at selection time no tool has been
chosen yet. That makes it a word-list, and a word-list is a heuristic: it will
miss an effect phrased in words nobody wrote down here.

Which is why it is deliberately lopsided. A false *high* costs a workflow one
approval step its creator can delete on the canvas. A false *low* is a workflow
that sends a real email with nothing standing in front of it, and the creator
finds out afterwards. So a compound word that merely contains an effect word
counts, rather than being excluded for safety's sake.

Nothing downstream treats this as the final word: the graph validator enforces
approval-before-`MCP` structurally (ADR-0023 §5), and an admin reviewing a
registered tool may override the risk it computed.
"""

import unicodedata

#: Effects a business person names in plain words when they mean "this actually
#: happens to somebody". Both languages, because the platform's creators write
#: their specs in Vietnamese and its templates and tools are named in English.
#:
#: Only unrecoverable effects belong here. "Cập nhật" (update) and "tạo" (create)
#: are deliberately absent: they are the `medium` middle ADR-0023 §3 leaves to a
#: judgement call, and pulling them in would make nearly every action high risk,
#: which is the same as having no signal at all.
_HIGH_RISK_WORDS: frozenset[str] = frozenset(
    {
        # Sends — the effect that leaves the building.
        "gui",
        "goi thu",
        "send",
        "email",
        "post",
        # Deletions and cancellations — nothing puts them back.
        "xoa",
        "huy",
        "delete",
        "remove",
        "cancel",
        # Money.
        "hoan tien",
        "chuyen tien",
        "thanh toan",
        "refund",
        "transfer",
        "payment",
        "pay",
        # Anything that becomes public.
        "cong bo",
        "publish",
    }
)


#: Noun phrases that contain an effect word without naming an effect. *"người
#: gửi"* is the sender — the column a mailbox listing shows — and *"Liệt kê các
#: thư tìm được với thông tin người gửi, tiêu đề, ngày"* sends nothing. Left
#: in, that sentence made a read-only search spec "irreversible": the composer
#: was then told to put an approval before every `mcp` while also told a
#: read-only action needs none, drew the honest graph three times, was refused
#: three times, and the creator got the document-QA template with an empty
#: `knowledge_search` that could not be confirmed. A false high used to cost one
#: deletable approval step; with a composer in the loop it costs the draft.
_NOT_EFFECTS: tuple[str, ...] = ("nguoi gui",)


def is_high_risk(action: str) -> bool:
    """Whether `action` describes an effect that cannot be undone.

    Matched on a diacritic-stripped, lowercased form of the sentence, so
    *"GỬI THƯ"*, *"gửi thư"* and *"gui thu"* are one action described three
    ways rather than three different answers.
    """
    plain = without_diacritics(action)
    for phrase in _NOT_EFFECTS:
        plain = plain.replace(phrase, " ")
    return any(word in plain for word in _HIGH_RISK_WORDS)


def has_high_risk_action(actions: list[str]) -> bool:
    """Whether any of a spec's `actions` is an unrecoverable effect.

    Any, not most: a spec is as risky as its riskiest action, and the one that
    sends the email is not made safe by the four beside it that only read.
    """
    return any(is_high_risk(action) for action in actions)


def without_diacritics(text: str) -> str:
    """`text` lowercased with Vietnamese diacritics dropped.

    Decomposed to base letters plus combining marks, then the marks discarded —
    so `ử` becomes `u` — with `đ` handled by hand, because it is a distinct
    letter rather than a `d` wearing an accent and so survives decomposition.

    Public because the Generator slugifies the same sentences this module reads
    (`src.workflow.fill._slug`), and two copies of a normalisation rule are two
    answers to what one action is called.
    """
    lowered = text.lower().replace("đ", "d")
    decomposed = unicodedata.normalize("NFD", lowered)
    return "".join(character for character in decomposed if not unicodedata.combining(character))
