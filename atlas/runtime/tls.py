"""Outbound TLS using the operating-system certificate store.

Python's default SSL stack (and therefore httpx / google-genai / ADK) often
cannot see Windows corporate or native CAs. truststore injects the OS trust
store into ssl.SSLContext without disabling verification.
"""

from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_configured = False
_attempted = False


def configure_native_tls() -> bool:
    """Install OS trust-store verification for subsequent SSL contexts.

    Idempotent. Never sets verify=False or unverified SSL contexts.
    Returns True when the native store is in use.
    """
    global _configured, _attempted
    with _lock:
        if _configured:
            return True
        if _attempted:
            return False
        _attempted = True
        try:
            import truststore
        except ImportError:
            logger.warning(
                "truststore is not installed; Python will use its default CA bundle"
            )
            return False
        try:
            truststore.inject_into_ssl()
        except Exception:
            logger.warning(
                "Native OS TLS trust store could not be injected; "
                "falling back to the default Python CA bundle",
                exc_info=True,
            )
            return False
        _configured = True
        logger.info("Outbound TLS uses the native OS certificate store")
        return True


def native_tls_configured() -> bool:
    """Whether OS trust-store injection succeeded in this process."""
    return _configured
