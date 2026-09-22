"""UC1-35 (#147), spec.md §10.4/T5, C1 — `prohibitions.external_egress: false`
"biên dịch thành NetworkPolicy deny-all, allow-list dựng từ connections[]"
(`Prohibitions.external_egress`'s own field description). This module is that
compile step, the same "deploy generates an artifact from `spec.connections[]`"
shape `src.agentspec.roles.provision_agent_role` already has for the Postgres
role — read together with that module's own docstring for why deploy-time
artifact generation belongs next to the spec, not inside `src.compiler`
(which never touches a database or the network layer, by design).

§12.1's own package layout has no `deploy/helm/` content yet ("nêu trong tài
liệu, không làm" — k8s is out of scope), so "NetworkPolicy" here means what
AC-8 actually verifies: a Docker network, `internal: true`, that leaves a
container with no route to anything outside it except whichever other
compose services this function also names as members — Docker's own embedded
per-network DNS resolves only the names attached to the same network, so
"nslookup a domain outside the allowlist fails to resolve" (AC-8) falls out of
that for free, with no separate DNS allow-list mechanism to keep in sync.

`connections[].allowed_hosts` names real, customer-facing hostnames
(`docs/usecase_spec/uc1-ar-assistant/agentspec.yaml`'s own reference spec:
`smtp.internal.corp`, `tasks.internal.corp`) — never the name of a
`docker-compose.yml` service, which is a deployment detail those hostnames
know nothing about. So an allowed host cannot become a network member on its
own: `host_aliases` (a `{hostname: compose_service_name}` mapping an operator
configures once per environment — `settings.network_policy_host_aliases`, the
same "environment fact the caller supplies" shape
`src.compiler.tools.resolve_credential` already is for a connection's DSN) is
what says *which real, already-running compose service answers for that
hostname here*. An `allowed_hosts` entry with no entry in `host_aliases` is
rendered as no member at all — fail-closed, the same direction every other
"unknown" this module reaches defaults to, never a fabricated `services:`
entry naming a service that does not exist (which `docker compose` would
either ignore or error on, and either way grants nothing while looking like
it does).

**Known gap**, the same kind `src.compiler.tools`'s own `_ERP_READONLY_ROLE`
comment already flags for a different reason: §6.2's `runtime` service (one
container per deployed agent) is not built yet — every agent's tool calls run
inside the one shared `backend` container today. Attaching `backend` itself to
a generated network would leak every other agent's allow-list to it (a shared
container cannot be a member of one agent's isolated network without also
being reachable from it), so `write_network_policy` below writes a real,
loadable compose override — proven correct by this module's own tests and by
`docker network inspect` once applied — without attaching `backend` as a
member. Wiring a real per-agent container to its own generated network is the
runtime-per-agent ticket's job, not this one's.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

#: No published stubs (already an unstubbed runtime dep elsewhere — see
#: tests/test_agentspec/conftest.py).
import yaml  # type: ignore[import-untyped]

from src.agentspec.spec import AgentSpec

#: The empty default `render_compose_network_override`/`write_network_policy`
#: take for `host_aliases` — no mapping known means every `allowed_hosts`
#: entry renders as no member, which is deny-all's own safe direction, not a
#: caller-must-remember-to-pass-something footgun.
_NO_HOST_ALIASES: Mapping[str, str] = MappingProxyType({})

#: `docker-compose.<agent_id>.network.yml` — an additional `-f` file for
#: `docker compose up`, the same "profile" shape §12.1 already gives
#: `docker-compose.airgap.yml`. Not merged into the base `docker-compose.yml`:
#: that file is checked in and shared across every agent, while this one is
#: regenerated per agent, per deploy.
_OVERRIDE_FILENAME = "docker-compose.{agent_id}.network.yml"


@dataclass(frozen=True, slots=True)
class NetworkPolicy:
    """One agent's egress rule, independent of any compose file shape — the
    same "policy object first, rendering second" split `src.policy.pep`'s
    own `Decision` makes, so a future Kubernetes `NetworkPolicy` renderer
    (§12.1's own "profile k8s ... không làm" — someday, not this ticket)
    reads this, not `render_compose_network_override`'s YAML shape.
    """

    agent_id: str
    #: `False` only when `prohibitions.external_egress: true` — an agent that
    #: explicitly opted out of egress restriction gets no isolation at all,
    #: the honest rendering of "not our decision to make for this spec".
    deny_all: bool
    #: Every `connections[].allowed_hosts` entry, deduplicated and sorted —
    #: sorted so two compiles of the same spec produce byte-identical output,
    #: which is what makes redeploying a diff-free no-op when nothing changed.
    allowed_hosts: tuple[str, ...] = field(default_factory=tuple)


def generate_network_policy(spec: AgentSpec) -> NetworkPolicy:
    """`spec` → the egress rule its own `prohibitions`/`connections[]` describe.

    Reads `allowed_hosts` off every connection, regardless of `type` —
    `Connection.allowed_hosts`' own field description ("với rest/smtp") names
    the two kinds that actually populate it today, but a connection type this
    function has never heard of naming a host is still a host this agent is
    allowed to reach, not one this function has an opinion about.
    """
    hosts = sorted({host for connection in spec.connections for host in connection.allowed_hosts})
    return NetworkPolicy(
        agent_id=spec.metadata.id,
        deny_all=not spec.prohibitions.external_egress,
        allowed_hosts=tuple(hosts),
    )


def render_compose_network_override(
    policy: NetworkPolicy, *, host_aliases: Mapping[str, str] = _NO_HOST_ALIASES
) -> dict[str, Any]:
    """`policy` → a `docker-compose`-shaped fragment, deny-all by construction
    when `policy.deny_all`: an `internal: true` network whose only members
    are the real compose services `host_aliases` names for an
    `allowed_hosts` entry — a compose service *not* named here has no
    `networks:` entry pointing at it, and therefore no route onto this
    network at all, which is the deny-all itself, not a rule enforced
    against a list. Each such service also gets `aliases: [<host>]` on its
    membership, which is what lets `nslookup <host>` resolve from inside
    this network at all (AC-8's own other half) — plain membership alone
    would only make the service reachable under its own compose name.

    `policy.deny_all is False` renders no network at all (`{}`): nothing to
    additionally restrict beyond whatever the base `docker-compose.yml`
    already grants that agent's own service.
    """
    if not policy.deny_all:
        return {}
    network_name = f"{policy.agent_id}_net"
    services: dict[str, Any] = {}
    for host in policy.allowed_hosts:
        backing_service = host_aliases.get(host)
        if backing_service is None:
            continue  # no known real service answers for this host here — fail-closed, see module docstring
        services[backing_service] = {"networks": {network_name: {"aliases": [host]}}}
    return {
        "networks": {network_name: {"internal": True, "driver": "bridge"}},
        "services": services,
    }


def write_network_policy(
    spec: AgentSpec, output_dir: Path, *, host_aliases: Mapping[str, str] = _NO_HOST_ALIASES
) -> Path | None:
    """FR-34's own deploy-time side effect, `provision_agent_role`'s sibling:
    write `spec`'s network policy as a loadable compose override under
    `output_dir`, returning the path written — or `None` when
    `render_compose_network_override` produced nothing to write
    (`external_egress: true`), so a caller can tell "no restriction" apart
    from "restriction not written yet" without inspecting the filesystem.
    """
    policy = generate_network_policy(spec)
    fragment = render_compose_network_override(policy, host_aliases=host_aliases)
    path = output_dir / _OVERRIDE_FILENAME.format(agent_id=spec.metadata.id)
    if not fragment:
        path.unlink(missing_ok=True)
        return None
    output_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(fragment, sort_keys=True), encoding="utf-8")
    return path


def revoke_network_policy(agent_id: str, output_dir: Path) -> None:
    """`suspend_agent_role`'s sibling (FR-35): delete `agent_id`'s override so
    the next `docker compose up` no longer loads it — the same "no file, no
    grant" property `write_network_policy` already gives `external_egress:
    true`, reached here by suspension instead of by the spec's own field.
    """
    (output_dir / _OVERRIDE_FILENAME.format(agent_id=agent_id)).unlink(missing_ok=True)
