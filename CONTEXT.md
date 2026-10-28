# AI Agent Builder

A platform where a person describes a business problem in their own words and
gets back a working agent: interviewed into a specification, compiled into a
workflow, rehearsed in a sandbox, evaluated, approved, and published.

> **Note (2026-10-25):** V2 is the platform's architecture. UC-1's mechanisms
> are being folded into it as capabilities of a described agent, not kept as a
> parallel pipeline — [ADR-0059](docs/adr/0059-hop-nhat-uc1-vao-v2-thanh-nang-luc-data-source.md),
> spec [`docs/specs/data-source-agent.md`](docs/specs/data-source-agent.md).
> UC-1's parallel pipeline was removed from `src/` (DS-21); its words
> (`AgentSpec`, `capability`, `connection`, `data_scope`, `PEP`) survive only in
> history — don't introduce new uses of them.

## Language

### Data Sources

**Data Source**:
Anything an agent can read. Two kinds: a **Knowledge Base** (Documents,
searched by meaning) and a **Database** (a business database, queried by
structure). In the spec, `DataSource.kind` names which, and `ref` points at the
one a person attached.
_Vietnamese_: nguồn dữ liệu
_Avoid_: connection, datasource config

**Database**:
A business database a creator registered as a Data Source: its dialect
(PostgreSQL, SQL Server, MySQL), a DSN held only in the secret store, the views
an agent may read, and its Access Rules. On the platform it points at a sample
or staging copy with the real schema; the production DSN is filled in only
inside an Exported Package.
_Vietnamese_: hệ CSDL
_Avoid_: ERP connection, UC1Connection, external connection (that is a BYODB vector store)

**Access Rules**:
Which rows of which view each position may see — an `identity` (the employee
view, the column the authenticated value matches, the attributes read off it)
and `rules` (position → view → `all` or `where` view column = principal
attribute). Deny by default. Applied in code to every query; a model never
chooses a scope.
_Vietnamese_: quy tắc truy cập
_Avoid_: data_scope, RLS policy, authz

**Principal**:
The authenticated person a run answers to, with the attributes Access Rules
read from the employee view. Comes from authentication (or, in the sandbox,
from the creator's "run as" choice) — never from what was said in the
conversation.
_Vietnamese_: người hỏi
_Avoid_: user (that is a platform account), requester

**Exported Package**:
What a creator downloads to run an approved agent inside a company's own
infrastructure: a fixed runtime, the agent bundle, `access.yaml` and
`runtime.yaml`. The company edits configuration and the auth adapter, never the
runtime.
_Vietnamese_: gói chạy độc lập
_Avoid_: export (alone — the JSON bundle that re-imports into the platform is also an export)

### Retrieval

**Knowledge Base**:
A named shelf of Documents that a `KnowledgeSearch` node is pointed at. What a
node addresses — never a single file. Its contents change over time while the
workflow that names it does not.
_Vietnamese_: tủ tài liệu
_Avoid_: library, document library, corpus, collection, vector store

**Document**:
One file a creator put on a Knowledge Base. The unit a person uploads,
recognises by name, and removes.
_Avoid_: file, source file, blob

**Passage**:
One stretch of a Document handed to a model to answer from, carrying enough of
where it came from to be cited.
_Avoid_: chunk, snippet, fragment, context

**Section Path**:
Where a Passage sits in the hierarchy its author wrote — the breadcrumb shown
when the Passage is cited.
_Avoid_: heading, breadcrumb, location

**Source Ref**:
The string in `KnowledgeSearchConfig.source_ids` and `DataSource.ref` that names
a Knowledge Base. Attached by a person, never invented by a model reading a
description.
_Avoid_: source id, kb id, doc ref

### Workflow
**Data Query**:
The `data_query` node: the model fills a structured query (view, columns,
whitelisted filters, aggregates, order, limit) against one Database; code
builds parameterised SQL and always ANDs the Principal's scope. Every row it
returns is cited (Data Source, view, row key) and marked untrusted for later
steps. Never text-to-SQL.
_Vietnamese_: truy vấn dữ liệu
_Avoid_: SQL node, erp tool

**Collector**:
The `collect` node in front of an action: it reads the bound action's required
parameters off the schema, asks the user for exactly the missing ones, and
passes through for free when there is nothing to ask. Never a prompt or a
condition written by a model — [ADR-0052](docs/adr/0052-node-collect-va-bon-nguyen-tac-meta-agent.md).
_Avoid_: gate, "đã đủ thông tin?" node, extraction step

**Evidence**:
The tool calls and results of one run, as the eval judge read them and as the
eval record stores them beside the verdict.
_Avoid_: trace, log, transcript (that is the whole run; evidence is its tool lines)

### Run ledger
**Run record**:
One `agent_runs` row per leg of one run (V2; older rows from the removed UC-1 pipeline stay readable),
carrying the question, the answer, the transcript and the attributes a PM
groups by (`trigger`, `outcome`, `failure_node_type`, `actions`). The PM's
ledger; `run_costs` stays V2's bill —
[ADR-0053](docs/adr/0053-so-run-dung-chung-va-dashboard-pm-theo-chuan-ai-evaluation.md).
_Avoid_: trace row, run log, cost row (that is the bill)

**Trace code**:
One of the expert's words for why a run failed (`src.pm.taxonomy.TRACE_CODES`:
`missing_lookup`, `fabricated_content`, `tool_failure_hidden`…), chosen from a
list when a person labels a run. Categorical, the team's own, grows by editing
the list.
_Avoid_: error tag, failure reason (that is the note), outcome (that is the runtime's word)

**Label**:
The expert's verdict on one run — `run_labels`: Đạt / Không đạt, trace codes,
a note. Distinct from a **thumbs** (`quality_scores/thumbs_up`, the person
who got the answer) and from the **judge** (`quality_scores/judge_pass`, the
sampled model). Calibration is the judge against the label on the same runs.
_Avoid_: review, score (a label is binary), rating
