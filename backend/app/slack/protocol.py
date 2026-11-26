"""Verify signed Slack requests and confine file traffic to Slack."""

import hashlib
import hmac
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html import escape
from math import ceil
from time import time
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx
from anyio import fail_after
from fastapi import HTTPException, Request

from app.request_receipts import fingerprint
from app.workbook_upload import MAX_UPLOAD_BYTES, validate_filename

MAX_EVENT_BYTES = 64 * 1024
SIGNATURE_AGE = 300
SLACK_API = "https://slack.com/api/"
FILE_HOSTS = frozenset({"files.slack.com", "files-origin.slack.com", "downloads.slack-edge.com"})
DM = re.compile(r"D[A-Z0-9]{2,63}")
USER = re.compile(r"[UW][A-Z0-9]{2,63}")
FILE = re.compile(r"F[A-Z0-9]{2,63}")
TS = re.compile(r"[0-9]{1,16}\.[0-9]{6}")
DIGEST = re.compile(r"[a-f0-9]{64}")
EVENT = re.compile(r"Ev[A-Za-z0-9]{2,63}")


def valid(pattern, item):
    return isinstance(item, str) and pattern.fullmatch(item) is not None


@dataclass(frozen=True)
class SlackConfig:
    team: str
    app: str
    signing_secret: str = field(repr=False)
    bot_token: str = field(repr=False)
    operators: dict[str, str]
    samples: frozenset[str]

    @classmethod
    def load(cls, config, operators):
        if not config.slack_enabled:
            return None
        try:
            mapping = json.loads(config.slack_operator_map)
            samples = frozenset(config.slack_demo_source_sha256.split())
            if (
                not valid(re.compile(r"T[A-Z0-9]{2,63}"), config.slack_team_id)
                or not valid(re.compile(r"A[A-Z0-9]{2,63}"), config.slack_app_id)
                or not config.slack_signing_secret or not config.slack_signing_secret.get_secret_value()
                or not config.slack_bot_token or not config.slack_bot_token.get_secret_value().startswith("xoxb-")
                or not isinstance(mapping, dict) or not mapping
                or not all(valid(USER, user) and isinstance(name, str) and name in operators for user, name in mapping.items())
                or len(set(mapping.values())) != len(mapping)
                or not samples or not all(valid(DIGEST, sample) for sample in samples)
            ):
                raise ValueError
            return cls(
                config.slack_team_id, config.slack_app_id, config.slack_signing_secret.get_secret_value(),
                config.slack_bot_token.get_secret_value(), mapping, samples,
            )
        except (TypeError, ValueError):
            raise ValueError("Slack DM configuration is incomplete or invalid") from None


