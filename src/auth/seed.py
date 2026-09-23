"""The demo accounts a fresh database starts with — one per role.

Both roles are seeded so the platform is usable the moment migrations run: the
approval flow needs an admin to exist before any creator can be reviewed, and a
demo cannot begin with an empty `users` table.

Shared by the Alembic migration and the test suite, which builds its schema from
`Base.metadata` rather than from migrations — one definition, so the two can
never drift about who the demo accounts are.
"""

from src.auth.passwords import hash_password
from src.config import get_settings
from src.db.models import UserRole, UserStatus


def seed_user_rows() -> list[dict[str, object]]:
    """Row values for the seeded accounts, with passwords already hashed.

    Hashing happens at call time from the current settings, so an environment
    that overrides the seed passwords gets *its* passwords, not the defaults
    baked into a migration file.

    No `status` key: these rows predate UC1-01's `status` column by several
    migrations (`1135e617defc` runs long before `9602748a105a` adds it), so
    the column can't be in this dict without breaking that migration's
    `op.bulk_insert`. `ACTIVE` reaches these two rows anyway — the later
    migration adds `status` with `server_default="ACTIVE"`, which Postgres
    backfills onto every existing row, seeded ones included.
    """
    settings = get_settings()
    return [
        {
            "email": settings.seed_creator_email,
            "password_hash": hash_password(settings.seed_creator_password),
            "role": UserRole.CREATOR,
        },
        {
            "email": settings.seed_admin_email,
            "password_hash": hash_password(settings.seed_admin_password),
            "role": UserRole.ADMIN,
        },
    ]


def seed_agent_admin_row() -> dict[str, object]:
    """The one account UC1-01 (#117), FR-03 promises every install starts with:
    already `agent_admin`, already `active`, no manual step and no runtime
    "am I the first account" guess (see `src.auth.registration`'s module
    docstring for why that guess was tried and reverted).

    A separate function from `seed_user_rows()` rather than a third entry in
    it: those two rows are V2's and were written by migration `1135e617defc`,
    several migrations before `status`/`employee_id`/`job_title`/`department`
    existed — adding this row's shape there would break that migration's own
    `op.bulk_insert` against a `users` table that doesn't have those columns
    yet at that point in history. This row is UC-1's, seeded by a later
    migration (`d13a1d1e5b78`) that runs after the columns exist.
    """
    settings = get_settings()
    return {
        "email": settings.seed_agent_admin_email,
        "password_hash": hash_password(settings.seed_agent_admin_password),
        "role": UserRole.AGENT_ADMIN,
        "status": UserStatus.ACTIVE,
        "employee_id": "E001",
        "job_title": "Quản trị hệ thống",
        "department": "IT",
    }


def seed_sales_demo_rows() -> list[dict[str, object]]:
    """The two business personas a UC-1 agent is actually demonstrated to.

    `seed_agent_admin_row` above gives every install someone who can *build* and
    approve an agent; neither it nor the V2 pair gives it anyone who can *use*
    one. A UC-1 spec's `audience.roles` names `sales_rep`/`sales_manager`, and
    the PEP refuses any caller outside it — so on a fresh database the finished,
    deployed agent answers "caller holds no role listed in audience.roles" to
    every account that exists, which reads as a broken agent rather than as a
    missing account.

    `department` is `SALES` on both because `authz.rego`'s audience check reads
    it, and `employee_id` is E102/E201 because the demo *data* is keyed to those
    two — see `Settings.seed_sales_rep_email`'s own note. Both are `ACTIVE`: an
    account seeded as `pending` would need the agent_admin to activate it before
    it could log in, which is a manual step this exists to remove.
    """
    settings = get_settings()
    return [
        {
            "email": settings.seed_sales_rep_email,
            "password_hash": hash_password(settings.seed_sales_rep_password),
            "role": UserRole.SALES_REP,
            "status": UserStatus.ACTIVE,
            "employee_id": "E102",
            "job_title": "Nhân viên kinh doanh",
            "department": "SALES",
        },
        {
            "email": settings.seed_sales_manager_email,
            "password_hash": hash_password(settings.seed_sales_manager_password),
            "role": UserRole.SALES_MANAGER,
            "status": UserStatus.ACTIVE,
            "employee_id": "E201",
            "job_title": "Trưởng nhóm kinh doanh",
            "department": "SALES",
        },
    ]


def seed_pm_row() -> dict[str, object]:
    """The PM control room's own account (ADR-0051), role `pm`, already `active`.

    Its own function for the reason `seed_agent_admin_row` is: it carries the
    UC1-01 columns (`status`, `employee_id`, ...) that V2's `seed_user_rows`
    cannot, and it is written by its own migration (`b3c8e47a9d15`) that runs
    after both the columns and the `PM` enum label exist. `department` is
    `PRODUCT` rather than `IT`: the PM is not the IT team that builds agents
    and not the admin that activates accounts, and nothing about their job is
    inferred from being either.

    `employee_id` deliberately does **not** match the `E{id:03d}` shape
    `src.auth.registration` mints for self-registered accounts: a seeded
    `E010` collides with whichever registration happens to land on user id
    10, and the collision surfaces as a 409 on an unrelated signup.
    """
    settings = get_settings()
    return {
        "email": settings.seed_pm_email,
        "password_hash": hash_password(settings.seed_pm_password),
        "role": UserRole.PM,
        "status": UserStatus.ACTIVE,
        "employee_id": "PM001",
        "job_title": "Quản lý sản phẩm",
        "department": "PRODUCT",
    }
