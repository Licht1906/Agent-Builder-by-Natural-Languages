"""The "Xuất gói chạy độc lập" button — one approved version as a zip a company runs itself (DS-19).

    bundle.json           the agent (`export_bundle`) + the columns it was built on (DS-15)
    runtime.yaml          written for this agent: its Hệ CSDL, its models, mail if it sends,
                          documents if it searches — every value a company must change says so
    access.yaml           each Hệ CSDL's quy tắc truy cập (`access-<id>.yaml` when there are several)
    documents/            the shelves it searches, with the platform's extractions (DS-18)
    docker-compose.yml    runtime + its own Postgres + its own Qdrant (+ reranker profile)
    .env.example          the NAME of every secret runtime.yaml requires — never a value
    README.md             fill in the secrets, connect SSO, point at the internal LLM, MySQL notes
    images/               only with "Kèm images cho air-gap": the signed images tar (UC1-34's way)

**Nothing secret travels.** Nothing here reads the secret store: a DSN is a
secret *name* in runtime.yaml and a line `AGENT_SECRET_<NAME>=` in
.env.example, filled in by the company. A creator's Gmail token, a deployment
key, a BYODB credential — none is a column this module reads.

**Only an approved version** (`VersionStatus.APPROVED`): what leaves the
platform to run unattended inside a company is what an admin approved.

`assemble_package` is the pure half — pieces in, a zip out — which the CI
smoke test drives directly; `export_package` gathers the pieces from the
database.
"""

import json
import tempfile
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.agents.package_documents import searched_shelves, write_documents
from src.agents.portability import export_bundle
from src.config import get_settings
from src.datasources.access import AccessRules, to_yaml
from src.datasources.sources import views_built_on
from src.db.models import Agent, AgentVersion, DatabaseSource, VersionStatus
from src.models.node_registry import NodeType
from src.workflow.tools import GMAIL

#: The compose file every package ships — kept as a file so it is read, diffed and validated as one.
COMPOSE_TEMPLATE = Path(__file__).resolve().parents[2] / "deploy" / "package" / "docker-compose.yml"

#: `wire_name(GMAIL, "send_email")`, spelled here so this module does not import the runtime.
MAIL_SEND = f"{GMAIL}.send_email"


class PackageExportError(ValueError):
    """A version that cannot become a package. Message is user-facing."""


@dataclass(frozen=True)
class PackageSource:
    """One Hệ CSDL as the package needs it: what to call it, how to reach it (by secret name), who may see what."""

    id: str
    name: str
    dialect: str
    access_rules: dict[str, Any]

    @property
    def dsn_secret(self) -> str:
        return f"dsn_{self.id}"


def airgap_images() -> Path | None:
    """The signed images tar an admin provided (`package_images_path`), or `None`."""
    configured = get_settings().package_images_path
    path = Path(configured) if configured else None
    return path if path is not None and path.is_file() else None


def export_package(db: Session, agent: Agent, version: AgentVersion, target: Path, *, airgap: bool = False) -> Path:
    """`version` as a package zip at `target` — `PackageExportError` naming why not otherwise."""
    if version.status is not VersionStatus.APPROVED:
        raise PackageExportError("Chỉ version đã duyệt mới xuất được gói chạy độc lập")
    images = airgap_images() if airgap else None
    if airgap and images is None:
        raise PackageExportError(
            "Nền tảng chưa có bộ images air-gap — quản trị viên chạy scripts/package_runtime_images.sh "
            "rồi đặt PACKAGE_IMAGES_PATH"
        )
    bundle = export_bundle(agent, version, database_schema=views_built_on(db, agent.owner_id, version.workflow))
    sources = _sources(db, agent.owner_id, version.workflow)
    with tempfile.TemporaryDirectory() as staging:
        documents: Path | None = None
        if searched_shelves(version.workflow):
            documents = Path(staging) / "documents"
            written = write_documents(db, agent.owner_id, version.workflow, documents)
            if written.problems:
                raise PackageExportError("Không mang được tài liệu theo gói: " + "; ".join(written.problems))
        return assemble_package(target, bundle=bundle, sources=sources, documents=documents, images=images)


