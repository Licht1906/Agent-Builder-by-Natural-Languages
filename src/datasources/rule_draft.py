"""Quy tắc truy cập drafted from words — DS-12 / ADR-0059 §6.

The creator says *"nhân viên chỉ xem khách của mình, trưởng nhóm xem cả
phòng"*; a model turns it into `rules` against the Hệ CSDL as it really is —
the identity the form already holds, the positions the employee view really
contains, the columns of the views that are ticked.

**A draft, never a decision.** Nothing here saves anything. What comes back
is shown in the DS-03 form for the creator to read, change and save through
the same door every rule goes through (`save_access_rules`, which checks it
all over again). A model never decides scope (ADR-0059 §6): it proposes, and a
person who knows the company disposes.

**Whatever the model names that is not there is dropped, and said.** A
position the employee view does not hold, a view that is not ticked, a column
a view does not have, an attribute the identity does not read — each is cut
out of the draft and named in `dropped`. A filter cut down to nothing drops
its whole grant: it is **never** widened to `all`, because a rule that lost
its condition is a rule that would show more, and the one safe direction to
be wrong in is less.
"""

import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from src.datasources.access import AccessRules, Identity, Rule, WhereGrant
from src.db.models import PlatformCallPurpose
from src.evals.model_json import UnreadableModelReplyError, json_object
from src.services.llm import ChatModelFactory
from src.services.model_document import write_document

_EXHAUSTED = "Không đọc được đề xuất quy tắc truy cập sau {attempts} lần thử — bạn tự điền quy tắc, hoặc thử lại sau."

_PROMPT = """\
Bạn giúp một người tạo agent khai QUY TẮC TRUY CẬP cho một Hệ CSDL của công ty: chức vụ nào được xem \
những dòng nào của view nào. Người tạo mô tả bằng lời; bạn dịch lời đó thành JSON. Người tạo sẽ rà lại \
trước khi lưu — bạn chỉ đề xuất.

Người tạo mô tả:
\"\"\"
{description}
\"\"\"

Người hỏi được nhận diện qua view nhân viên {identity_view}. Mỗi người hỏi có các THUỘC TÍNH sau (tên \
thuộc tính → cột trong view nhân viên):
{attributes}

Các CHỨC VỤ có thật trong view nhân viên — chỉ được dùng đúng nguyên văn các tên này:
{positions}

Các VIEW được phép đọc, với cột của từng view:
{views}

Mỗi rule gán cho MỘT chức vụ, trên từng view, một trong hai quyền:
- "all": xem mọi dòng của view — CHỈ khi mô tả nói rõ chức vụ đó xem hết, xem toàn bộ, không giới hạn.
- {{"where": {{"<cột của view>": "<tên thuộc tính của người hỏi>"}}}}: chỉ xem dòng mà cột đó bằng đúng thuộc \
tính của người hỏi. "Khách mình phụ trách" là cột người phụ trách của view bằng mã nhân viên của người hỏi; \
"cả phòng" là cột phòng ban của view bằng phòng ban của người hỏi — đó là một "where", KHÔNG phải "all".

Quy tắc:
- Chức vụ mô tả không nhắc tới thì KHÔNG viết rule — không có rule nghĩa là bị cấm, đúng như mặc định.
- View mô tả không cho chức vụ đó xem thì không ghi vào "views" của chức vụ đó.
- Chỉ dùng chức vụ, view, cột và tên thuộc tính có trong các danh sách trên, đúng chính tả.
- Không bao giờ viết SQL, điều kiện so sánh khác bằng, hay giá trị cố định.

Chỉ trả lời bằng một JSON object, không kèm giải thích:
{{"rules": [{{"position": "...", "views": {{"<view>": "all"}}}}]}}
"""


@dataclass(frozen=True, slots=True)
class RulesDraft:
    """A draft the form can show: `rules` already fitted to the Hệ CSDL, and what was cut out to fit them."""

    rules: AccessRules
    dropped: list[str] = field(default_factory=list)


