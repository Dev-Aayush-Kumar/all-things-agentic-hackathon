"""Strategy learning: evaluation, aggregation, retrieval, and influence."""

from __future__ import annotations

from pathlib import Path

import pytest

from atlas.agent.local_decider import LocalDecisionMaker
from atlas.agent.local_reasoner import LocalFallbackReasoner
from atlas.agent.tools import ANALYZE_DUPLICATES, ANALYZE_OUTLIERS, PROFILE_DATASET, ToolContext
from atlas.config.settings import Settings
from atlas.domain.enums import (
    EventType,
    ExperienceOutcome,
    MissionCategory,
    MissionStatus,
    PlannerSource,
    StepStatus,
)
from atlas.domain.exceptions import ModelDecisionError, StrategyValidationError
from atlas.domain.models import (
    DatasetCharacteristics,
    DelegationPlan,
    EvidenceRecord,
    Mission,
    SpecialistTask,
    StrategyRecord,
    WorkingCopyState,
)
from atlas.investigation.parser import parse_csv_bytes
from atlas.ops.decisions import parse_model_decision, validate_decision
from atlas.ops.learning.evaluate import evaluate_mission
from atlas.ops.learning.extract import record_experience_and_strategy
from atlas.ops.learning.influence import strategy_follow_ups
from atlas.ops.learning.policy import (
    assert_strategy_safe,
    compute_confidence,
    merge_strategy,
    recommendable_observations,
    strategy_from_experience,
)
from atlas.ops.learning.retrieve import StrategyQuery, StrategyRetriever
from atlas.ops.learning.signatures import dataset_signature
from atlas.ops.reasoning_context import build_reasoning_context
from atlas.ops.registry import default_registry
from atlas.ops.supervisor import Supervisor
from atlas.ops.workspace import MissionWorkspace
from atlas.persistence.codec import (
    document_to_experience,
    document_to_strategy,
    experience_to_document,
    strategy_to_document,
)
from atlas.persistence.firestore_learning_repository import FirestoreLearningRepository
from atlas.persistence.memory_store import MemoryDocumentStore
from atlas.persistence.sqlite_learning_repository import SQLiteLearningRepository
from atlas.storage.local_storage import LocalFileStorage
from tests.conftest import FIXTURES_DIR


def _settings(**kwargs) -> Settings:
    return Settings(
        _env_file=None,
        planner_backend="local",
        memory_enabled=False,
        strategy_enabled=True,
        **kwargs,
    )


def _frame(name: str = "survey_quality.csv"):
    return parse_csv_bytes((FIXTURES_DIR / name).read_bytes())


async def _noop() -> None:
    return None


def _workspace(mission: Mission, settings: Settings | None = None) -> MissionWorkspace:
    return MissionWorkspace(
        mission=mission,
        tool_context=ToolContext("ds", "survey_quality.csv", _frame()),
        persist=_noop,
        lock=__import__("asyncio").Lock(),
        settings=settings or _settings(),
        reasoner=LocalFallbackReasoner(),
        registry=default_registry(),
        plan_source=PlannerSource.LOCAL_FALLBACK,
    )


def _completed_quality_mission(mission_id: str = "m-quality") -> Mission:
    from atlas.investigation.profile import build_profile

    mission = Mission(
        goal="Investigate data quality problems.",
        dataset_id="ds",
        mission_id=mission_id,
    )
    mission.status = MissionStatus.COMPLETED
    mission.dataset_profile = build_profile(_frame())
    mission.reasoning_iteration = 4
    mission.delegation_plan = DelegationPlan(
        objective="investigate",
        source=PlannerSource.LOCAL_FALLBACK,
        tasks=[
            SpecialistTask(
                mission_id=mission.mission_id,
                agent_id="data-analyst",
                objective="profile",
                capability=PROFILE_DATASET,
                status=StepStatus.COMPLETED,
            ),
            SpecialistTask(
                mission_id=mission.mission_id,
                agent_id="data-analyst",
                objective="duplicates",
                capability=ANALYZE_DUPLICATES,
                status=StepStatus.COMPLETED,
            ),
            SpecialistTask(
                mission_id=mission.mission_id,
                agent_id="data-analyst",
                objective="outliers",
                capability=ANALYZE_OUTLIERS,
                status=StepStatus.COMPLETED,
            ),
        ],
    )
    mission.evidence_records = [
        EvidenceRecord(tool_name=PROFILE_DATASET, execution_status="COMPLETED"),
        EvidenceRecord(tool_name=ANALYZE_DUPLICATES, execution_status="COMPLETED"),
        EvidenceRecord(tool_name=ANALYZE_OUTLIERS, execution_status="COMPLETED"),
    ]
    return mission


