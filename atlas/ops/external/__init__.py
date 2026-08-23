"""Controlled external-tool layer. The model proposes; ATLAS executes."""

from atlas.ops.external.registry import CAPABILITY_FETCH_URL, ExternalToolRegistry

__all__ = [
    "CAPABILITY_FETCH_URL",
    "ExternalToolRegistry",
]