async def draft_access_rules(
    *,
    description: str,
    identity: Identity,
    positions: Sequence[str],
    view_columns: Mapping[str, Sequence[str]],
    model_factory: ChatModelFactory,
) -> RulesDraft:
    """`description` as draft rules over `identity`, `positions` and `view_columns` — never saved here.

    `view_columns` is the views a rule may grant (ticked, the employee view
    left out) with the columns the last probe saw. A reply that is not JSON
    is asked again (`write_document`); one that is JSON but names things that
    do not exist is not — those are dropped and reported, because asking a
    model to try again does not make a position exist.
    """
    prompt = _PROMPT.format(
        description=description.strip(),
        identity_view=identity.view,
        attributes="\n".join(f"- {name} → cột {column}" for name, column in identity.attributes.items()),
        positions="\n".join(f"- {position}" for position in positions) or "- (view nhân viên chưa có chức vụ nào)",
        views="\n".join(f"- {view}: {', '.join(columns)}" for view, columns in view_columns.items()) or "- (không có)",
    )
    return await write_document(
        build_prompt=lambda: prompt,
        parse=lambda reply: fitted_draft(
            json_object(reply), identity=identity, positions=positions, view_columns=view_columns
        ),
        model_factory=model_factory,
        purpose=PlatformCallPurpose.ACCESS_RULES_DRAFT,
        exhausted=_EXHAUSTED,
        failure=lambda message, _reason: UnreadableModelReplyError(message),
    )


def fitted_draft(
    document: Mapping[str, Any],
    *,
    identity: Identity,
    positions: Sequence[str],
    view_columns: Mapping[str, Sequence[str]],
) -> RulesDraft:
    """The model's `{"rules": [...]}` cut down to what exists, and a sentence for every cut. Pure."""
    real = {_key(position): str(position) for position in positions}
    dropped: list[str] = []
    rules: list[Rule] = []
    seen: set[str] = set()
    for raw in document.get("rules") or []:
        if not isinstance(raw, Mapping):
            continue
        written = str(raw.get("position") or "").strip()
        position = real.get(_key(written))
        if position is None:
            dropped.append(f"chức vụ {written!r} không có trong view nhân viên {identity.view} — bỏ rule này")
            continue
        if position in seen:
            dropped.append(f"chức vụ {position!r} được đề xuất hai lần — giữ lần đầu")
            continue
        views = _fitted_views(raw.get("views"), position, identity=identity, view_columns=view_columns, dropped=dropped)
        if not views:
            dropped.append(f"chức vụ {position!r} không còn quyền xem view nào hợp lệ — bỏ rule, chức vụ này bị cấm")
            continue
        seen.add(position)
        rules.append(Rule(position=position, views=views))
    if not rules and not dropped:
        dropped.append("mô tả không cho chức vụ nào xem gì — mọi người hỏi đều bị cấm cho tới khi có rule")
    return RulesDraft(rules=AccessRules(identity=identity, rules=rules), dropped=dropped)


def _fitted_views(
    raw: object,
    position: str,
    *,
    identity: Identity,
    view_columns: Mapping[str, Sequence[str]],
    dropped: list[str],
) -> dict[str, Any]:
    """One rule's `views`, each grant kept only as far as the Hệ CSDL and the identity can carry it."""
    if not isinstance(raw, Mapping):
        return {}
    views: dict[str, Any] = {}
    for view, grant in raw.items():
        view = str(view)
        if view not in view_columns:
            dropped.append(f"view {view!r} không được phép đọc (quy tắc của {position}) — bỏ quyền này")
            continue
        if grant == "all":
            views[view] = "all"
            continue
        where = grant.get("where") if isinstance(grant, Mapping) else None
        if not isinstance(where, Mapping):
            dropped.append(f"quyền của {position} trên {view} không phải 'all' hay 'where' — bỏ quyền này")
            continue
        kept: dict[str, str] = {}
        for column, attribute in where.items():
            column, attribute = str(column), str(attribute)
            if column not in view_columns[view]:
                dropped.append(f"view {view} không có cột {column!r} (quy tắc của {position}) — bỏ điều kiện này")
            elif attribute not in identity.attributes:
                dropped.append(
                    f"người hỏi không có thuộc tính {attribute!r} (quy tắc của {position} trên {view}) — bỏ điều kiện này"
                )
            else:
                kept[column] = attribute
        if kept:
            views[view] = WhereGrant(where=kept)
        else:
            # Never widened to "all": a filter that lost its condition would show more, not less.
            dropped.append(f"quyền của {position} trên {view} không còn điều kiện lọc nào hợp lệ — bỏ quyền này")
    return views


def _key(position: str) -> str:
    """How a position written by a model meets one read from the database: trimmed, NFC, case-folded."""
    return unicodedata.normalize("NFC", position.strip()).casefold()


__all__ = ["RulesDraft", "draft_access_rules", "fitted_draft"]