def assemble_package(
    target: Path,
    *,
    bundle: dict[str, Any],
    sources: Sequence[PackageSource],
    documents: Path | None = None,
    images: Path | None = None,
) -> Path:
    """The zip, from its pieces. Pure: reads only `documents`, `images` and the compose template."""
    workflow = bundle.get("workflow") or {}
    sends_mail = MAIL_SEND in _mcp_actions(workflow)
    searches = bool(searched_shelves(workflow))
    if searches and documents is None:
        raise PackageExportError("Workflow tìm trong tài liệu nhưng không có thư mục documents để mang theo")
    access_files = {source.id: _access_file(source, len(sources)) for source in sources}
    secrets = _secret_names(sources, sends_mail=sends_mail)
    name = str(bundle.get("name") or "Agent")

    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("bundle.json", json.dumps(bundle, ensure_ascii=False, indent=2))
        archive.writestr(
            "runtime.yaml",
            runtime_yaml(sources, access_files, models=_models(workflow), sends_mail=sends_mail, searches=searches),
        )
        for source in sources:
            archive.writestr(access_files[source.id], _access_yaml(source))
        archive.write(COMPOSE_TEMPLATE, "docker-compose.yml")
        archive.writestr(".env.example", env_example(secrets))
        archive.writestr(
            "README.md",
            readme(name, sources, secrets, sends_mail=sends_mail, searches=searches, airgap=images is not None),
        )
        if documents is not None:
            for path in sorted(documents.rglob("*")):
                if path.is_file():
                    archive.write(path, f"documents/{path.relative_to(documents).as_posix()}")
        if images is not None:
            # Stored, not deflated: a `docker save` tar of compressed layers gains nothing but CPU time.
            archive.write(images, f"images/{images.name}", compress_type=zipfile.ZIP_STORED)
            for signature in (images.with_name(images.name + ".sig"), images.with_name(images.name + ".minisig")):
                if signature.is_file():
                    archive.write(signature, f"images/{signature.name}")
            archive.writestr("images/load_images.sh", LOAD_IMAGES_SH)
    return target


# --------------------------------------------------------------------------- #
# The pieces
# --------------------------------------------------------------------------- #


def _sources(db: Session, owner_id: int, workflow: dict[str, Any] | None) -> list[PackageSource]:
    """Every Hệ CSDL a `data_query` node reads, owned by `owner_id` — refused if one has no rules to ship."""
    wanted = sorted(
        {
            str(node["config"].get("source_id", ""))
            for node in (workflow or {}).get("nodes", [])
            if isinstance(node, dict)
            and node.get("type") == NodeType.DATA_QUERY
            and isinstance(node.get("config"), dict)
        }
    )
    ids = [int(ref) for ref in wanted if ref.isdigit()]
    rows = {
        str(row.id): row
        for row in db.scalars(
            select(DatabaseSource).where(DatabaseSource.id.in_(ids), DatabaseSource.owner_id == owner_id)
        )
    }
    problems = [f"Hệ CSDL {ref} không tồn tại hoặc không phải của chủ agent" for ref in wanted if ref not in rows]
    problems += [f"Hệ CSDL {row.name!r} chưa có quy tắc truy cập" for row in rows.values() if not row.access_rules]
    if problems:
        raise PackageExportError("; ".join(problems))
    return [
        PackageSource(
            id=ref,
            name=rows[ref].name,
            dialect=rows[ref].dialect.value,
            access_rules=dict(rows[ref].access_rules or {}),
        )
        for ref in wanted
    ]


def _access_file(source: PackageSource, count: int) -> str:
    return "access.yaml" if count == 1 else f"access-{source.id}.yaml"


def _access_yaml(source: PackageSource) -> str:
    rules = AccessRules.model_validate(source.access_rules)
    return (
        f"# Quy tắc truy cập của Hệ CSDL {source.name} — ai được xem dòng nào của view nào.\n"
        "# Mặc định cấm: chức vụ hay view không có ở đây thì không thấy gì. Sửa được; gói đọc lại khi khởi động.\n"
        + to_yaml(rules)
    )


def _mcp_actions(workflow: dict[str, Any]) -> set[str]:
    return {
        f"{node['config'].get('tool_id', '')}.{node['config'].get('action', '')}"
        for node in workflow.get("nodes", [])
        if isinstance(node, dict) and node.get("type") == NodeType.MCP and isinstance(node.get("config"), dict)
    }


def _models(workflow: dict[str, Any]) -> list[str]:
    return sorted(
        {
            str(node["config"]["model"])
            for node in workflow.get("nodes", [])
            if isinstance(node, dict) and isinstance(node.get("config"), dict) and node["config"].get("model")
        }
    )


