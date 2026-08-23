"""Google Cloud Pub/Sub mission dispatcher.

Publishes a small mission_id message. Does not embed datasets or secrets.
"""

from __future__ import annotations

import asyncio
import logging

from atlas.domain.exceptions import CloudDispatchError, CloudDispatchNotConfiguredError
from atlas.execution.pubsub_messages import encode_execution_message

logger = logging.getLogger(__name__)

_MAX_PUBLISH_ATTEMPTS = 3


class PubSubDispatcher:
    """Publishes mission execution requests to a Pub/Sub topic."""

    def __init__(
        self,
        *,
        project: str,
        topic: str,
        publisher: object | None = None,
    ) -> None:
        if not project or not topic:
            raise CloudDispatchNotConfiguredError(
                "Pub/Sub dispatch requires GOOGLE_CLOUD_PROJECT and ATLAS_PUBSUB_TOPIC"
            )
        self._project = project
        self._topic = topic
        self._publisher = publisher

    @property
    def backend_name(self) -> str:
        return "pubsub"

    @property
    def configured(self) -> bool:
        return True

    def _ensure_publisher(self) -> object:
        if self._publisher is not None:
            return self._publisher
        try:
            from google.cloud import pubsub_v1
        except ImportError as exc:
            raise CloudDispatchNotConfiguredError(
                "google-cloud-pubsub is not installed"
            ) from exc
        try:
            self._publisher = pubsub_v1.PublisherClient()
        except Exception as exc:
            raise CloudDispatchError(f"Failed to create Pub/Sub publisher: {exc}") from exc
        return self._publisher

    async def dispatch(self, mission_id: str) -> None:
        payload = encode_execution_message(mission_id)
        publisher = self._ensure_publisher()
        topic_path = publisher.topic_path(self._project, self._topic)  # type: ignore[attr-defined]
        last_error: Exception | None = None

        for attempt in range(_MAX_PUBLISH_ATTEMPTS):
            try:
                message_id = await asyncio.to_thread(
                    _publish_once, publisher, topic_path, payload, mission_id
                )
                logger.info(
                    "Published mission %s to Pub/Sub message %s",
                    mission_id,
                    message_id,
                )
                return
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "Pub/Sub publish attempt %s/%s failed for mission %s: %s",
                    attempt + 1,
                    _MAX_PUBLISH_ATTEMPTS,
                    mission_id,
                    exc,
                )
                if attempt + 1 < _MAX_PUBLISH_ATTEMPTS:
                    await asyncio.sleep(0.25 * (2**attempt))

        raise CloudDispatchError(
            f"Failed to publish mission '{mission_id}' to Pub/Sub: {last_error}"
        )


def _publish_once(publisher: object, topic_path: str, payload: bytes, mission_id: str) -> str:
    future = publisher.publish(  # type: ignore[attr-defined]
        topic_path,
        payload,
        mission_id=mission_id,
        source="atlas",
    )
    return str(future.result(timeout=30))
