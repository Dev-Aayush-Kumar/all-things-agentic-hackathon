"""Deterministic investigation pipeline tests."""

import pytest

from atlas.domain.enums import FindingCategory
from atlas.domain.exceptions import DatasetParseError
from atlas.investigation.parser import parse_csv_bytes
from atlas.investigation.pipeline import InvestigationPipeline
from atlas.investigation.prioritize import prioritize_findings
from tests.conftest import FIXTURES_DIR

SURVEY_CSV = FIXTURES_DIR / "survey_quality.csv"


def _run_fixture() -> tuple:
    content = SURVEY_CSV.read_bytes()
    pipeline = InvestigationPipeline()
    frame = pipeline.parse(content)
    stages = pipeline.run_stages(frame)
    result = pipeline.assemble(frame, stages)
    return frame, result


def test_fixture_csv_is_actually_analyzed() -> None:
    frame, result = _run_fixture()
    assert result.profile.row_count == len(frame) == 11
    assert result.profile.column_count == 9
    column_names = [column.name for column in result.profile.columns]
    assert "age" in column_names
    assert "income" in column_names
    age_profile = next(column for column in result.profile.columns if column.name == "age")
    assert age_profile.inferred_type == "numeric"
    assert age_profile.numeric_stats is not None
    assert age_profile.numeric_stats.max == 1500


def test_missing_value_findings_from_fixture() -> None:
    _, result = _run_fixture()
    missing = [
        finding
        for finding in result.findings
        if finding.category == FindingCategory.MISSING_DATA
    ]
    affected = {column for finding in missing for column in finding.affected_columns}
    assert "age" in affected
    assert "income" in affected or "score" in affected
    for finding in missing:
        assert "missing_count" in finding.evidence
        assert finding.affected_row_count
        assert finding.detection_method == "null_count"


def test_duplicate_findings_from_fixture() -> None:
    _, result = _run_fixture()
    duplicates = [
        finding
        for finding in result.findings
        if finding.category == FindingCategory.DUPLICATE_ROWS
    ]
    assert len(duplicates) == 1
    finding = duplicates[0]
    assert finding.affected_row_count == 1
    assert finding.evidence["duplicate_row_count"] == 1
    assert finding.evidence["total_rows"] == 11


def test_additional_anomaly_categories_from_fixture() -> None:
    _, result = _run_fixture()
    categories = {finding.category for finding in result.findings}
    assert FindingCategory.TYPE_FORMAT_ANOMALY in categories
    assert FindingCategory.NUMERIC_OUTLIER in categories
    assert FindingCategory.CONSISTENCY_VIOLATION in categories
    assert FindingCategory.CATEGORICAL_INCONSISTENCY in categories

    outliers = [
        finding
        for finding in result.findings
        if finding.category == FindingCategory.NUMERIC_OUTLIER
    ]
    assert any("age" in finding.affected_columns for finding in outliers)
    assert any(1500 in finding.evidence.get("sample_outliers", []) for finding in outliers)

    consistency = [
        finding
        for finding in result.findings
        if finding.category == FindingCategory.CONSISTENCY_VIOLATION
    ]
    assert any("quantity" in finding.affected_columns for finding in consistency)
    assert any(
        "start_date" in finding.affected_columns and "end_date" in finding.affected_columns
        for finding in consistency
    )


def test_findings_contain_evidence_tied_to_data() -> None:
    _, result = _run_fixture()
    assert result.findings
    for finding in result.findings:
        assert finding.evidence
        assert finding.detection_method
        assert finding.severity
        assert finding.suggested_action


def test_prioritization_is_deterministic() -> None:
    _, first = _run_fixture()
    _, second = _run_fixture()
    first_ids = [(item.category, item.title, item.priority) for item in first.findings]
    second_ids = [(item.category, item.title, item.priority) for item in second.findings]
    assert first_ids == second_ids
    priorities = [item.priority for item in first.findings]
    assert priorities == list(range(1, len(first.findings) + 1))

    shuffled = list(reversed(first.findings))
    reranked = prioritize_findings(
        [item.model_copy(update={"priority": 0}) for item in shuffled]
    )
    assert [item.title for item in reranked] == [item.title for item in first.findings]


def test_invalid_header_csv_fails_to_parse() -> None:
    content = (FIXTURES_DIR / "invalid_header.csv").read_bytes()
    with pytest.raises(DatasetParseError):
        parse_csv_bytes(content)
