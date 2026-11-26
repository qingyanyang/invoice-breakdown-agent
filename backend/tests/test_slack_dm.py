import hashlib
import hmac
import json
from io import BytesIO
from time import time
from urllib.parse import parse_qs, urlencode

import httpx
import pytest
from anyio import sleep_forever
from pydantic import SecretStr
from pypdf import PdfWriter
from sqlalchemy import select

from app import classification_model, main
from app.checkpoints import setup_checkpoints
from app.config import settings
from app.slack.protocol import SlackClient, SlackError
from app.slack.routes import process_one
from app.slack.service import OUT_OF_SCOPE, preview_sheets
from app.slack.tables import files, inbox, threads
from app.task_tables import tasks
from test_allocation_api import fixture_model
from test_classification_api import configured_skill
from test_reconciliation_api import ALICE, FIXTURES, service, success
from test_workbook_qa import real_engine

TEAM = "TTEST"
APP = "ATEST"
USER = "UALICE"
OTHER_USER = "UBOB"
CHANNEL = "DTEST1"
THREAD = "1700000000.000001"
SECRET = "mock-slack-signature-secret"
TOKEN = "xoxb-mock-test"


@pytest.fixture
def slack_demo(task_runtime, monkeypatch):
    source = (FIXTURES / "reconciliation.xlsx").read_bytes()
    monkeypatch.setattr(settings, "slack_enabled", True)
    monkeypatch.setattr(settings, "slack_team_id", TEAM)
    monkeypatch.setattr(settings, "slack_app_id", APP)
    monkeypatch.setattr(settings, "slack_signing_secret", SecretStr(SECRET))
    monkeypatch.setattr(settings, "slack_bot_token", SecretStr(TOKEN))
    monkeypatch.setattr(settings, "slack_operator_map", json.dumps({USER: "alice", OTHER_USER: "bob"}))
    monkeypatch.setattr(settings, "slack_demo_source_sha256", hashlib.sha256(source).hexdigest())

    async def offline(_app, _config):
        await sleep_forever()

    monkeypatch.setattr(main, "run_slack_worker", offline)
    return source


def signed(payload, *, form=False, timestamp=None, secret=SECRET):
    raw = json.dumps(payload).encode()
    media_type = "application/json"
    if form:
        raw = urlencode({"payload": raw}).encode()
        media_type = "application/x-www-form-urlencoded"
    timestamp = str(int(time()) if timestamp is None else timestamp)
    signature = hmac.new(secret.encode(), f"v0:{timestamp}:".encode() + raw, hashlib.sha256).hexdigest()
    return {"content": raw, "headers": {
        "Content-Type": media_type, "X-Slack-Signature": "v0=" + signature,
        "X-Slack-Request-Timestamp": timestamp,
    }}


def event(event_id, *, caption="", file_id=None, thread=None, user=USER, channel=CHANNEL):
    return {
        "type": "event_callback", "team_id": TEAM, "api_app_id": APP, "event_id": event_id,
        "event": {
            "type": "message", "channel_type": "im", "user": user, "channel": channel,
            "ts": "1700000010.000002" if thread else THREAD, "thread_ts": thread, "text": caption,
            **({"files": [{"id": file_id}], "subtype": "file_share"} if file_id else {}),
        },
    }


def interaction(button, *, thread=THREAD, user=USER):
    return {
        "type": "block_actions", "team": {"id": TEAM}, "api_app_id": APP, "user": {"id": user},
        "container": {"type": "message", "channel_id": CHANNEL}, "channel": {"id": CHANNEL},
        "message": {"thread_ts": thread},
        "actions": [{"action_id": button["action_id"], "value": button["value"]}],
    }