async def authenticated_payload(request: Request, config: SlackConfig):
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    provided = request.headers.get("X-Slack-Signature", "")
    if (
        not re.fullmatch(r"[0-9]{1,12}", timestamp)
        or abs(time() - int(timestamp)) > SIGNATURE_AGE
        or not re.fullmatch(r"v0=[a-f0-9]{64}", provided)
    ):
        raise HTTPException(401, "Slack signature required")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_EVENT_BYTES:
            raise HTTPException(413, "Slack payload is too large")
        body.extend(chunk)
    expected = "v0=" + hmac.new(
        config.signing_secret.encode(), b"v0:" + timestamp.encode() + b":" + body, hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(provided, expected):
        raise HTTPException(401, "Slack signature required")
    try:
        media_type = request.headers.get("content-type", "").split(";", 1)[0]
        if media_type == "application/x-www-form-urlencoded":
            body = parse_qs(body.decode(), strict_parsing=True, max_num_fields=4)["payload"][0]
        elif media_type != "application/json":
            raise ValueError
        value = json.loads(body)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (KeyError, UnicodeError, ValueError, RecursionError):
        raise HTTPException(400, "Invalid Slack payload") from None


def as_dict(value):
    return value if isinstance(value, dict) else {}


def normalize(payload: dict, config: SlackConfig):
    """Return only authorized DM events and the fields needed for recovery."""
    if payload.get("api_app_id") != config.app or payload.get("is_ext_shared_channel"):
        return None
    kind = payload.get("type")
    if kind == "event_callback":
        entry = as_dict(payload.get("event"))
        event_id = payload.get("event_id")
        team = payload.get("team_id")
        if (
            entry.get("type") != "message" or entry.get("channel_type") != "im" or entry.get("bot_id")
            or entry.get("subtype") not in (None, "file_share") or not valid(EVENT, event_id)
            or entry.get("is_ext_shared") or (entry.get("user_team") not in (None, team))
        ):
            return None
        channel, user = entry.get("channel"), entry.get("user")
        timestamp = entry.get("ts")
        thread = entry.get("thread_ts") or timestamp
        if not valid(TS, timestamp):
            return None
        attachments = entry.get("files", [])
        if not isinstance(attachments, list):
            return None
        file_ids = [as_dict(file).get("id") for file in attachments[:2]]
        text = entry.get("text", "")
        if not isinstance(text, str):
            return None
        material = {"kind": "message", "root": timestamp == thread, "text": text[:1600], "files": file_ids}
        identity = [config.team, "event", event_id]
    elif kind == "block_actions":
        team = as_dict(payload.get("team")).get("id")
        user = as_dict(payload.get("user")).get("id")
        container = as_dict(payload.get("container"))
        message = as_dict(payload.get("message"))
        channel = container.get("channel_id")
        thread = container.get("thread_ts") or message.get("thread_ts")
        actions = payload.get("actions")
        if (
            container.get("type") != "message" or as_dict(payload.get("channel")).get("id") != channel
            or not isinstance(actions, list) or len(actions) != 1
        ):
            return None
        action = as_dict(actions[0])
        control = action.get("value")
        if not valid(DIGEST, control) or action.get("action_id") != "invoice_action_" + control[:16]:
            return None
        material = {"kind": "button", "control": control}
        identity = [config.team, channel, thread, user, "button", control]
    else:
        return None
    if team != config.team or not valid(DM, channel) or not valid(USER, user) or not valid(TS, thread):
        return None
    operator = config.operators.get(user)
    if operator is None:
        return None
    binding = {"team": team, "channel": channel, "thread": thread, "user": user, "operator": operator}
    return fingerprint(identity), {**material, "binding": binding}


class SlackError(Exception):
    def __init__(self, code: str, *, retryable=False):
        self.code = code
        self.retryable = retryable
        super().__init__(code)


def retry_after_seconds(value):
    if value and re.fullmatch(r"[0-9]{1,8}", value):
        return max(1, int(value))
    if value:
        try:
            timestamp = parsedate_to_datetime(value)
            return max(1, ceil((timestamp - datetime.now(UTC)).total_seconds()))
        except (TypeError, ValueError, OverflowError):
            pass
    return 1


class SlackRateLimited(SlackError):
    def __init__(self, delay):
        super().__init__("slack_rate_limited", retryable=True)
        self.delay = delay


def checked_url(value):
    if not isinstance(value, str) or len(value) > 4096:
        raise SlackError("slack_file_url_invalid")
    try:
        parts = urlsplit(value)
        if (
            parts.scheme == "https" and parts.hostname in FILE_HOSTS and parts.port in (None, 443)
            and parts.username is None and parts.password is None and not parts.fragment
            and "\\" not in value and not any(ord(char) <= 32 for char in value)
        ):
            return value
    except ValueError:
        pass
    raise SlackError("slack_file_url_invalid")


class SlackClient:
    def __init__(self, client: httpx.AsyncClient, config: SlackConfig):
        self.client = client
        self.authorization = {"Authorization": f"Bearer {config.bot_token}"}

    async def api(self, method, *, form=False, **payload):
        try:
            with fail_after(12):
                encoding = {"data" if form else "json": payload}
                response = await self.client.post(SLACK_API + method, headers=self.authorization, **encoding)
                if response.status_code == 429:
                    raise SlackRateLimited(retry_after_seconds(response.headers.get("Retry-After")))
                if response.is_error:
                    raise SlackError("slack_api_unavailable", retryable=response.status_code >= 500)
                result = as_dict(response.json())
        except (httpx.HTTPError, TimeoutError, ValueError):
            raise SlackError("slack_api_unavailable", retryable=True) from None
        if not result.get("ok"):
            if result.get("error") == "ratelimited":
                raise SlackRateLimited(retry_after_seconds(response.headers.get("Retry-After")))
            retryable = result.get("error") in {"internal_error", "service_unavailable"}
            raise SlackError("slack_api_rejected", retryable=retryable)
        return result

    async def verify_workspace(self, team):
        if (await self.api("auth.test")).get("team_id") != team:
            raise SlackError("slack_workspace_unverified")

    async def verify_dm(self, binding):
        channel = as_dict((await self.api("conversations.info", channel=binding["channel"])).get("channel"))
        if (
            channel.get("id") != binding["channel"] or channel.get("is_im") is not True
            or channel.get("user") != binding["user"] or channel.get("is_ext_shared")
            or channel.get("is_user_deleted")
        ):
            raise SlackError("slack_recipient_unverified")

    async def source(self, file_id, binding):
        if not valid(FILE, file_id):
            raise SlackError("slack_invalid_attachment")
        item = as_dict((await self.api("files.info", file=file_id)).get("file"))
        if (
            item.get("id") != file_id or item.get("user") != binding["user"] or item.get("mode") != "hosted"
            or item.get("is_external") or item.get("is_public") or item.get("public_url_shared")
        ):
            raise SlackError("slack_invalid_attachment")
        name, size = item.get("name"), item.get("size")
        if not isinstance(name, str) or type(size) is not int or not 0 < size <= MAX_UPLOAD_BYTES:
            raise SlackError("slack_invalid_attachment")
        validate_filename(name)
        url = checked_url(item.get("url_private_download") or item.get("url_private"))
        try:
            with fail_after(30):
                for _ in range(3):
                    async with self.client.stream("GET", url, headers=self.authorization) as response:
                        if response.is_redirect and "location" in response.headers:
                            url = checked_url(urljoin(url, response.headers["location"]))
                            continue
                        response.raise_for_status()
                        data = bytearray()
                        async for chunk in response.aiter_bytes():
                            if len(data) + len(chunk) > size:
                                raise SlackError("slack_attachment_size_mismatch")
                            data.extend(chunk)
                        if len(data) != size:
                            raise SlackError("slack_attachment_size_mismatch")
                        return name, bytes(data)
        except (httpx.HTTPError, TimeoutError):
            raise SlackError("slack_attachment_unavailable", retryable=True) from None
        raise SlackError("slack_file_redirect_limit")

    async def stage_file(self, name, size):
        result = await self.api("files.getUploadURLExternal", form=True, filename=name, length=size)
        if not valid(FILE, result.get("file_id")):
            raise SlackError("slack_file_slot_invalid")
        return result["file_id"], checked_url(result.get("upload_url"))

    async def upload_bytes(self, url, data):
        try:
            with fail_after(45):
                response = await self.client.post(checked_url(url), content=data, headers={"Content-Type": "application/octet-stream"})
                response.raise_for_status()
        except (httpx.HTTPError, TimeoutError):
            raise SlackError("slack_file_staging_failed", retryable=True) from None

    async def send_message(self, binding, text, blocks=None):
        body = {"channel": binding["channel"], "thread_ts": binding["thread"], "text": text,
                "mrkdwn": False, "unfurl_links": False, "unfurl_media": False}
        if blocks:
            body["blocks"] = blocks
        await self.api("chat.postMessage", **body)

    async def share_file(self, file_id, name, binding, caption):
        result = await self.api(
            "files.completeUploadExternal", files=[{"id": file_id, "title": name}],
            channel_id=binding["channel"], thread_ts=binding["thread"], initial_comment=escape(caption, quote=False),
        )
        if not isinstance(result.get("files"), list) or not any(
            as_dict(item).get("id") == file_id for item in result["files"]
        ):
            raise SlackError("slack_file_share_unconfirmed")

    async def confirm_file(self, file_id, binding):
        item = as_dict((await self.api("files.info", file=file_id)).get("file"))
        if (
            item.get("id") != file_id or item.get("is_public") or item.get("public_url_shared")
            or item.get("channels") or item.get("groups")
        ):
            return False
        shares = as_dict(item.get("shares"))
        private = as_dict(shares.get("private"))
        channel = binding["channel"]
        if set(private) != {channel} or shares.get("public") or set(item.get("ims") or []) - {channel}:
            return False
        entries = private[channel]
        return isinstance(entries, list) and any(as_dict(entry).get("thread_ts") == binding["thread"] for entry in entries)
