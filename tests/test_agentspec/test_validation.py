"""UC1-03 — one test per rule the gate enforces.

Seam: `validate_spec` / `validate_drafted_spec`. Nothing here imports the Pydantic
models or the detector directly — a spec is plain data, so the gate *is* the public
interface, and the point of the ticket is that no writer reaches a stored spec
without passing it.

Deliberately one test per rule rather than one per input. Where several inputs
exercise the same rule (an unknown field and a missing one are both "wrong shape",
a DSN and an API key are both "credential-shaped"), they sit inside one test and
the rule is named once.
"""

import pytest

from src.agentspec import SpecValidationError, validate_drafted_spec, validate_spec
from src.agentspec.guardrails import BASELINE_INPUT, BASELINE_OUTPUT
from src.agentspec.spec import Autonomy, InputGuardrail, OutputGuardrail, SpecStatus
from tests.test_agentspec.conftest import spec_payload


def refusal(payload: dict) -> SpecValidationError:
    with pytest.raises(SpecValidationError) as caught:
        validate_spec(payload)
    return caught.value


def test_the_reference_uc1_spec_validates(reference_spec: dict) -> None:
    """AC 1. `agentspec.yaml` is the spec of record — if it does not parse, this module is wrong.

    Parsed from the real file rather than a copy, so drift in the reference spec
    shows up here instead of at compile time.
    """
    spec = validate_spec(reference_spec)

    assert spec.metadata.id == "agt_ar_assistant"
    assert spec.metadata.status is SpecStatus.PENDING_APPROVAL
    assert spec.authority.autonomy is Autonomy.PROPOSE_THEN_ACT
    assert spec.authority.approval_matrix["task.create"].condition is not None
    assert spec.authority.approval_matrix["task.create"].condition.value == "$principal.employee_id"
    assert [connection.credential_ref for connection in spec.connections] == [
        "vault://conn/erp_readonly",
        "vault://conn/smtp_internal",
        "vault://conn/tasktracker",
    ]


def test_a_spec_of_the_wrong_shape_is_refused_and_the_field_is_named() -> None:
    """AC 2. `additionalProperties: false` in both directions, with a path the form can use."""
    unknown = refusal(spec_payload(shadow_budget={"unlimited": True}))
    assert [(problem.code, problem.path) for problem in unknown.problems] == [("unknown_field", "shadow_budget")]

    incomplete = spec_payload()
    del incomplete["authority"]
    assert [(problem.code, problem.path) for problem in refusal(incomplete).problems] == [
        ("missing_field", "authority")
    ]


def test_the_schema_rules_pydantic_cannot_express_are_enforced() -> None:
    """`agentspec.schema.json`'s `allOf`, plus the `propertyNames` constraint on `approval_matrix`.

    Their twin lives in the JSON Schema and cannot enforce itself here, so this is
    the test that notices if the two copies drift apart.
    """
    autonomous = spec_payload(
        authority={"autonomy": "autonomous", "approval_quorum": 1, "budget": {"max_tool_calls_per_session": 40}}
    )
    assert "approval_quorum" in str(refusal(autonomous))

    read_only_writer = spec_payload(
        capabilities=[
            {"tool": "erp.query_receivables", "scope": "read"},
            {"tool": "mail.send_reminder", "scope": "write"},
        ]
    )
    assert "mail.send_reminder" in str(refusal(read_only_writer))

    inert_approval = spec_payload(
        authority={
            "autonomy": "read_only",
            "budget": {"max_tool_calls_per_session": 40},
            "approval_matrix": {"anything at all": {"approver_roles": ["sales_manager"]}},
        }
    )
    assert "approval_matrix" in str(refusal(inert_approval))


def test_a_credential_shaped_string_is_refused_without_being_echoed_back() -> None:
    """AC 3. FR-22 — and the refusal itself must not carry the secret into a log or a response."""
    dsn = spec_payload()
    dsn["metadata"]["description"] = "Kết nối postgres://erp_user:hunter2@db.internal.corp:5432/erp"
    refused = refusal(dsn)
    assert [(problem.code, problem.path) for problem in refused.problems] == [("secret_shape", "metadata.description")]
    assert "hunter2" not in str(refused)
    assert "hunter2" not in repr(refused.problems)

    api_key = spec_payload()
    api_key["prohibitions"]["deny_topics"] = ["sk-proj-Ab3dEf7hIjKlMnOpQrStUvWxYz012345678901234567"]
    assert [problem.path for problem in refusal(api_key).problems] == ["prohibitions.deny_topics[0]"]


