"""Stable fingerprints for persisted commands and evidence."""

import json
from decimal import Decimal
from hashlib import sha256
from uuid import UUID

from pydantic import BaseModel


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
