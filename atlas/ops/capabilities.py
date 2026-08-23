"""Machine-readable allowlist the decision-maker may request.

Internal implementation details stay out of this catalog.
"""

from __future__ import annotations

from atlas.agent.tools import (
    ANALYZE_CONSISTENCY,
    ANALYZE_DUPLICATES,
    ANALYZE_MISSING,
    ANALYZE_OUTLIERS,
    ANALYZE_TYPE_FORMAT,
    INSPECT_COLUMN,
    PROFILE_DATASET,
)
from atlas.domain.models import CapabilityDescription
from atlas.ops.actions.registry import (
    ACTION_FILL_MISSING_VALUES,
    ACTION_REMOVE_DUPLICATES,
    CAPABILITY_FILL_MISSING,
    CAPABILITY_REMOVE_DUPLICATES,
)
from atlas.ops.external.registry import CAPABILITY_FETCH_URL, default_external_registry
from atlas.ops.registry import (
    CAPABILITY_INVESTIGATE,
    CAPABILITY_INVESTIGATE_COLUMN,
    CAPABILITY_SYNTHESIZE,
    DATA_ANALYST_ID,
    INVESTIGATOR_ID,
    REMEDIATOR_ID,
    REPORTER_ID,
)

TOOL_ALIASES = {
    "analyze_missing": ANALYZE_MISSING,
    "profile": PROFILE_DATASET,
    "duplicates": ANALYZE_DUPLICATES,
}

ROLE_ALIASES = {
    "DATA_ANALYST": PROFILE_DATASET,
    "INVESTIGATOR": CAPABILITY_INVESTIGATE,
    "REPORTER": CAPABILITY_SYNTHESIZE,
    DATA_ANALYST_ID: PROFILE_DATASET,
    INVESTIGATOR_ID: CAPABILITY_INVESTIGATE,
    REPORTER_ID: CAPABILITY_SYNTHESIZE,
}

ACTION_ALIASES = {
    CAPABILITY_REMOVE_DUPLICATES: ACTION_REMOVE_DUPLICATES,
    CAPABILITY_FILL_MISSING: ACTION_FILL_MISSING_VALUES,
    "remove_duplicates": ACTION_REMOVE_DUPLICATES,
    "fill_missing_values": ACTION_FILL_MISSING_VALUES,
}


def observation_catalog() -> list[CapabilityDescription]:
    return [
        CapabilityDescription(
            name=PROFILE_DATASET,
            kind="observation",
            purpose="Measure dataset shape, column types, and numeric statistics.",
            restrictions=["Read-only", "Does not change the dataset"],
            expected_output="DatasetProfile plus row/column counts",
        ),
        CapabilityDescription(
            name=ANALYZE_MISSING,
            kind="observation",
            purpose="Count missing values per column.",
            restrictions=["Read-only"],
            expected_output="MISSING_DATA findings with counts and percents",
        ),
        CapabilityDescription(
            name=ANALYZE_DUPLICATES,
            kind="observation",
            purpose="Count exact duplicate rows.",
            restrictions=["Read-only"],
            expected_output="DUPLICATE_ROWS finding with duplicate_row_count",
        ),
        CapabilityDescription(
            name=ANALYZE_TYPE_FORMAT,
            kind="observation",
            purpose="Detect type/format coercion failures and categorical variants.",
            restrictions=["Read-only"],
            expected_output="TYPE_FORMAT_ANOMALY findings",
        ),
        CapabilityDescription(
            name=ANALYZE_OUTLIERS,
            kind="observation",
            purpose="Tukey IQR outlier scan on eligible numeric columns.",
            restrictions=["Read-only"],
            expected_output="NUMERIC_OUTLIER findings",
        ),
        CapabilityDescription(
            name=ANALYZE_CONSISTENCY,
            kind="observation",
            purpose="Check explicit cross-column consistency rules.",
            restrictions=["Read-only"],
            expected_output="CONSISTENCY_VIOLATION findings",
        ),
        CapabilityDescription(
            name=INSPECT_COLUMN,
            kind="observation",
            purpose="Inspect one named column already present in the dataset.",
            allowed_inputs=["column_name"],
            required_inputs=["column_name"],
            restrictions=["Read-only", "column_name must exist in the bound dataset"],
            expected_output="Column-level observed facts",
        ),
    ]


