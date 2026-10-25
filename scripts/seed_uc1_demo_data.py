#!/usr/bin/env python3
"""UC1-15 (#116) — seed Olist-shaped demo data for the AR assistant.

Olist has no receivables concept (spec.md §9), so this script does two things
in order: generate a plausible slice of `customers`/`orders` in the real
Olist dataset's own shape, then synthesize `fin_invoice` and
`crm_sales_assignment` *from* that — honest demo data, not a claim that the
public dataset contains billing information it never had.

Two sales-org personas are pinned to specific `employee_id`s so a live demo
(or a registration through UC1-01's `POST /auth/register`, which accepts an
explicit `employee_id`) can log in as exactly the account this data was built
for:

    E201  sales_manager persona — 23 invoices open and >30 days overdue
    E102  sales_rep persona     — 7 invoices open and >30 days overdue

`E102` is not an arbitrary choice: it is the employee_id spec.md's own §8
sequence diagram uses for the `row_filter{own_records, E102}` -> "7 dòng"
example. Both counts are the ones tickets.md's UC1-15 acceptance criterion
names, and the ones spec.md §15's demo script expects at 4:00 / 4:45.

Every row this script writes is a real, committed row (unlike
`verify_uc1_data_surface.py`, which always rolls back) — this is what a fresh
install's demo data actually looks like, not a check. IDs are `demo-`
prefixed so a re-run can find and replace them: pass `--reset` to delete a
previous run's rows first, or run without it to fail loudly on a conflict
rather than silently duplicating the dataset.

Usage:
    python scripts/seed_uc1_demo_data.py [--reset]
"""

from __future__ import annotations

import argparse
import random
import sys
from dataclasses import dataclass
from datetime import date, timedelta

import psycopg
from psycopg import sql

from src.config import get_settings

#: Fixed seed: the two demo personas' counts are load-bearing (tickets.md's
#: own AC), so this run must produce the same dataset every time, not merely
#: a plausible one.
_RANDOM_SEED = 20260813

MANAGER_EMPLOYEE_ID = "E201"
REP_EMPLOYEE_ID = "E102"
MANAGER_TARGET_OVERDUE_ROWS = 23
REP_TARGET_OVERDUE_ROWS = 7

#: §2.1: "Phòng Kinh doanh có 18 nhân viên (sales_rep) và 3 trưởng nhóm
#: (sales_manager)". REP_EMPLOYEE_ID/MANAGER_EMPLOYEE_ID are one of each.
REP_EMPLOYEE_IDS = [f"E1{n:02d}" for n in range(1, 19)]  # E101..E118
MANAGER_EMPLOYEE_IDS = [f"E2{n:02d}" for n in range(1, 4)]  # E201..E203
DEPARTMENT = "SALES"

assert REP_EMPLOYEE_ID in REP_EMPLOYEE_IDS
assert MANAGER_EMPLOYEE_ID in MANAGER_EMPLOYEE_IDS

#: How many customers each persona's book holds *before* the calibration pass
#: forces the exact overdue count — bigger than the target so the dataset
#: also has non-overdue and settled invoices in the mix, not just the ones
#: the demo query returns.
#: ~68% of a persona's book ends up open (not canceled, not settled) with the
#: weights below, so these are sized at roughly 2x their overdue target —
#: comfortable margin against random variance under a fixed seed, not a
#: number tuned to just barely clear it.
_MANAGER_BOOK_SIZE = 50
_REP_BOOK_SIZE = 20
_OTHER_STAFF_BOOK_SIZE = 14  # per remaining rep/manager, for a realistic-sized dataset

_CITIES = [
    ("Sao Paulo", "SP"), ("Rio de Janeiro", "RJ"), ("Belo Horizonte", "MG"),
    ("Curitiba", "PR"), ("Porto Alegre", "RS"), ("Salvador", "BA"),
    ("Fortaleza", "CE"), ("Brasilia", "DF"), ("Recife", "PE"), ("Campinas", "SP"),
]  # fmt: skip

