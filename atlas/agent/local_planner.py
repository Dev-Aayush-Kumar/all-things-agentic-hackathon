"""Local development fallback planner.

This planner does NOT use Gemini or Google ADK. It produces deterministic,
rule-based execution plans for local development and testing.
"""

from atlas.agent.base import MissionPlanner
from atlas.domain.enums import PlannerSource, StepStatus
from atlas.domain.models import ExecutionPlan, PlanStep


class LocalFallbackPlanner(MissionPlanner):
    """Rule-based planner for local development without cloud credentials."""

    @property
    def source_name(self) -> str:
        return "LOCAL_DEVELOPMENT_FALLBACK"

    async def create_plan(self, goal: str, dataset_id: str | None = None) -> ExecutionPlan:
        """Generate a structured plan based on goal keywords and optional dataset."""
        if dataset_id:
            return self._dataset_investigation_plan(goal, dataset_id)

        goal_lower = goal.lower()
        steps: list[PlanStep] = []

        steps.append(
            PlanStep(
                id="step_1",
                title="Understand mission goal",
                description=f"Parse and clarify the mission objective: {goal}",
                status=StepStatus.PENDING,
            )
        )

        if any(word in goal_lower for word in ("dataset", "data", "csv", "table")):
            steps.extend(
                [
                    PlanStep(
                        id="step_2",
                        title="Inspect dataset",
                        description="Load and examine the provided dataset structure, columns, and sample records.",
                        status=StepStatus.PENDING,
                    ),
                    PlanStep(
                        id="step_3",
                        title="Profile data quality",
                        description="Check for missing values, type mismatches, duplicates, and schema violations.",
                        status=StepStatus.PENDING,
                    ),
                    PlanStep(
                        id="step_4",
                        title="Identify inconsistencies",
                        description="Detect major inconsistencies such as conflicting values, outliers, and referential integrity issues.",
                        status=StepStatus.PENDING,
                    ),
                    PlanStep(
                        id="step_5",
                        title="Prepare action recommendations",
                        description="Summarize findings and propose concrete remediation actions.",
                        status=StepStatus.PENDING,
                    ),
                ]
            )
        elif any(word in goal_lower for word in ("analyze", "analysis", "review")):
            steps.extend(
                [
                    PlanStep(
                        id="step_2",
                        title="Gather context",
                        description="Collect relevant inputs and constraints needed for analysis.",
                        status=StepStatus.PENDING,
                    ),
                    PlanStep(
                        id="step_3",
                        title="Perform analysis",
                        description="Execute the core analysis required by the mission goal.",
                        status=StepStatus.PENDING,
                    ),
                    PlanStep(
                        id="step_4",
                        title="Summarize findings",
                        description="Produce a structured summary of analysis results.",
                        status=StepStatus.PENDING,
                    ),
                ]
            )
        else:
            steps.extend(
                [
                    PlanStep(
                        id="step_2",
                        title="Plan approach",
                        description="Determine the sequence of actions needed to achieve the goal.",
                        status=StepStatus.PENDING,
                    ),
                    PlanStep(
                        id="step_3",
                        title="Execute core work",
                        description="Carry out the primary work required by the mission.",
                        status=StepStatus.PENDING,
                    ),
                    PlanStep(
                        id="step_4",
                        title="Finalize results",
                        description="Compile outputs and verify the mission objective is addressed.",
                        status=StepStatus.PENDING,
                    ),
                ]
            )

        return ExecutionPlan(
            steps=steps,
            planner_source=PlannerSource.LOCAL_FALLBACK,
            summary=f"Local fallback plan for: {goal}",
        )

    def _dataset_investigation_plan(self, goal: str, dataset_id: str) -> ExecutionPlan:
        """Plan that mirrors the deterministic investigation pipeline stages."""
        steps = [
            PlanStep(
                id="step_1",
                title="Understand mission goal",
                description=f"Clarify the investigation objective: {goal}",
            ),
            PlanStep(
                id="step_2",
                title="Inspect dataset",
                description="Load the uploaded CSV and profile rows, columns, types, and numeric statistics.",
            ),
            PlanStep(
                id="step_3",
                title="Analyze missing data",
                description="Measure missing-value counts and percentages per column.",
            ),
            PlanStep(
                id="step_4",
                title="Analyze duplicates",
                description="Count exact duplicate rows in the uploaded file.",
            ),
            PlanStep(
                id="step_5",
                title="Detect type and format anomalies",
                description="Find values that fail numeric/date coercion and inconsistent categorical formatting.",
            ),
            PlanStep(
                id="step_6",
                title="Detect numeric outliers",
                description="Apply IQR-based outlier detection on appropriate numeric columns.",
            ),
            PlanStep(
                id="step_7",
                title="Check cross-column consistency",
                description="Run explicit consistency rules (date order, non-negative fields) where justified.",
            ),
            PlanStep(
                id="step_8",
                title="Prioritize findings and produce report",
                description="Rank evidence-based findings and produce a structured investigation report.",
            ),
        ]
        return ExecutionPlan(
            steps=steps,
            planner_source=PlannerSource.LOCAL_FALLBACK,
            summary=(
                f"Local fallback investigation plan for dataset {dataset_id}: {goal}"
            ),
        )