def specialist_catalog() -> list[CapabilityDescription]:
    return [
        CapabilityDescription(
            name="DATA_ANALYST",
            kind="specialist",
            purpose="Run observation measurements. Maps to profile_dataset unless a tool is named.",
            restrictions=["Cannot execute remediations or arbitrary code"],
            expected_output="Structured evidence from allowlisted tools",
        ),
        CapabilityDescription(
            name="INVESTIGATOR",
            kind="specialist",
            purpose="Interpret measured findings and optionally inspect a column.",
            allowed_inputs=["column_name"],
            restrictions=["inspect_column only"],
            expected_output="Notes plus optional inspect_column evidence",
        ),
        CapabilityDescription(
            name="REPORTER",
            kind="specialist",
            purpose="Synthesize the final evidence-based report.",
            restrictions=["Interpretation only", "Cannot run tools or actions"],
            expected_output="InvestigationReport",
        ),
    ]


def action_catalog() -> list[CapabilityDescription]:
    return [
        CapabilityDescription(
            name=ACTION_REMOVE_DUPLICATES,
            kind="action",
            purpose="Drop exact duplicate rows on the working copy.",
            restrictions=[
                "Working copy only",
                "Original upload is immutable",
                "Postcondition verification is mandatory",
            ],
            expected_output="duplicate_count == 0 after verification",
        ),
        CapabilityDescription(
            name=ACTION_FILL_MISSING_VALUES,
            kind="action",
            purpose="Fill missing values in one named working-copy column.",
            allowed_inputs=["column_name", "strategy"],
            required_inputs=["column_name"],
            restrictions=[
                "Working copy only",
                "Original upload is immutable",
                "Postcondition verification is mandatory",
            ],
            expected_output="named column missing_count == 0; row count unchanged",
        ),
    ]


def external_catalog(settings=None) -> list[CapabilityDescription]:
    return default_external_registry().catalog(settings)


def capability_catalog(settings=None) -> list[CapabilityDescription]:
    return [
        *observation_catalog(),
        *specialist_catalog(),
        *action_catalog(),
        *external_catalog(settings),
    ]


def resolve_observation_name(name: str) -> str | None:
    if name in {
        PROFILE_DATASET,
        ANALYZE_MISSING,
        ANALYZE_DUPLICATES,
        ANALYZE_TYPE_FORMAT,
        ANALYZE_OUTLIERS,
        ANALYZE_CONSISTENCY,
        INSPECT_COLUMN,
    }:
        return name
    return TOOL_ALIASES.get(name)


def resolve_specialist_capability(name: str) -> str | None:
    resolved = resolve_observation_name(name)
    if resolved:
        return resolved
    if name in {
        CAPABILITY_INVESTIGATE,
        CAPABILITY_INVESTIGATE_COLUMN,
        CAPABILITY_SYNTHESIZE,
        CAPABILITY_REMOVE_DUPLICATES,
        CAPABILITY_FILL_MISSING,
    }:
        return name
    return ROLE_ALIASES.get(name)


def resolve_external_capability(name: str) -> str | None:
    token = name.strip()
    if token == CAPABILITY_FETCH_URL or token.upper() == CAPABILITY_FETCH_URL:
        return CAPABILITY_FETCH_URL
    if token.lower() == "fetch_url":
        return CAPABILITY_FETCH_URL
    return None


def resolve_action_type(name: str) -> str | None:
    if name in {ACTION_REMOVE_DUPLICATES, ACTION_FILL_MISSING_VALUES}:
        return name
    return ACTION_ALIASES.get(name)


FORBIDDEN_CAPABILITIES = frozenset(
    {
        "EXECUTE_SHELL",
        "UNKNOWN_AGENT",
        "DIRECT_FILE_WRITE",
        "eval",
        "exec",
        "shell",
        "subprocess",
        "http",
        "HTTP_API",
        "WEB_FETCH",
        "curl",
        "wget",
        "os.system",
    }
)
