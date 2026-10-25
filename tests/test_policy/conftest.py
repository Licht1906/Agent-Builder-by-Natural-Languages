"""A minimal `AuthzRequest` matching the sample shape in `authz.rego`'s header."""

import pytest

from src.agentspec import AgentSpec, validate_spec
from src.agentspec.spec import Scope
from src.policy.pep import Action, AuthzRequest, Principal, Provenance, Session
from tests.test_agentspec.conftest import spec_payload


@pytest.fixture
def deployed_spec() -> AgentSpec:
    payload = spec_payload(metadata={**spec_payload()["metadata"], "status": "deployed"})
    return validate_spec(payload)


@pytest.fixture
def authz_request(deployed_spec: AgentSpec) -> AuthzRequest:
    return AuthzRequest(
        spec=deployed_spec,
        principal=Principal(sub="u1", employee_id="E102", roles=["sales_rep"], department="sales"),
        action=Action(
            hash="abc123",
            tool="erp.query_receivables",
            scope=Scope.READ,
            mode="execute",
            connection_id="conn_erp",
            args={},
        ),
        provenance=Provenance(from_untrusted_data=False),
        session=Session(tool_calls_used=0),
    )
