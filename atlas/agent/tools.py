"""Constrained investigation tools callable by the agent loop.

Tools operate only on the in-memory dataset bound to the current mission.
They never receive filesystem paths, shell commands, or secret access.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

import pandas as pd

from atlas.domain.models import DatasetProfile, Finding
from atlas.investigation.consistency import analyze_consistency
from atlas.investigation.duplicates import analyze_duplicates
from atlas.investigation.missing import analyze_missing
from atlas.investigation.outliers import analyze_outliers
from atlas.investigation.profile import build_profile
from atlas.investigation.type_format import analyze_type_format
from atlas.investigation.typing import infer_column_type, non_null_series, numeric_conversion

PROFILE_DATASET = "profile_dataset"
ANALYZE_MISSING = "analyze_missing_values"
ANALYZE_DUPLICATES = "analyze_duplicates"
ANALYZE_TYPE_FORMAT = "analyze_type_format"
ANALYZE_OUTLIERS = "analyze_outliers"
ANALYZE_CONSISTENCY = "analyze_consistency"
INSPECT_COLUMN = "inspect_column"

INVESTIGATION_TOOLS = (
    PROFILE_DATASET,
    ANALYZE_MISSING,
    ANALYZE_DUPLICATES,
    ANALYZE_TYPE_FORMAT,
    ANALYZE_OUTLIERS,
    ANALYZE_CONSISTENCY,
    INSPECT_COLUMN,
)

TOOL_ALLOWED_ARGS: dict[str, frozenset[str]] = {
    PROFILE_DATASET: frozenset(),
    ANALYZE_MISSING: frozenset(),
    ANALYZE_DUPLICATES: frozenset(),
    ANALYZE_TYPE_FORMAT: frozenset(),
    ANALYZE_OUTLIERS: frozenset(),
    ANALYZE_CONSISTENCY: frozenset(),
    INSPECT_COLUMN: frozenset({"column_name"}),
}

TOOL_STAGE_NAMES = {
    PROFILE_DATASET: "profile",
    ANALYZE_MISSING: "missing",
    ANALYZE_DUPLICATES: "duplicates",
    ANALYZE_TYPE_FORMAT: "type_format",
    ANALYZE_OUTLIERS: "outliers",
    ANALYZE_CONSISTENCY: "consistency",
    INSPECT_COLUMN: "inspect_column",
}


class ToolSecurityError(Exception):
    """Raised when a tool call violates the allowlist or argument policy."""


class ToolExecutionError(Exception):
    """Raised when a allowed tool fails during execution."""


@dataclass
class ToolResult:
    """Structured output from a constrained investigation tool."""

    tool_name: str
    observed_facts: dict[str, Any]
    findings: list[Finding] = field(default_factory=list)
    profile: DatasetProfile | None = None
    summary: str = ""


@dataclass
class ToolContext:
    """Mission-scoped dataset handle. Tools cannot reach other data."""

    dataset_id: str
    original_filename: str
    frame: pd.DataFrame


def invoke_tool(context: ToolContext, tool_name: str, **kwargs: Any) -> ToolResult:
    """Execute a whitelisted tool against the bound dataset."""
    if tool_name not in TOOL_ALLOWED_ARGS:
        raise ToolSecurityError(f"Tool '{tool_name}' is not an allowed ATLAS capability")

    allowed = TOOL_ALLOWED_ARGS[tool_name]
    extra = set(kwargs) - allowed
    if extra:
        raise ToolSecurityError(
            f"Tool '{tool_name}' rejected disallowed arguments: {sorted(extra)}"
        )
    filtered = {key: kwargs[key] for key in allowed if key in kwargs}

    handlers: dict[str, Callable[..., ToolResult]] = {
        PROFILE_DATASET: _profile_dataset,
        ANALYZE_MISSING: _analyze_missing,
        ANALYZE_DUPLICATES: _analyze_duplicates,
        ANALYZE_TYPE_FORMAT: _analyze_type_format,
        ANALYZE_OUTLIERS: _analyze_outliers,
        ANALYZE_CONSISTENCY: _analyze_consistency,
        INSPECT_COLUMN: _inspect_column,
    }
    try:
        result = handlers[tool_name](context, **filtered)
        result.observed_facts = _json_safe(result.observed_facts)
        return result
    except (ToolSecurityError, ToolExecutionError):
        raise
    except Exception as exc:
        raise ToolExecutionError(f"Tool '{tool_name}' failed: {exc}") from exc


def _json_safe(value: Any) -> Any:
    """Convert tool output into JSON-serializable Python values."""
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            return str(value)
    return str(value)


def _profile_dataset(context: ToolContext) -> ToolResult:
    profile = build_profile(context.frame)
    facts = {
        "row_count": profile.row_count,
        "column_count": profile.column_count,
        "columns": [column.model_dump() for column in profile.columns],
    }
    return ToolResult(
        tool_name=PROFILE_DATASET,
        observed_facts=facts,
        profile=profile,
        summary=f"Profiled {profile.row_count} rows × {profile.column_count} columns",
    )


def _analyze_missing(context: ToolContext) -> ToolResult:
    findings = analyze_missing(context.frame)
    facts = {
        "finding_count": len(findings),
        "columns_with_missing": [
            {
                "column": finding.affected_columns[0] if finding.affected_columns else None,
                "missing_count": finding.evidence.get("missing_count"),
                "missing_percent": finding.evidence.get("missing_percent"),
                "materially_incomplete": finding.evidence.get("materially_incomplete"),
            }
            for finding in findings
        ],
    }
    return ToolResult(
        tool_name=ANALYZE_MISSING,
        observed_facts=facts,
        findings=findings,
        summary=f"Missing-value analysis produced {len(findings)} finding(s)",
    )


def _analyze_duplicates(context: ToolContext) -> ToolResult:
    findings = analyze_duplicates(context.frame)
    facts = {
        "finding_count": len(findings),
        "duplicate_row_count": findings[0].evidence.get("duplicate_row_count") if findings else 0,
        "total_rows": int(len(context.frame)),
    }
    return ToolResult(
        tool_name=ANALYZE_DUPLICATES,
        observed_facts=facts,
        findings=findings,
        summary=f"Duplicate analysis produced {len(findings)} finding(s)",
    )


def _analyze_type_format(context: ToolContext) -> ToolResult:
    findings = analyze_type_format(context.frame)
    facts = {
        "finding_count": len(findings),
        "affected_columns": sorted(
            {column for finding in findings for column in finding.affected_columns}
        ),
    }
    return ToolResult(
        tool_name=ANALYZE_TYPE_FORMAT,
        observed_facts=facts,
        findings=findings,
        summary=f"Type/format analysis produced {len(findings)} finding(s)",
    )


def _analyze_outliers(context: ToolContext) -> ToolResult:
    findings = analyze_outliers(context.frame)
    facts = {
        "finding_count": len(findings),
        "affected_columns": sorted(
            {column for finding in findings for column in finding.affected_columns}
        ),
    }
    return ToolResult(
        tool_name=ANALYZE_OUTLIERS,
        observed_facts=facts,
        findings=findings,
        summary=f"Outlier analysis produced {len(findings)} finding(s)",
    )


def _analyze_consistency(context: ToolContext) -> ToolResult:
    findings = analyze_consistency(context.frame)
    facts = {
        "finding_count": len(findings),
        "affected_columns": sorted(
            {column for finding in findings for column in finding.affected_columns}
        ),
    }
    return ToolResult(
        tool_name=ANALYZE_CONSISTENCY,
        observed_facts=facts,
        findings=findings,
        summary=f"Consistency analysis produced {len(findings)} finding(s)",
    )


def _inspect_column(context: ToolContext, column_name: str | None = None) -> ToolResult:
    if not column_name or not isinstance(column_name, str):
        raise ToolSecurityError("inspect_column requires a column_name argument")
    if column_name not in context.frame.columns:
        raise ToolExecutionError(
            f"Column '{column_name}' is not in the mission dataset"
        )

    series = context.frame[column_name]
    total = int(len(context.frame))
    null_count = int(series.isna().sum())
    values = non_null_series(series)
    inferred = infer_column_type(series)
    sample = values.head(8).tolist()
    facts: dict[str, Any] = {
        "column_name": column_name,
        "inferred_type": inferred,
        "row_count": total,
        "null_count": null_count,
        "null_percent": round((null_count / total) * 100.0, 2) if total else 0.0,
        "non_null_count": int(len(values)),
        "unique_count": int(values.nunique()) if not values.empty else 0,
        "sample_values": sample,
    }
    if inferred == "numeric":
        converted = numeric_conversion(series).dropna()
        if not converted.empty:
            facts["numeric_min"] = float(converted.min())
            facts["numeric_max"] = float(converted.max())
            facts["numeric_mean"] = float(converted.mean())
            facts["numeric_median"] = float(converted.median())
    summary = (
        f"Inspected column '{column_name}': type={inferred}, "
        f"missing={facts['null_percent']}%"
    )
    return ToolResult(
        tool_name=INSPECT_COLUMN,
        observed_facts=facts,
        summary=summary,
    )
