"""Process durable Slack events outside the webhook."""

import logging

import httpx
from anyio import CancelScope, fail_after, sleep
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.channels.slack.protocol import SlackClient, SlackError, SlackRateLimited
from app.channels.slack.service import Conversation, notice_for
from app.channels.slack.store import MAX_EVENT_ATTEMPTS, claim_event, defer_event, finish_event
from app.config import settings
from app.outcomes import FailureKind, UseCaseError
from app.workflows.pipeline import InvoiceWorkflow, StageRejected

logger = logging.getLogger(__name__)
LOCK = text("SELECT pg_try_advisory_lock(hashtextextended(:key, 0))")
UNLOCK = text("SELECT pg_advisory_unlock(hashtextextended(:key, 0))")
EVENT_TIMEOUT = 900


def core_failure(stage, kind, detail):
    if kind in {FailureKind.NOT_FOUND, FailureKind.CONFLICT}:
        return "stale_control", False, notice_for("stale_control")
    if stage == "source" and kind in {
        FailureKind.INVALID_REQUEST, FailureKind.TOO_LARGE, FailureKind.UNSUPPORTED_FORMAT, FailureKind.INVALID_DATA,
    }:
        return "invalid_workbook", False, "上传未通过工作簿检查。请提交符合要求的 .xlsx 测试文件。"
    if stage == "answers" and kind in {FailureKind.INVALID_REQUEST, FailureKind.INVALID_DATA}:
        return "invalid_answer", False, "答案未通过复核。请查看当前问题并提交有效选项和必要说明。"
    # Keep existing operator-visible diagnostic codes for events already in the inbox.
    legacy_codes = {
        FailureKind.INVALID_REQUEST: "core_400", FailureKind.TOO_LARGE: "core_413",
        FailureKind.UNSUPPORTED_FORMAT: "core_415", FailureKind.INVALID_DATA: "core_422",
        FailureKind.FAILED: "core_500", FailureKind.UNAVAILABLE: "core_503", FailureKind.TIMED_OUT: "core_504",
    }
    code = (detail.get("code") if isinstance(detail, dict) else None) or legacy_codes[kind]
    message = f" {detail[:400]}" if isinstance(detail, str) else ""
    return code, kind in {FailureKind.UNAVAILABLE, FailureKind.TIMED_OUT}, (
        f"流程暂停（{stage} · {code}）。{message} 请在原任务消息串发送 retry。"
    )


async def process_event(app, slack, config, row):
    event = row["event"]
    engine = app.state.database_engine
    binding = event["binding"]
    operator = binding["operator"]
    if (
        binding["team"] != config.team or config.operators.get(binding["user"]) != operator
        or operator not in app.state.operator_credentials
    ):
        await finish_event(engine, row, "failed", "operator_mapping_changed")
        return
    workflow = InvoiceWorkflow(app.state, operator)
    conversation = Conversation(engine, slack, workflow, config, event, row["key"])
    try:
        with fail_after(EVENT_TIMEOUT):
            await conversation.handle()
    except SlackRateLimited as error:
        if await defer_event(engine, row, error.delay, error.code):
            return
        code, retryable, notice = "event_deferral_exhausted", False, notice_for("event_deferral_exhausted")
    except SlackError as error:
        code, retryable, notice = error.code, error.retryable, notice_for(error.code)
    except StageRejected as error:
        rejection = error.rejection
        if rejection.kind == FailureKind.PENDING:
            if await defer_event(engine, row, rejection.retry_after, "core_execution_pending"):
                return
            code, retryable, notice = "event_deferral_exhausted", False, notice_for("event_deferral_exhausted")
        else:
            code, retryable, notice = core_failure(error.stage, rejection.kind, rejection.detail)
    except UseCaseError as error:
        code, retryable, notice = core_failure("source", error.kind, None)
    except TimeoutError:
        code, retryable, notice = "event_timeout", True, notice_for("event_timeout")
    except Exception as error:
        logger.warning("Slack event processing failed: %s", type(error).__name__)
        code, retryable, notice = "event_processing_failed", False, notice_for("event_processing_failed")
    else:
        await finish_event(engine, row, "done")
        return

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
            except (SQLAlchemyError, UseCaseError, TimeoutError) as error:
                logger.warning("Slack inbox temporarily unavailable: %s", type(error).__name__)
            await sleep(1)