def _secret_names(sources: Sequence[PackageSource], *, sends_mail: bool) -> list[tuple[str, str]]:
    """`(secret name, what it is)` for every secret the written runtime.yaml requires, in reading order."""
    names = [
        ("runtime_db", "URL Postgres riêng của gói, ví dụ postgresql://agent:<AGENT_DB_PASSWORD>@postgres:5432/agent")
    ]
    names += [(source.dsn_secret, f"DSN chỉ-đọc tới Hệ CSDL {source.name} ({source.dialect})") for source in sources]
    names.append(("llm_key", "API key của endpoint LLM nội bộ (điền bất kỳ nếu endpoint không cần)"))
    if sends_mail:
        names.append(("smtp_pass", "mật khẩu tài khoản SMTP gửi mail"))
    return names


def _yaml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def runtime_yaml(
    sources: Sequence[PackageSource],
    access_files: dict[str, str],
    *,
    models: Sequence[str],
    sends_mail: bool,
    searches: bool,
) -> str:
    """The runtime.yaml this agent needs, every placeholder marked — it loads as written (`load_runtime_config`)."""
    lines = [
        "# runtime.yaml — cấu hình của gói. IT công ty sửa file này và access.yaml; không sửa bundle.json.",
        "# Mọi secret nêu dưới đây là BẮT BUỘC: thiếu một cái thì gói không khởi động và nói rõ thiếu gì.",
        "# Giá trị secret điền trong .env (AGENT_SECRET_<TÊN>), không bao giờ ghi vào file này.",
        "",
        "bundle: bundle.json",
        "",
        "database:",
        "  url_secret: runtime_db          # Postgres RIÊNG của gói (service postgres trong docker-compose.yml)",
        "",
    ]
    if sources:
        lines.append(
            "data_sources:                   # các Hệ CSDL agent đọc — trỏ DSN tới tài khoản CHỈ-ĐỌC trên DB thật"
        )
        for source in sources:
            lines += [
                f"  - id: {_yaml_string(source.id)}",
                f"    name: {_yaml_string(source.name)}",
                f"    dialect: {source.dialect}",
                f"    dsn_secret: {source.dsn_secret}",
                f"    access: {access_files[source.id]}",
            ]
        lines.append("")
    else:
        lines += ["data_sources: []", ""]
    lines += [
        "# ⚠ CẢNH BÁO: câu hỏi, các dòng dữ liệu agent đọc được và đoạn tài liệu agent tìm thấy đều được gửi tới",
        "# llm.base_url. Dữ liệu chỉ ở lại trong công ty khi base_url trỏ tới một LLM chạy TRONG công ty.",
        "llm:",
        "  base_url: http://llm.noibo.local/v1   # ĐỔI: endpoint OpenAI-compatible nội bộ (vLLM, Ollama, LiteLLM…)",
        "  api_key_secret: llm_key",
        "  default_model: qwen2.5-32b-instruct   # ĐỔI: model nội bộ dùng khi không có ánh xạ bên dưới",
    ]
    if models:
        lines.append("  models:                       # model agent được eval trên nền tảng -> model nội bộ thay thế")
        lines += [f"    {_yaml_string(model)}: qwen2.5-32b-instruct   # ĐỔI" for model in models]
    lines.append("")
    if searches:
        lines += [
            "# Tài liệu: documents/ được index vào Qdrant của gói ở lần khởi động đầu (lần sau không index lại).",
            "# ⚠ Mọi đoạn tài liệu được gửi tới embedding.base_url — trỏ vào trong công ty, như llm.base_url.",
            "documents: documents",
            "embedding:",
            "  base_url: http://llm.noibo.local/v1   # ĐỔI: endpoint /embeddings OpenAI-compatible",
            "  api_key_secret: llm_key",
            "  model: bge-m3                        # ĐỔI nếu cần; đổi model = index lại toàn bộ",
            "qdrant:",
            "  url: http://qdrant:6333",
            "  collection: agent_documents",
            "reranker:",
            "  enabled: false                       # true + `docker compose --profile reranker up -d` để bật",
            "  url: http://reranker:8001",
            "",
        ]
    lines += [
        "# Ai đang hỏi. trusted_header CHỈ an toàn khi một gateway đã đăng nhập người dùng đứng trước gói",
        "# (docker-compose.yml chỉ mở cổng trên 127.0.0.1). Nối SSO công ty: đổi sang oidc — xem README.md.",
        "auth:",
        "  mode: trusted_header",
        "  header: X-Asker",
        "  # mode: oidc",
        "  # claim: email",
        "  # oidc:",
        "  #   issuer: https://sso.congty.vn/realms/staff",
        "  #   audience: agent-tro-ly",
        "  #   client_id: agent-tro-ly       # public client (PKCE); redirect URI = địa chỉ của gói + /",
        "",
    ]
    if sends_mail:
        lines += [
            "mail:                           # mail agent soạn chỉ được gửi sau khi người hỏi duyệt",
            "  mode: smtp                    # smtp | gmail_oauth | custom — đổi không cần sửa bundle",
            "  smtp:",
            "    host: smtp.congty.vn         # ĐỔI",
            "    port: 587",
            "    from: no-reply@congty.vn     # ĐỔI: mọi mail đi từ địa chỉ này",
            "    user: agent-mailer           # ĐỔI",
            "    password_secret: smtp_pass",
            "    starttls: true",
            "",
        ]
    lines += [
        "secrets:",
        "  backend: env                  # env | file | vault",
        "  env_prefix: AGENT_SECRET_",
        "",
        "trusted_hosts: []               # host DB trong mạng nội bộ mà gói được phép kết nối, ví dụ [erp-db.congty.local]",
        "",
    ]
    return "\n".join(lines)


