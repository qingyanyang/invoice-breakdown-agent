"""Bind normalized mutation requests to durable, owner-scoped receipts."""

from dataclasses import dataclass
from hashlib import sha256

from pydantic_core import to_jsonable_python
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from app.outcomes import FailureKind, UseCaseError
from app.identity import fingerprint
from app.execution_tables import request_receipts

@dataclass(frozen=True)
class ReceiptKey:
    operator: str
    operation: str
    key_sha256: str
    payload_sha256: str

    @property
    def identity(self):
        return {
            "operator": self.operator, "operation": self.operation, "key_sha256": self.key_sha256,
        }

    @classmethod
    def from_request(cls, operator, operation, key, payload):
        if key is None:
            return None
        return cls(operator, operation, sha256(key.encode()).hexdigest(), fingerprint(payload))


async def reserve_receipt(connection, key: ReceiptKey | None):
    if key is None:
        return None
    await connection.execute(
        insert(request_receipts).values(**key.identity, payload_sha256=key.payload_sha256).on_conflict_do_nothing()
    )
    receipt = (await connection.execute(
        select(request_receipts).filter_by(**key.identity).with_for_update(),
    )).mappings().one()
    if receipt["payload_sha256"] != key.payload_sha256:
        raise UseCaseError(FailureKind.CONFLICT, "Idempotency key is already bound to different input")
    return receipt["response"]


async def bind_receipt_context(connection, key: ReceiptKey | None, digest: str):
    """Bind a pending mutation to the effective inputs from its first claim."""
    if key is None:
        return
    previous = await connection.scalar(
        select(request_receipts.c.context_sha256).filter_by(**key.identity).with_for_update(),
    )
    if previous is None:
        await connection.execute(
            update(request_receipts).filter_by(**key.identity).values(context_sha256=digest),
        )
    elif previous != digest:
        raise UseCaseError(FailureKind.CONFLICT, "Inputs have changed since this request was accepted")


async def save_receipt(connection, key: ReceiptKey | None, response):
    if key is not None:
        await connection.execute(update(request_receipts).filter_by(**key.identity).values(
            response=to_jsonable_python(response, by_alias=True),
        ))
    return response
