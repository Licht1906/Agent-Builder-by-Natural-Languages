"""Write grants in a drafted quy tắc truy cập — ADR-0094. `fitted_draft` alone: pure, no model, no database."""

from src.datasources.access import Identity
from src.datasources.rule_draft import _PROMPT, _TABLES_EXAMPLE, _TABLES_SECTION, fitted_draft

IDENTITY = Identity(
    view="v_employees", match_by="email", attributes={"employee_code": "emp_id", "position": "position"}
)
VIEWS = {"v_ar_open": ["customer_id", "sales_rep_id"]}
TABLES = {"ar_followups": ["customer_id", "sales_rep_id", "note"]}


def _draft(rules: list[dict[str, object]], tables: dict[str, list[str]] | None = TABLES):
    return fitted_draft(
        {"rules": rules},
        identity=IDENTITY,
        positions=["Nhân viên KD", "Kế toán trưởng"],
        view_columns=VIEWS,
        table_columns=tables,
    )


def test_a_write_grant_is_kept_with_only_what_exists() -> None:
    draft = _draft(
        [
            {
                "position": "nhân viên kd",
                "views": {"v_ar_open": {"where": {"sales_rep_id": "employee_code"}}},
                "tables": {
                    "ar_followups": {
                        "operations": ["insert", "update", "truncate", "insert"],
                        "where": {"sales_rep_id": "employee_code", "owner": "employee_code"},
                    }
                },
            }
        ]
    )

    [rule] = draft.rules.model_dump(mode="json")["rules"]
    assert rule["tables"] == {
        "ar_followups": {"operations": ["insert", "update"], "where": {"sales_rep_id": "employee_code"}}
    }
    assert any("'truncate'" in line for line in draft.dropped)
    assert any("'owner'" in line for line in draft.dropped)


def test_a_write_grant_that_lost_its_filter_is_dropped_never_widened_to_every_row() -> None:
    draft = _draft(
        [
            {
                "position": "Kế toán trưởng",
                "tables": {"ar_followups": {"operations": ["delete"], "where": {"zzz": "employee_code"}}},
            }
        ]
    )

    assert draft.rules.rules == []
    assert any("không còn điều kiện lọc" in line for line in draft.dropped)


def test_every_row_is_kept_only_when_written_out_and_a_rule_may_be_write_only() -> None:
    draft = _draft(
        [{"position": "Kế toán trưởng", "tables": {"ar_followups": {"operations": ["delete"], "all_rows": True}}}]
    )

    assert draft.rules.model_dump(mode="json")["rules"] == [
        {"position": "Kế toán trưởng", "tables": {"ar_followups": {"operations": ["delete"], "all_rows": True}}}
    ]


def test_a_table_not_ticked_for_writing_is_cut_and_said() -> None:
    draft = _draft(
        [
            {
                "position": "Nhân viên KD",
                "views": {"v_ar_open": "all"},
                "tables": {"fin_invoice": {"operations": ["update"], "all_rows": True}},
            }
        ]
    )

    [rule] = draft.rules.model_dump(mode="json")["rules"]
    assert "tables" not in rule
    assert any("'fin_invoice'" in line for line in draft.dropped)


def test_no_writable_table_means_no_write_grant_at_all() -> None:
    draft = _draft(
        [
            {
                "position": "Nhân viên KD",
                "views": {"v_ar_open": "all"},
                "tables": {"ar_followups": {"operations": ["insert"], "all_rows": True}},
            }
        ],
        tables=None,
    )

    assert "tables" not in draft.rules.model_dump(mode="json")["rules"][0]


def test_the_prompt_formats_with_and_without_the_tables_section() -> None:
    common = {"description": "d", "identity_view": "v", "attributes": "a", "positions": "p", "views": "v"}
    plain = _PROMPT.format(**common, tables_section="", tables_example="")
    assert '"tables"' not in plain
    writing = _PROMPT.format(
        **common,
        tables_section=_TABLES_SECTION.format(tables="- ar_followups: note"),
        tables_example=_TABLES_EXAMPLE.format(),
    )
    assert "- ar_followups: note" in writing and '"all_rows": true' in writing and '"tables": {"<bảng>"' in writing
