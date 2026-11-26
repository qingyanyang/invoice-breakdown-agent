import json
import re
from hashlib import sha256
from hmac import compare_digest
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import SecretStr

bearer = HTTPBearer(auto_error=False)


def operator_credentials(secret: SecretStr | None) -> dict[str, bytes]:
    if secret is None:
        return {}
    try:
        entries = json.loads(secret.get_secret_value())
        if not isinstance(entries, dict) or not entries:
            raise ValueError
        credentials = {}
        for name, token in entries.items():
            if not re.fullmatch(r"[a-zA-Z0-9_.@-]{1,128}", name):
                raise ValueError
            if not isinstance(token, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{32,256}", token):
                raise ValueError
            credentials[name] = sha256(token.encode()).digest()
        if len(set(credentials.values())) != len(credentials):
            raise ValueError
        return credentials
    except ValueError:
        raise ValueError("OPERATOR_TOKENS must map distinct operator names to distinct tokens") from None


def require_operator(
    request: Request,
    authorization: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> str:
    credentials = request.app.state.operator_credentials
    if not credentials:
        raise HTTPException(503, "Operator authentication unavailable")
    if authorization is not None:
        provided = sha256(authorization.credentials.encode()).digest()
        matched = None
        for operator, expected in credentials.items():
            if compare_digest(provided, expected):
                matched = operator
        if matched is not None:
            return matched
    raise HTTPException(401, "Operator authentication required", {"WWW-Authenticate": "Bearer"})


Operator = Annotated[str, Depends(require_operator)]
