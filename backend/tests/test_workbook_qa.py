import os
import shutil
import subprocess
import sys
from decimal import Decimal
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree
from zipfile import ZipFile

import pytest
from lxml import etree as xml
from openpyxl import load_workbook
from pypdf import PdfWriter

from app import classification_model, main
from app.allocation import AllocationPolicy, allocate
from app.checkpoints import setup_checkpoints
from app.config import settings
from app.extraction_reader import extract_workbook
from app.qa_checks import CHECKS, MACHINE_CHECKS, actual_errors, verify_preview
from app.qa_engine import EngineUnavailable, QAEngine, decode_result
from app.qa_packages import S, QAProblem, QABook, candidate_from_calculation, remove_values, strip_formula_caches
from app.reconcile import reconcile
from app.reconciliation_evidence import Reference
from app.workbook_generation import generate_workbook
from test_allocation_api import TRADE_BY_ROW, allocation_workbook, approve_target, fixture_model, reconcile_and_classify
from test_classification_api import START, configured_skill
from test_reconciliation import SOURCE
from test_reconciliation_api import ALICE, BOB, FIXTURES, create_source, establish_evidence, service, success

WORKER = Path(__file__).resolve().parents[1] / "qa_worker" / "worker.py"
NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


class NativeTestEngine:
    def run(self, operation, data):
        result = subprocess.run([
            "/usr/bin/sandbox-exec", "-p", "(version 1)(allow default)(deny network*)",
            sys.executable, str(WORKER), operation,
        ], input=data, capture_output=True, check=True, timeout=48)
        return decode_result(result.stdout)


@pytest.fixture
def real_engine():
    image = os.environ.get("TEST_QA_WORKER_IMAGE")
    if image:
        return QAEngine(os.environ.get("TEST_QA_DOCKER_RUNTIME", "docker"), image)
    if sys.platform == "darwin" and shutil.which("soffice") and Path("/usr/bin/sandbox-exec").is_file():
        return NativeTestEngine()
    pytest.skip("The real workbook QA test requires TEST_QA_WORKER_IMAGE or offline LibreOffice on macOS")


def fixture_artifact(source, target=None):
    extraction = extract_workbook(source)
    recon = reconcile(extraction, SOURCE, {})
    assignments = []
    for item in recon["selected_rows"]:
        key = Reference.from_dict(item["reference"]).key
        trade = TRADE_BY_ROW.get(key, "Other")
        assignments.append({
            "row": key, "trade": trade, "area": "Level 1", "amount": item["contribution"],
            "scope_origin": "in_source", "protected_by_default": trade in {"Make Good", "Access Panel"},
        })
    exact = sum((Decimal(item["amount"]) for item in assignments), Decimal(0))
    classification = {
        "assignments": assignments, "gst_basis": recon["gst_basis"],
        "working_scope_total": recon["working_scope_total"], "classified_total_exact": str(exact),
    }
    basis = "excluding_gst" if target is not None else None
    report = allocate(recon, classification, target, basis, AllocationPolicy())
    assert report["status"] == "ready_for_workbook"
    return generate_workbook(source, extraction, recon, report)


def hidden_fixture(workbook_with):
    with ZipFile(FIXTURES / "reconciliation.xlsx") as source:
        workbook = ElementTree.fromstring(source.read("xl/workbook.xml"))
        notes = ElementTree.fromstring(source.read("xl/worksheets/sheet2.xml"))
    workbook.find(NS + "sheets")[1].set("state", "veryHidden")
    notes.find(NS + "sheetData")[1].set("hidden", "1")
    return workbook_with({
        "xl/workbook.xml": ElementTree.tostring(workbook),
        "xl/worksheets/sheet2.xml": ElementTree.tostring(notes),
    })


def test_real_recalculation_fills_missing_caches_and_preserves_exact_content(real_engine):
    generated = fixture_artifact((FIXTURES / "reconciliation.xlsx").read_bytes()).data
    book = QABook(generated)
    summary = book.sheets["Invoice Breakdown"]
    ref, = [ref for ref, cell in summary.cells.items() if cell["formula"] and cell["raw_value"] == "260.00"
            and cell["formula"]["text"].startswith("ROUND(B") and "-" not in cell["formula"]["text"]]
    remove_values(summary.nodes[ref])
    absent = book.dump([summary])
    clear, count = strip_formula_caches(absent)
    assert count > 0 and QABook(clear).sheets[summary.name].cells[ref]["raw_value"] is None

    calculated = real_engine.run("calculate", clear)
    result = candidate_from_calculation(absent, calculated.data)
    candidate = load_workbook(BytesIO(result), data_only=True)
    assert candidate[summary.name][ref].value == 260
    assert result != absent
    assert candidate[summary.name]["A2"].value is None
    assert load_workbook(BytesIO(absent), data_only=True)[summary.name][ref].value is None


