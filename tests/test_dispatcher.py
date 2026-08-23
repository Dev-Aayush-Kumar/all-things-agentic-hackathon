"""Dispatcher abstraction tests. No cloud resources required."""

import json

import pytest

from atlas.config.settings import Settings
from atlas.domain.exceptions import CloudDispatchError, CloudDispatchNotConfiguredError
from atlas.execution.dispatcher import LocalAsyncDispatcher
from atlas.execution.factory import create_dispatcher
from atlas.execution.pubsub_dispatcher import PubSubDispatcher
from atlas.execution.pubsub_messages import decode_execution_message


class _UnusedWorker:
    """Factory only needs a worker object for the local dispatcher path."""


class _Future:
    def result(self, timeout=None):
        return "msg-1"


class RecordingPublisher:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bytes, dict]] = []

    def topic_path(self, project: str, topic: str) -> str:
        return f"projects/{project}/topics/{topic}"

    def publish(self, topic_path: str, data: bytes, **attributes: str) -> _Future:
        self.calls.append((topic_path, data, attributes))
        return _Future()


class FailingPublisher:
    def topic_path(self, project: str, topic: str) -> str:
        return f"projects/{project}/topics/{topic}"

    def publish(self, topic_path: str, data: bytes, **attributes: str):
        raise RuntimeError("pubsub unavailable")


def _settings(**kwargs) -> Settings:
    return Settings(_env_file=None, **kwargs)


def test_factory_default_is_local() -> None:
    settings = _settings(dispatcher="local", runtime_mode="local")
    dispatcher = create_dispatcher(settings, worker=_UnusedWorker())  # type: ignore[arg-type]
    assert isinstance(dispatcher, LocalAsyncDispatcher)
    assert dispatcher.backend_name == "local_async"


def test_factory_pubsub_without_config_raises() -> None:
    settings = _settings(dispatcher="pubsub", runtime_mode="cloud")
    with pytest.raises(CloudDispatchNotConfiguredError, match="ATLAS_PUBSUB_TOPIC"):
        create_dispatcher(settings, worker=_UnusedWorker())  # type: ignore[arg-type]


def test_factory_pubsub_returns_real_dispatcher() -> None:
    settings = _settings(
        dispatcher="pubsub",
        google_cloud_project="demo-project",
        pubsub_topic="atlas-missions",
    )
    dispatcher = create_dispatcher(settings)
    assert isinstance(dispatcher, PubSubDispatcher)
    assert dispatcher.backend_name == "pubsub"
    assert dispatcher.configured is True


@pytest.mark.asyncio
async def test_pubsub_dispatcher_message_shape() -> None:
    publisher = RecordingPublisher()
    dispatcher = PubSubDispatcher(
        project="demo-project",
        topic="atlas-missions",
        publisher=publisher,
    )
    await dispatcher.dispatch("mission-123")
    assert len(publisher.calls) == 1
    topic, data, attributes = publisher.calls[0]
    assert topic == "projects/demo-project/topics/atlas-missions"
    payload = json.loads(data.decode("utf-8"))
    assert payload == {"mission_id": "mission-123", "source": "atlas"}
    assert "dataset" not in payload
    assert "api_key" not in payload
    assert attributes["mission_id"] == "mission-123"
    assert attributes["source"] == "atlas"
    assert decode_execution_message(data) == "mission-123"


@pytest.mark.asyncio
async def test_pubsub_dispatcher_publish_failure_is_explicit() -> None:
    dispatcher = PubSubDispatcher(
        project="demo-project",
        topic="atlas-missions",
        publisher=FailingPublisher(),
    )
    with pytest.raises(CloudDispatchError, match="Failed to publish"):
        await dispatcher.dispatch("mission-123")