def test_dataset_signature_has_no_raw_values() -> None:
    mission = Mission(goal="Investigate quality", dataset_id="ds")
    mission.dataset_profile = __import__(
        "atlas.investigation.profile", fromlist=["build_profile"]
    ).build_profile(_frame())
    signature = dataset_signature(mission)
    blob = " ".join(signature.column_names + signature.column_types + [signature.fingerprint])
    raw = (FIXTURES_DIR / "survey_quality.csv").read_text(encoding="utf-8")
    assert "not_a_number" not in blob
    assert "1500" not in blob
    assert "not_a_number" in raw
    assert signature.has_numeric is True
    assert "age" in signature.column_names


def test_confidence_is_bounded_and_sample_sensitive() -> None:
    one = compute_confidence(
        runs=1, success_rate=1.0, average_evidence=1.0, average_efficiency=1.0
    )
    two = compute_confidence(
        runs=2, success_rate=1.0, average_evidence=1.0, average_efficiency=1.0
    )
    many = compute_confidence(
        runs=40, success_rate=1.0, average_evidence=1.0, average_efficiency=1.0
    )
    failed = compute_confidence(
        runs=4, success_rate=0.2, average_evidence=0.3, average_efficiency=0.2
    )
    assert 0.0 < one < 0.60
    assert two >= 0.60
    assert many <= 0.95
    assert failed < two


def test_forbidden_capabilities_cannot_be_recommended() -> None:
    assert "EXECUTE_SHELL" not in recommendable_observations(
        ["profile_dataset", "EXECUTE_SHELL", "analyze_outliers"]
    )
    record = StrategyRecord(
        fingerprint="x",
        mission_category=MissionCategory.DATA_QUALITY,
        dataset_characteristics=DatasetCharacteristics(),
        recommended_capabilities=["EXECUTE_SHELL"],
        historical_runs=1,
        confidence=0.9,
    )
    with pytest.raises(StrategyValidationError):
        assert_strategy_safe(record)


@pytest.mark.asyncio
async def test_successful_mission_produces_experience_without_duplicates(
    tmp_path: Path,
) -> None:
    repo = SQLiteLearningRepository(tmp_path / "learn.db")
    settings = _settings()
    raw = (FIXTURES_DIR / "survey_quality.csv").read_bytes()
    storage = LocalFileStorage(tmp_path)
    await storage.save("source.csv", raw)
    mission = Mission(
        goal="Investigate data quality problems.",
        dataset_id="ds",
    )
    mission.working_copy = WorkingCopyState(
        source_dataset_id="ds",
        source_stored_filename="source.csv",
        source_original_filename="survey_quality.csv",
    )
    await Supervisor(
        reasoner=LocalFallbackReasoner(),
        settings=settings,
        plan_source=PlannerSource.LOCAL_FALLBACK,
        dataset_storage=storage,
        decision_maker=LocalDecisionMaker(),
    ).run(mission, ToolContext("ds", "survey_quality.csv", parse_csv_bytes(raw)), _noop)
    first, strategy = await record_experience_and_strategy(mission, repo, repo, settings)
    assert first is not None
    assert first.outcome in {ExperienceOutcome.SUCCESS, ExperienceOutcome.PARTIAL}
    assert 0.0 <= first.success_score <= 1.0
    assert 0.0 <= first.efficiency_score <= 1.0
    assert 0.0 <= first.evidence_score <= 1.0
    assert EventType.EXPERIENCE_RECORDED in {event.type for event in mission.events}
    again, _ = await record_experience_and_strategy(mission, repo, repo, settings)
    assert again is not None
    assert again.experience_id == first.experience_id
    listed = await repo.get_by_mission(mission.mission_id)
    assert listed is not None
    assert listed.experience_id == first.experience_id
    if strategy is not None:
        assert strategy.historical_runs == 1
        assert strategy.confidence < settings.strategy_min_confidence


