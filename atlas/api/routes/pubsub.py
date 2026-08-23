"""Pub/Sub push endpoint for the Cloud Run worker."""

from __future__ import annotations

import binascii
import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from atlas.api.dependencies import get_mission_worker
from atlas.domain.exceptions import (
    CloudPersistenceError,
    CloudStorageError,
    MissionNotExecutableError,
)
from atlas.execution.pubsub_handler import handle_mission_message
from atlas.execution.pubsub_messages import parse_push_envelope
from atlas.execution.worker import MissionWorker

logger = logging.getLogger(__name__)

router = APIRouter(tags=["pubsub"])


@router.post("/internal/pubsub/push")
async def pubsub_push(
    body: dict[str, Any],
    worker: MissionWorker = Depends(get_mission_worker),
) -> dict[str, str]:
    """Receive a Pub/Sub push message and execute the identified mission.

    The payload must only identify a durable mission. Authoritative state is
    loaded from the configured repository. Invalid messages are rejected with
    400 so they are not retried forever. Transient cloud failures return 503.
    """
    try:
        mission_id = parse_push_envelope(body)
    except (ValueError, binascii.Error, json.JSONDecodeError, KeyError, TypeError) as exc:
        logger.warning("Rejected invalid Pub/Sub push payload: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid Pub/Sub payload",
        ) from exc

    try:
        outcome = await handle_mission_message(
            mission_id,
            repository=worker.repository,
            worker=worker,
        )
    except MissionNotExecutableError as exc:
        logger.info("Pub/Sub message ignored: %s", exc)
        return {"status": "ignored", "reason": exc.reason, "mission_id": mission_id}
    except CloudPersistenceError as exc:
        logger.exception("Persistence unavailable while handling mission %s", mission_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Persistence unavailable",
        ) from exc
    except CloudStorageError as exc:
        logger.exception("Storage unavailable while handling mission %s", mission_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Storage unavailable",
        ) from exc
    except Exception as exc:
        logger.exception("Unexpected worker failure for mission %s", mission_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Mission execution failed",
        ) from exc

    return {"status": outcome, "mission_id": mission_id}