class FakeSlack:
    def __init__(self, source):
        self.source = source
        self.messages = []
        self.shared = {}
        self.staged = {}
        self.uploads = {}
        self.download_url = "https://files.slack.com/private/sample"
        self.final_visibility = False
        self.final_completion_attempts = 0

    def button(self, label):
        return next(
            element for message in reversed(self.messages) for block in message.get("blocks", [])
            for element in block.get("elements", []) if element["text"]["text"] == label
        )

    def matching_buttons(self, label):
        return [
            element for message in self.messages for block in message.get("blocks", [])
            for element in block.get("elements", []) if element["text"]["text"] == label
        ]

    @staticmethod
    def ok(**payload):
        return httpx.Response(200, json={"ok": True, **payload})

    async def respond(self, request):
        if request.url.host == "files.slack.com":
            if request.method == "GET":
                assert request.headers["Authorization"] == "Bearer " + TOKEN
                return httpx.Response(200, content=self.source)
            assert "Authorization" not in request.headers
            file_id = request.url.path.split("/")[-1]
            assert file_id in self.staged
            self.uploads[file_id] = request.content
            return httpx.Response(200)
        assert request.url.host == "slack.com"
        assert request.headers["Authorization"] == "Bearer " + TOKEN
        method = request.url.path.split("/")[-1]
        if method == "files.getUploadURLExternal":
            name = parse_qs(request.content.decode())["filename"][0]
            file_id = f"FOUT{len(self.staged) + 1}"
            self.staged[file_id] = name
            return self.ok(file_id=file_id, upload_url=f"https://files.slack.com/upload/{file_id}")
        payload = json.loads(request.content)
        if method == "conversations.info":
            return self.ok(channel={"id": payload["channel"], "user": USER, "is_im": True})
        if method == "chat.postMessage":
            self.messages.append(payload)
            return self.ok()
        if method == "files.info":
            file_id = payload["file"]
            if file_id == "FSOURCE":
                return self.ok(file={
                    "id": file_id, "mode": "hosted", "user": USER, "name": "test.xlsx",
                    "size": len(self.source), "url_private_download": self.download_url,
                })
            shared = self.shared.get(file_id)
            if shared and (not self.staged[file_id].endswith(".xlsx") or self.final_visibility):
                return self.ok(file={
                    "id": file_id, "ims": [shared["channel_id"]],
                    "shares": {"private": {shared["channel_id"]: [{"thread_ts": shared["thread_ts"]}]}},
                })
            return self.ok(file={"id": file_id})
        if method == "files.completeUploadExternal":
            file_id = payload["files"][0]["id"]
            assert file_id in self.uploads
            assert payload["channel_id"] == CHANNEL and payload["thread_ts"] == THREAD
            if self.staged[file_id].endswith(".xlsx"):
                # Slack shares the correct file, but the client loses its response.
                self.final_completion_attempts += 1
                self.shared[file_id] = payload
                raise httpx.ReadTimeout("mock lost response", request=request)
            self.shared[file_id] = payload
            return self.ok(files=[{"id": file_id}])
        pytest.fail(f"Unexpected Slack method {method}")


async def drain(slack):
    config = main.app.state.slack_config
    async with httpx.AsyncClient(transport=httpx.MockTransport(slack.respond)) as client:
        provider = SlackClient(client, config)
        while await process_one(main.app, provider, config):
            pass


async def rows(table):
    async with main.app.state.database_engine.connect() as connection:
        return (await connection.execute(select(table))).mappings().all()


@pytest.mark.anyio
async def test_signed_ingress_persists_only_authorized_events_and_rejects_http_impersonation(slack_demo):
    async with service() as client:
        path = "/integrations/slack/events"
        data = event("EvTest", file_id="FSOURCE")
        assert (await client.post(path, **signed(data, secret="forged"))).status_code == 401
        assert (await client.post(path, **signed(data, timestamp=int(time()) - 301))).status_code == 401
        assert (await client.post(path, **signed(event("EvOther", user="UUNMAPPED")))).status_code == 200
        assert (await client.post(path, **signed(event("EvGroup", channel="CGENERAL")))).status_code == 200
        config = {"type": "url_verification", "challenge": "signed-challenge"}
        assert success(await client.post(path, **signed(config))) == {"challenge": "signed-challenge"}
        assert (await client.post("/tasks", headers={"invoice.internal_operator": "alice"}, json={})).status_code == 401
        assert (await client.post(path, **signed(data))).status_code == 200
        assert (await client.post(path, **signed(data))).status_code == 200
        queued, = await rows(inbox)
        assert queued["state"] == "queued" and await rows(tasks) == []


@pytest.mark.anyio
async def test_slack_source_refuses_non_slack_url_and_unapproved_bytes(slack_demo):
    fake = FakeSlack(slack_demo)
    async with service() as client:
        fake.download_url = "https://files.slack.com.attacker.invalid/workbook.xlsx"
        assert (await client.post("/integrations/slack/events", **signed(event("EvUrl", file_id="FSOURCE")))).status_code == 200
        await drain(fake)
        never_fetched, = await rows(inbox)
        assert never_fetched["error"] == "slack_file_url_invalid"
        fake.download_url = "https://files.slack.com/private/changed"
        fake.source += b"unapproved bytes"
        assert (await client.post("/integrations/slack/events", **signed(event("EvHash", file_id="FSOURCE")))).status_code == 200
        await drain(fake)
        assert {item["error"] for item in await rows(inbox)} == {"slack_file_url_invalid", "demo_source_unapproved"}
        assert await rows(tasks) == [] and fake.staged == {}


