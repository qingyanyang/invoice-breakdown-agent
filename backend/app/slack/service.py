"""Drive the existing Invoice Breakdown stages from durable Slack DM actions."""

import re
from hashlib import sha256
from io import BytesIO
from urllib.parse import unquote
from uuid import UUID

from anyio import to_thread
from pdfminer.pdfpage import PDFPage
from pdfminer.psexceptions import PSException

from app.request_receipts import fingerprint
from app.reconciliation_evidence import Reference
from app.slack.core import CoreError
from app.slack.protocol import SlackError
from app.slack.store import Store
from app.workbook_upload import XLSX_MEDIA_TYPE

OUT_OF_SCOPE = "此请求超出 Aflux Invoice Breakdown Agent 的工作范围。我只能处理 Aflux Invoice Breakdown 工作簿及相关金额分配、核对和问答。"
TARGET = re.compile(r"breakdown(?: target ([0-9]{1,16}(?:\.[0-9]{1,2})?)(?: (ex_gst|inc_gst))?)?", re.I)
REASON = re.compile(r"answer ([a-f0-9]{64}) ([\s\S]{1,800})")
GST = {"ex_gst": "excluding_gst", "inc_gst": "including_gst", None: "unknown"}


def parse_target(text):
    if not text:
        return None
    match = TARGET.fullmatch(text)
    if match is None:
        raise SlackError("unsupported_request")
    amount, basis = match.groups()
    return {"amount": amount, "gst_basis": GST[basis.lower() if basis else None]} if amount else None


def plain(value, limit=2400):
    # Suppress Slack mentions, links and formatting in imported evidence.
    return {"type": "plain_text", "text": str(value)[:limit] or " "}


def button(label, key):
    return {"type": "button", "text": plain(label, 75), "value": key, "action_id": "invoice_action_" + key[:16]}


def notice_for(error):
    return {
        "unsupported_request": OUT_OF_SCOPE,
        "slack_invalid_attachment": "请在私聊中上传一份由你上传的 .xlsx 文件（最大 10 MiB）。",
        "demo_source_unapproved": "此测试工作区只接受已登记校验和的合成工作簿。请上传获批的测试文件。",
        "slack_attachment_size_mismatch": "文件大小与 Slack 提供的信息不一致。请重新上传测试文件。",
        "delivery_unknown": "Slack 文件发送结果尚未确认（delivery_unknown）。在本任务消息串发送 retry 可核对原上传。",
        "review_preview_incomplete": "审核预览缺页或页码不完整。请重新运行当前工作簿的 QA。",
        "stale_control": "此操作不适用于当前任务或修订。请使用当前问题或预览中的按钮。",
        "empty_file_selection": "请在私聊中单独上传一份合成 .xlsx 文件。",
    }.get(error, "Slack 操作暂未完成。请在原任务消息串发送 retry，或联系管理员。")


def preview_sheets(coverage, pdf):
    """Map every page in the verified preview artifact to exactly one worksheet."""
    try:
        count = sum(1 for _ in PDFPage.get_pages(BytesIO(pdf)))
        sheets = {}
        seen = set()
        for item in coverage:
            sheet = item["sheet"]
            pages = item["pages"] if "pages" in item else [item["page"]]
            if (
                not isinstance(sheet, str) or not 1 <= len(sheet) <= 31
                or not isinstance(pages, list) or not pages
                or any(type(page) is not int or not 1 <= page <= count for page in pages)
                or len(set(pages)) != len(pages) or seen.intersection(pages)
            ):
                raise ValueError
            sheets.setdefault(sheet, []).extend(pages)
            seen.update(pages)
        if not sheets or seen != set(range(1, count + 1)):
            raise ValueError
        return {sheet: sorted(pages) for sheet, pages in sheets.items()}
    except (ValueError, KeyError, TypeError, PSException):
        raise SlackError("review_preview_incomplete") from None