#: Olist's real order_status vocabulary. `canceled` orders are excluded from
#: `v_ar_open` by construction (schema.sql's `o.order_status <> 'canceled'`).
_ORDER_STATUSES_WEIGHTED = ["delivered"] * 8 + ["shipped"] * 1 + ["canceled"] * 1

_PAYMENT_TERMS_DAYS = 30

#: Comfortably on either side of the 30-day boundary the demo query filters
#: on, so calibrated rows are unambiguously in or out regardless of exactly
#: which day this script runs.
_OVERDUE_DAYS_PAST_DUE_RANGE = (35, 90)
_NOT_OVERDUE_DAYS_PAST_DUE_RANGE = (-10, 20)


@dataclass
class Invoice:
    customer_id: str
    order_id: str
    amount_due: float
    amount_paid: float
    issued_at: date
    due_date: date
    settled_at: date | None


@dataclass
class Order:
    """`order_id`, `status` and `purchased` travel together from generation
    through to whichever of `orders`/`fin_invoice` reads them — bundled so
    `_random_invoice` takes one order, not three loose values.
    """

    order_id: str
    status: str
    purchased: date


def _random_customer(rng: random.Random, index: int) -> tuple[str, str, str, str]:
    customer_id = f"demo-cust-{index:05d}"
    city, state = rng.choice(_CITIES)
    return customer_id, f"{customer_id}-u", city, state


def _random_order(rng: random.Random, index: int) -> Order:
    status = rng.choice(_ORDER_STATUSES_WEIGHTED)
    purchased = date.today() - timedelta(days=rng.randint(40, 150))
    return Order(order_id=f"demo-order-{index:05d}", status=status, purchased=purchased)


def _random_invoice(rng: random.Random, customer_id: str, order: Order) -> Invoice:
    amount_due = round(rng.uniform(80, 3500), 2)
    issued_at = order.purchased
    due_date = issued_at + timedelta(days=_PAYMENT_TERMS_DAYS)
    # Most invoices in the wider dataset are open (unsettled); a minority are
    # already paid, so `v_ar_open`'s `settled_at IS NULL` filter has rows to exclude.
    settled = rng.random() < 0.25
    settled_at = due_date + timedelta(days=rng.randint(1, 15)) if settled else None
    amount_paid = amount_due if settled else round(rng.uniform(0, amount_due * 0.3), 2)
    return Invoice(customer_id, order.order_id, amount_due, amount_paid, issued_at, due_date, settled_at)


def _force_overdue(invoice: Invoice, rng: random.Random, *, overdue: bool) -> Invoice:
    """Recompute `due_date` so this open invoice is unambiguously overdue (or not) by >30 days.

    Only meaningful for an *open* invoice (`settled_at is None`, `amount_paid
    < amount_due`) — the calibration pass below only ever applies this to
    rows it already knows are open.
    """
    days_range = _OVERDUE_DAYS_PAST_DUE_RANGE if overdue else _NOT_OVERDUE_DAYS_PAST_DUE_RANGE
    past_due = rng.randint(*days_range)
    invoice.due_date = date.today() - timedelta(days=past_due)
    invoice.issued_at = invoice.due_date - timedelta(days=_PAYMENT_TERMS_DAYS)
    return invoice


def _is_open(invoice: Invoice) -> bool:
    return invoice.settled_at is None and invoice.amount_paid < invoice.amount_due


def _calibrate_persona(invoices: list[Invoice], rng: random.Random, target_overdue_rows: int) -> None:
    """Force exactly `target_overdue_rows` of this persona's *open* invoices
    to be >30 days overdue, and every other open invoice to unambiguously not be.

    Random generation alone would only land on the exact target by luck —
    this is the deterministic correction pass that makes the count an
    invariant of the seed rather than a hope.
    """
    open_invoices = [inv for inv in invoices if _is_open(inv)]
    if len(open_invoices) < target_overdue_rows:
        raise ValueError(
            f"persona has only {len(open_invoices)} open invoices, needs at least {target_overdue_rows}"
        )
    rng.shuffle(open_invoices)
    for invoice in open_invoices[:target_overdue_rows]:
        _force_overdue(invoice, rng, overdue=True)
    for invoice in open_invoices[target_overdue_rows:]:
        _force_overdue(invoice, rng, overdue=False)