@pytest.mark.asyncio
async def test_repeated_success_improves_confidence_and_failures_reduce_it(
    tmp_path: Path,
) -> None:
    repo = SQLiteLearningRepository(tmp_path / "learn.db")
    settings = _settings()
    mission = _completed_quality_mission()
    experience = evaluate_mission(mission, settings)
    strategy = strategy_from_experience(experience)
    assert strategy is not None
    assert strategy.confidence < 0.60
    second_mission = _completed_quality_mission("mission-b")
    second = evaluate_mission(second_mission, settings)
    merged = merge_strategy(strategy, second)
    assert merged.historical_runs == 2
    assert merged.confidence >= 0.60
    assert merged.confidence <= 0.95
    two_run_rate = merged.success_rate
    two_run_confidence = merged.confidence
    failed_mission = _completed_quality_mission("mission-fail")
    failed_mission.status = MissionStatus.FAILED
    failed = evaluate_mission(failed_mission, settings)
    assert failed.outcome == ExperienceOutcome.FAILURE
    assert failed.success_score == 0.0
    degraded = merge_strategy(merged, failed)
    assert degraded.failure_count >= 1
    assert degraded.success_rate < two_run_rate
    assert degraded.confidence <= two_run_confidence
    stored = await repo.upsert(experience)
    await repo.upsert(evaluate_mission(mission, settings))
    same = await repo.get_by_mission(mission.mission_id)
    assert same is not None
    assert same.experience_id == stored.experience_id


@pytest.mark.asyncio
async def test_retrieval_ranks_relevant_and_excludes_low_confidence(
    tmp_path: Path,
) -> None:
    repo = SQLiteLearningRepository(tmp_path / "learn.db")
    settings = _settings()
    relevant = StrategyRecord(
        fingerprint="rel",
        mission_category=MissionCategory.DATA_QUALITY,
        dataset_characteristics=DatasetCharacteristics(has_numeric=True),
        recommended_capabilities=[PROFILE_DATASET, ANALYZE_DUPLICATES, ANALYZE_OUTLIERS],
        historical_runs=3,
        success_rate=0.9,
        average_efficiency=0.8,
        average_evidence_score=0.85,
        confidence=0.78,
    )
    irrelevant = StrategyRecord(
        fingerprint="irr",
        mission_category=MissionCategory.CONSISTENCY,
        dataset_characteristics=DatasetCharacteristics(has_numeric=False),
        recommended_capabilities=[PROFILE_DATASET, "analyze_consistency"],
        historical_runs=8,
        success_rate=0.95,
        average_efficiency=0.9,
        average_evidence_score=0.9,
        confidence=0.9,
    )
    weak = StrategyRecord(
        fingerprint="weak",
        mission_category=MissionCategory.DATA_QUALITY,
        dataset_characteristics=DatasetCharacteristics(has_numeric=True),
        recommended_capabilities=[PROFILE_DATASET, ANALYZE_DUPLICATES],
        historical_runs=1,
        success_rate=1.0,
        average_efficiency=1.0,
        average_evidence_score=1.0,
        confidence=0.40,
    )
    await repo.upsert(relevant)
    await repo.upsert(irrelevant)
    await repo.upsert(weak)
    retriever = StrategyRetriever(repo, settings)
    results = await retriever.retrieve(
        StrategyQuery(
            goal="Check the dataset for duplicate rows.",
            category=MissionCategory.DUPLICATES,
            characteristics=DatasetCharacteristics(has_numeric=True),
            limit=3,
        )
    )
    assert results
    assert results[0].fingerprint == "rel"
    assert all(item.confidence >= settings.strategy_min_confidence for item in results)
    assert all(item.fingerprint != "weak" for item in results)
    assert len(results) <= settings.strategy_max_retrieval


@pytest.mark.asyncio
async def test_firestore_experience_and_strategy_roundtrip() -> None:
    repo = FirestoreLearningRepository(MemoryDocumentStore())
    settings = _settings()
    mission = _completed_quality_mission()
    experience = evaluate_mission(mission, settings)
    stored = await repo.upsert(experience)
    restored = document_to_experience(experience_to_document(stored))
    assert restored.mission_id == stored.mission_id
    again = await repo.get_by_mission(mission.mission_id)
    assert again is not None
    strategy = strategy_from_experience(stored)
    assert strategy is not None
    saved = await repo.upsert(strategy)
    fetched = await repo.get(saved.strategy_id)
    assert fetched is not None
    assert document_to_strategy(strategy_to_document(saved)).strategy_id == saved.strategy_id