class Conversation:
    def __init__(self, engine, slack, core, config, event, event_key):
        self.slack = slack
        self.core = core
        self.config = config
        self.event = event
        self.event_key = event_key
        self.binding = event["binding"]
        self.store = Store(engine, self.binding)
        self.thread = None
        self.verified = False

    @property
    def path(self):
        return f"/tasks/{self.thread['task_id']}"

    @property
    def revision(self):
        return self.thread["revision"]

    async def tell(self, text, blocks=None):
        await self.slack.send_message(self.binding, text, blocks=blocks)

    async def current(self):
        task = await self.core.json("GET", self.path)
        source = task.get("source")
        if task["revision"] != self.revision or (
            not source or source["sha256"] not in self.config.samples
        ):
            raise SlackError("stale_control")
        return task

    async def handle(self):
        if self.config.operators.get(self.binding["user"]) != self.binding["operator"]:
            return
        await self.slack.verify_dm(self.binding)
        self.verified = True
        self.thread = await self.store.thread()
        if self.event["kind"] == "button":
            await self.action(self.event["control"])
            return
        text = self.event["text"].strip()
        if self.event["root"] and self.event["files"]:
            await self.upload(text, self.event["files"])
        elif self.thread and self.revision > 1 and text.casefold() == "retry":
            await self.advance()
        elif self.thread and (reason := REASON.fullmatch(text)):
            await self.action(reason[1], reason[2].strip())
        elif text.casefold() == "breakdown":
            await self.tell("请直接上传一份合成 .xlsx 工作簿。可在附件说明中填写 breakdown target 2500 ex_gst。")
        else:
            await self.tell(OUT_OF_SCOPE)

    async def upload(self, text, file_ids):
        if len(file_ids) != 1:
            raise SlackError("empty_file_selection")
        target = parse_target(text)
        if self.thread is not None and self.thread["binding"]["source_event"] != self.event_key:
            raise SlackError("stale_control")
        if self.thread is None or self.revision == 1:
            name, data = await self.slack.source(file_ids[0], self.binding)
            if sha256(data).hexdigest() not in self.config.samples:
                raise SlackError("demo_source_unapproved")
            if self.thread is None:
                task = await self.core.json("POST", "/tasks", json={"target": target}, action=[self.event_key, "create"])
                await self.store.bind_task(UUID(task["id"]), task["revision"], self.event_key)
                self.thread = await self.store.thread()
            uploaded = await self.core.json(
                "PUT", self.path + "/source", content=data, headers={"Content-Type": XLSX_MEDIA_TYPE},
                params={"expected_revision": self.revision, "filename": name},
                action=[self.event_key, "upload"],
            )
            await self.store.set_uploaded(self.revision, uploaded["revision"])
            self.thread = await self.store.thread()
        await self.advance()

    async def post(self, endpoint, **payload):
        return await self.core.json(
            "POST", self.path + "/" + endpoint, json={"expected_revision": self.revision, **payload},
            action=[self.thread["task_id"], self.event_key, endpoint, payload],
        )

    async def ask(self, stage, questions):
        for item in questions:
            options = item["options"]
            prompt = item["prompt"]
            evidence = [Reference.from_dict(ref).key if isinstance(ref, dict) else ref for ref in item.get("evidence", [])]
            if evidence:
                prompt += "\n来源：" + "、".join(evidence[:10])
            if not options:
                await self.tell("流程暂停。" + prompt)
                continue
            buttons = []
            for option in options:
                key = await self.store.create_control({
                    "kind": "answer", "revision": self.revision, "stage": stage, "question_id": item["id"],
                    "answer": option, "requires_reason": bool(item.get("requires_reason")),
                })
                buttons.append(button(option, key))
            for start in range(0, len(buttons), 100):
                await self.tell(prompt, [
                    {"type": "section", "text": plain(prompt)},
                    *[{"type": "actions", "elements": buttons[index:index + 5]}
                      for index in range(start, min(start + 100, len(buttons)), 5)],
                ])

    async def advance(self):
        await self.current()
        workflow = await self.post("workflow")
        if workflow["status"] == "awaiting_clarification":
            question = dict(workflow["question"])
            amount = question["evidence"]["amount"]
            question.update(prompt=f"目标金额 {amount} 含 GST 还是未含 GST？", evidence=[])
            await self.ask("workflow", [question])
            return
        if workflow["status"] != "ready_for_extraction":
            raise SlackError("workflow_unavailable", retryable=True)
        await self.post("extraction")
        for stage, ready in (("reconciliation", "ready_for_classification"), ("classification", "ready_for_allocation")):
            result = await self.post(stage)
            if result["status"] != ready:
                questions = result["report"]["questions"]
                if questions:
                    await self.ask(stage, questions)
                else:
                    await self.tell(f"流程暂停（{stage}）。请检查当前源文件与记录的证据。")
                return
        approved = result["proposal_id"]
        try:
            allocation = await self.core.json("GET", self.path + "/allocation")
        except CoreError as error:
            if error.status != 404:
                raise
            allocation = await self.post("allocation", expected_classification_id=approved)
        if allocation["status"] != "ready_for_workbook":
            code = allocation["report"].get("error") or allocation["report"].get("reason") or "allocation_blocked"
            await self.tell(f"金额分配暂停（{code}）。请核对目标金额及批准范围。")
            return
        artifact = await self.post("workbook", expected_allocation_id=allocation["allocation_id"])
        qa = await self.post("delivery/qa", expected_artifact_id=artifact["artifact_id"])
        if qa["status"] == "awaiting_visual_review":
            await self.request_review(qa)
        elif qa["status"] == "ready_for_delivery":
            await self.deliver(qa)
        else:
            failures = [f"{name} · {check['detail']}" for name, check in qa["checks"].items()
                        if check["status"] in {"failed", "not_performed"} and name != "owner_visual_review"]
            if qa["checks"]["owner_visual_review"]["status"] == "failed":
                failures.append("owner_visual_review · 工作簿视觉审核未通过")
            await self.tell("QA 阻止交付。\n" + "\n".join(failures)[:2600])

    def qa_identity(self, qa):
        return {
            "revision": self.revision, "expected_artifact_id": qa["artifact_id"], "expected_qa_id": qa["qa_id"],
            "candidate_sha256": qa["candidate"]["sha256"], "preview_sha256": qa["preview"]["sha256"],
        }

    async def current_qa(self, identity, status):
        await self.current()
        qa = await self.core.json("GET", self.path + "/delivery/qa")
        if qa["status"] != status or not qa.get("candidate") or not qa.get("preview") or self.qa_identity(qa) != identity:
            raise SlackError("stale_control")
        return qa

    async def request_review(self, qa):
        identity = self.qa_identity(qa)
        response = await self.core.request("GET", self.path + "/delivery/qa/preview")
        preview = response.content
        if sha256(preview).hexdigest() != identity["preview_sha256"]:
            raise SlackError("stale_control")
        coverage = qa["checks"]["preview_coverage"]["evidence"]["worksheets"]
        sheets = await to_thread.run_sync(preview_sheets, coverage, preview)
        await self.publish_file(
            "preview", identity, "Invoice Breakdown QA Preview.pdf", preview,
            "请打开完整预览 PDF，并按每个工作表列出的全部页码完成视觉检查。此文件用于审核。",
        )
        for sheet, pages in sheets.items():
            actions = []
            for outcome, label in (("passed", "确认已审阅全部页面"), ("failed", "报告视觉问题")):
                key = await self.store.create_control({
                    "kind": "review", "identity": identity, "sheet": sheet, "outcome": outcome,
                })
                actions.append(button(label, key))
            text = f"{sheet} · PDF 页码 {', '.join(map(str, pages))}"
            await self.tell(text, [
                {"type": "section", "text": plain(text)},
                {"type": "actions", "elements": actions},
            ])

    async def action(self, control_id, reason=None):
        if self.thread is None or self.revision < 2:
            raise SlackError("stale_control")
        await self.current()
        control = await self.store.control(control_id)
        if control is None:
            raise SlackError("stale_control")
        if control["kind"] == "review" and reason is None:
            await self.review(control)
            return
        if control["kind"] != "answer" or control["revision"] != self.revision:
            raise SlackError("stale_control")
        if control["requires_reason"] and not reason:
            await self.tell(f"请在此消息串回复 answer {control_id} 并加上你的理由。")
            return
        if reason and not control["requires_reason"]:
            raise SlackError("stale_control")
        payload = {"question_id": control["question_id"], "answer": control["answer"]}
        if reason:
            payload["reason"] = reason
        await self.post(control["stage"] + "/answers", **payload)
        await self.advance()

    async def review(self, control):
        identity = control["identity"]
        # A restart after the review commits can resume the exact same approved file.
        qa = await self.core.json("GET", self.path + "/delivery/qa")
        if not qa.get("candidate") or not qa.get("preview") or identity != self.qa_identity(qa):
            raise SlackError("stale_control")
        if qa["status"] == "ready_for_delivery":
            await self.deliver(qa)
            return
        if qa["status"] != "awaiting_visual_review":
            raise SlackError("stale_control")
        preview = await self.store.file(self.file_key("preview", identity))
        if preview is None or preview["state"] != "sent":
            raise SlackError("review_preview_incomplete")
        expected = {item["sheet"] for item in qa["checks"]["preview_coverage"]["evidence"]["worksheets"]}
        if control["sheet"] not in expected:
            raise SlackError("stale_control")
        votes = await self.store.vote(UUID(identity["expected_qa_id"]), control["sheet"], control["outcome"])
        if votes[control["sheet"]] != control["outcome"]:
            await self.tell("此工作表的视觉检查结果已有记录。")
        if votes.keys() != expected:
            await self.tell(f"已记录 {len(votes)}/{len(expected)} 张工作表的视觉检查结果。")
            return
        sheets = [
            {"sheet": sheet, "outcome": vote, **(
                {"note": "Owner reported a visual issue in the Slack preview."} if vote == "failed" else {}
            )}
            for sheet, vote in sorted(votes.items())
        ]
        result = await self.post("delivery/qa/visual-review", **{
            name: value for name, value in identity.items() if name != "revision"
        }, sheets=sheets)
        if result["status"] == "ready_for_delivery":
            await self.deliver(result)
        else:
            await self.tell("工作簿视觉审核未通过。请在此任务中检查被标记的工作表。")

    async def deliver(self, qa):
        identity = self.qa_identity(qa)
        response = await self.core.request("GET", self.path + "/delivery/download")
        if (
            response.headers.get("X-Workbook-Status") != "ready_for_delivery"
            or sha256(response.content).hexdigest() != identity["candidate_sha256"]
        ):
            raise SlackError("stale_control")
        filename = unquote(response.headers.get("Content-Disposition", "").partition("filename*=UTF-8''")[2])
        if not filename.lower().endswith(".xlsx") or any(char in filename for char in "/\\\r\n"):
            filename = "Invoice Breakdown.xlsx"
        summary = qa["delivery_summary"]
        # File delivery is itself the link. Do not expose the internal download route in Slack.
        message = "已通过 QA，交付核准工作簿。\n" + "\n".join(
            f"{key}：{value}" for key, value in summary.items() if key != "文件"
        )
        await self.publish_file("final", identity, filename, response.content, message[:2600])

    def file_key(self, kind, identity):
        return fingerprint([self.store.thread_key, kind, identity])

    async def publish_file(self, kind, identity, name, content, caption):
        status = "ready_for_delivery" if kind == "final" else "awaiting_visual_review"
        key = self.file_key(kind, identity)
        await self.current_qa(identity, status)
        await self.slack.verify_dm(self.binding)
        prior = await self.store.file(key)
        if prior and prior["state"] == "sent":
            return
        if prior and prior["state"] == "delivery_unknown":
            await self.confirm_delivery(key, prior["file_id"])
            return
        file_id, url = await self.slack.stage_file(name, len(content))
        if not await self.store.stage_file(key, file_id):
            raise SlackError("delivery_unknown")
        await self.slack.upload_bytes(url, content)
        await self.current_qa(identity, status)
        await self.slack.verify_dm(self.binding)
        # The response can be lost or the process can stop during completion.
        # Persist uncertainty first so recovery keeps the original Slack file ID.
        if not await self.store.transition_file(key, file_id, {"staged"}, "delivery_unknown"):
            raise SlackError("delivery_unknown")
        try:
            await self.slack.share_file(file_id, name, self.binding, caption)
        except SlackError:
            await self.confirm_delivery(key, file_id)
        else:
            await self.store.transition_file(key, file_id, {"delivery_unknown"}, "sent")

    async def confirm_delivery(self, key, file_id):
        try:
            confirmed = await self.slack.confirm_file(file_id, self.binding)
        except SlackError:
            confirmed = False
        if not confirmed:
            raise SlackError("delivery_unknown")
        await self.store.transition_file(key, file_id, {"delivery_unknown"}, "sent")