def test_real_formula_errors_are_distinct_from_literal_error_text(real_engine):
    generated = fixture_artifact((FIXTURES / "reconciliation.xlsx").read_bytes()).data
    book = QABook(generated)
    sheet = book.sheets["Notes"]
    row = sheet.root.find(f"{S}sheetData/{S}row")
    bad = xml.SubElement(row, S + "c", r="B1")
    xml.SubElement(bad, S + "f").text = "1/0"
    literal = xml.SubElement(row, S + "c", r="C1", t="inlineStr")
    xml.SubElement(xml.SubElement(literal, S + "is"), S + "t").text = "#REF!"
    injected = book.dump([sheet])
    uncached, _ = strip_formula_caches(injected)
    result = real_engine.run("calculate", uncached)
    candidate = candidate_from_calculation(injected, result.data)
    assert actual_errors(QABook(candidate)) == [{"sheet": "Notes", "cell": "B1", "value": "#DIV/0!"}]


def test_preview_rejects_missing_or_truncated_content():
    source = (FIXTURES / "reconciliation.xlsx").read_bytes()
    book = QABook(fixture_artifact(source).data)
    pdf = PdfWriter()
    for _ in book.sheets:
        pdf.add_blank_page(612, 792)
    buffer = BytesIO()
    pdf.write(buffer)
    with pytest.raises(QAProblem, match="omits or clips") as error:
        verify_preview(book, buffer.getvalue())
    assert {"sheet": "Invoice Breakdown", "cell": "A1"} in error.value.locations
    assert {"sheet": "Notes", "cell": "A1"} in error.value.locations


async def start_delivery(client, source, target="282.54"):
    allocation_path, request = await approve_target(client, source, target)
    allocation = success(await client.post(allocation_path, headers=ALICE, json=request))
    assert allocation["status"] == "ready_for_workbook", allocation
    task = allocation_path.removesuffix("/allocation")
    artifact = success(await client.post(task + "/workbook", headers=ALICE, json={
        **START, "expected_allocation_id": allocation["allocation_id"],
    }))
    return task, artifact


def visual_request(qa):
    return {
        **START, "expected_artifact_id": qa["artifact_id"], "expected_qa_id": qa["qa_id"],
        "candidate_sha256": qa["candidate"]["sha256"], "preview_sha256": qa["preview"]["sha256"],
        "sheets": [{"sheet": item["sheet"], "outcome": "passed"}
                   for item in qa["checks"]["preview_coverage"]["evidence"]["worksheets"]],
    }


@pytest.mark.anyio
async def test_delivery_of_exact_candidate_requires_all_owner_checks_and_expires_on_revision(
    task_runtime, configured_skill, monkeypatch, workbook_with, real_engine,
):
    monkeypatch.setattr(classification_model, "propose", fixture_model)
    await setup_checkpoints(settings)
    source = hidden_fixture(workbook_with)
    calls = []

    class Engine:
        def run(self, operation, data):
            calls.append(operation)
            if calls == ["calculate", "render"]:
                raise EngineUnavailable("The renderer is temporarily unavailable.")
            return real_engine.run(operation, data)

    async with service() as client:
        main.app.state.qa_engine = Engine()
        task, artifact = await start_delivery(client, source)
        qa_path = task + "/delivery/qa"
        payload = {**START, "expected_artifact_id": artifact["artifact_id"]}
        assert (await client.post(qa_path, headers=BOB, json=payload)).status_code == 404
        paused = success(await client.post(qa_path, headers=ALICE, json=payload))
        assert paused["status"] == "blocked" and paused["candidate"] is not None
        assert paused["checks"]["preview_coverage"]["status"] == "not_performed"
        qa = success(await client.post(qa_path, headers=ALICE, json=payload))
        assert calls == ["calculate", "render", "render"]
        assert qa["candidate"] == paused["candidate"]
        assert qa["status"] == "awaiting_visual_review", qa["checks"]
        assert all(qa["checks"][name]["status"] == "passed" for name in MACHINE_CHECKS)
        assert qa["checks"]["owner_visual_review"]["status"] == "not_performed"
        assert qa["candidate"]["sha256"] != artifact["sha256"]
        notes = next(item for item in qa["checks"]["preview_coverage"]["evidence"]["worksheets"] if item["sheet"] == "Notes")
        assert notes["visibility"] == "veryHidden" and notes["range"] == "A1:A3"
        assert success(await client.post(qa_path, headers=ALICE, json=payload)) == qa

        assert (await client.get(task + "/delivery/download", headers=ALICE)).status_code == 409
        review = await client.get(task + "/workbook/download", headers=ALICE)
        assert review.headers["x-workbook-status"] == "generated_awaiting_qa"
        assert sha256(review.content).hexdigest() == artifact["sha256"]
        assert (await client.get(qa_path + "/preview", headers=BOB)).status_code == 404
        preview = await client.get(qa_path + "/preview", headers=ALICE)
        assert sha256(preview.content).hexdigest() == qa["preview"]["sha256"]

    async with service() as client:
        approved_input = visual_request(qa)
        assert (await client.post(qa_path + "/visual-review", headers=BOB, json=approved_input)).status_code == 404
        assert (await client.post(qa_path + "/visual-review", headers=ALICE, json={
            **approved_input, "sheets": approved_input["sheets"][:-1],
        })).status_code == 422
        assert (await client.post(qa_path + "/visual-review", headers=ALICE, json={
            **approved_input, "candidate_sha256": "f" * 64,
        })).status_code == 409
        ready = success(await client.post(qa_path + "/visual-review", headers=ALICE, json=approved_input))
        assert ready["status"] == "ready_for_delivery"
        assert ready["delivery_summary"]["最终核对差额"] == "0.00"
        assert all(ready["checks"][name]["status"] == "passed" for name in CHECKS)
        final = await client.get(task + "/delivery/download", headers=ALICE)
        assert final.status_code == 200
        assert final.headers["x-workbook-status"] == "ready_for_delivery"
        assert sha256(final.content).hexdigest() == qa["candidate"]["sha256"]
        stored_preview, = (task_runtime / "qa").rglob("preview.pdf")
        intact_preview = stored_preview.read_bytes()
        stored_preview.write_bytes(b"corrupt preview")
        assert (await client.get(task + "/delivery/download", headers=ALICE)).status_code == 503
        assert (await client.get(qa_path, headers=ALICE)).status_code == 409
        stored_preview.write_bytes(intact_preview)
        assert (await client.get(task + "/source", headers=ALICE)).content == source
        success(await client.put(task + "/target", headers=ALICE, json={
            **START, "target": {"amount": "290.00", "gst_basis": "excluding_gst"},
        }))
        assert (await client.get(task + "/delivery/download", headers=ALICE)).status_code == 409
        assert (await client.post(qa_path + "/visual-review", headers=ALICE, json=approved_input)).status_code == 409


