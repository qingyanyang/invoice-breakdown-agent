"""Coordinate external file dispatch with changes to a task's delivery inputs."""

from contextlib import asynccontextmanager

from anyio import CancelScope, fail_after
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

LOCK = text("SELECT pg_try_advisory_lock(hashtextextended(:key, 0))")
UNLOCK = text("SELECT pg_advisory_unlock(hashtextextended(:key, 0))")
WRITER_LOCK = text("SELECT pg_try_advisory_xact_lock_shared(hashtextextended(:key, 0))")


class DeliveryBusy(Exception):
    """A delivery or a competing mutation currently owns the gate."""


def guard_key(task_id):
    return {"key": f"invoice-breakdown:delivery:{task_id}"}


async def guard_delivery_inputs(connection, task_id):
    # Writers may already hold the task row lock. Try the gate without waiting
    # so the dispatch-side check can acquire the task row after rollback.
    if not await connection.scalar(WRITER_LOCK, guard_key(task_id)):
        raise HTTPException(503, {"code": "delivery_in_progress", "message": "Retry after the current delivery finishes"})


@asynccontextmanager
async def hold_delivery_guard(engine, task_id):
    params = guard_key(task_id)
    async with engine.connect() as connection:
        held = False
        try:
            with fail_after(5):
                held = await connection.scalar(LOCK, params)
                await connection.commit()
            if not held:
                raise DeliveryBusy
            # The session lock remains held after commit. No transaction is
            # open during external dispatch.
            yield
        finally:
            with CancelScope(shield=True):
                if held:
                    try:
                        with fail_after(3):
                            await connection.scalar(UNLOCK, params)
                            await connection.commit()
                    except (SQLAlchemyError, TimeoutError):
                        await connection.invalidate()
                elif connection.in_transaction():
                    await connection.invalidate()