def test_the_secret_scan_runs_before_pydantic_can_quote_the_value() -> None:
    """The ordering *is* the protection, not a detail of it.

    A DSN in `credential_ref` also fails the `vault://` pattern — and a pattern
    error quotes its input. Run the scan second and the check built to stop a leak
    becomes the thing that leaks.
    """
    refused = refusal(
        spec_payload(
            connections=[
                {
                    "id": "conn_erp",
                    "type": "postgres",
                    "credential_ref": "postgres://erp_user:hunter2@db.internal.corp:5432/erp",
                    "read_only": True,
                }
            ]
        )
    )

    assert [problem.code for problem in refused.problems] == ["secret_shape"]
    assert "hunter2" not in str(refused)


def test_vault_pointers_and_ordinary_prose_are_not_mistaken_for_secrets(reference_spec: dict) -> None:
    """A detector that refuses `vault://conn/erp_readonly` refuses every real spec."""
    validate_spec(reference_spec)

    prose = spec_payload()
    prose["metadata"]["description"] = (
        "Tra cứu công nợ quá hạn theo phạm vi khách hàng mà người hỏi phụ trách, "
        "soạn email nhắc nợ và tạo task theo dõi."
    )
    validate_spec(prose)


def test_removing_a_baseline_guardrail_is_refused_rather_than_quietly_undone() -> None:
    """AC 4. FR-13 — the user adds guardrails, never removes a baseline one.

    Force-merging here instead would return 200, show the edit as taken, and store
    something else: worse than the refusal it replaces, because nothing reports it.
    """
    kept_input = [guardrail.value for guardrail in BASELINE_INPUT if guardrail is not InputGuardrail.PII_REDACTOR]
    refused = refusal(
        spec_payload(
            guardrails={
                "input": kept_input,
                "output": [guardrail.value for guardrail in BASELINE_OUTPUT],
                "fallback": "escalate_to_human",
            }
        )
    )

    assert [(problem.code, problem.path) for problem in refused.problems] == [
        ("baseline_guardrail_removed", "guardrails.input")
    ]
    assert "pii_redactor" in refused.problems[0].message


def test_the_draft_door_merges_the_baseline_and_still_goes_through_the_gate() -> None:
    """§11.1's force-merge, and the property that makes it safe to have a second door.

    `validate_drafted_spec` skips exactly one rule — the baseline must already be
    present — by unioning it in and then delegating. Everything else the gate does
    still happens, which is why the secret scan below still fires.
    """
    drafted = validate_drafted_spec(
        spec_payload(
            metadata={
                **spec_payload()["metadata"],
                "status": "approved",  # hard rule #6: the model does not name the starting state
            },
            guardrails={
                "input": ["language_filter"],
                "output": ["no_speculation"],
                "fallback": "escalate_to_human",
            },
            clarifications=[
                {
                    "field": "capabilities",
                    "question": "Agent có được gửi email thay người dùng không?",
                    "why": "Quyết định tool mail.send_reminder có mặt hay không",
                }
            ],
        )
    )

    # Union, not overwrite: baseline leads so a version diff shows the author's
    # edits rather than a reshuffle, and what the model added survives.
    assert drafted.spec.guardrails.input == [*BASELINE_INPUT, InputGuardrail.LANGUAGE_FILTER]
    assert drafted.spec.guardrails.output == [*BASELINE_OUTPUT, OutputGuardrail.NO_SPECULATION]
    assert drafted.spec.metadata.status is SpecStatus.DRAFT
    assert [clarification.field for clarification in drafted.clarifications] == ["capabilities"]

    # The merged draft survives the strict gate — the draft door is defined in terms of it.
    validate_spec(drafted.spec.model_dump(mode="json", by_alias=True))

    # And the draft door is not a way around the scan.
    with_secret = spec_payload()
    with_secret["metadata"]["description"] = "postgres://erp_user:hunter2@db.internal.corp:5432/erp"
    with pytest.raises(SpecValidationError) as caught:
        validate_drafted_spec(with_secret)
    assert [problem.code for problem in caught.value.problems] == ["secret_shape"]
