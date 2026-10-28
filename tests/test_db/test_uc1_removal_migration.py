"""The UC-1 pipeline's data goes, on a database that still holds it — DS-21 / ADR-0059 §11.

A database migrated to the revision before `a7d2c4e9f1b3`, given what the UC-1
pipeline wrote — a spec version, a sandbox run, an eval run and approval, a
pending action, a suite keyed by a UC-1 agent id with a case, and its two
audience accounts — then migrated to head:

- every `uc1_*` table and its enum type is gone;
- the UC-1 suite and its case are gone, a V2 suite is untouched;
- the sales accounts are `creator`s, and `userrole` no longer has their labels;
- `agent_runs` written by UC-1 stay, as the PM's history.
"""

from sqlalchemy import create_engine, text

from tests.test_db.conftest import upgrade_to_head
from tests.test_db.test_database_sources_migration import _upgrade_to

#: The revision just before the UC-1 pipeline was dropped.
BEFORE = "e7b4c1d8a2f6"


def test_a_database_holding_uc1_data_migrates_to_head(empty_database_url: str) -> None:
    _upgrade_to(empty_database_url, BEFORE)
    engine = create_engine(empty_database_url)
    with engine.begin() as connection:
        admin = connection.execute(text("SELECT id FROM users WHERE role = 'AGENT_ADMIN' LIMIT 1")).scalar_one()
        rep = connection.execute(text("SELECT id FROM users WHERE role = 'SALES_REP'")).scalar_one()
        agent = connection.execute(text("SELECT id FROM agents ORDER BY id LIMIT 1")).scalar_one()
        connection.execute(
            text(
                "INSERT INTO uc1_agent_spec_versions (agent_id, version, spec, created_by_id) "
                "VALUES ('agt_ar_assistant', 1, '{}', :admin)"
            ),
            {"admin": admin},
        )
        connection.execute(
            text(
                "INSERT INTO uc1_sandbox_runs (agent_id, version, principal_employee_id, thread_id, message, status, "
                "transcript, total_cost_usd) VALUES ('agt_ar_assistant', 1, 'E102', 't', 'hỏi', 'completed', '[]', 0)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO uc1_eval_runs (agent_id, version, passed_count, total_count, pass_rate, case_results) "
                "VALUES ('agt_ar_assistant', 1, 1, 1, 1.0, '[]')"
            )
        )
        connection.execute(
            text("INSERT INTO uc1_version_approvals (agent_id, version, admin_id) VALUES ('agt_ar_assistant', 1, :a)"),
            {"a": admin},
        )
        connection.execute(
            text(
                "INSERT INTO uc1_pending_actions (agent_id, version, tool, args, action_hash, connection_id, "
                "approver_roles, requested_by_id, requested_by_sub, requested_by_employee_id, requested_by_department, "
                "requested_by_roles) VALUES ('agt_ar_assistant', 1, 'mail.send_reminder', '{}', 'h', 'conn_mail', "
                "'[]', :rep, 'sub', 'E102', 'SALES', '[]')"
            ),
            {"rep": rep},
        )
        uc1_suite = connection.execute(
            text("INSERT INTO eval_suites (uc1_agent_id, name) VALUES ('agt_ar_assistant', 'UC-1') RETURNING id")
        ).scalar_one()
        v2_suite = connection.execute(
            text("INSERT INTO eval_suites (agent_id, name) VALUES (:agent, 'V2') RETURNING id"), {"agent": agent}
        ).scalar_one()
        for suite in (uc1_suite, v2_suite):
            connection.execute(
                text(
                    "INSERT INTO eval_cases (suite_id, origin, tags, criterion, input_message, expected) "
                    "VALUES (:suite, 'MANUAL', '[]', 'đúng', 'hỏi', 'đáp')"
                ),
                {"suite": suite},
            )

    upgrade_to_head(empty_database_url)

    with engine.connect() as connection:
        tables = set(connection.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")).scalars())
        types = set(connection.execute(text("SELECT typname FROM pg_type")).scalars())
        suites = connection.execute(text("SELECT name FROM eval_suites ORDER BY id")).scalars().all()
        cases = connection.execute(text("SELECT suite_id FROM eval_cases")).scalars().all()
        roles = (
            connection.execute(
                text("SELECT role::text FROM users WHERE email IN ('sales.rep@demo.local', 'sales.manager@demo.local')")
            )
            .scalars()
            .all()
        )
        labels = connection.execute(text("SELECT unnest(enum_range(NULL::userrole))::text")).scalars().all()
        columns = set(
            connection.execute(
                text("SELECT column_name FROM information_schema.columns WHERE table_name = 'eval_suites'")
            ).scalars()
        )
    engine.dispose()

    assert not {table for table in tables if table.startswith("uc1_")}
    assert not {name for name in types if name.startswith("uc1")}
    assert suites == ["V2"] and cases == [v2_suite]
    assert "uc1_agent_id" not in columns
    assert roles == ["CREATOR", "CREATOR"]
    assert sorted(labels) == ["ADMIN", "AGENT_ADMIN", "CREATOR", "PM"]
