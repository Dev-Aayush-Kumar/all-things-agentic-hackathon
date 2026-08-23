"""Specialist agent implementations."""

from __future__ import annotations

import logging
from typing import Protocol

from atlas.agent.tools import INSPECT_COLUMN
from atlas.domain.enums import PlannerSource, Severity
from atlas.domain.models import (
    AgentDescriptor,
    SpecialistFollowUp,
    SpecialistTask,
    SpecialistTaskResult,
)
from atlas.investigation.pipeline import InvestigationResult
from atlas.investigation.prioritize import prioritize_findings
from atlas.investigation.report import build_report
from atlas.ops.registry import (
    CAPABILITY_SYNTHESIZE,
    DATA_ANALYST_ID,
    INVESTIGATOR_ID,
    REPORTER_ID,
)
from atlas.ops.tooling import run_authorized_tool, tool_already_completed
from atlas.ops.workspace import MissionWorkspace

logger = logging.getLogger(__name__)

_RELATED_COLUMN_HINTS = ("source", "period", "date", "year", "month", "region", "batch")


class SpecialistAgent(Protocol):
    """Execution contract for a bounded-domain specialist."""

    @property
    def descriptor(self) -> AgentDescriptor: ...

    async def execute(
        self, task: SpecialistTask, workspace: MissionWorkspace
    ) -> SpecialistTaskResult: ...


class DataAnalystAgent:
    """Runs allowlisted dataset measurement tools."""

    def __init__(self, descriptor: AgentDescriptor) -> None:
        self._descriptor = descriptor

    @property
    def descriptor(self) -> AgentDescriptor:
        return self._descriptor

    async def execute(
        self, task: SpecialistTask, workspace: MissionWorkspace
    ) -> SpecialistTaskResult:
        tool_name = task.capability
        if tool_name not in self._descriptor.allowed_tools:
            raise PermissionError(
                f"DATA_ANALYST cannot use unauthorized tool '{tool_name}'"
            )
        arguments = {
            key: value
            for key, value in task.inputs.items()
            if key != "adaptive"
        }
        adaptive = bool(task.inputs.get("adaptive", False))
        if tool_already_completed(workspace.mission, tool_name, arguments):
            logger.info(
                "Skipping completed analyst tool mission=%s tool=%s",
                task.mission_id,
                tool_name,
            )
            return SpecialistTaskResult(
                summary=f"Reused completed {tool_name} result",
                provenance=workspace.plan_source,
            )

        result = await run_authorized_tool(
            workspace,
            agent_id=self._descriptor.id,
            tool_name=tool_name,
            arguments=arguments,
            adaptive=adaptive,
            specialist_task_id=task.task_id,
        )
        return SpecialistTaskResult(
            summary=result.summary,
            finding_ids=[finding.finding_id for finding in result.findings],
            evidence_ids=[workspace.mission.evidence_records[-1].evidence_id]
            if workspace.mission.evidence_records
            else [],
            provenance=workspace.plan_source,
        )


class InvestigatorAgent:
    """Connects measured findings and inspects columns when warranted."""

    def __init__(self, descriptor: AgentDescriptor) -> None:
        self._descriptor = descriptor

    @property
    def descriptor(self) -> AgentDescriptor:
        return self._descriptor

    async def execute(
        self, task: SpecialistTask, workspace: MissionWorkspace
    ) -> SpecialistTaskResult:
        if task.capability not in self._descriptor.capabilities:
            raise PermissionError(
                f"INVESTIGATOR cannot execute capability '{task.capability}'"
            )
        column = task.inputs.get("column_name")
        evidence_ids: list[str] = []
        finding_ids: list[str] = []
        follow_ups: list[SpecialistFollowUp] = []
        notes_parts: list[str] = []

        related = [
            finding
            for finding in workspace.mission.findings
            if not isinstance(column, str)
            or column in finding.affected_columns
        ]
        finding_ids = [finding.finding_id for finding in related]
        high = [
            finding
            for finding in related
            if finding.severity in {Severity.HIGH, Severity.CRITICAL}
        ]

        if isinstance(column, str) and INSPECT_COLUMN in self._descriptor.allowed_tools:
            if not tool_already_completed(
                workspace.mission, INSPECT_COLUMN, {"column_name": column}
            ):
                result = await run_authorized_tool(
                    workspace,
                    agent_id=self._descriptor.id,
                    tool_name=INSPECT_COLUMN,
                    arguments={"column_name": column},
                    adaptive=True,
                    specialist_task_id=task.task_id,
                )
                finding_ids.extend(finding.finding_id for finding in result.findings)
                notes_parts.append(result.summary)
            else:
                notes_parts.append(f"Column '{column}' was already inspected.")
            if workspace.mission.evidence_records:
                evidence_ids.append(workspace.mission.evidence_records[-1].evidence_id)

            follow_ups.extend(_related_column_follow_ups(workspace, column))

        if high:
            notes_parts.append(
                "High-impact findings: " + "; ".join(item.title for item in high[:3])
            )
        elif related:
            notes_parts.append(
                f"{len(related)} related finding(s) were examined without a new root cause."
            )
        else:
            notes_parts.append("No additional findings required investigator follow-up.")

        summary = (
            f"Investigator reviewed evidence for {column or 'the current findings'}."
        )
        return SpecialistTaskResult(
            summary=summary,
            finding_ids=list(dict.fromkeys(finding_ids)),
            evidence_ids=evidence_ids,
            follow_ups=follow_ups,
            notes=" ".join(notes_parts),
            provenance=PlannerSource.LOCAL_FALLBACK
            if workspace.plan_source != PlannerSource.GEMINI_ADK
            else workspace.plan_source,
        )


