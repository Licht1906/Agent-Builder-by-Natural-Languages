"""A Hệ CSDL is a data source the creator attaches, not one the model names — DS-01 / ADR-0059 §2.

`database` joins the two kinds that address a real resource: its `ref` is a
Hệ CSDL id, chosen by a person on the spec screen, and `None` for the whole
interview — the same rule `uploaded_file` lives by.
"""

import pytest
from pydantic import ValidationError

from src.models.solution_spec import (
    ADDRESSABLE_DATA_KINDS,
    AgentSolutionSpecification,
    DataSource,
    DataSourceKind,
)
from src.solution.draft import SpecDraft

#: A spec as stored before `database` existed — what every row on the dev
#: database looks like today.
OLD_SPEC = {
    "goal": "Trả lời khiếu nại của khách",
    "users": ["Nhân viên chăm sóc khách hàng"],
    "data": [
        {"name": "Hộp thư hỗ trợ", "kind": "connected_account", "ref": "conn://gmail-1"},
        {"name": "Sổ tay xử lý khiếu nại", "kind": "uploaded_file", "ref": None},
    ],
    "actions": ["Soạn thư trả lời"],
    "expected_outcome": ["Một thư trả lời đã soạn sẵn"],
    "success_criteria": [{"text": "Thư nêu đúng mã đơn hàng", "category": "correctness"}],
    "prohibitions": ["Không tự ý hứa hoàn tiền"],
}


def test_a_database_source_with_no_ref_yet_is_valid() -> None:
    source = DataSource(name="ERP công nợ", kind="database", ref=None)

    assert source.kind is DataSourceKind.DATABASE
    assert source.ref is None


def test_a_database_source_addresses_the_database_it_was_attached_to() -> None:
    assert DataSourceKind.DATABASE in ADDRESSABLE_DATA_KINDS
    assert DataSource(name="ERP công nợ", kind="database", ref="12").ref == "12"


def test_a_ref_on_a_kind_with_nothing_to_point_at_is_still_refused() -> None:
    with pytest.raises(ValidationError, match="không trỏ tới tài nguyên"):
        DataSource(name="Tìm trên web", kind="web", ref="12")


def test_the_interview_never_keeps_a_ref_on_a_database_source() -> None:
    draft = SpecDraft.read({"data": [{"name": "ERP công nợ", "kind": "database", "ref": "db-the-model-made-up"}]})

    assert [(source.kind, source.ref) for source in draft.data] == [(DataSourceKind.DATABASE, None)]


def test_a_spec_with_an_unattached_database_source_validates() -> None:
    spec = AgentSolutionSpecification.model_validate(
        {**OLD_SPEC, "data": [{"name": "ERP công nợ", "kind": "database", "ref": None}]}
    )

    assert spec.data[0].kind is DataSourceKind.DATABASE


def test_a_spec_written_before_database_existed_still_reads() -> None:
    spec = AgentSolutionSpecification.model_validate(OLD_SPEC)

    assert [source.kind for source in spec.data] == [DataSourceKind.CONNECTED_ACCOUNT, DataSourceKind.UPLOADED_FILE]
