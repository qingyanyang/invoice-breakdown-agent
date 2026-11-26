"""Call the existing application API in-process with an authenticated operator."""

import httpx

from app.operators import INTERNAL_OPERATOR
from app.request_receipts import fingerprint


class CoreError(Exception):
    def __init__(self, step, status, code=None, detail=None):
        self.step = step
        self.status = status
        self.code = code
        self.detail = detail
        super().__init__(step)


class Core:
    def __init__(self, app, operator):
        # An external HTTP client can never set this ASGI-only property.
        async def authenticated(scope, receive, send):
            await app({**scope, INTERNAL_OPERATOR: operator}, receive, send)

        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=authenticated), base_url="http://invoice.internal", trust_env=False,
        )

    async def close(self):
        await self.client.aclose()

    async def request(self, method, path, *, action=None, **kwargs):
        if action is not None:
            headers = {**kwargs.pop("headers", {}), "Idempotency-Key": "slack-" + fingerprint(action)}
            kwargs["headers"] = headers
        response = await self.client.request(method, path, **kwargs)
        if response.is_error:
            try:
                detail = response.json().get("detail")
            except ValueError:
                detail = None
            code = detail.get("code") if isinstance(detail, dict) else None
            message = detail[:400] if isinstance(detail, str) else None
            raise CoreError(path.rsplit("/", 1)[-1], response.status_code, code, message)
        return response

    async def json(self, method, path, **kwargs):
        return (await self.request(method, path, **kwargs)).json()
