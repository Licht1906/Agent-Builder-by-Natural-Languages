-- UC-1 accounts-receivable assistant: least-privilege data surface.
--
-- Built on top of the Olist tables (orders, customers, order_items). Olist has
-- no receivables concept, so fin_invoice is synthesised from orders — this is
-- honest demo data, not a claim that the public dataset contains it.
--
-- The security argument this file makes, in order:
--   1. The agent gets its own NOLOGIN role with SELECT on two views only.
--   2. Those views are security_invoker, so RLS is evaluated as the agent role.
--   3. RLS keys off a per-request GUC set from the CALLER's JWT, not from a
--      connection-level credential. Same connection, same agent, different
--      caller, different rows.
--   4. Sensitive columns never appear in the view at all. Masking is applied
--      in the projection, not left to the model to remember.
--
-- Requires PostgreSQL 15+ for security_invoker views.

-- ---------------------------------------------------------------------------
-- 1. Ownership + synthetic receivables
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS crm_sales_assignment (
    customer_id   text PRIMARY KEY REFERENCES customers (customer_id),
    sales_rep_id  text NOT NULL,
    department    text NOT NULL DEFAULT 'SALES',
    assigned_at   date NOT NULL DEFAULT CURRENT_DATE
);

CREATE INDEX IF NOT EXISTS idx_crm_assignment_rep
    ON crm_sales_assignment (sales_rep_id);

CREATE TABLE IF NOT EXISTS fin_invoice (
    invoice_id      bigserial PRIMARY KEY,
    order_id        text NOT NULL REFERENCES orders (order_id),
    customer_id     text NOT NULL REFERENCES customers (customer_id),
    amount_due      numeric(14,2) NOT NULL CHECK (amount_due >= 0),
    amount_paid     numeric(14,2) NOT NULL DEFAULT 0 CHECK (amount_paid >= 0),
    issued_at       date NOT NULL,
    due_date        date NOT NULL,
    settled_at      date,
    -- Columns the agent must never see. They live here so the demo can prove
    -- they are excluded, not merely absent.
    bank_account    text,
    tax_id          text,
    credit_limit_internal numeric(14,2)
);

CREATE INDEX IF NOT EXISTS idx_fin_invoice_customer
    ON fin_invoice (customer_id) WHERE settled_at IS NULL;

-- ---------------------------------------------------------------------------
-- 2. Per-request identity
--
-- The runtime opens a transaction and issues:
--     SET LOCAL app.principal_id  = '<employee_id from the caller JWT>';
--     SET LOCAL app.department    = '<department from the caller JWT>';
--     SET LOCAL app.scope         = 'own_records' | 'department' | 'all';
--
-- app.scope is written from the OPA decision's row_filter.scope, so the DB and
-- the policy engine cannot drift apart. SET LOCAL dies with the transaction,
-- so a leaked pooled connection carries no identity.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION app_principal_id() RETURNS text
LANGUAGE sql STABLE AS $$
    SELECT nullif(current_setting('app.principal_id', true), '')
$$;

CREATE OR REPLACE FUNCTION app_department() RETURNS text
LANGUAGE sql STABLE AS $$
    SELECT nullif(current_setting('app.department', true), '')
$$;

CREATE OR REPLACE FUNCTION app_scope() RETURNS text
LANGUAGE sql STABLE AS $$
    SELECT coalesce(nullif(current_setting('app.scope', true), ''), 'none')
$$;

-- ---------------------------------------------------------------------------
-- 3. Row-level security on the assignment table
--
-- Every downstream view joins through crm_sales_assignment, so restricting
-- this one table is enough to restrict the whole surface. Fail-closed: if no
-- GUC is set, app_scope() returns 'none' and no policy matches.
-- ---------------------------------------------------------------------------

ALTER TABLE crm_sales_assignment ENABLE ROW LEVEL SECURITY;
ALTER TABLE crm_sales_assignment FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS p_assignment_scope ON crm_sales_assignment;
CREATE POLICY p_assignment_scope ON crm_sales_assignment
FOR SELECT
USING (
    CASE app_scope()
        WHEN 'own_records' THEN sales_rep_id = app_principal_id()
        WHEN 'department'  THEN department   = app_department()
        WHEN 'all'         THEN true
        ELSE false
    END
);

-- ---------------------------------------------------------------------------
-- 4. The only two objects the agent may touch
-- ---------------------------------------------------------------------------

CREATE OR REPLACE VIEW v_ar_open
WITH (security_invoker = true) AS
SELECT
    i.order_id,
    i.customer_id,
    a.sales_rep_id,
    a.department,
    (i.amount_due - i.amount_paid)                        AS amount_due,
    i.due_date,
    GREATEST(0, CURRENT_DATE - i.due_date)::int           AS days_overdue,
    o.order_status
FROM fin_invoice i
JOIN crm_sales_assignment a ON a.customer_id = i.customer_id
JOIN orders o               ON o.order_id    = i.order_id
WHERE i.settled_at IS NULL
  AND i.amount_due > i.amount_paid
  AND o.order_status <> 'canceled';

COMMENT ON VIEW v_ar_open IS
    'Open receivables. bank_account, tax_id and credit_limit_internal are '
    'structurally absent — not filtered, not masked, simply not projected.';