@pytest.mark.anyio
async def test_mocked_slack_flow_recovers_clarification_and_uncertain_delivery(
    slack_demo, configured_skill, real_engine, monkeypatch,
):
    monkeypatch.setattr(classification_model, "propose", fixture_model)
    await setup_checkpoints(settings)
    fake = FakeSlack(slack_demo)
    webhook = "/integrations/slack/events"

    async with service() as client:
        start = event("EvStart", file_id="FSOURCE", caption="breakdown target 282.54")
        assert (await client.post(webhook, **signed(start))).status_code == 200
        await drain(fake)
        linked, = await rows(threads)
        path = f"/tasks/{linked['task_id']}"
        before = success(await client.get(path + "/workflow", headers=ALICE))
        assert before["status"] == "awaiting_clarification"
        gst_button = fake.button("ex_gst")

    async with service() as client:
        assert (await client.post(webhook, **signed(interaction(gst_button, user=OTHER_USER), form=True))).status_code == 200
        assert (await client.post(webhook, **signed(interaction(gst_button, thread="1700000002.000001"), form=True))).status_code == 200
        assert (await client.post(webhook, **signed(event("EvYes", caption="yes", thread=THREAD)))).status_code == 200
        await drain(fake)
        assert success(await client.get(path + "/workflow", headers=ALICE)) == before
        assert any(message["text"] == OUT_OF_SCOPE for message in fake.messages)
        assert fake.staged == {}

        click = interaction(gst_button)
        assert (await client.post(webhook, **signed(click, form=True))).status_code == 200
        click["actions"][0]["action_ts"] = "1700000013.000001"
        assert (await client.post(webhook, **signed(click, form=True))).status_code == 200
        await drain(fake)
        workflow = success(await client.get(path + "/workflow", headers=ALICE))
        assert workflow["status"] == "ready_for_extraction" and workflow["revision"] == before["revision"]
        blocked = success(await client.get(path + "/delivery/qa", headers=ALICE))
        assert blocked["status"] == "blocked" and fake.staged == {}
        assert any("QA 阻止交付" in message["text"] for message in fake.messages)

        main.app.state.qa_engine = real_engine
        assert (await client.post(webhook, **signed(event("EvQARetry", caption="retry", thread=THREAD)))).status_code == 200
        await drain(fake)
        qa = success(await client.get(path + "/delivery/qa", headers=ALICE))
        assert qa["status"] == "awaiting_visual_review"
        preview_ids = [key for key, name in fake.staged.items() if name.endswith(".pdf")]
        preview_id, = preview_ids
        assert hashlib.sha256(fake.uploads[preview_id]).hexdigest() == qa["preview"]["sha256"]
        reviews = fake.matching_buttons("确认已审阅全部页面")
        assert len(reviews) == len(qa["checks"]["preview_coverage"]["evidence"]["worksheets"])
        assert (await client.get(path + "/delivery/download", headers=ALICE)).status_code == 409
        for click in reviews[:-1]:
            assert (await client.post(webhook, **signed(interaction(click), form=True))).status_code == 200
        await drain(fake)
        assert fake.final_completion_attempts == 0

        assert (await client.post(webhook, **signed(interaction(reviews[-1]), form=True))).status_code == 200
        await drain(fake)
        ready = success(await client.get(path + "/delivery/qa", headers=ALICE))
        assert ready["status"] == "ready_for_delivery"
        assert fake.final_completion_attempts == 1
        unknown, = [row for row in await rows(files) if row["state"] == "delivery_unknown"]
        final_data = (await client.get(path + "/delivery/download", headers=ALICE)).content
        assert fake.uploads[unknown["file_id"]] == final_data
        assert hashlib.sha256(final_data).hexdigest() == ready["candidate"]["sha256"]
        assert any("delivery_unknown" in message["text"] for message in fake.messages)

    async with service() as client:
        main.app.state.qa_engine = real_engine
        fake.final_visibility = True
        retry = event("EvRetry", caption="retry", thread=THREAD)
        assert (await client.post(webhook, **signed(retry))).status_code == 200
        await drain(fake)
        receipts = await rows(files)
        assert len(receipts) == 2 and all(row["state"] == "sent" for row in receipts)
        assert len(fake.staged) == 2 and fake.final_completion_attempts == 1
        assert "最终核对差额：0.00" in fake.shared[unknown["file_id"]]["initial_comment"]

        updated = success(await client.put(path + "/target", headers=ALICE, json={
            "expected_revision": before["revision"], "target": {"amount": "285.00", "gst_basis": "excluding_gst"},
        }))
        assert updated["revision"] == before["revision"] + 1
        stale_review = interaction(fake.button("报告视觉问题"))
        assert (await client.post(webhook, **signed(stale_review, form=True))).status_code == 200
        await drain(fake)
        assert len(fake.staged) == 2 and fake.final_completion_attempts == 1
        assert (await client.get(path + "/delivery/download", headers=ALICE)).status_code == 409
        assert any(row["error"] == "stale_control" for row in await rows(inbox))


def test_multi_page_visual_control_requires_complete_preview_coverage():
    writer = PdfWriter()
    for _ in range(3):
        writer.add_blank_page(width=450, height=500)
    output = BytesIO()
    writer.write(output)
    coverage = [{"sheet": "Breakdown", "pages": [1, 2]}, {"sheet": "Notes", "page": 3}]
    assert preview_sheets(coverage, output.getvalue()) == {"Breakdown": [1, 2], "Notes": [3]}
    assert preview_sheets([
        {"sheet": "Breakdown", "page": 2}, {"sheet": "Breakdown", "page": 1}, coverage[1],
    ], output.getvalue()) == {"Breakdown": [1, 2], "Notes": [3]}
    coverage[0]["pages"] = [1]
    with pytest.raises(SlackError, match="review_preview_incomplete"):
        preview_sheets(coverage, output.getvalue())
