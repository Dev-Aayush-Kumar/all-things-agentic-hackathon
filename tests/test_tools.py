"""Constrained investigation tools."""

from atlas.investigation.parser import parse_csv_bytes
import pytest

from atlas.agent.tools import (
    ANALYZE_MISSING,
    INSPECT_COLUMN,
    PROFILE_DATASET,
    ToolContext,
    ToolSecurityError,
    invoke_tool,
)
from tests.conftest import FIXTURES_DIR


def _context(path, dataset_id="ds-1") -> ToolContext:
    frame = parse_csv_bytes(path.read_bytes())
    return ToolContext(dataset_id=dataset_id, original_filename=path.name, frame=frame)


def test_profile_tool_returns_structured_evidence() -> None:
    context = _context(FIXTURES_DIR / "survey_quality.csv")
    result = invoke_tool(context, PROFILE_DATASET)
    assert result.profile is not None
    assert result.observed_facts["row_count"] == 11
    assert result.observed_facts["column_count"] == 9
    assert "columns" in result.observed_facts


def test_missing_tool_returns_findings_from_real_data() -> None:
    context = _context(FIXTURES_DIR / "survey_quality.csv")
    result = invoke_tool(context, ANALYZE_MISSING)
    assert result.findings
    assert result.observed_facts["finding_count"] == len(result.findings)


def test_unknown_tool_is_rejected() -> None:
    context = _context(FIXTURES_DIR / "clean_numeric.csv")
    with pytest.raises(ToolSecurityError, match="not an allowed"):
        invoke_tool(context, "run_shell")
    with pytest.raises(ToolSecurityError, match="not an allowed"):
        invoke_tool(context, "eval_python")


def test_disallowed_arguments_are_rejected() -> None:
    context = _context(FIXTURES_DIR / "clean_numeric.csv")
    with pytest.raises(ToolSecurityError, match="disallowed arguments"):
        invoke_tool(context, PROFILE_DATASET, path="/etc/passwd")
    with pytest.raises(ToolSecurityError, match="disallowed arguments"):
        invoke_tool(context, INSPECT_COLUMN, column_name="age", code="print(1)")


def test_inspect_column_is_bound_to_context_frame() -> None:
    context = _context(FIXTURES_DIR / "clean_numeric.csv")
    result = invoke_tool(context, INSPECT_COLUMN, column_name="age")
    assert result.observed_facts["column_name"] == "age"
    assert result.observed_facts["row_count"] == 10
    with pytest.raises(Exception, match="not in the mission dataset"):
        invoke_tool(context, INSPECT_COLUMN, column_name="secret")