CREATE OR REPLACE VIEW v_customer_safe
WITH (security_invoker = true) AS
SELECT
    c.customer_id,
    initcap(c.customer_city)                              AS customer_name,
    a.sales_rep_id,
    a.department,
    -- Contact email is masked in the projection so the model never holds the
    -- full address. The mail tool resolves the real address server-side from
    -- customer_id; the LLM never needs it.
    regexp_replace(c.customer_id, '^(.{3}).*$', '\1***@masked.local')
                                                          AS contact_email,
    c.customer_city                                       AS city,
    c.customer_state                                      AS state
FROM customers c
JOIN crm_sales_assignment a ON a.customer_id = c.customer_id;

-- ---------------------------------------------------------------------------
-- 5. The agent's role
--
-- NOLOGIN: nobody authenticates as this role directly. The runtime connects as
-- a low-privilege service account and issues SET LOCAL ROLE per transaction, so
-- privileges are scoped to the statement, not to the pooled connection.
-- ---------------------------------------------------------------------------

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agt_ar_assistant_ro') THEN
        CREATE ROLE agt_ar_assistant_ro NOLOGIN;
    END IF;
END
$$;

REVOKE ALL ON ALL TABLES IN SCHEMA public FROM agt_ar_assistant_ro;
REVOKE ALL ON SCHEMA public FROM agt_ar_assistant_ro;

GRANT USAGE  ON SCHEMA public                TO agt_ar_assistant_ro;
GRANT SELECT ON v_ar_open, v_customer_safe   TO agt_ar_assistant_ro;
GRANT SELECT ON crm_sales_assignment         TO agt_ar_assistant_ro;  -- RLS applies
GRANT SELECT (order_id, order_status) ON orders TO agt_ar_assistant_ro;

-- Both views above are `security_invoker = true` (§4), so Postgres checks
-- *this* role's own privileges on the tables they read, not the view owner's —
-- the whole reason that setting exists is so RLS on crm_sales_assignment
-- evaluates as agt_ar_assistant_ro rather than as whoever ran this migration
-- (who, in every environment this ships to, is the Postgres superuser, and a
-- superuser bypasses row-level security unconditionally — FORCE ROW LEVEL
-- SECURITY does not and cannot override that). The tradeoff is that
-- `security_invoker` views need real, if narrow, column grants on their
-- underlying tables: exactly the columns each view actually projects, chosen
-- the same deliberate way `orders` already is above.
GRANT SELECT (order_id, customer_id, amount_due, amount_paid, due_date, settled_at)
    ON fin_invoice TO agt_ar_assistant_ro;
GRANT SELECT (customer_id, customer_city, customer_state)
    ON customers TO agt_ar_assistant_ro;

-- `bank_account`, `tax_id`, `credit_limit_internal` are never granted on
-- fin_invoice, and no column of customers beyond the three above is either —
-- Postgres denies `SELECT *` outright unless every expanded column is
-- privileged, so the unqualified form fails even though six of nine
-- fin_invoice columns are individually readable. Verify with:
--   SET LOCAL ROLE agt_ar_assistant_ro;
--   SELECT * FROM fin_invoice;   -- ERROR: permission denied
--   SELECT order_id, amount_due FROM fin_invoice;   -- works: a granted subset

-- ---------------------------------------------------------------------------
-- 6. Append-only audit log with a hash chain
--
-- Tamper evidence, not tamper prevention. An attacker with table ownership can
-- still rewrite history, but they cannot do so without breaking the chain.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS audit_log (
    seq          bigserial PRIMARY KEY,
    occurred_at  timestamptz NOT NULL DEFAULT now(),
    agent_id     text NOT NULL,
    principal_id text NOT NULL,
    tool         text NOT NULL,
    effect       text NOT NULL CHECK (effect IN ('allow','deny','require_approval')),
    reasons      jsonb NOT NULL DEFAULT '[]',
    action_hash  text NOT NULL,
    payload      jsonb NOT NULL,
    prev_hash    text NOT NULL,
    entry_hash   text NOT NULL
);

CREATE OR REPLACE FUNCTION audit_chain() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    prev text;
BEGIN
    SELECT coalesce(entry_hash, repeat('0', 64)) INTO prev
    FROM audit_log ORDER BY seq DESC LIMIT 1;

    NEW.prev_hash  := coalesce(prev, repeat('0', 64));
    NEW.entry_hash := encode(sha256(convert_to(
        NEW.prev_hash || NEW.agent_id || NEW.principal_id || NEW.tool ||
        NEW.effect || NEW.action_hash || NEW.payload::text, 'UTF8')), 'hex');
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS trg_audit_chain ON audit_log;
CREATE TRIGGER trg_audit_chain BEFORE INSERT ON audit_log
FOR EACH ROW EXECUTE FUNCTION audit_chain();

CREATE RULE audit_no_update AS ON UPDATE TO audit_log DO INSTEAD NOTHING;
CREATE RULE audit_no_delete AS ON DELETE TO audit_log DO INSTEAD NOTHING;

-- Chain verification. Returns the first broken link, or zero rows if intact.
CREATE OR REPLACE VIEW v_audit_integrity AS
SELECT a.seq, a.occurred_at, a.tool
FROM audit_log a
LEFT JOIN audit_log p ON p.seq = a.seq - 1
WHERE a.prev_hash IS DISTINCT FROM coalesce(p.entry_hash, repeat('0', 64))
ORDER BY a.seq
LIMIT 1;
