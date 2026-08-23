"""Dispatcher abstraction tests. No cloud resources required."""

import pytest

from atlas.config.settings import Settings
from atlas.domain.exceptions import CloudDispatchNotConfiguredError
from atlas.execution.dispatcher import PubSubDispatcherStub
from atlas.execution.factory import create_dispatcher


class _UnusedWorker:
    """Factory only needs a worker object for the local dispatcher path."""


def test_pubsub_stub_is_not_a_deployment() -> None:
    stub = PubSubDispatcherStub()
    assert stub.backend_name == "pubsub_stub"
    assert stub.configured is False


@pytest.mark.asyncio
async def test_pubsub_stub_refuses_to_dispatch() -> None:
    stub = PubSubDispatcherStub()
    with pytest.raises(CloudDispatchNotConfiguredError, match="not implemented"):
        await stub.dispatch("mission-1")


def test_factory_default_is_local() -> None:
    settings = Settings(dispatcher="local")
    dispatcher = create_dispatcher(settings, worker=_UnusedWorker())  # type: ignore[arg-type]
    assert dispatcher.backend_name == "local_async"


def test_factory_pubsub_returns_stub_not_a_live_client() -> None:
    settings = Settings(dispatcher="pubsub")
    dispatcher = create_dispatcher(settings, worker=_UnusedWorker())  # type: ignore[arg-type]
    assert isinstance(dispatcher, PubSubDispatcherStub)
    assert dispatcher.configured is False
