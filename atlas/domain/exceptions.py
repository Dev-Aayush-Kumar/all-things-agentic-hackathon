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
