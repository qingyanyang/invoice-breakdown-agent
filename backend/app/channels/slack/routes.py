"""Verify and enqueue inbound Slack events."""

from anyio import fail_after
from fastapi import APIRouter, HTTPException, Request, Response

from app.channels.slack.protocol import authenticated_payload, normalize
from app.channels.slack.store import enqueue
from app.database import database_for
from app.http import http_error
from app.outcomes import UseCaseError

router = APIRouter(prefix="/integrations/slack", tags=["Slack DM"])


@router.post("/events")
async def events(request: Request):
    config = request.app.state.slack_config
    if config is None:
        raise HTTPException(404, "Slack DM integration is unavailable")
    try:
        with fail_after(2.5):
            payload = await authenticated_payload(request, config)
            if payload.get("type") == "url_verification":
                challenge = payload.get("challenge")
                if not isinstance(challenge, str) or not 1 <= len(challenge) <= 255:
                    raise HTTPException(400, "Invalid Slack challenge")
                return {"challenge": challenge}
            event = normalize(payload, config)
            if event is not None:
                await enqueue(database_for(request.app.state), *event)
    except TimeoutError:
        raise HTTPException(503, "Slack event could not be queued") from None
    except UseCaseError as error:
        raise http_error(error) from None
    return Response(status_code=200)