def env_example(secrets: Sequence[tuple[str, str]]) -> str:
    lines = [
        "# Đổi tên thành .env rồi điền giá trị. File này chỉ có TÊN — gói xuất ra không bao giờ mang giá trị secret.",
        "",
        "# Mật khẩu Postgres riêng của gói (service postgres) — dùng lại trong AGENT_SECRET_RUNTIME_DB.",
        "AGENT_DB_PASSWORD=",
        "",
    ]
    for name, what in secrets:
        lines += [f"# {what}", f"AGENT_SECRET_{name.upper()}=", ""]
    lines += [
        "# Image (mặc định: agent-runtime:latest, agent-reranker:latest — xem images/ nếu gói kèm air-gap)",
        "# AGENT_RUNTIME_IMAGE=registry.congty.local/agent-runtime:<tag>",
        "# Cổng: mặc định chỉ mở trên 127.0.0.1:8080, cho gateway cùng máy",
        "# AGENT_BIND=127.0.0.1",
        "# AGENT_PORT=8080",
        "",
    ]
    return "\n".join(lines)


def readme(
    name: str,
    sources: Sequence[PackageSource],
    secrets: Sequence[tuple[str, str]],
    *,
    sends_mail: bool,
    searches: bool,
    airgap: bool,
) -> str:
    """The steps, for the company's IT — what to fill in, in the order it is needed."""
    sections = [
        f"# {name} — gói chạy độc lập",
        "",
        "Gói này chạy agent trong hạ tầng của công ty: trang chat ở `/`, API ở `/chat`, `/approvals`, `/health`.",
        "Không có màn hình dựng agent nào đi kèm. Agent đọc dữ liệu theo đúng quy tắc truy cập, và mọi mail",
        "agent soạn chỉ được gửi sau khi người hỏi duyệt.",
        "",
        "## 1. Image",
        "",
    ]
    if airgap:
        sections += [
            "Gói kèm `images/` cho máy không có mạng ngoài. Kiểm chữ ký rồi nạp:",
            "",
            "```sh",
            "cd images && ./load_images.sh <public-key>    # cosign hoặc minisign, theo đuôi file chữ ký",
            "```",
        ]
    else:
        sections += [
            "Cần image `agent-runtime` (và `agent-reranker` nếu bật reranker). Lấy từ registry của nhà cung cấp",
            "và đặt `AGENT_RUNTIME_IMAGE` trong `.env`, hoặc xin bản xuất kèm images cho air-gap.",
        ]
    sections += [
        "",
        "## 2. Secret",
        "",
        "```sh",
        "cp .env.example .env",
        "```",
        "",
        "Điền từng dòng trong `.env`. Các secret gói cần:",
        "",
    ]
    sections += [f"- `AGENT_SECRET_{secret.upper()}` — {what}" for secret, what in secrets]
    sections += [
        "",
        "Thiếu một secret thì gói không khởi động và in ra đúng tên secret đó. Có thể dùng `secrets.backend: file`",
        "(Docker/Kubernetes secrets) hoặc `vault` thay cho biến môi trường — xem chú thích trong `runtime.yaml`.",
        "",
        "## 3. Trỏ LLM nội bộ",
        "",
        "Sửa `llm.base_url`, `llm.default_model` và bảng `llm.models` trong `runtime.yaml`. **Câu hỏi và mọi dữ liệu",
        "agent đọc được đều gửi tới `llm.base_url`** — chỉ trỏ ra ngoài công ty nếu chính sách cho phép.",
    ]
    if searches:
        sections += [
            "",
            "Agent tìm trong tài liệu (`documents/`): sửa thêm `embedding.base_url` và `embedding.model`. Lần khởi động",
            "đầu sẽ index tài liệu vào Qdrant của gói; các lần sau không index lại.",
        ]
    sections += [
        "",
        "## 4. Nối SSO",
        "",
        "Mặc định `auth.mode: trusted_header`: gói tin header `X-Asker` mà gateway phía trước đặt, nên cổng chỉ mở",
        "trên `127.0.0.1`. Để nhân viên đăng nhập thẳng bằng SSO (OpenID Connect):",
        "",
        "1. Tạo một **public client** (PKCE, không client secret) trên SSO; redirect URI là địa chỉ của gói + `/`,",
        "   web origin là địa chỉ của gói.",
        "2. Trong `runtime.yaml`: `auth.mode: oidc`, `auth.oidc.issuer`, `auth.oidc.client_id`, và `auth.oidc.audience`",
        "   bằng chính `client_id` (trang chat gửi ID token).",
        "3. `auth.claim` là claim khớp với cột `identity.match_by` trong `access.yaml` (thường là `email`).",
        "",
        "Còn có `jwt` (token công ty tự ký) và `custom` (file `auth_adapter.py` của công ty).",
    ]
    if sources:
        sections += [
            "",
            "## 5. Hệ CSDL",
            "",
            "Trỏ mỗi `AGENT_SECRET_DSN_<id>` tới một tài khoản **chỉ đọc** trên DB thật, và thêm host của DB vào",
            "`trusted_hosts` nếu nó nằm trong mạng nội bộ. Khi khởi động, gói so DB thật với các view/cột agent dùng",
            "và từ chối chạy nếu thiếu, kèm danh sách thiếu gì.",
            "",
        ]
        sections += [f"- `{source.dsn_secret}` — {source.name} ({source.dialect})" for source in sources]
        if any(source.dialect == "mysql" for source in sources):
            sections += [
                "",
                "### Lưu ý MySQL",
                "",
                "- MySQL không có thuộc tính phiên kiểu `SET LOCAL` (Postgres) hay `sp_set_session_context` (SQL Server),",
                "  nên row-level security riêng của DB **không** biết ai đang hỏi. Phạm vi vẫn được gói áp vào từng câu",
                "  truy vấn theo `access.yaml`; đừng dựa vào RLS phía MySQL cho gói này.",
                "- Tên view phân biệt hoa thường tuỳ `lower_case_table_names` của máy chủ — giữ đúng tên như trong",
                "  `access.yaml` và `bundle.json`.",
                "- Tài khoản chỉ-đọc cần quyền `SELECT` trên các view, và `SHOW VIEW` để gói kiểm schema lúc khởi động.",
            ]
    if sends_mail:
        sections += [
            "",
            "## 6. Mail",
            "",
            "Sửa khối `mail` trong `runtime.yaml` (relay SMTP của công ty, địa chỉ `from`). Có thể đổi sang",
            "`gmail_oauth` hoặc `custom` mà không cần sửa bundle. Mail chỉ đi sau khi người hỏi duyệt đúng nội dung.",
        ]
    sections += [
        "",
        "## Chạy",
        "",
        "```sh",
        "docker compose up -d",
        "curl http://127.0.0.1:8080/health",
        "```",
        "",
        "Gói không khởi động thì `docker compose logs runtime` cho biết lý do: secret nào thiếu, view/cột nào",
        "thiếu, endpoint nào không trả lời.",
        "",
    ]
    return "\n".join(sections)


LOAD_IMAGES_SH = """#!/usr/bin/env bash
# Nạp images của gói trên máy không có mạng ngoài (UC1-34): kiểm chữ ký TRƯỚC, rồi mới `docker load`.
#   ./load_images.sh <public-key>      # chữ ký cosign (.sig) hoặc minisign (.minisig) nằm cạnh file .tar
set -euo pipefail
cd "$(dirname "$0")"
[[ $# -eq 1 ]] || { echo "cách dùng: $0 <public-key>" >&2; exit 2; }
KEY="$1"
TAR="$(ls ./*.tar | head -n1)"
if [[ -f "${TAR}.sig" ]]; then
    cosign verify-blob --key "$KEY" --signature "${TAR}.sig" "$TAR"
elif [[ -f "${TAR}.minisig" ]]; then
    minisign -V -p "$KEY" -m "$TAR" -x "${TAR}.minisig"
else
    echo "không có chữ ký cạnh ${TAR} — không nạp image chưa ký" >&2
    exit 1
fi
docker load -i "$TAR"
"""
