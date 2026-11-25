"""Bind normalized mutation requests to durable, owner-scoped receipts."""

import json
from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
from typing import Annotated
from uuid import UUID

from fastapi import Header, HTTPException
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from app.execution_tables import request_receipts

RequestKey = Annotated[
    str | None, Header(alias="Idempotency-Key", min_length=1, max_length=128, pattern=r"^[!-~]+$"),
]


def encode_identity(value):
    if isinstance(value, BaseModel):
        return value.model_dump(mode="python")
    if isinstance(value, Decimal):
        if not value:
            return "0"
        fixed = format(value, "f")
        return fixed.rstrip("0").rstrip(".") if "." in fixed else fixed
    if isinstance(value, UUID):
        return str(value)
    raise TypeError("Unsupported value in request identity")


def fingerprint(value) -> str:
    encoded = json.dumps(value, default=encode_identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return sha256(encoded.encode()).hexdigest()


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
        raise HTTPException(409, "Idempotency key is already bound to different input")
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
        raise HTTPException(409, "Inputs have changed since this request was accepted")


async def save_receipt(connection, key: ReceiptKey | None, response):
    if key is not None:
        await connection.execute(update(request_receipts).filter_by(**key.identity).values(
            response=jsonable_encoder(response),
        ))
    return response
