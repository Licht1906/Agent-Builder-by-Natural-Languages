from functools import lru_cache
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.models.model_catalog import JUDGE_MODEL, pinned_judge_model

#: Published in `.env.example` and in this file, therefore known to everyone —
#: usable while developing, refused in production by `Settings` below.
DEV_JWT_SECRET = "dev-only-insecure-secret-change-me"

#: Same shape, same reason — the envelope key `src.services.secret_store`
#: derives its AES-256 key from (ADR-0045).
DEV_SECRET_STORE_KEY = "dev-only-insecure-secret-store-key-change-me"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # App
    app_name: str = "Agent Builder"
    app_env: Literal["development", "production", "test"] = "development"
    app_port: int = Field(default=8000, ge=1, le=65535)
    app_host: str = "0.0.0.0"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    #: The zone an agent means by "today".
    #:
    #: A model has no clock — it knows only what its training data ended on — so
    #: an agent asked for "giá vàng hôm nay" resolves "hôm nay" from whatever the
    #: search results happen to show, which is reliably a day behind. The runtime
    #: tells it the date instead, and this is the zone that date is stated in.
    #:
    #: Not UTC: the people this platform serves are in UTC+7, and between 00:00
    #: and 07:00 local the UTC date is still yesterday's — precisely the window
    #: in which "today" being wrong is least likely to be noticed and most
    #: likely to matter.
    app_timezone: str = "Asia/Ho_Chi_Minh"
    # The Next.js frontend and the Vite builder app, in their dev-server default
    # ports. Only consulted for requests a browser makes *directly* to this API —
    # the builder app proxies through Vite in development, so it normally never
    # appears here at all.
    cors_origins: str = "http://localhost:3000,http://localhost:5173"

    # LLM
    openai_api_key: str = ""
    model_name: str = "gpt-4o-mini"
    llm_temperature: float = Field(default=0.7, ge=0.0, le=2.0)

    # Eval judge model (ADR-0054 §4), a dated snapshot since EV-04 (ADR-0069 §9;
    # `src.models.model_catalog.JUDGE_MODEL`). Changing it is choosing a new
    # baseline, not tuning a knob — `eval_runs.judge_model` records which one
    # graded each run, and `src.pm.reports` refuses to diff a run against a
    # baseline that used another.
    judge_model: str = JUDGE_MODEL

    # EV-01 / ADR-0069 §7: how many cases of one eval run are in flight at once.
    # A case is a whole agent run plus a judge call, nearly all of it waiting on
    # a provider, so 8 turns a 100-case gate from ~30 minutes into a few. Bounded
    # because every case in flight holds model calls open against the same keys.
    eval_concurrency: int = 8

    # Database — Postgres only; see docs/adr and issue #3 for why SQLite is not
    # an option (the conversation checkpointer is Postgres-only).
    database_url: str = "postgresql://postgres:postgres@localhost:5432/app_dev"

    # Auth (ticket #4). 32 bytes is the floor for HS256 (RFC 7518 §3.2) — a
    # shorter secret is a weaker signature, so it is rejected at startup rather
    # than warned about.
    jwt_secret: str = Field(default=DEV_JWT_SECRET, min_length=32)
    jwt_expire_minutes: int = Field(default=60, gt=0)

    # Secret store (RAG-19 / ADR-0045). Hashed with SHA-256 into a 32-byte
    # AES-256 key by `src.services.secret_store` — a `min_length` floor here,
    # same as `jwt_secret`, rather than a fixed byte count, so an operator
    # sets one memorable string instead of a base64 blob.
    secret_store_key: str = Field(default=DEV_SECRET_STORE_KEY, min_length=32)

    # Google OAuth — the per-user credential half ADR-0023 named `UserConnection`.
    # Empty by default: the Gmail/Drive connections refuse with "chưa cấu hình"
    # rather than the platform failing to boot on a checkout without them.
    google_client_id: str = ""
    google_client_secret: str = ""
    #: Must match an Authorized redirect URI on the OAuth client in Google Cloud
    #: Console, character for character — Google refuses the exchange otherwise.
    google_oauth_redirect_url: str = "http://localhost:8065/api/v1/user-connections/google/callback"
    #: Where the browser is sent after the callback has stored the tokens — the
    #: screen the person pressed "Kết nối" on, so the flow visibly returns home.
    frontend_url: str = "http://localhost:5173"

    # Seeded demo accounts — one per role, so the app is usable the moment the
    # migrations run. Override the passwords anywhere the app is reachable.
    seed_creator_email: str = "creator@example.com"
    seed_creator_password: str = "creator-dev-password"
    seed_admin_email: str = "admin@example.com"
    seed_admin_password: str = "admin-dev-password"

    # UC1-01 (#117), FR-03: "tài khoản đầu tiên được seed làm agent_admin khi
    # cài đặt" — seeded by migration `d13a1d1e5b78`, the same way seed_admin_*
    # above already is, not minted by whoever happens to be first to call
    # POST /auth/register. Every self-registered account (including a literal
    # first-ever call to that endpoint) is `pending`, no exceptions — see
    # `src.auth.registration.register_user`.
    seed_agent_admin_email: str = "agent-admin@example.com"
    seed_agent_admin_password: str = "agent-admin-dev-password"

    # ADR-0051: the PM control room's own account, role `pm` — seeded by
    # migration `b3c8e47a9d15` the way the others are. Not an admin: the
    # platform admin and the PM are different people with different powers,
    # and the board's levers (pause, decide, thumbs, gate, promote) are the
    # PM's by job.
    seed_pm_email: str = "pm@example.com"
    seed_pm_password: str = "pm-dev-password"

    # LDAP / Active Directory bind (UC1-02 / #118, FR-05 — Should). An empty
    # `ldap_server_url` means "not configured": `POST /auth/ldap-bind` falls
    # back to the same local Argon2id check `POST /auth/login` uses, so turning
    # LDAP off is blanking this field, not a second code path to keep in sync.
    # `ldap_user_dn_template` builds the bind DN from the caller's email, e.g.
    # "uid={email},ou=people,dc=corp,dc=local".
    ldap_server_url: str = ""
    ldap_user_dn_template: str = ""

    # Observability (ticket #11). Both keys empty = no tracing, which is the
    # normal state in tests and local development: the platform records what a
    # run cost either way, and only the trace is lost. Never a startup failure —
    # see `src.services.tracing`.
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"
    # The project the keys belong to — only needed to *link* to a trace
    # (`{host}/project/{id}/traces/{trace_id}`), which the PM board does from
    # a run's row (ADR-0053). Empty = no link, tracing itself unaffected.
    langfuse_project_id: str = ""

    # Retrieval for `KnowledgeSearch` (ADR-0045). No key for the configured
    # provider is not a broken install: the store falls back to lexical-only
    # search, which is what every test run and every checkout without an account
    # uses. What changes is recall, not whether the node works.
    #
    # Which provider is read off the *model name* (`text-embedding-*` = OpenAI,
    # `voyage-*` = Voyage), so there is no second setting that could disagree
    # with the first. OpenAI's key is `openai_api_key`, above.
    voyage_api_key: str = ""
    #: Comma-separated and **tried in order** — which is how "what happens when
    #: this model can no longer be used" is configured (ADR-0045 §8): one entry
    #: means stop using embeddings and fall back to keyword search, several mean
    #: move to the next one first. Entries may name different providers.
    #:
    #: Changing it re-embeds every library by itself, because `ensure_indexed`
    #: stores the model name beside each chunk and treats a different one as a
    #: stale index — vectors from two models are not comparable, not even
    #: approximately. Every entry is asked for `EMBEDDING_DIMENSIONS`-wide
    #: vectors, so no model here needs a migration.
    #:
    #: Voyage first, OpenAI second. The second entry is a real fallback when
    #: `VOYAGE_API_KEY` is absent or its allowance is exhausted; see
    #: `src.workflow.embedding_budget.get_embedder`.
    embedding_model: str = "voyage-4,text-embedding-3-small"
    #: A ceiling on tokens **per model**, or `0` for none. It exists because a
    #: provider's free grant is not something the API can be asked about
    #: (ADR-0045 §8) — Voyage gives 200M per model, OpenAI gives none and simply
    #: bills. `0` is the default now that the default provider is a paid one:
    #: the ledger goes on counting, so spending stays visible, but nothing is
    #: ever retired behind your back.
    embedding_token_allowance: int = Field(default=0, ge=0)

    @property
    def embedding_models(self) -> tuple[str, ...]:
        """`embedding_model` as the ordered list it actually is.

        Parsed here rather than at each call site, so "the first model still
        usable" is answered from one reading of the setting and a trailing comma
        cannot become an empty model name in a request.
        """
        return tuple(name.strip() for name in self.embedding_model.split(",") if name.strip())

    # Vector Store
    chroma_persist_dir: str = "./data/chroma"

    # Qdrant (RAG-01 / ADR-0044 §1). One collection, tenant-scoped by payload
    # filter — see src/services/qdrant.py for why there is no per-KB collection.
    qdrant_url: str = "http://localhost:6333"

    # Embeddings (RAG-02 / ADR-0044 §2) không còn khai báo lại ở đây: stack này
    # dùng CHUNG `embedding_model` ở trên với stack thư viện, và
    # `src.services.embedding` là seam duy nhất gọi tới nó.
    #
    # Chỗ này từng có `ollama_url` cùng một `embedding_model` THỨ HAI mặc định
    # `bge-m3:567m`. Trong một class Pydantic, khai báo sau thắng — nên field ở
    # §ADR-0045 phía trên, cùng danh sách fallback `voyage-4,text-embedding-3-small`
    # mà comment của nó mô tả, là code chết: mọi deployment đều chạy bge-m3 qua
    # Ollama, kể cả nơi không có Ollama nào. Nói cách khác, một biến
    # `EMBEDDING_MODEL` nuôi hai hệ thống con hiểu nó theo hai nghĩa khác nhau.
    # Trên Cloud Run điều đó nghĩa là `httpx.ConnectError` mỗi batch và một
    # knowledge base rỗng báo thành công.
    #
    # `src.services.embedding` giờ lấy embedder từ `src.workflow.embedding_budget`,
    # cùng một nguồn mà `src.workflow.knowledge` hỏi — nên index và search không
    # thể bất đồng về model đang dùng.

    # Reranker (RAG-03 / ADR-0044 §2). `bge-reranker-v2-m3` behind its own
    # FastAPI service — see reranker/app.py for why it is a service of its own.
    reranker_url: str = "http://localhost:8001"
    #: The reranker gets a timeout of its own rather than borrowing
    #: `tool_http_timeout_seconds` (15s), because the right number is a different
    #: one — and here a larger one, not a smaller. 15s is sized for a slow
    #: *external* API; a cross-encoder scoring 40 candidates is slow **local**
    #: work whose duration is a known function of how much CPU it was given:
    #:
    #:     candidates │  2 vCPU │  4 vCPU │  8 vCPU
    #:     ───────────┼─────────┼─────────┼─────────
    #:             10 │ 14.68s  │  7.01s  │  2.83s
    #:             20 │ 25.58s  │ 12.26s  │  5.52s
    #:
    #: (measured on `bge-reranker-v2-m3`, CPU, passages from the 34-page
    #: regulation). Under the borrowed 15s, a 2-vCPU deployment times out on
    #: *every* search and `_rerank` degrades to the unthresholded fused order —
    #: paying for the service in full and getting the result of not having it.
    #: 30s is not a target, it is headroom: the deployment is sized so this is
    #: never reached (see `.github/workflows/deploy.yml`), and reaching it means
    #: the reranker is under-provisioned, which is a thing to fix rather than to
    #: absorb silently.
    reranker_timeout_seconds: float = Field(default=30.0, gt=0)

    # Hybrid retrieval (RAG-10 / ADR-0044 §3, §2). `dense`/`sparse` top_k are
    # each Prefetch's own limit, fused by RRF before reranking — 10 is the
    # candidate count ADR-0044 §2's measured table calls out as the honest
    # default for a CPU-only reranker (~2.2s), not the 20-candidate row
    # (~3.7s) that assumes a GPU every operator does not have. The threshold
    # applies to cross-encoder scores only (never to RRF's own rank score,
    # which is not comparable across queries) — ADR-0044 §2's own worked
    # example scores an irrelevant decoy at +0.003 and a relevant passage at
    # +0.291, so 0.0 already separates them without having to be tuned per corpus.
    #: 20 and not the 10 ADR-0044 §2 settled on, because the measurement that
    #: chose 10 no longer holds. §2 preferred 10 over 20 on reranker latency
    #: alone, on a box where the reranker was slow enough that the difference
    #: looked like noise. Sized properly it is not noise, and the recall is
    #: worth more than the seconds. Measured end to end on the 22-question
    #: regulation set (`eval/rag_benchmark.py --rerank`, hit@8):
    #:
    #:     top_k │ hit@8 │  MRR  │ rerank at 4 vCPU │ at 8 vCPU
    #:     ──────┼───────┼───────┼──────────────────┼───────────
    #:        10 │ 17/22 │ 0.683 │      7.01s       │   2.83s
    #:        20 │ 19/22 │ 0.742 │     12.26s       │   5.52s
    #:
    #: Two questions and 0.06 MRR is what halving the pool costs, and it is the
    #: same two the older note named: an answer sparse ranks ninth is inside a
    #: pool of 20 and outside a pool of 10. Larger is not better without limit —
    #: at 40 the fused list dilutes past where the reranker recovers it.
    #:
    #: **This number is a contract with the reranker's CPU.** Raising it without
    #: raising `--cpu` is how a deployment ends up timing out on every search;
    #: the two are set together, here and in `.github/workflows/deploy.yml`.
    knowledge_dense_top_k: int = 20
    knowledge_sparse_top_k: int = 20
    knowledge_rerank_score_threshold: float = 0.0

    # Extraction (RAG-06 / ADR-0044 §5). Scanned/photographed pages go through
    # a vision model over OpenRouter — a locally-hosted 4B/Q4 VLM was tried and
    # rejected as not accurate enough on Vietnamese scans to be worth indexing.
    # Cached by the file's own SHA-256 (src/services/extraction.py), so this
    # directory is safe to point at a shared/persistent volume.
    #
    # `openrouter_api_key` is now also what the **agent con** authenticates with
    # (§6.3 / `AGENT_MODEL`), not just the extraction VLM — so an empty value here
    # no longer costs you only scanned-page ingest, it costs you every agent chat
    # turn. `src.services.llm` raises on that rather than letting the call fail as
    # an opaque 401 from OpenRouter.
    #
    # Two URLs on purpose, and they must name the same host:
    #   - `openrouter_url` is a **complete endpoint**, POSTed with raw httpx by
    #     `src.services.extraction` (a one-shot vision call, no chat model needed).
    #   - `openrouter_base_url` is the **API root**, handed to `ChatOpenAI`, which
    #     appends `/chat/completions` itself. Passing the full endpoint here gets
    #     you a request to `/chat/completions/chat/completions`.
    openrouter_api_key: str = ""
    openrouter_url: str = "https://openrouter.ai/api/v1/chat/completions"
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    extraction_vlm_model: str = "qwen/qwen3-vl-32b-instruct"
    extraction_cache_dir: str = "./data/extraction_cache"
    # DS-19: the signed images tar `scripts/package_runtime_images.sh` writes —
    # what "Kèm images cho air-gap" puts in an exported package. Empty: the
    # option is offered greyed out, saying an admin has not provided one yet.
    package_images_path: str = ""

    # Ingest cost (RAG-15 / #101). `None` = no soft limit configured, which is
    # the honest default: a ceiling nobody set should not silently start
    # warning. Checked against one owner's calendar-month total in
    # `src.services.ingestion_costs.check_soft_limit`.
    ingestion_cost_soft_limit_usd: float | None = None
    # How many Documents one user may upload per minute, across every Knowledge
    # Base they own — `src.services.upload_rate_limit`. Same window
    # (`WINDOW_SECONDS = 60`) as the Deployment rate limiter (#51 / ADR-0037).
    document_upload_rate_limit_per_user: int = Field(default=20, ge=1)

    # BYODB (RAG-20 / #106). `/external-connections/{id}/test` dials a
    # creator-supplied address for real — the ticket's own "Rate limit /test",
    # so the endpoint cannot be turned into a network-scan throttle-free
    # amplifier. Lower than the upload ceiling: a probe is a single request a
    # person makes by hand while setting up a connection, not a bulk operation.
    external_connection_test_rate_limit_per_user: int = Field(default=10, ge=1)

    # Hệ CSDL (DS-02 / ADR-0059 §2). A staging ERP usually sits on a private
    # network — exactly the addresses `src.services.ssrf_guard` refuses — so an
    # operator lists, comma-separated and spelled as a DSN spells them, the
    # hosts `POST /database-sources/{id}/test` may dial anyway. Empty: public
    # addresses only. The demo's own ERP service (DS-23) is listed here, not
    # exempted in code.
    database_source_trusted_hosts: str = ""

    @property
    def database_source_trusted_host_set(self) -> frozenset[str]:
        """`database_source_trusted_hosts` as the set it is — `embedding_models`'s reason for parsing once."""
        return frozenset(host.strip() for host in self.database_source_trusted_hosts.split(",") if host.strip())

    # No Vault settings (DS-20 / ADR-0059 §9): every platform credential is read
    # through `src.services.secret_store.SecretStore`. An old `.env` still
    # naming VAULT_URL/VAULT_TOKEN is ignored (`extra="ignore"` above).

    # Control plane (UC1-26 / #142, §4.7/§12.4, FR-52). The *one* pull-only door
    # to the vendor: `src.services.control_plane.pull_template` is the only
    # function in this codebase allowed to reach it, and nothing on the
    # request path (agent creation, a chat turn) ever calls it — see that
    # module's own docstring. Empty by default: a platform that never pulls a
    # template makes zero outbound calls here, which is the honest default
    # for §12.4's own "Tạo agent | Không | —" row. `control_plane_public_key`
    # is the vendor's Ed25519 public key, base64-encoded (32 raw bytes) — a
    # pulled artifact whose signature does not verify against it is refused,
    # never applied.
    control_plane_url: str = ""
    control_plane_public_key: str = ""
    control_plane_http_timeout_seconds: float = Field(default=5.0, gt=0)

    # Tools (real implementations; empty = tool raises ToolUnavailableError)
    search_api_key: str = ""
    search_api_url: str = "https://api.tavily.com/search"
    smtp_host: str = ""
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from: str = "no-reply@example.com"
    tool_file_base_dir: str = "./data/workspace"
    tool_http_timeout_seconds: float = Field(default=15.0, gt=0)

    @field_validator("judge_model")
    @classmethod
    def _dated_judge(cls, value: str) -> str:
        """EV-04 / ADR-0069 §9: the judge is a dated snapshot — an alias drifts, and so would every baseline."""
        return pinned_judge_model(value)

    @model_validator(mode="after")
    def _refuse_public_secrets_in_production(self) -> Self:
        """A length floor cannot catch the one weak secret that actually ships.

        `DEV_JWT_SECRET` is long enough to pass any length check and public to
        anyone with the repository, so production refuses it by name.
        """
        if self.app_env == "production" and self.jwt_secret == DEV_JWT_SECRET:
            raise ValueError("JWT_SECRET must be set to a private value in production")
        if self.app_env == "production" and self.secret_store_key == DEV_SECRET_STORE_KEY:
            raise ValueError("SECRET_STORE_KEY must be set to a private value in production")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
