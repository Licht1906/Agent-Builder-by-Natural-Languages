"""`scope_on` — the one door a `data_query` (DS-04) asks a Hệ CSDL through. DS-03.

`scope_for` trusts its rules to fit the database. Rules are checked when
saved, but a creator can untick a view or re-probe afterwards; `scope_on`
is what keeps that from turning saved rules into a grant on a view nobody
allows any more. No database is touched — the row is built in memory.
"""

from src.datasources.access import All, Deny, DenyCause, Principal, Where
from src.datasources.sources import scope_on
from src.db.models import DatabaseDialect, DatabaseSource

RULES = {
    "identity": {
        "view": "v_employees",
        "match_by": "email",
        "attributes": {"employee_code": "emp_id", "position": "position"},
    },
    "rules": [
        {"position": "Nhân viên KD", "views": {"v_ar_open": {"where": {"sales_rep_id": "employee_code"}}}},
        {"position": "Kế toán trưởng", "views": {"v_ar_open": "all"}},
    ],
}

REP = Principal(match_value="an@cty.vn", attributes={"employee_code": "E102", "position": "Nhân viên KD"})
CHIEF = Principal(match_value="chi@cty.vn", attributes={"employee_code": "E300", "position": "Kế toán trưởng"})


def _source(**overrides: object) -> DatabaseSource:
    fields: dict[str, object] = {
        "owner_id": 1,
        "name": "ERP",
        "dialect": DatabaseDialect.POSTGRESQL,
        "allowed_views": ["v_employees", "v_ar_open", "v_customer_safe"],
        "view_columns": {
            "v_employees": ["email", "emp_id", "position"],
            "v_ar_open": ["customer_id", "sales_rep_id"],
            "v_customer_safe": ["customer_id"],
        },
        "view_row_counts": {},
        "base_tables": [],
        "access_rules": RULES,
    }
    fields.update(overrides)
    return DatabaseSource(**fields)


def test_rules_that_still_fit_are_applied() -> None:
    source = _source()

    assert scope_on(source, REP, "v_ar_open") == Where(pairs=(("sales_rep_id", "E102"),))
    assert scope_on(source, CHIEF, "v_ar_open") == All()


def test_a_source_with_no_rules_denies_everyone() -> None:
    assert scope_on(_source(access_rules=None), CHIEF, "v_ar_open") == Deny(cause=DenyCause.NO_RULES)


def test_a_view_unticked_after_the_rules_were_saved_is_denied() -> None:
    source = _source(allowed_views=["v_employees", "v_customer_safe"])

    assert scope_on(source, CHIEF, "v_ar_open") == Deny(cause=DenyCause.RULES_OUT_OF_DATE)


def test_a_column_the_last_probe_no_longer_has_denies_everyone() -> None:
    """Rules that no longer fit are not partly trusted: one dropped column closes the whole source."""
    source = _source(view_columns={"v_employees": ["email", "emp_id", "position"], "v_ar_open": ["customer_id"]})

    assert scope_on(source, CHIEF, "v_ar_open") == Deny(cause=DenyCause.RULES_OUT_OF_DATE)


def test_a_view_that_is_not_ticked_is_denied_even_with_no_rule_naming_it() -> None:
    assert scope_on(_source(), CHIEF, "fin_invoice") == Deny(cause=DenyCause.VIEW_NOT_ALLOWED)


def test_a_view_ticked_but_not_granted_is_the_engines_deny() -> None:
    assert scope_on(_source(), CHIEF, "v_customer_safe") == Deny(cause=DenyCause.VIEW_NOT_GRANTED)
