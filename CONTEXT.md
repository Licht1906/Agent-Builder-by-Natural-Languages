# AI Agent Builder

A platform where a person describes a business problem in their own words and
gets back a working agent: interviewed into a specification, compiled into a
workflow, rehearsed in a sandbox, evaluated, approved, and published.

> **Note (2026-10-26):** the vocabulary below describes the V2 architecture
> that `src/` currently implements. Per [ADR-0046](docs/adr/0046-uc1-architecture-supersedes-v2.md),
> UC-1's architecture (`docs/usecase_spec/uc1-ar-assistant/`) is now the
> platform's build target and uses a different vocabulary (`AgentSpec`,
> `capability`, `connection`, `data_scope`, …) not yet reconciled here. Don't
> assume the terms below still apply without checking ADR-0046 first.

## Language

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
One `agent_runs` row per leg of one run, in either pipeline (V2 or UC-1),
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
