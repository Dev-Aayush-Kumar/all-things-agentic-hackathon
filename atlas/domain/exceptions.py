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


class CloudDispatchNotConfiguredError(Exception):
    """Raised when a cloud dispatcher is selected but not implemented/configured."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