@pytest.mark.anyio
async def test_missing_worker_records_unperformed_checks_and_preserves_review_download(
    task_runtime, configured_skill, monkeypatch,
):
    monkeypatch.setattr(classification_model, "propose", fixture_model)
    await setup_checkpoints(settings)
    async with service() as client:
        main.app.state.qa_engine = QAEngine("docker", None)
        task, artifact = await start_delivery(client, (FIXTURES / "reconciliation.xlsx").read_bytes())
        report = success(await client.post(task + "/delivery/qa", headers=ALICE, json={
            **START, "expected_artifact_id": artifact["artifact_id"],
        }))
        assert report["status"] == "blocked"
        assert report["checks"]["artifact_integrity"]["status"] == "passed"
        assert report["checks"]["recalculation"]["status"] == "not_performed"
        assert report["checks"]["preview_coverage"]["status"] == "not_performed"
        assert report["candidate"] is None
        assert (await client.get(task + "/delivery/download", headers=ALICE)).status_code == 409
        assert (await client.get(task + "/workbook/download", headers=ALICE)).status_code == 200


@pytest.mark.anyio
async def test_nonzero_no_target_display_difference_blocks_delivery(
    task_runtime, configured_skill, monkeypatch, workbook_with, real_engine,
):
    from test_workbooks_api import misc_model

    monkeypatch.setattr(classification_model, "propose", misc_model)
    await setup_checkpoints(settings)
    source = allocation_workbook(workbook_with, [
        ("Hoarding", "1.005", "Area A"), ("Fencing", "1.005", "Area B"),
    ])
    async with service() as client:
        main.app.state.qa_engine = real_engine
        task_id = await create_source(client, "rounding.xlsx", source)
        task = f"/tasks/{task_id}"
        await establish_evidence(client, task_id)
        classification = await reconcile_and_classify(client, task)
        allocation = success(await client.post(task + "/allocation", headers=ALICE, json={
            **START, "expected_classification_id": classification["proposal_id"],
        }))
        artifact = success(await client.post(task + "/workbook", headers=ALICE, json={
            **START, "expected_allocation_id": allocation["allocation_id"],
        }))
        report = success(await client.post(task + "/delivery/qa", headers=ALICE, json={
            **START, "expected_artifact_id": artifact["artifact_id"],
        }))
        assert report["status"] == "blocked"
        assert report["checks"]["recalculation"]["status"] == "passed"
        assert report["checks"]["financial_reconciliation"]["status"] == "failed"
        assert "difference must be zero" in report["checks"]["financial_reconciliation"]["detail"]
        assert report["checks"]["owner_visual_review"]["status"] == "not_performed"
        assert (await client.get(task + "/delivery/download", headers=ALICE)).status_code == 409
