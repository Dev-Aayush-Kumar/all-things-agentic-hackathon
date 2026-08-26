"""ATLAS - Autonomous operations agent."""

from atlas.runtime.tls import configure_native_tls

__version__ = "0.12.0"

# Must run before Google GenAI / ADK / httpx clients are constructed.
configure_native_tls()