@pytest.mark.asyncio
async def test_strategy_aware_mission_differs_from_baseline(tmp_path: Path) -> None:
    repo = SQLiteLearningRepository(tmp_path / "learn.db")
    settings = _settings()
    raw = (FIXTURES_DIR / "survey_quality.csv").read_bytes()
    storage = LocalFileStorage(tmp_path)
    await storage.save("source.csv", raw)
    mission_a = Mission(
        goal="Investigate data quality problems.",
        dataset_id="ds",
    )
    mission_a.working_copy = WorkingCopyState(
        source_dataset_id="ds",
        source_stored_filename="source.csv",
        source_original_filename="survey_quality.csv",
    )
    await Supervisor(
        reasoner=LocalFallbackReasoner(),
        settings=settings,
        plan_source=PlannerSource.LOCAL_FALLBACK,
        dataset_storage=storage,
        decision_maker=LocalDecisionMaker(),
    ).run(mission_a, ToolContext("ds", "survey_quality.csv", parse_csv_bytes(raw)), _noop)
    experience, _ = await record_experience_and_strategy(mission_a, repo, repo, settings)
    assert experience is not None
    twin = mission_a.model_copy(update={"mission_id": "mission-a2"})
    await record_experience_and_strategy(twin, repo, repo, settings)
    clean = (FIXTURES_DIR / "clean_numeric.csv").read_bytes()
    await storage.save("clean.csv", clean)
    retriever = StrategyRetriever(repo, settings)
    control = Mission(goal="Check the dataset for duplicate rows.", dataset_id="ds-control")
    control.working_copy = WorkingCopyState(
        source_dataset_id="ds-control",
        source_stored_filename="clean.csv",
        source_original_filename="clean_numeric.csv",
    )
    mission_b = Mission(goal="Check the dataset for duplicate rows.", dataset_id="ds-b")
    mission_b.working_copy = WorkingCopyState(
        source_dataset_id="ds-b",
        source_stored_filename="clean.csv",
        source_original_filename="clean_numeric.csv",
    )
    await Supervisor(
        reasoner=LocalFallbackReasoner(),
        settings=settings,
        plan_source=PlannerSource.LOCAL_FALLBACK,
        dataset_storage=storage,
        decision_maker=LocalDecisionMaker(),
    ).run(control, ToolContext("ds-control", "clean_numeric.csv", parse_csv_bytes(clean)), _noop)
    await Supervisor(
        reasoner=LocalFallbackReasoner(),
        settings=settings,
        plan_source=PlannerSource.LOCAL_FALLBACK,
        dataset_storage=storage,
        decision_maker=LocalDecisionMaker(),
        strategy_retriever=retriever,
    ).run(mission_b, ToolContext("ds-b", "clean_numeric.csv", parse_csv_bytes(clean)), _noop)
    control_caps = [
        task.capability
        for task in (control.delegation_plan.tasks if control.delegation_plan else [])
    ]
    capabilities = [
        task.capability
        for task in (mission_b.delegation_plan.tasks if mission_b.delegation_plan else [])
    ]
    assert ANALYZE_DUPLICATES in control_caps
    assert ANALYZE_OUTLIERS not in control_caps
    assert ANALYZE_DUPLICATES in capabilities
    assert ANALYZE_OUTLIERS in capabilities
    assert mission_b.strategy_ids_considered
    assert mission_b.strategy_ids_influenced
    context = build_reasoning_context(
        MissionWorkspace(
            mission=mission_b,
            tool_context=ToolContext("ds-b", "clean_numeric.csv", parse_csv_bytes(clean)),
            persist=_noop,
            lock=__import__("asyncio").Lock(),
            settings=settings,
            reasoner=LocalFallbackReasoner(),
            registry=default_registry(),
            plan_source=PlannerSource.LOCAL_FALLBACK,
            retrieved_strategies=await retriever.retrieve_for_mission(mission_b),
        )
    )
    assert context["historical_strategies"]
    assert "evidence" in context
    assert "relevant_memory" in context
    strategy_ids = {item["strategy_id"] for item in context["historical_strategies"]}
    evidence_ids = {item["evidence_id"] for item in context["evidence"]}
    assert strategy_ids.isdisjoint(evidence_ids)


