"""Resolve QA evidence and verify the files attached to a recorded run."""

from hashlib import sha256

from anyio import fail_after
from sqlalchemy import select

from app.features.qa.checks import CHECKS, MACHINE_CHECKS, QA_VERSION
from app.features.qa.tables import workbook_qa_runs
from app.features.workbooks.layout import metadata
from app.features.workbooks.use_cases import find_workbook, workbook_inputs
from app.outcomes import FailureKind, UseCaseError
from app.source_storage import SourceIntegrityError, StorageError, StorageNotFound, read_verified_source


def current_status(checks: dict) -> str:
    if all(checks[name]["status"] == "passed" for name in CHECKS):
        return "ready_for_delivery"
    if all(checks[name]["status"] == "passed" for name in MACHINE_CHECKS) and (
        checks["owner_visual_review"]["status"] == "not_performed"
    ):
        return "awaiting_visual_review"
    return "blocked"


def approved_for_delivery(record) -> bool:
    approval, candidate, preview = record["owner_review"], record["candidate"], record["preview"]
    return (
        current_status(record["checks"]) == "ready_for_delivery"
        and approval is not None and candidate is not None and preview is not None
        and approval["candidate_sha256"] == candidate["sha256"]
        and approval["preview_sha256"] == preview["sha256"]
    )


def retryable(record) -> bool:
    if record is None:
        return True
    statuses = [record["checks"][name]["status"] for name in MACHINE_CHECKS]
    return record["status"] == "blocked" and "failed" not in statuses and "not_performed" in statuses


async def current_artifact(connection, revision, expected=None):
    inputs = await workbook_inputs(connection, revision)
    record = await find_workbook(connection, inputs)
    if record is None:
        raise UseCaseError(FailureKind.CONFLICT, "Current allocation requires a generated review workbook")
    if expected is not None and record["artifact_id"] != expected:
        raise UseCaseError(FailureKind.CONFLICT, "The current review workbook has changed")
    return inputs, dict(record)


async def latest_run(connection, artifact):
    result = await connection.execute(
        select(workbook_qa_runs).filter_by(
            artifact_id=artifact["artifact_id"], artifact_sha256=artifact["sha256"], qa_version=QA_VERSION,
        ).order_by(workbook_qa_runs.c.created_at.desc(), workbook_qa_runs.c.id.desc()).limit(1)
    )
    row = result.mappings().one_or_none()
    return dict(row) if row is not None else None


def stored_file(key, data):
    return {"storage_key": key, "size": len(data), "sha256": sha256(data).hexdigest()}


def expose_file(file):
    return {"size": file["size"], "sha256": file["sha256"]} if file is not None else None


def summary_in_chinese(inputs):
    recon, report = inputs.reconciliation, inputs.allocation
    fields, _ = metadata(inputs.extraction)
    totals = report["display"]["trade_totals"]
    result = {
        "项目": fields.get("project_name") or "源文件未注明",
        "工作范围源金额": report["working_total"],
        "最终金额": report["display"]["total"],
        "Make Good": totals.get("Make Good", "0.00"),
        "Access Panel": totals.get("Access Panel", "0.00"),
        "最终核对差额": report["display"]["rounding"]["difference"],
    }
    if report["target"] is not None:
        result["目标金额"] = report["target"]
    if factor := report.get("factor"):
        result["调整系数"] = f"{factor['numerator']} ÷ {factor['denominator']}"
    if recon["scope_additions"] or recon["scope_exclusions"]:
        result["原始源表总额"] = recon["reported_total"]
    if inputs.approved.classification.decisions or report["warnings"]:
        result["审核事项"] = {
            "分类澄清": list(inputs.approved.classification.decisions),
            "分配提示": report["warnings"],
        }
    return result


def response_body(record, inputs, artifact):
    status = current_status(record["checks"])
    body = {
        "qa_id": record["id"], "status": status, "qa_version": record["qa_version"],
        "artifact_id": artifact["artifact_id"], "artifact_sha256": artifact["sha256"],
        "revision": artifact["revision"], "allocation_id": artifact["allocation_id"],
        "source_sha256": artifact["source_sha256"], "checks": record["checks"],
        "candidate": expose_file(record["candidate"]), "preview": expose_file(record["preview"]),
        "owner_review": record["owner_review"],
    }
    if approved_for_delivery(record):
        body["delivery_summary"] = summary_in_chinese(inputs)
    return body


async def verify_object(resources, storage, descriptor) -> bytes:
    return await resources.storage_worker.run(
        read_verified_source, storage, descriptor["storage_key"], descriptor["size"], descriptor["sha256"],
    )


async def previous_files_available(resources, storage, record) -> bool:
    if record is None or record["candidate"] is None:
        return False
    try:
        with fail_after(25):
            await verify_object(resources, storage, record["candidate"])
            if record["preview"] is not None:
                await verify_object(resources, storage, record["preview"])
    except (SourceIntegrityError, StorageNotFound):
        return False
    except TimeoutError:
        raise StorageError from None
    return True
