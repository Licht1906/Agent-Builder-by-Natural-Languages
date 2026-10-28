-- "ERP mẫu" — the demo company's business database (DS-23, docs/specs/data-source-agent.md §Kịch bản demo).
--
-- Derived from docs/usecase_spec/uc1-ar-assistant/schema.sql (Olist customers/orders, synthetic receivables)
-- with UC-1's database-side security taken out on purpose: no RLS, no per-agent role, no session GUCs, no
-- audit table. In V2 the platform scopes every read in code (a Hệ CSDL's quy tắc truy cập, DS-03/04) and
-- keeps its own audit chain (DS-10); this database only has to hold the data and the views a creator ticks.
--
-- Idempotent: every statement can run again (scripts/seed_erp_mau.py applies it before every seed).

-- ---------------------------------------------------------------------------
-- Base tables — Olist's shape, plus what Olist never had
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS customers (
    customer_id        text PRIMARY KEY,
    customer_unique_id text NOT NULL,
    customer_city      text NOT NULL,
    customer_state     text NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    order_id                 text PRIMARY KEY,
    customer_id              text NOT NULL REFERENCES customers (customer_id),
    order_status             text NOT NULL,
    order_purchase_timestamp timestamptz NOT NULL
);

-- Who works here. `email` is what a Hệ CSDL's identity matches the asker on; `position` keys the rules.
CREATE TABLE IF NOT EXISTS hr_employee (
    employee_code text PRIMARY KEY,
    full_name     text NOT NULL,
    email         text NOT NULL UNIQUE,
    department    text NOT NULL,
    position      text NOT NULL
);

-- Who looks after which customer, and how to reach them. `contact_email` points at a mailbox the person
-- running the demo controls (ERP_MAU_CUSTOMER_MAILBOX); `note` is free text a sales rep typed — one of them
-- carries an instruction a model must not follow.
CREATE TABLE IF NOT EXISTS crm_customer (
    customer_id   text PRIMARY KEY REFERENCES customers (customer_id),
    customer_name text NOT NULL,
    sales_rep_id  text NOT NULL REFERENCES hr_employee (employee_code),
    department    text NOT NULL,
    contact_email text NOT NULL,
    note          text
);

CREATE TABLE IF NOT EXISTS fin_invoice (
    invoice_id            text PRIMARY KEY,
    order_id              text NOT NULL REFERENCES orders (order_id),
    customer_id           text NOT NULL REFERENCES customers (customer_id),
    amount_due            numeric(14,2) NOT NULL CHECK (amount_due >= 0),
    amount_paid           numeric(14,2) NOT NULL DEFAULT 0 CHECK (amount_paid >= 0),
    issued_at             date NOT NULL,
    due_date              date NOT NULL,
    settled_at            date,
    -- Columns no view projects. They are here so the demo can show they are never read, not merely absent.
    bank_account          text,
    tax_id                text,
    credit_limit_internal numeric(14,2)
);

-- ---------------------------------------------------------------------------
-- The views a creator ticks
-- ---------------------------------------------------------------------------

CREATE OR REPLACE VIEW v_employees AS
SELECT email, employee_code, full_name, department, position
FROM hr_employee;

CREATE OR REPLACE VIEW v_ar_open AS
SELECT
    i.invoice_id,
    i.order_id,
    i.customer_id,
    c.customer_name,
    c.sales_rep_id,
    c.department,
    (i.amount_due - i.amount_paid)              AS amount_due,
    i.due_date,
    GREATEST(0, CURRENT_DATE - i.due_date)::int AS days_overdue
FROM fin_invoice i
JOIN crm_customer c ON c.customer_id = i.customer_id
JOIN orders o       ON o.order_id    = i.order_id
WHERE i.settled_at IS NULL
  AND i.amount_due > i.amount_paid
  AND o.order_status <> 'canceled';

COMMENT ON VIEW v_ar_open IS
    'Open receivables. bank_account, tax_id and credit_limit_internal are not projected.';

CREATE OR REPLACE VIEW v_customer_safe AS
SELECT
    c.customer_id,
    c.customer_name,
    c.sales_rep_id,
    c.department,
    c.contact_email,
    cu.customer_city AS city,
    c.note
FROM crm_customer c
JOIN customers cu ON cu.customer_id = c.customer_id;
