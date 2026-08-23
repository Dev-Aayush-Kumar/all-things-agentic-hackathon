"""CSV parsing for investigation."""

from io import BytesIO

import pandas as pd

from atlas.domain.exceptions import DatasetParseError


def parse_csv_bytes(content: bytes) -> pd.DataFrame:
    """Parse CSV bytes into a DataFrame of original string values.

    All columns are read as strings so type/format anomalies can be measured
    against the source values rather than pandas' coerced types.
    """
    if not content or not content.strip():
        raise DatasetParseError("CSV file is empty or contains no parseable content")
    if b"\x00" in content:
        raise DatasetParseError("CSV contains binary or NUL data and cannot be parsed")

    _reject_empty_header(content)

    last_error: Exception | None = None
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            frame = pd.read_csv(
                BytesIO(content),
                dtype=str,
                encoding=encoding,
                keep_default_na=True,
                na_values=["", "NA", "N/A", "na", "n/a", "null", "NULL", "None", "NaN"],
            )
            break
        except UnicodeDecodeError as exc:
            last_error = exc
            continue
        except pd.errors.EmptyDataError as exc:
            raise DatasetParseError("CSV file has no columns or header row") from exc
        except pd.errors.ParserError as exc:
            raise DatasetParseError(f"CSV could not be parsed: {exc}") from exc
        except Exception as exc:
            raise DatasetParseError(f"CSV could not be parsed: {exc}") from exc
    else:
        raise DatasetParseError(
            f"CSV encoding is not supported: {last_error}"
        ) from last_error

    frame.columns = [str(column).strip() for column in frame.columns]
    if any(column == "" for column in frame.columns) or len(set(frame.columns)) != len(
        frame.columns
    ):
        # Preserve empty/duplicate headers as-is after stripping; empty names are invalid.
        if any(column == "" for column in frame.columns):
            raise DatasetParseError("CSV contains an empty column name")

    if frame.shape[1] == 0:
        raise DatasetParseError("CSV file has no columns")

    return frame


def _reject_empty_header(content: bytes) -> None:
    """Reject CSVs whose header row has an empty column name."""
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            text = content.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        return

    first_line = text.splitlines()[0] if text.splitlines() else ""
    fields = [field.strip().strip('"').strip("'") for field in first_line.split(",")]
    if fields and any(field == "" for field in fields):
        raise DatasetParseError("CSV contains an empty column name")
