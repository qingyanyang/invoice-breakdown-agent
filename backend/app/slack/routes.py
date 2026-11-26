"""Acknowledge durable Slack ingress and process it outside the webhook."""

import logging

import httpx
from anyio import CancelScope, fail_after, sleep
from fastapi import APIRouter, HTTPException, Request, Response
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.config import settings
from app.slack.core import Core, CoreError
from app.slack.protocol import SlackClient, SlackError, authenticated_payload, normalize
from app.slack.service import Conversation, notice_for
from app.slack.store import MAX_EVENT_ATTEMPTS, claim_event, enqueue, finish_event
from app.tasks import database_for

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/integrations/slack", tags=["Slack DM"])
LOCK = text("SELECT pg_try_advisory_lock(hashtextextended(:key, 0))")
UNLOCK = text("SELECT pg_advisory_unlock(hashtextextended(:key, 0))")
EVENT_TIMEOUT = 900


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
                await enqueue(database_for(request), *event)
    except TimeoutError:
        raise HTTPException(503, "Slack event could not be queued") from None
    return Response(status_code=200)


def core_failure(error):
    if error.status in {401, 403, 404, 409}:
        return "stale_control", False, notice_for("stale_control")
    if error.status in {400, 413, 415, 422} and error.step == "source":
        return "invalid_workbook", False, "上传未通过工作簿检查。请提交符合要求的 .xlsx 测试文件。"
    if error.status in {400, 422} and error.step == "answers":
        return "invalid_answer", False, "答案未通过复核。请查看当前问题并提交有效选项和必要说明。"
    code = error.code or f"core_{error.status}"
    detail = f" {error.detail}" if error.detail else ""
    return code, error.status in {429, 502, 503, 504}, (
        f"流程暂停（{error.step} · {code}）。{detail} 请在原任务消息串发送 retry。"
    )


async def process_event(app, slack, config, row):
    event = row["event"]
    engine = app.state.database_engine
    binding = event["binding"]
    if binding["team"] != config.team or config.operators.get(binding["user"]) != binding["operator"]:
        await finish_event(engine, row, "failed", "operator_mapping_changed")
        return
    core = Core(app, binding["operator"])
    conversation = Conversation(engine, slack, core, config, event, row["key"])
    try:
        with fail_after(EVENT_TIMEOUT):
            await conversation.handle()
    except SlackError as error:
        code, retryable, notice = error.code, error.retryable, notice_for(error.code)
    except CoreError as error:
        code, retryable, notice = core_failure(error)
    except HTTPException as error:
        code, retryable, notice = core_failure(CoreError("source", error.status_code))
    except TimeoutError:
        code, retryable, notice = "event_timeout", True, notice_for("event_timeout")
    except Exception as error:
        logger.warning("Slack event processing failed: %s", type(error).__name__)
        code, retryable, notice = "event_processing_failed", False, notice_for("event_processing_failed")
    else:
        await finish_event(engine, row, "done")
        return
    finally:
        with CancelScope(shield=True):
            await core.close()

    if retryable and row["attempts"] < MAX_EVENT_ATTEMPTS:
        await finish_event(engine, row, "queued", code)
        return
    state = "delivery_unknown" if code == "delivery_unknown" else "failed"
    await finish_event(engine, row, state, code)
    if conversation.verified and code != "slack_recipient_unverified":
        try:
            await slack.verify_dm(binding)
            await conversation.tell(notice)
        except SlackError as error:
            logger.warning("Slack status notice unavailable: %s", error.code)


async def process_one(app, slack, config):
    """Serialize events across replicas without keeping a transaction open."""
    engine = app.state.database_engine
    if engine is None:
        return False
    params = {"key": f"{settings.postgres_schema}:invoice-breakdown:slack:{config.team}"}
    async with engine.connect() as lock:
        held = False
        attempted = False
        try:
            with fail_after(5):
                attempted = True
                held = await lock.scalar(LOCK, params)
                await lock.commit()
            if not held:
                return False
            row = await claim_event(engine)
            if row is None:
                return False
            await process_event(app, slack, config, row)
            return True
        finally:
            if attempted:
                with CancelScope(shield=True):
                    if held:
                        try:
                            with fail_after(3):
                                await lock.scalar(UNLOCK, params)
                                await lock.commit()
                        except (SQLAlchemyError, TimeoutError):
                            await lock.invalidate()
                    elif lock.in_transaction():
                        # The server may have granted the lock just before cancellation.
                        await lock.invalidate()


async def run_worker(app, config):
    async with httpx.AsyncClient(timeout=httpx.Timeout(12), follow_redirects=False, trust_env=False) as client:
        slack = SlackClient(client, config)
        verified = False
        while True:
            try:
                if not verified:
                    await slack.verify_workspace(config.team)
                    verified = True
                if await process_one(app, slack, config):
                    continue
            except SlackError as error:
                logger.warning("Slack workspace authentication unavailable: %s", error.code)
                await sleep(15)
            except (SQLAlchemyError, HTTPException, TimeoutError) as error:
                logger.warning("Slack inbox temporarily unavailable: %s", type(error).__name__)
            await sleep(1)