def _fake_sensitive_value(prefix: str, customer_id: str) -> str:
    """Obviously-fake but non-null — schema.sql keeps these columns *in*
    `fin_invoice` on purpose so the demo can prove they never reach a view or
    a query result, which only works if there is something here to leak.
    """
    return f"{prefix}-{customer_id[-8:]}"


class _DatasetBuilder:
    """Accumulates customers/invoices/assignments while books are built one persona at a time.

    A small stateful object rather than a closure over `nonlocal` counters:
    `book_for` is the only thing any caller needs to know about, and its own
    running index lives on `self` instead of being smuggled into an enclosing
    function's local scope.
    """

    def __init__(self, rng: random.Random) -> None:
        self.rng = rng
        self.customers: list[tuple[str, str, str, str]] = []
        self.invoices: list[Invoice] = []
        self.assignment: dict[str, str] = {}
        self._next_index = 1

    def book_for(self, sales_rep_id: str, size: int) -> list[Invoice]:
        book: list[Invoice] = []
        for _ in range(size):
            index = self._next_index
            self._next_index += 1

            customer = _random_customer(self.rng, index)
            self.customers.append(customer)
            customer_id = customer[0]
            self.assignment[customer_id] = sales_rep_id

            order = _random_order(self.rng, index)
            if order.status == "canceled":
                continue  # no invoice for a canceled order — v_ar_open excludes it anyway
            invoice = _random_invoice(self.rng, customer_id, order)
            book.append(invoice)
            self.invoices.append(invoice)
        return book


def build_dataset(rng: random.Random) -> tuple[list[tuple[str, str, str, str]], list[Invoice], dict[str, str]]:
    """Returns (customers, invoices, customer_id -> sales_rep_id)."""
    builder = _DatasetBuilder(rng)
    manager_book = builder.book_for(MANAGER_EMPLOYEE_ID, _MANAGER_BOOK_SIZE)
    rep_book = builder.book_for(REP_EMPLOYEE_ID, _REP_BOOK_SIZE)

    for staff_id in [*MANAGER_EMPLOYEE_IDS, *REP_EMPLOYEE_IDS]:
        if staff_id not in (MANAGER_EMPLOYEE_ID, REP_EMPLOYEE_ID):
            builder.book_for(staff_id, _OTHER_STAFF_BOOK_SIZE)

    _calibrate_persona(manager_book, rng, MANAGER_TARGET_OVERDUE_ROWS)
    _calibrate_persona(rep_book, rng, REP_TARGET_OVERDUE_ROWS)

    return builder.customers, builder.invoices, builder.assignment


def _reset(cur: psycopg.Cursor) -> None:
    cur.execute("DELETE FROM fin_invoice WHERE customer_id LIKE 'demo-cust-%'")
    cur.execute("DELETE FROM crm_sales_assignment WHERE customer_id LIKE 'demo-cust-%'")
    cur.execute("DELETE FROM orders WHERE customer_id LIKE 'demo-cust-%'")
    cur.execute("DELETE FROM customers WHERE customer_id LIKE 'demo-cust-%'")


def _already_seeded(cur: psycopg.Cursor) -> bool:
    cur.execute("SELECT 1 FROM customers WHERE customer_id LIKE 'demo-cust-%' LIMIT 1")
    return cur.fetchone() is not None


def _copy_customers(cur: psycopg.Cursor, customers: list[tuple[str, str, str, str]]) -> None:
    with cur.copy(
        sql.SQL("COPY customers (customer_id, customer_unique_id, customer_city, customer_state) FROM STDIN")
    ) as copy:
        for customer_id, unique_id, city, state in customers:
            copy.write_row((customer_id, unique_id, city, state))


