"""Local filesystem storage for uploaded datasets."""

from pathlib import Path


class LocalFileStorage:
    """Stores uploads under a configured directory using generated names."""

    backend_name = "local_fs"

    def __init__(self, root: Path) -> None:
        self._root = root

    def _safe_path(self, stored_filename: str) -> Path:
        if not stored_filename or stored_filename != Path(stored_filename).name:
            raise ValueError("stored_filename must be a basename with no path components")
        if ".." in stored_filename:
            raise ValueError("stored_filename must not contain '..'")
        root = self._root.resolve()
        path = (self._root / stored_filename).resolve()
        if not path.is_relative_to(root):
            raise ValueError("path traversal is not allowed")
        return path

    async def save(self, stored_filename: str, content: bytes) -> str:
        path = self._safe_path(stored_filename)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return stored_filename

    async def load(self, stored_filename: str) -> bytes:
        path = self._safe_path(stored_filename)
        if not path.exists():
            raise FileNotFoundError(f"Stored dataset '{stored_filename}' was not found")
        return path.read_bytes()
