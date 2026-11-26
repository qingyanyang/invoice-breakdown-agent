"""Persist only normalized inbound data and the receipts needed for retries."""

from datetime import timedelta

from sqlalchemy import and_, func, select, update
from sqlalchemy.dialects.postgresql import insert

from app.request_receipts import fingerprint
from app.slack.tables import controls, files, inbox, threads, visual_votes
from app.tasks import transaction

MAX_EVENT_ATTEMPTS = 3


async def enqueue(engine, key, event):
    async with transaction(engine) as connection:
        await connection.execute(insert(inbox).values(
            key=key, event=event, state="queued", attempts=0,
        ).on_conflict_do_nothing())


async def claim_event(engine):
    # The Slack worker owns a deployment-wide advisory lock while it claims
    # and handles an event. A running row here therefore belongs to a dead
    # worker and can be resumed without waiting through human review.
    async with transaction(engine) as connection:
        await connection.execute(update(inbox).where(
            inbox.c.state.in_(["queued", "running"]),
            inbox.c.attempts >= MAX_EVENT_ATTEMPTS,
        ).values(state="failed", error="event_attempts_exhausted"))
        key = await connection.scalar(select(inbox.c.key).where(
            inbox.c.state.in_(["queued", "running"]), inbox.c.available_at <= func.now(),
            inbox.c.attempts < MAX_EVENT_ATTEMPTS,
        ).order_by(inbox.c.available_at, inbox.c.created_at).limit(1))
        if key is None:
            return None
        result = await connection.execute(update(inbox).where(inbox.c.key == key).values(
            state="running", attempts=inbox.c.attempts + 1,
        ).returning(inbox))
        return result.mappings().one()


async def finish_event(engine, row, state, error=None):
    async with transaction(engine) as connection:
        await connection.execute(update(inbox).where(
            inbox.c.key == row["key"], inbox.c.attempts == row["attempts"], inbox.c.state == "running",
        ).values(
            state=state, error=error, available_at=func.now() + timedelta(seconds=10 * row["attempts"]),
        ))


class Store:
    def __init__(self, engine, binding):
        self.engine = engine
        self.binding = binding
        self.thread_key = fingerprint(binding)

    async def thread(self):
        async with transaction(self.engine) as connection:
            return (await connection.execute(select(threads).where(
                threads.c.key == self.thread_key,
            ))).mappings().one_or_none()

    async def bind_task(self, task_id, revision, source_event):
        async with transaction(self.engine) as connection:
            await connection.execute(insert(threads).values(
                key=self.thread_key, binding={**self.binding, "source_event": source_event},
                task_id=task_id, revision=revision,
            ).on_conflict_do_nothing())

    async def set_uploaded(self, expected, revision):
        async with transaction(self.engine) as connection:
            await connection.execute(update(threads).where(
                threads.c.key == self.thread_key, threads.c.revision == expected,
            ).values(revision=revision))

    async def create_control(self, payload):
        key = fingerprint([self.thread_key, payload])
        async with transaction(self.engine) as connection:
            await connection.execute(insert(controls).values(
                id=key, thread_key=self.thread_key, payload=payload,
            ).on_conflict_do_nothing())
        return key

    async def control(self, key):
        async with transaction(self.engine) as connection:
            return await connection.scalar(select(controls.c.payload).where(
                controls.c.id == key, controls.c.thread_key == self.thread_key,
            ))

    async def vote(self, qa_id, sheet, outcome):
        async with transaction(self.engine) as connection:
            key = {"thread_key": self.thread_key, "qa_id": qa_id, "sheet": sheet}
            await connection.execute(insert(visual_votes).values(**key, outcome=outcome).on_conflict_do_nothing())
            result = await connection.execute(select(visual_votes.c.sheet, visual_votes.c.outcome).where(
                visual_votes.c.thread_key == self.thread_key, visual_votes.c.qa_id == qa_id,
            ))
            return {name: value for name, value in result}

    async def file(self, key):
        async with transaction(self.engine) as connection:
            return (await connection.execute(select(files).where(
                files.c.key == key, files.c.thread_key == self.thread_key,
            ))).mappings().one_or_none()

    async def stage_file(self, key, file_id):
        # Only an object never submitted to Slack's completion endpoint can
        # be replaced. Such bytes have never been shared in the conversation.
        async with transaction(self.engine) as connection:
            statement = insert(files).values(key=key, thread_key=self.thread_key, file_id=file_id, state="staged")
            accepted = await connection.scalar(statement.on_conflict_do_update(
                index_elements=[files.c.key],
                set_={"file_id": file_id, "updated_at": func.now()},
                where=and_(files.c.state == "staged", files.c.thread_key == self.thread_key),
            ).returning(files.c.file_id))
            return accepted == file_id

    async def transition_file(self, key, file_id, allowed, state):
        async with transaction(self.engine) as connection:
            return await connection.scalar(update(files).where(
                files.c.key == key, files.c.thread_key == self.thread_key, files.c.file_id == file_id,
                files.c.state.in_(allowed),
            ).values(state=state, updated_at=func.now()).returning(files.c.file_id)) == file_id