def _copy_orders(
    cur: psycopg.Cursor, customers: list[tuple[str, str, str, str]], invoices_by_customer: dict[str, Invoice]
) -> None:
    with cur.copy(
        sql.SQL("COPY orders (order_id, customer_id, order_status, order_purchase_timestamp) FROM STDIN")
    ) as copy:
        for customer_id, *_rest in customers:
            invoice = invoices_by_customer.get(customer_id)
            order_id = invoice.order_id if invoice else f"canceled-{customer_id}"
            status = "delivered" if invoice else "canceled"
            purchased = invoice.issued_at if invoice else date.today() - timedelta(days=60)
            copy.write_row((order_id, customer_id, status, purchased))


def _copy_fin_invoice(cur: psycopg.Cursor, invoices: list[Invoice], rng: random.Random) -> None:
    with cur.copy(
        sql.SQL(
            "COPY fin_invoice (order_id, customer_id, amount_due, amount_paid, issued_at, due_date, "
            "settled_at, bank_account, tax_id, credit_limit_internal) FROM STDIN"
        )
    ) as copy:
        for inv in invoices:
            copy.write_row((
                inv.order_id,
                inv.customer_id,
                inv.amount_due,
                inv.amount_paid,
                inv.issued_at,
                inv.due_date,
                inv.settled_at,
                _fake_sensitive_value("BANK", inv.customer_id),
                _fake_sensitive_value("TAX", inv.customer_id),
                round(inv.amount_due * rng.uniform(2, 5), 2),
            ))


def _copy_crm_sales_assignment(cur: psycopg.Cursor, assignment: dict[str, str]) -> None:
    with cur.copy(
        sql.SQL("COPY crm_sales_assignment (customer_id, sales_rep_id, department) FROM STDIN")
    ) as copy:
        for customer_id, sales_rep_id in assignment.items():
            copy.write_row((customer_id, sales_rep_id, DEPARTMENT))


def seed(cur: psycopg.Cursor, *, reset: bool) -> None:
    """Generate the dataset and write it — real, committed rows, in FK order."""
    if reset:
        _reset(cur)
    elif _already_seeded(cur):
        raise RuntimeError("demo data already present — re-run with --reset to replace it")

    rng = random.Random(_RANDOM_SEED)
    customers, invoices, assignment = build_dataset(rng)
    invoices_by_customer = {inv.customer_id: inv for inv in invoices}

    _copy_customers(cur, customers)
    _copy_orders(cur, customers, invoices_by_customer)
    _copy_fin_invoice(cur, invoices, rng)
    _copy_crm_sales_assignment(cur, assignment)


def _verify(cur: psycopg.Cursor) -> None:
    """Fail loudly if the seed did not land where the demo needs it — the same
    query `v_ar_open` answers, run directly against the base tables so this
    check does not depend on `agt_ar_assistant_ro`'s grants being wired up yet.
    """
    for label, sales_rep_id, expected in (
        ("manager", MANAGER_EMPLOYEE_ID, MANAGER_TARGET_OVERDUE_ROWS),
        ("rep", REP_EMPLOYEE_ID, REP_TARGET_OVERDUE_ROWS),
    ):
        cur.execute(
            """
            SELECT count(*)
            FROM fin_invoice i
            JOIN crm_sales_assignment a ON a.customer_id = i.customer_id
            JOIN orders o ON o.order_id = i.order_id
            WHERE a.sales_rep_id = %s
              AND i.settled_at IS NULL
              AND i.amount_due > i.amount_paid
              AND o.order_status <> 'canceled'
              AND (CURRENT_DATE - i.due_date) > 30
            """,
            (sales_rep_id,),
        )
        row = cur.fetchone()
        assert row is not None  # COUNT(*) always returns exactly one row
        (actual,) = row
        if actual != expected:
            raise RuntimeError(f"{label} ({sales_rep_id}): expected {expected} overdue rows, seeded {actual}")
        print(f"OK    {label} ({sales_rep_id}): {actual} rows overdue >30 days, as the demo expects")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--reset", action="store_true", help="delete a previous run's demo-* rows first")
    args = parser.parse_args()

    dsn = get_settings().database_url
    with psycopg.connect(dsn, autocommit=False) as conn, conn.cursor() as cur:
        try:
            seed(cur, reset=args.reset)
            _verify(cur)
        except Exception:
            conn.rollback()
            raise
        conn.commit()
    print("\nSeed committed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
