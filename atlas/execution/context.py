"""Ownership context for a claimed mission execution."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ExecutionContext:
    """Identifies the worker that currently owns a mission execution."""

    execution_id: str
    worker_id: str
