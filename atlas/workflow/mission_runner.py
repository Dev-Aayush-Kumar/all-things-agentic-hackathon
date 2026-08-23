"""Mission workflow orchestration."""

import asyncio
import logging

from atlas.agent.base import MissionPlanner
from atlas.agent.reasoner_base import InvestigationReasoner
from atlas.domain.enums import EventType, MissionStatus, StepStatus
from atlas.domain.exceptions import DatasetParseError
from atlas.domain.models import Mission, MissionEvent, utc_now
from atlas.investigation.pipeline import InvestigationPipeline, StageResult
from atlas.investigation.report import build_report
from atlas.persistence.base import MissionRepository
from atlas.persistence.dataset_base import DatasetRepository
from atlas.storage.base import DatasetStorage
from atlas.workflow.step_executor import StepExecutor

logger = logging.getLogger(__name__)

STAGE_EVENTS = {
    "profile": (
        EventType.DATASET_PROFILE_COMPLETED,
        "Dataset profile completed",
    ),
    "missing": (
        EventType.MISSING_DATA_ANALYSIS_COMPLETED,
        "Missing data analysis completed",
    ),
    "duplicates": (
        EventType.DUPLICATE_ANALYSIS_COMPLETED,
        "Duplicate analysis completed",
    ),
    "type_format": (
        EventType.TYPE_FORMAT_ANALYSIS_COMPLETED,
        "Type/format analysis completed",
    ),
    "outliers": (
        EventType.OUTLIER_ANALYSIS_COMPLETED,
        "Outlier analysis completed",
    ),
    "consistency": (
        EventType.CONSISTENCY_ANALYSIS_COMPLETED,
        "Consistency analysis completed",
    ),
}

STAGE_STEP_KEYWORDS = {
    "profile": ("inspect", "profile", "understand"),
    "missing": ("missing",),
    "duplicates": ("duplicate",),
    "type_format": ("type", "format"),
    "outliers": ("outlier",),
    "consistency": ("consisten",),
}