class ReporterAgent:
    """Produces the final evidence-based mission report."""

    def __init__(self, descriptor: AgentDescriptor) -> None:
        self._descriptor = descriptor

    @property
    def descriptor(self) -> AgentDescriptor:
        return self._descriptor

    async def execute(
        self, task: SpecialistTask, workspace: MissionWorkspace
    ) -> SpecialistTaskResult:
        if task.capability not in {CAPABILITY_SYNTHESIZE, "prioritize_findings"}:
            raise PermissionError(
                f"REPORTER cannot execute capability '{task.capability}'"
            )
        profile = workspace.mission.dataset_profile
        if profile is None:
            raise RuntimeError("Reporter cannot synthesize without a dataset profile")

        ranked = prioritize_findings(list(workspace.mission.findings))
        workspace.mission.findings = ranked
        reasoning = await workspace.reasoner.interpret(
            workspace.mission.goal, profile, ranked
        )
        evidence_ids = [record.evidence_id for record in workspace.mission.evidence_records]
        finding_ids = [finding.finding_id for finding in ranked]
        from atlas.domain.models import AgentInterpretation

        interpretations = [
            AgentInterpretation(
                kind="mission_summary",
                text=reasoning.mission_summary,
                related_evidence_ids=evidence_ids,
                related_finding_ids=finding_ids,
            ),
            AgentInterpretation(
                kind="investigation_summary",
                text=reasoning.investigation_summary,
                related_evidence_ids=evidence_ids,
                related_finding_ids=finding_ids,
            ),
            AgentInterpretation(
                kind="overall_assessment",
                text=reasoning.overall_assessment,
                related_evidence_ids=evidence_ids,
                related_finding_ids=finding_ids,
            ),
        ]
        workspace.mission.interpretations.extend(interpretations)
        result = InvestigationResult(
            profile=profile,
            findings=ranked,
            frame=workspace.tool_context.frame,
        )
        workspace.mission.investigation_report = build_report(
            dataset_id=workspace.tool_context.dataset_id,
            original_filename=workspace.tool_context.original_filename,
            result=result,
            mission_summary=reasoning.mission_summary,
            investigation_summary=reasoning.investigation_summary,
            overall_assessment=reasoning.overall_assessment,
            recommended_actions=reasoning.recommended_actions,
            reasoning_source=reasoning.source,
        )
        workspace.mission.investigation_report.interpretations = list(
            workspace.mission.interpretations
        )
        workspace.mission.investigation_report.evidence_records = list(
            workspace.mission.evidence_records
        )
        return SpecialistTaskResult(
            summary="Final report synthesized from measured findings",
            finding_ids=finding_ids,
            evidence_ids=evidence_ids,
            notes=reasoning.overall_assessment,
            provenance=reasoning.source,
        )


def _related_column_follow_ups(
    workspace: MissionWorkspace, column: str
) -> list[SpecialistFollowUp]:
    follow_ups: list[SpecialistFollowUp] = []
    for name in workspace.tool_context.frame.columns:
        text = str(name).lower()
        if name == column or name in workspace.inspected_columns:
            continue
        if any(hint in text for hint in _RELATED_COLUMN_HINTS):
            follow_ups.append(
                SpecialistFollowUp(
                    capability=INSPECT_COLUMN,
                    objective=f"Inspect related column '{name}' after investigating '{column}'",
                    arguments={"column_name": str(name), "adaptive": True},
                    reason=(
                        f"Missingness/anomalies in '{column}' may concentrate by '{name}'"
                    ),
                )
            )
            break
    return follow_ups


def build_specialists(registry) -> dict[str, SpecialistAgent]:
    """Instantiate the default specialist set from a registry."""
    return {
        DATA_ANALYST_ID: DataAnalystAgent(registry.get(DATA_ANALYST_ID)),
        INVESTIGATOR_ID: InvestigatorAgent(registry.get(INVESTIGATOR_ID)),
        REPORTER_ID: ReporterAgent(registry.get(REPORTER_ID)),
    }
