"""The Workflow Generator and the graph it produces — V2's second stage, see #43.

A confirmed Agent Solution Specification arrives; a `WorkflowJSON` at
`workflow_pending_confirmation` leaves. Six modules, in the order the work
happens:

- `templates` — the code-defined catalog of static graph skeletons the Generator
  is allowed to choose from (ADR-0025). Requirement §2 rules out a freehand
  LLM-drawn workflow, and this registry is what "choose, don't draw" means.
- `effects` — whether a sentence the creator wrote describes something that
  cannot be undone. One place, because template selection and (later) tool risk
  assignment must not disagree about the same words.
- `selection` — the model proposes a template, a rule layer may turn the
  proposal down, and no spec is ever refused a workflow.
- `fill` — placeholders become configuration: resources by rule, wording by
  model (ADR-0025 §5).
- `validation` — whether a graph is one the runtime could honestly execute.
  Reported freely on a draft, enforced once at the confirm gate.
- `generator` — the stage gate and the write to `AgentVersion.workflow`.

The rest pick the document up once it is confirmed (#45):

- `documents` — the per-creator library a `source_ids` ref names.
- `knowledge` — where a `KnowledgeSearch` node's passages come from, as a seam
  rather than a hard-wired file read.
- `runtime` — the confirmed graph compiled into a running LangGraph, for the
  linear subset of node types (`IfElse`/`While`/`HumanApproval`/`MCP` are #47).

Five of them are the retrieval pipeline ADR-0045 put behind that seam, in the
order a question travels through them — each one separable, and each one
testable without the next:

- `chunking` — a document becomes passages. The decision nothing downstream can
  undo, since a retriever can only return a chunk somebody already cut.
- `embeddings` — the one seam allowed to turn text into a vector.
- `embedding_budget` — what each model has spent, and which one to use now.
- `index` — those passages and their vectors in Postgres, kept in step with the
  files they were built from.
- `retrieval` — the ranking itself: BM25, cosine, and the fusion of the two.
  Pure functions over text, which is what makes retrieval quality something the
  suite can measure rather than something a reviewer has to take on faith.

The documents themselves live in `src.models.workflow` and
`src.models.node_registry`, beside the other stored schemas rather than in here,
because the canvas and the runtime read them without going through any of this.
"""
