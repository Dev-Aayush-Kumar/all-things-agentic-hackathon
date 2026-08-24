"""Native OS TLS trust-store initialization. Does not call Gemini."""

from __future__ import annotations

import ssl
import sys

import atlas
from atlas.runtime import tls


def test_atlas_import_configures_native_tls() -> None:
    assert atlas.__version__ == "0.12.0"
    assert tls.native_tls_configured() is True


def test_configure_native_tls_is_idempotent(monkeypatch) -> None:
    calls: list[str] = []

    class _Truststore:
        @staticmethod
        def inject_into_ssl() -> None:
            calls.append("inject")

    monkeypatch.setattr(tls, "_configured", False)
    monkeypatch.setattr(tls, "_attempted", False)
    monkeypatch.setitem(sys.modules, "truststore", _Truststore)
    assert tls.configure_native_tls() is True
    assert tls.configure_native_tls() is True
    assert calls == ["inject"]
    assert tls.native_tls_configured() is True


def test_missing_truststore_does_not_crash_or_disable_verify(monkeypatch) -> None:
    monkeypatch.setattr(tls, "_configured", False)
    monkeypatch.setattr(tls, "_attempted", False)

    real_import = __import__

    def _blocked(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "truststore":
            raise ImportError("simulated missing truststore")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr("builtins.__import__", _blocked)
    assert tls.configure_native_tls() is False
    assert tls.native_tls_configured() is False
    context = ssl.create_default_context()
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_native_tls_keeps_certificate_verification_enabled() -> None:
    tls.configure_native_tls()
    context = ssl.create_default_context()
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert getattr(ssl, "_create_unverified_context", None) is not ssl.create_default_context
