"""Domain enums."""

from enum import Enum


class MissionStatus(str, Enum):
    """Lifecycle states for a mission."""

    CREATED = "CREATED"
    PLANNING = "PLANNING"
    EXECUTING = "EXECUTING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class StepStatus(str, Enum):
    """Lifecycle states for a plan step."""

    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class EventType(str, Enum):
    """Types of mission events."""

    MISSION_CREATED = "MISSION_CREATED"
    PLANNING_STARTED = "PLANNING_STARTED"
    EXECUTION_PLAN_GENERATED = "EXECUTION_PLAN_GENERATED"
    EXECUTION_STARTED = "EXECUTION_STARTED"
    STEP_STARTED = "STEP_STARTED"
    STEP_COMPLETED = "STEP_COMPLETED"
    STEP_FAILED = "STEP_FAILED"
    MISSION_COMPLETED = "MISSION_COMPLETED"
    MISSION_FAILED = "MISSION_FAILED"


class PlannerSource(str, Enum):
    """Identifies which planner produced a plan."""

    GEMINI_ADK = "GEMINI_ADK"
    LOCAL_FALLBACK = "LOCAL_FALLBACK"