@pytest.mark.asyncio
async def test_malicious_strategy_cannot_execute_or_bypass_validation() -> None:
    settings = _settings()
    mission = Mission(goal="Analyze this numeric CSV.", dataset_id="ds")
    workspace = _workspace(mission, settings)
    evil = StrategyRecord(
        fingerprint="evil",
        mission_category=MissionCategory.GENERAL,
        dataset_characteristics=DatasetCharacteristics(has_numeric=True),
        recommended_capabilities=["EXECUTE_SHELL", "FETCH_URL", "REMOVE_DUPLICATES"],
        historical_runs=9,
        success_rate=1.0,
        confidence=0.94,
    )
    from atlas.domain.models import DelegationPlan, SpecialistTask
    from atlas.investigation.profile import build_profile

    mission.dataset_profile = build_profile(_frame("clean_numeric.csv"))
    mission.delegation_plan = DelegationPlan(
        objective="analyze",
        source=PlannerSource.LOCAL_FALLBACK,
        tasks=[
            SpecialistTask(
                mission_id=mission.mission_id,
                agent_id="atlas.data_analyst",
                objective="profile",
                capability=PROFILE_DATASET,
                status=StepStatus.COMPLETED,
            )
        ],
    )
    workspace.retrieved_strategies = [evil]
    decision = LocalDecisionMaker().decide_from_workspace(workspace)
    if decision.tasks:
        assert all(task.capability != "EXECUTE_SHELL" for task in decision.tasks)
        assert all(task.capability != "FETCH_URL" for task in decision.tasks)
        assert all(task.capability != "REMOVE_DUPLICATES" for task in decision.tasks)
    follow_ups = strategy_follow_ups(workspace)
    assert all(item.capability != "EXECUTE_SHELL" for item in follow_ups)
    before = [item.id for item in workspace.registry.list_all()]
    with pytest.raises(ModelDecisionError):
        validate_decision(
            parse_model_decision(
                {
                    "decision": "DELEGATE",
                    "reason": "historical strategy said so",
                    "tasks": [{"capability": "EXECUTE_SHELL"}],
                }
            ),
            workspace,
        )
    with pytest.raises(ModelDecisionError):
        validate_decision(
            parse_model_decision(
                {
                    "decision": "ACTION",
                    "reason": "strategy said remediate",
                    "action": {
                        "type": "REMOVE_DUPLICATES",
                        "parameters": {"shell": "rm -rf /"},
                    },
                }
            ),
            workspace,
        )
    with pytest.raises(ModelDecisionError):
        validate_decision(
            parse_model_decision(
                {
                    "decision": "EXTERNAL",
                    "reason": "strategy said fetch",
                    "external": {"capability": "FETCH_URL", "arguments": {"url": "https://example.com"}},
                }
            ),
            workspace,
        )
    after = [item.id for item in workspace.registry.list_all()]
    assert before == after


@pytest.mark.asyncio
async def test_invalid_historical_capability_still_fails_validation() -> None:
    mission = Mission(goal="Analyze this numeric CSV.", dataset_id="ds")
    workspace = _workspace(mission, _settings())
    workspace.tool_context = ToolContext(
        "ds", "clean_numeric.csv", _frame("clean_numeric.csv")
    )
    with pytest.raises(ModelDecisionError, match="not in the bound dataset"):
        validate_decision(
            parse_model_decision(
                {
                    "decision": "OBSERVE",
                    "reason": "historical strategy mentioned a missing column",
                    "tool": {
                        "name": "inspect_column",
                        "arguments": {"column_name": "not_a_real_column"},
                    },
                }
            ),
            workspace,
        )


@pytest.mark.asyncio
async def test_learning_failure_does_not_fail_completed_mission(tmp_path: Path) -> None:
    class Boom:
        async def find_by_fingerprint(self, _fingerprint):
            raise RuntimeError("store down")

        async def upsert(self, _record):
            raise RuntimeError("store down")

        async def get_by_mission(self, _mission_id):
            return None

    mission = Mission(goal="Investigate", dataset_id="ds")
    mission.status = MissionStatus.COMPLETED
    stored, strategy = await record_experience_and_strategy(
        mission, Boom(), Boom(), _settings()
    )
    assert stored is None
    assert strategy is None
    assert mission.status == MissionStatus.COMPLETED
    assert EventType.STRATEGY_EXTRACTION_FAILED in {event.type for event in mission.events}


@pytest.mark.asyncio
async def test_learning_api_lists_records(tmp_path: Path) -> None:
    from atlas.api.dependencies import get_learning_service
    from atlas.main import create_app
    from atlas.services.learning_service import LearningService
    from httpx import ASGITransport, AsyncClient

    repo = SQLiteLearningRepository(tmp_path / "api-learn.db")
    mission = _completed_quality_mission("api-mission")
    experience = evaluate_mission(mission, _settings())
    stored_exp = await repo.upsert(experience)
    strategy = strategy_from_experience(experience)
    assert strategy is not None
    stored_strategy = await repo.upsert(strategy)
    app = create_app()
    app.dependency_overrides[get_learning_service] = lambda: LearningService(repo, repo)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        listed = await ac.get("/strategies")
        assert listed.status_code == 200
        payload = listed.json()
        assert payload["count"] >= 1
        detail = await ac.get(f"/strategies/{stored_strategy.strategy_id}")
        assert detail.status_code == 200
        assert "EXECUTE_SHELL" not in str(detail.json())
        experience_payload = await ac.get(f"/experiences/{mission.mission_id}")
        assert experience_payload.status_code == 200
        assert experience_payload.json()["experience_id"] == stored_exp.experience_id
        assert "not_a_number" not in experience_payload.text
        missing = await ac.get("/strategies/does-not-exist")
        assert missing.status_code == 404
