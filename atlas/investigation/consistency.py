"""Explicit, extensible cross-column consistency rules.

Rules fire only when column semantics can be justified from names plus
successful type coercion. They do not invent arbitrary relationships.
"""

from __future__ import annotations

import re
from typing import Protocol

import pandas as pd

from atlas.domain.enums import FindingCategory, Severity
from atlas.domain.models import Finding
from atlas.investigation.findings import new_finding
from atlas.investigation.typing import datetime_conversion, infer_column_type, numeric_conversion

NON_NEGATIVE_NAME_RE = re.compile(
    r"(^|_)(quantity|qty|count|age|price|amount|duration)(_|$)",
    re.IGNORECASE,
)
START_NAME_RE = re.compile(r"(start|begin)", re.IGNORECASE)
END_NAME_RE = re.compile(r"(end|finish|stop)", re.IGNORECASE)


class ConsistencyRule(Protocol):
    """A named consistency check over a DataFrame."""

    name: str

    def evaluate(self, frame: pd.DataFrame) -> list[Finding]:
        """Return findings supported by this rule, or an empty list."""
        ...


class DateOrderRule:
    """If a start-like datetime column and end-like datetime column exist, start must be <= end."""

    name = "date_order"

    def evaluate(self, frame: pd.DataFrame) -> list[Finding]:
        start_columns = [
            name
            for name in frame.columns
            if START_NAME_RE.search(str(name)) and infer_column_type(frame[name]) == "datetime"
        ]
        end_columns = [
            name
            for name in frame.columns
            if END_NAME_RE.search(str(name))
            and not START_NAME_RE.search(str(name))
            and infer_column_type(frame[name]) == "datetime"
        ]
        findings: list[Finding] = []
        total_rows = int(len(frame))

        for start_name in start_columns:
            for end_name in end_columns:
                start_values = datetime_conversion(frame[start_name])
                end_values = datetime_conversion(frame[end_name])
                aligned_start, aligned_end = start_values.align(end_values, join="inner")
                comparable = aligned_start.notna() & aligned_end.notna()
                violations = comparable & (aligned_start > aligned_end)
                count = int(violations.sum())
                if count == 0:
                    continue
                samples = []
                for index in aligned_start[violations].index[:3]:
                    samples.append(
                        {
                            "start": str(aligned_start.loc[index]),
                            "end": str(aligned_end.loc[index]),
                        }
                    )
                findings.append(
                    new_finding(
                        category=FindingCategory.CONSISTENCY_VIOLATION,
                        title=f"'{start_name}' occurs after '{end_name}'",
                        description=(
                            f"{count} row(s) have {start_name} later than {end_name}, "
                            "which is inconsistent for a start/end date pair."
                        ),
                        affected_columns=[start_name, end_name],
                        evidence={
                            "rule": self.name,
                            "violation_count": count,
                            "sample_violations": samples,
                        },
                        affected_row_count=count,
                        total_rows=total_rows,
                        severity=Severity.HIGH if count / max(total_rows, 1) >= 0.1 else Severity.MEDIUM,
                        suggested_action=(
                            f"Correct swapped or mistyped dates so '{start_name}' "
                            f"is on or before '{end_name}'."
                        ),
                        detection_method="date_order_rule",
                    )
                )
        return findings


class NonNegativeRule:
    """Columns whose names strongly imply counts/amounts should not be negative."""

    name = "non_negative"

    def evaluate(self, frame: pd.DataFrame) -> list[Finding]:
        total_rows = int(len(frame))
        findings: list[Finding] = []
        for name in frame.columns:
            if not NON_NEGATIVE_NAME_RE.search(str(name)):
                continue
            if infer_column_type(frame[name]) != "numeric":
                continue
            converted = numeric_conversion(frame[name])
            negatives = converted.notna() & (converted < 0)
            count = int(negatives.sum())
            if count == 0:
                continue
            samples = [float(value) for value in converted[negatives].head(5).tolist()]
            findings.append(
                new_finding(
                    category=FindingCategory.CONSISTENCY_VIOLATION,
                    title=f"Negative values in non-negative column '{name}'",
                    description=(
                        f"Column '{name}' is named as a count/amount/age/price field "
                        f"but contains {count} negative value(s)."
                    ),
                    affected_columns=[str(name)],
                    evidence={
                        "rule": self.name,
                        "negative_count": count,
                        "sample_values": samples,
                    },
                    affected_row_count=count,
                    total_rows=total_rows,
                    severity=Severity.HIGH,
                    suggested_action=(
                        f"Replace or investigate negative values in '{name}'; "
                        "this field is expected to be zero or positive."
                    ),
                    detection_method="non_negative_rule",
                )
            )
        return findings


DEFAULT_CONSISTENCY_RULES: list[ConsistencyRule] = [
    DateOrderRule(),
    NonNegativeRule(),
]


def analyze_consistency(
    frame: pd.DataFrame,
    rules: list[ConsistencyRule] | None = None,
) -> list[Finding]:
    """Run explicit consistency rules. Unjustified relationships are not invented."""
    active_rules = rules if rules is not None else DEFAULT_CONSISTENCY_RULES
    findings: list[Finding] = []
    for rule in active_rules:
        findings.extend(rule.evaluate(frame))
    return findings
