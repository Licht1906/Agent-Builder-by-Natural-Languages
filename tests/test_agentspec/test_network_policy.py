"""UC1-35 (#147), spec.md §10.4/T5, C1 — `src.agentspec.network_policy`.

`generate_network_policy`/`render_compose_network_override` are pure
functions (no filesystem, no Docker) — the same "policy object first,
rendering second" reasoning the module's own docstring gives, so the rule
itself ("deny-all unless egress is explicitly opted into, allow-list from
connections[]") is tested without ever writing a file. `write_network_policy`/
`revoke_network_policy`'s own filesystem side effects get their own tests
below, against a throwaway directory.
"""

from pathlib import Path

import yaml

from src.agentspec.network_policy import (
    NetworkPolicy,
    generate_network_policy,
    render_compose_network_override,
    revoke_network_policy,
    write_network_policy,
)
from tests.test_agentspec.conftest import spec_payload


def _spec(**overrides):
    from src.agentspec.spec import AgentSpec

    return AgentSpec.model_validate(spec_payload(**overrides))


def _connection(*, allowed_hosts: list[str], conn_id: str = "conn_erp") -> dict:
    return {
        "id": conn_id,
        "type": "postgres",
        "credential_ref": "vault://conn/erp_readonly",
        "allowed_objects": ["v_ar_open"],
        "allowed_hosts": allowed_hosts,
        "read_only": True,
    }


# --------------------------------------------------------------------------- #
# generate_network_policy
# --------------------------------------------------------------------------- #


def test_external_egress_false_is_deny_all() -> None:
    spec = _spec(prohibitions={"external_egress": False})
    policy = generate_network_policy(spec)
    assert policy.deny_all is True


def test_external_egress_true_is_not_deny_all() -> None:
    spec = _spec(prohibitions={"external_egress": True})
    policy = generate_network_policy(spec)
    assert policy.deny_all is False


def test_allowed_hosts_are_collected_across_every_connection() -> None:
    spec = _spec(
        prohibitions={"external_egress": False},
        connections=[
            _connection(conn_id="conn_erp", allowed_hosts=["erp.internal"]),
            _connection(conn_id="conn_smtp", allowed_hosts=["smtp.internal", "erp.internal"]),
        ],
    )
    policy = generate_network_policy(spec)
    assert policy.allowed_hosts == ("erp.internal", "smtp.internal")  # deduped, sorted


def test_no_connections_means_no_allowed_hosts() -> None:
    spec = _spec(prohibitions={"external_egress": False}, connections=[])
    policy = generate_network_policy(spec)
    assert policy.allowed_hosts == ()


# --------------------------------------------------------------------------- #
# render_compose_network_override
# --------------------------------------------------------------------------- #


def test_deny_all_renders_an_internal_network_with_aliases_for_mapped_hosts() -> None:
    """`host_aliases` names which real compose service backs each hostname —
    `allowed_hosts` entries are customer-facing hostnames
    (`smtp.internal.corp`), never a compose service name, so the fragment
    must key `services` by the *real* service and alias the hostname onto
    its membership, not fabricate a service literally named after the host.
    """
    policy = NetworkPolicy(agent_id="agt_ar_assistant", deny_all=True, allowed_hosts=("erp.internal", "smtp.internal"))
    fragment = render_compose_network_override(
        policy, host_aliases={"erp.internal": "db", "smtp.internal": "mailhog"}
    )

    assert fragment["networks"] == {"agt_ar_assistant_net": {"internal": True, "driver": "bridge"}}
    assert fragment["services"] == {
        "db": {"networks": {"agt_ar_assistant_net": {"aliases": ["erp.internal"]}}},
        "mailhog": {"networks": {"agt_ar_assistant_net": {"aliases": ["smtp.internal"]}}},
    }


def test_an_allowed_host_with_no_known_backing_service_gets_no_membership() -> None:
    """Fail-closed: an `allowed_hosts` entry this deployment has no
    `host_aliases` mapping for grants nothing — the safe direction, not a
    guess at what compose service the operator meant.
    """
    policy = NetworkPolicy(agent_id="agt_ar_assistant", deny_all=True, allowed_hosts=("tasks.internal.corp",))
    fragment = render_compose_network_override(policy, host_aliases={})

    assert fragment["networks"] == {"agt_ar_assistant_net": {"internal": True, "driver": "bridge"}}
    assert fragment["services"] == {}


def test_deny_all_with_no_allowed_hosts_still_renders_the_network_with_no_members() -> None:
    """A spec that opts into deny-all but names no allowed host at all — a
    real "this agent reaches nothing" policy, not a reason to skip the
    network's own `internal: true` declaration.
    """
    policy = NetworkPolicy(agent_id="agt_lockdown", deny_all=True, allowed_hosts=())
    fragment = render_compose_network_override(policy)

    assert fragment["networks"] == {"agt_lockdown_net": {"internal": True, "driver": "bridge"}}
    assert fragment["services"] == {}


def test_external_egress_opted_in_renders_nothing() -> None:
    policy = NetworkPolicy(agent_id="agt_open", deny_all=False, allowed_hosts=("anything.example",))
    assert render_compose_network_override(policy, host_aliases={"anything.example": "db"}) == {}


# --------------------------------------------------------------------------- #
# write_network_policy / revoke_network_policy
# --------------------------------------------------------------------------- #


def test_write_network_policy_writes_a_loadable_compose_fragment(tmp_path: Path) -> None:
    spec = _spec(
        prohibitions={"external_egress": False},
        connections=[_connection(allowed_hosts=["erp.internal"])],
    )

    path = write_network_policy(spec, tmp_path, host_aliases={"erp.internal": "db"})

    assert path == tmp_path / "docker-compose.agt_ar_assistant.network.yml"
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert loaded["networks"]["agt_ar_assistant_net"]["internal"] is True
    assert loaded["services"]["db"]["networks"]["agt_ar_assistant_net"]["aliases"] == ["erp.internal"]


def test_write_network_policy_for_egress_opted_in_writes_nothing_and_returns_none(tmp_path: Path) -> None:
    spec = _spec(prohibitions={"external_egress": True})

    result = write_network_policy(spec, tmp_path)

    assert result is None
    assert not (tmp_path / "docker-compose.agt_ar_assistant.network.yml").exists()


def test_redeploying_with_egress_opted_in_removes_a_previously_written_deny_all_file(tmp_path: Path) -> None:
    """A version whose `external_egress` flipped from `false` to `true` on
    redeploy must not leave the old restrictive file behind for compose to
    still pick up.
    """
    write_network_policy(_spec(prohibitions={"external_egress": False}), tmp_path)
    assert (tmp_path / "docker-compose.agt_ar_assistant.network.yml").exists()

    write_network_policy(_spec(prohibitions={"external_egress": True}), tmp_path)

    assert not (tmp_path / "docker-compose.agt_ar_assistant.network.yml").exists()


def test_revoke_network_policy_deletes_the_file(tmp_path: Path) -> None:
    spec = _spec(prohibitions={"external_egress": False}, connections=[_connection(allowed_hosts=["erp.internal"])])
    write_network_policy(spec, tmp_path)

    revoke_network_policy(spec.metadata.id, tmp_path)

    assert not (tmp_path / "docker-compose.agt_ar_assistant.network.yml").exists()


def test_revoke_network_policy_is_a_noop_when_nothing_was_ever_written(tmp_path: Path) -> None:
    revoke_network_policy("agt_never_deployed", tmp_path)  # must not raise
