"""The two platform settings an admin owns — see #12 / ADR-0010.

The eval threshold decides how good is good enough; the model allowlist decides
what an agent may be built out of. Both live in the single `system_config` row,
and this module is the only place that reads or writes it.

Being the only reader is what makes "ngưỡng mới có hiệu lực với các lần chạy eval
sau đó" true by construction: `read_config` goes to the row every time and caches
nothing, so there is no copy of the old number for a later run to find. The cost
is one small indexed read per eval run and per spec saved, which is the right
trade for a value whose whole purpose is to change.

The rules in `_validated_*` are here rather than on the request schema because
they are facts about the platform, not about HTTP: a model must have been vetted
by a person, a pass rate is a fraction, and an empty allowlist is an outage
rather than a policy. A second caller — a CLI, a migration, a future settings
screen — gets the same refusals with the same wording.

There is deliberately no rule about the *shape* of the allowlist beyond that. An
admin who allows only cheap models has built a platform where no agent may use
tools, and that is a position they are allowed to take: a spec that tries anyway
is refused by name, step and model (ADR-0001), so nothing about it is silent.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.models import SystemConfig, default_system_config_row
from src.models.agent_spec import AgentSpec
from src.models.model_catalog import ASSIGNABLE_MODELS, MODEL_CATALOG


class ConfigValidationError(ValueError):
    """A configuration value that was refused, carrying the reason it was refused.

    The message is user-facing and says what is wrong with *this* value, not that
    a value was wrong — "giá trị cấu hình không hợp lệ bị từ chối kèm giải thích"
    (#12) is only met if the reader can act on what they are told.
    """


@dataclass(frozen=True, slots=True)
class PlatformConfig:
    """What the platform is currently configured to do. A snapshot, not a handle.

    Frozen, so nothing that reads the configuration can quietly change it: the
    only way to change these values is `update_config`, which is where the rules
    are.
    """

    eval_pass_threshold: float
    """The fraction of eval cases a version must pass. Read by #13 on every run."""

    model_allowlist: tuple[str, ...]
    """Which of `ASSIGNABLE_MODELS` a new spec may name. Never wider than that set."""

    updated_at: datetime


def read_config(db: Session) -> PlatformConfig:
    """The configuration in force right now.

    Re-read every call, deliberately. See the module docstring: a cached
    threshold is a threshold an admin changed and nothing obeyed.
    """
    return _as_config(_config_row(db))


def update_config(
    db: Session,
    *,
    eval_pass_threshold: float | None = None,
    model_allowlist: Sequence[str] | None = None,
) -> PlatformConfig:
    """Change either setting, or both. Returns the configuration as it now stands.

    `None` means "leave this one alone", so a screen that edits one setting does
    not have to send back the other and risk writing a value it read before
    somebody else changed it.

    Everything is validated before anything is written: a request whose allowlist
    is refused must not leave the threshold beside it banked, or a rejected save
    would half-apply and the screen would show a state nobody asked for.
    """
    row = _config_row(db)
    threshold = row.eval_pass_threshold if eval_pass_threshold is None else _validated_threshold(eval_pass_threshold)
    allowlist = tuple(row.model_allowlist) if model_allowlist is None else _validated_allowlist(model_allowlist)

    row.eval_pass_threshold = threshold
    row.model_allowlist = list(allowlist)
    db.commit()
    db.refresh(row)
    return _as_config(row)


def spec_validation_context(db: Session) -> dict[str, object]:
    """The validation context that binds a spec to the live allowlist.

    Hand it to `AgentSpec.model_validate(payload, context=...)` anywhere a *new*
    spec is accepted. Already-saved specs are validated without it, so tightening
    the allowlist never takes a running agent down (ADR-0003 §7, ADR-0010 §4).
    """
    return {"model_allowlist": list(read_config(db).model_allowlist)}


def refuse_withdrawn_models(db: Session, spec: AgentSpec) -> None:
    """Check `spec` against the allowlist as it stands now, and say so if it fails.

    `spec` was valid when it was built, but "valid" then meant the allowlist of
    then. Only the model rule can have changed underneath it — the rest of
    `AgentSpec` is code — so a failure here is always a model an admin withdrew,
    and the message names it.

    Lives here rather than beside its caller because it is a statement about the
    configuration, in the configuration's vocabulary. What a refusal *means* for
    a version is `src.agents.versions`' business.
    """
    try:
        AgentSpec.model_validate(spec.model_dump(mode="json"), context=spec_validation_context(db))
    except ValidationError as exc:
        raise ConfigValidationError(str(exc)) from None


def _config_row(db: Session) -> SystemConfig:
    """The one `system_config` row, seeded on the spot if the schema has none.

    The first migration writes it, so the fallback fires only on a database that
    was built some other way. Seeding beats raising: an admin whose config screen
    500s cannot fix the config, and the values written are the same defaults the
    migration would have written.
    """
    row = db.execute(select(SystemConfig).order_by(SystemConfig.id)).scalars().first()
    if row is None:
        row = SystemConfig(**default_system_config_row())
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


def _as_config(row: SystemConfig) -> PlatformConfig:
    return PlatformConfig(
        eval_pass_threshold=row.eval_pass_threshold,
        model_allowlist=tuple(row.model_allowlist),
        updated_at=row.updated_at,
    )


def _validated_threshold(value: float) -> float:
    """A pass rate is a fraction of the eval cases, so it lives in [0, 1].

    Both ends are allowed: 0 is "record the score and let everything through"
    and 1 is "every case must pass", and each is a position someone might
    genuinely take. A NaN fails both comparisons and is refused here too.
    """
    if not 0.0 <= value <= 1.0:
        raise ConfigValidationError(
            f"Ngưỡng eval phải nằm trong khoảng 0 đến 1 (tỷ lệ case đạt), nhận được {value}"
        )
    return float(value)


def _validated_allowlist(models: Sequence[str]) -> tuple[str, ...]:
    """`models`, de-duplicated in the order given — or the reason it cannot be used."""
    chosen = tuple(dict.fromkeys(models))
    if not chosen:
        raise ConfigValidationError(
            "Allowlist model không được để rỗng: không còn model nào thì không tạo được agent nào"
        )
    for name in chosen:
        _refuse_unassignable(name)
    return chosen


def _refuse_unassignable(name: str) -> None:
    """Two ways a name can fail, and they deserve different sentences.

    A model nobody vetted is a typo or a wish; the meta-agent's model is neither
    — it was vetted, it is simply not on offer here (#12). Saying "chưa được
    kiểm chứng" about it would send an admin off to vet a model that already is.
    """
    if name in ASSIGNABLE_MODELS:
        return
    if name in MODEL_CATALOG:
        raise ConfigValidationError(
            f"Model {name!r} là model của meta-agent, do đội cố định và không cấu hình được ở đây"
        )
    raise ConfigValidationError(
        f"Model {name!r} chưa được kiểm chứng là gọi tool đáng tin nên không thể đưa vào "
        f"allowlist; chỉ chọn trong: {sorted(ASSIGNABLE_MODELS)}"
    )
