"""FR-24 at compile time — `verified_objects_for_spec` (`src.workflow.uc1_connections`).

A code review found the compiler never re-checked `allowed_objects` against a
connection's own probe history; this is the join that fix relies on.
"""

from sqlalchemy.orm import Session

from src.agentspec import validate_spec
from src.agentspec.spec import ConnectionType
from src.db.models import UC1Connection, UC1ConnectionProbeStatus
from src.workflow.uc1_connections import verified_objects_for_spec
from tests.test_agentspec.conftest import spec_payload


def _spec_with_connection(*, credential_ref: str):
    return validate_spec(
        spec_payload(
            connections=[
                {
                    "id": "conn_erp",
                    "type": "postgres",
                    "credential_ref": credential_ref,
                    "allowed_objects": ["v_ar_open"],
                    "read_only": True,
                }
            ]
        )
    )


def test_a_successfully_probed_connection_contributes_its_verified_views(db_session: Session) -> None:
    db_session.add(
        UC1Connection(
            name="verify_erp_readonly",
            type=ConnectionType.POSTGRES,
            allowed_objects=["v_ar_open"],
            allowed_hosts=[],
            read_only=True,
            vault_path="conn/verify_erp_readonly",
            credential_ref="vault://conn/verify_erp_readonly",
            created_by_id=_a_user_id(db_session),
            last_probe_status=UC1ConnectionProbeStatus.CONNECTED,
            last_probe_objects=[{"object": "v_ar_open", "row_count": 1084}],
        )
    )
    db_session.commit()

    result = verified_objects_for_spec(db_session, _spec_with_connection(credential_ref="vault://conn/verify_erp_readonly"))

    assert result == {"conn_erp": ["v_ar_open"]}


def test_a_never_probed_connection_contributes_nothing(db_session: Session) -> None:
    result = verified_objects_for_spec(db_session, _spec_with_connection(credential_ref="vault://conn/never_probed"))

    assert result == {}


def test_a_failed_probe_contributes_nothing(db_session: Session) -> None:
    db_session.add(
        UC1Connection(
            name="erp_broken",
            type=ConnectionType.POSTGRES,
            allowed_objects=["v_ar_open"],
            allowed_hosts=[],
            read_only=True,
            vault_path="conn/erp_broken",
            credential_ref="vault://conn/erp_broken",
            created_by_id=_a_user_id(db_session),
            last_probe_status=UC1ConnectionProbeStatus.FAILED,
            last_probe_objects=[],
        )
    )
    db_session.commit()

    result = verified_objects_for_spec(db_session, _spec_with_connection(credential_ref="vault://conn/erp_broken"))

    assert result == {}


def _a_user_id(db_session: Session) -> int:
    from src.db.models import User

    return db_session.query(User.id).limit(1).scalar()