class MissionWorkflowRunner:
    """Runs the full mission lifecycle in the background."""

    def __init__(
        self,
        repository: MissionRepository,
        planner: MissionPlanner,
        step_executor: StepExecutor,
        dataset_repository: DatasetRepository | None = None,
        dataset_storage: DatasetStorage | None = None,
        reasoner: InvestigationReasoner | None = None,
        step_delay_seconds: float = 0.0,
    ) -> None:
        self._repository = repository
        self._planner = planner
        self._step_executor = step_executor
        self._dataset_repository = dataset_repository
        self._dataset_storage = dataset_storage
        self._reasoner = reasoner
        self._pipeline = InvestigationPipeline()
        self._step_delay_seconds = step_delay_seconds

    async def run(self, mission_id: str) -> None:
        """Execute planning and step execution for a mission."""
        mission = await self._repository.get(mission_id)
        if mission is None:
            logger.error("Mission %s not found for workflow execution", mission_id)
            return

        try:
            await self._plan(mission)
            await self._execute(mission)
        except Exception as exc:
            logger.exception("Mission %s failed", mission_id)
            await self._fail_mission(mission, str(exc))

    async def _plan(self, mission: Mission) -> None:
        mission.status = MissionStatus.PLANNING
        mission.touch()
        self._add_event(
            mission,
            EventType.PLANNING_STARTED,
            "Planning started",
            {"planner": self._planner.source_name},
        )
        await self._repository.update(mission)

        plan = await self._planner.create_plan(mission.goal, mission.dataset_id)
        mission.execution_plan = plan
        self._add_event(
            mission,
            EventType.EXECUTION_PLAN_GENERATED,
            "Execution plan generated",
            {
                "planner_source": plan.planner_source.value,
                "step_count": len(plan.steps),
            },
        )
        mission.touch()
        await self._repository.update(mission)

    async def _execute(self, mission: Mission) -> None:
        if mission.execution_plan is None:
            raise RuntimeError("Cannot execute mission without an execution plan")

        mission.status = MissionStatus.EXECUTING
        mission.touch()
        self._add_event(mission, EventType.EXECUTION_STARTED, "Execution started")
        await self._repository.update(mission)

        if mission.dataset_id:
            await self._execute_investigation(mission)
        else:
            await self._execute_generic_steps(mission)

        mission.status = MissionStatus.COMPLETED
        mission.completed_at = utc_now()
        mission.touch()
        self._add_event(mission, EventType.MISSION_COMPLETED, "Mission completed")
        await self._repository.update(mission)

    async def _execute_generic_steps(self, mission: Mission) -> None:
        assert mission.execution_plan is not None
        for step in mission.execution_plan.steps:
            self._add_event(
                mission,
                EventType.STEP_STARTED,
                f"Step started: {step.title}",
                {"step_id": step.id},
            )
            mission.touch()
            await self._repository.update(mission)

            updated_step = await self._step_executor.execute(step, mission.goal)
            step.status = updated_step.status
            step.result = updated_step.result
            step.error = updated_step.error

            if step.status == StepStatus.FAILED:
                self._add_event(
                    mission,
                    EventType.STEP_FAILED,
                    f"Step failed: {step.title}",
                    {"step_id": step.id, "error": step.error},
                )
                mission.touch()
                await self._repository.update(mission)
                raise RuntimeError(step.error or f"Step {step.id} failed")

            self._add_event(
                mission,
                EventType.STEP_COMPLETED,
                f"Step completed: {step.title}",
                {"step_id": step.id},
            )
            mission.touch()
            await self._repository.update(mission)

    async def _execute_investigation(self, mission: Mission) -> None:
        if self._dataset_repository is None or self._dataset_storage is None:
            raise RuntimeError("Dataset investigation dependencies are not configured")
        if self._reasoner is None:
            raise RuntimeError("Investigation reasoner is not configured")

        dataset = await self._dataset_repository.get(mission.dataset_id or "")
        if dataset is None:
            raise RuntimeError(f"Dataset '{mission.dataset_id}' was not found")

        try:
            content = await self._dataset_storage.load(dataset.stored_filename)
            frame = await asyncio.to_thread(self._pipeline.parse, content)
        except DatasetParseError:
            raise
        except FileNotFoundError as exc:
            raise DatasetParseError(str(exc)) from exc
        except Exception as exc:
            raise DatasetParseError(f"Failed to load dataset: {exc}") from exc

        self._add_event(
            mission,
            EventType.INVESTIGATION_STARTED,
            "Dataset investigation started",
            {
                "dataset_id": dataset.dataset_id,
                "original_filename": dataset.original_filename,
                "row_preview_bytes": len(content),
            },
        )
        await self._mark_matching_steps(mission, ("understand",), "Goal recorded; investigation started")
        mission.touch()
        await self._repository.update(mission)
        await self._pause()

        stages: list[StageResult] = []
        for stage_name in self._pipeline.stage_names:
            stage = await asyncio.to_thread(self._pipeline.run_stage, stage_name, frame)
            stages.append(stage)
            stages.append(stage)
            event_type, message = STAGE_EVENTS[stage.name]
            metadata: dict = {"stage": stage.name, "finding_count": len(stage.findings)}
            if stage.profile is not None:
                metadata["row_count"] = stage.profile.row_count
                metadata["column_count"] = stage.profile.column_count
            self._add_event(mission, event_type, message, metadata)
            await self._mark_matching_steps(
                mission,
                STAGE_STEP_KEYWORDS.get(stage.name, ()),
                message,
            )
            mission.touch()
            await self._repository.update(mission)
            await self._pause()

        result = self._pipeline.assemble(frame, stages)
        self._add_event(
            mission,
            EventType.FINDINGS_PRIORITIZED,
            "Findings prioritized",
            {"finding_count": len(result.findings)},
        )
        await self._mark_matching_steps(
            mission,
            ("priorit",),
            f"Prioritized {len(result.findings)} finding(s)",
        )
        mission.touch()
        await self._repository.update(mission)
        await self._pause()

        reasoning = await self._reasoner.interpret(
            mission.goal,
            result.profile,
            result.findings,
        )
        mission.investigation_report = build_report(
            dataset_id=dataset.dataset_id,
            original_filename=dataset.original_filename,
            result=result,
            mission_summary=reasoning.mission_summary,
            investigation_summary=reasoning.investigation_summary,
            overall_assessment=reasoning.overall_assessment,
            recommended_actions=reasoning.recommended_actions,
            reasoning_source=reasoning.source,
        )
        self._add_event(
            mission,
            EventType.FINAL_REPORT_GENERATED,
            "Final report generated",
            {
                "finding_count": len(result.findings),
                "reasoning_source": reasoning.source.value,
            },
        )
        self._complete_remaining_steps(
            mission,
            f"Investigation complete with {len(result.findings)} finding(s).",
        )
        mission.touch()
        await self._repository.update(mission)

    async def _mark_matching_steps(
        self,
        mission: Mission,
        keywords: tuple[str, ...],
        result: str,
    ) -> None:
        if mission.execution_plan is None or not keywords:
            return
        for step in mission.execution_plan.steps:
            haystack = f"{step.title} {step.description}".lower()
            if step.status == StepStatus.COMPLETED:
                continue
            if any(keyword in haystack for keyword in keywords):
                if step.status == StepStatus.PENDING:
                    self._add_event(
                        mission,
                        EventType.STEP_STARTED,
                        f"Step started: {step.title}",
                        {"step_id": step.id},
                    )
                step.status = StepStatus.COMPLETED
                step.result = result
                self._add_event(
                    mission,
                    EventType.STEP_COMPLETED,
                    f"Step completed: {step.title}",
                    {"step_id": step.id},
                )

    def _complete_remaining_steps(self, mission: Mission, result: str) -> None:
        if mission.execution_plan is None:
            return
        for step in mission.execution_plan.steps:
            if step.status == StepStatus.COMPLETED:
                continue
            self._add_event(
                mission,
                EventType.STEP_STARTED,
                f"Step started: {step.title}",
                {"step_id": step.id},
            )
            step.status = StepStatus.COMPLETED
            step.result = result
            self._add_event(
                mission,
                EventType.STEP_COMPLETED,
                f"Step completed: {step.title}",
                {"step_id": step.id},
            )

    async def _pause(self) -> None:
        if self._step_delay_seconds > 0:
            await asyncio.sleep(self._step_delay_seconds)

    async def _fail_mission(self, mission: Mission, error: str) -> None:
        mission.status = MissionStatus.FAILED
        mission.error = error
        mission.completed_at = utc_now()
        mission.touch()
        self._add_event(
            mission,
            EventType.MISSION_FAILED,
            "Mission failed",
            {"error": error},
        )
        await self._repository.update(mission)

    @staticmethod
    def _add_event(
        mission: Mission,
        event_type: EventType,
        message: str,
        metadata: dict | None = None,
    ) -> None:
        mission.events.append(
            MissionEvent(
                type=event_type,
                message=message,
                metadata=metadata or {},
            )
        )
