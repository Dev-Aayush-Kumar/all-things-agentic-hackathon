"""Domain exceptions."""


class DatasetNotFoundError(Exception):
    """Raised when a referenced dataset does not exist."""

    def __init__(self, dataset_id: str) -> None:
        self.dataset_id = dataset_id
        super().__init__(f"Dataset '{dataset_id}' not found")


class DatasetValidationError(Exception):
    """Raised when an uploaded file is rejected."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class DatasetParseError(Exception):
    """Raised when a stored CSV cannot be parsed for investigation."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class IdempotencyConflictError(Exception):
    """Raised when an idempotency key is reused with a different payload."""

    def __init__(self, idempotency_key: str) -> None:
        self.idempotency_key = idempotency_key
        super().__init__(
            f"Idempotency key '{idempotency_key}' was already used with a different mission payload"
        )


class StaleExecutionError(Exception):
    """Raised when a worker no longer owns the mission execution lease."""

    def __init__(self, mission_id: str, message: str | None = None) -> None:
        self.mission_id = mission_id
        super().__init__(message or f"Execution lease for mission '{mission_id}' is no longer valid")


class CloudPersistenceError(Exception):
    """Raised when a cloud persistence backend fails."""


class CloudStorageError(Exception):
    """Raised when Cloud Storage (or the configured object store) fails."""


class CloudDispatchError(Exception):
    """Raised when publishing a mission for execution fails."""


class CloudDispatchNotConfiguredError(Exception):
    """Raised when Pub/Sub dispatch is selected but not configured."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


class ActionAuthorizationError(Exception):
    """Raised when an agent is not allowed to execute an action."""


class ActionValidationError(Exception):
    """Raised when action parameters are missing or invalid."""


class UnknownActionError(Exception):
    """Raised when an action type is not registered."""


class ModelDecisionError(Exception):
    """Raised when a model decision is missing, malformed, or not allowlisted."""


class UnknownExternalToolError(Exception):
    """Raised when an external capability is not registered."""


class ExternalToolAuthorizationError(Exception):
    """Raised when a registered external tool is not authorized for this mission."""


class ExternalToolValidationError(Exception):
    """Raised when external-tool arguments or destination policy fail."""


class ExternalToolExecutionError(Exception):
    """Raised when a registered external tool fails during execution."""


class MemoryValidationError(Exception):
    """Raised when a memory proposal is malformed or not allowlisted."""


class UnknownMemoryError(Exception):
    """Raised when a memory id is not found."""


class ActionExecutionError(Exception):
    """Raised when a registered action fails during execution or verification."""


class MissionNotExecutableError(Exception):
    """Raised when a worker message refers to a mission that cannot be run."""

    def __init__(self, mission_id: str, reason: str) -> None:
        self.mission_id = mission_id
        self.reason = reason
        super().__init__(f"Mission '{mission_id}' is not executable: {reason}")
