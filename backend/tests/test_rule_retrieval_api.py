import hashlib

import pytest

from app import classification_model, main, rule_retrieval
from app.checkpoints import setup_checkpoints
from app.config import settings
from test_allocation_api import fixture_model
from test_classification import write_codex
from test_classification_api import START, configured_skill, model_answer, ready_task
from test_reconciliation import WORKSHEET, source_sheet
from test_reconciliation_api import ALICE, BOB, FIXTURES, service, success
from test_workbook_qa import real_engine, visual_request


def active_rules(monkeypatch, path, *, version=None):
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    monkeypatch.setattr(settings, "classification_skill_path", path)
    monkeypatch.setattr(settings, "classification_skill_sha256", digest)
    monkeypatch.setattr(settings, "classification_skill_version", version)
    return digest


def doors_source(workbook_with, amount="150.00"):
    return workbook_with({WORKSHEET: source_sheet([
        ("FF&E door hardware", amount, None),
        ("Project Total ex GST", amount, "SUM(D4:D4)"),
    ])})


async def doors_model(_settings, _specification, inputs):
    return model_answer(inputs, {row["row"]: "Doors" for row in inputs["rows"]})


@pytest.mark.anyio
@pytest.mark.parametrize("amount", ["150.00", "98765.43"])
async def test_classification_records_rule_matches_without_importing_historical_amounts(
    task_runtime, configured_skill, monkeypatch, workbook_with, amount,
):
    historical = "Historical example from an unrelated project: FF&E previously cost $98,765.43."
    configured_skill.write_text(
        configured_skill.read_text().replace("FF&E maps to Doors.\n", f"FF&E maps to Doors.\n\n{historical}\n"),
    )
    digest = active_rules(monkeypatch, configured_skill, version="synthetic-v1")
    seen = []

    async def model(config, specification, inputs):
        seen.append(specification)
        return await doors_model(config, specification, inputs)

    monkeypatch.setattr(classification_model, "propose", model)
    await setup_checkpoints(settings)
    async with service() as client:
        task_id, _ = await ready_task(client, doors_source(workbook_with, amount))
        path = f"/tasks/{task_id}/classification"
        result = success(await client.post(path, headers=ALICE, json=START))
        assert result["status"] == "ready_for_allocation"
        assert result["report"]["classified_total"] == amount
        rules = result["report"]["rule_context"]
        assert rules["primary"] == {"document": "SKILL.md", "version": "synthetic-v1", "sha256": digest}
        retrieval = rules["retrieval"]
        assert retrieval["status"] == "matched"
        match, = retrieval["passages"]
        assert match["section"] == "6" and match["sha256"] == digest
        assert match["version"] == "synthetic-v1"
        assert match["text"] == "FF&E maps to Doors."
        assert match["row_refs"] == ["Trade Summary!D4"]
        assert historical in seen[0].text
        assert len(seen[0].retrieved_rules) == 1
        assert success(await client.get(path, headers=ALICE)) == result
        assert (await client.get(f"/tasks/{task_id}/rules", headers=BOB)).status_code == 404


@pytest.mark.anyio
async def test_rule_upgrade_is_explicit_and_invalidates_old_downstream_results(
    task_runtime, configured_skill, monkeypatch, tmp_path, real_engine,
):
    first = active_rules(monkeypatch, configured_skill, version="test-v1")
    monkeypatch.setattr(classification_model, "propose", fixture_model)
    archive = tmp_path / "approved-rules"
    old_copy = archive / first / "SKILL.md"
    old_copy.parent.mkdir(parents=True)
    old_copy.write_bytes(configured_skill.read_bytes())
    monkeypatch.setattr(settings, "classification_rules_archive", archive)

    await setup_checkpoints(settings)
    source = (FIXTURES / "reconciliation.xlsx").read_bytes()
    async with service() as client:
        task_id, _ = await ready_task(client, source)
        path = f"/tasks/{task_id}"
        success(await client.post(path + "/classification", headers=ALICE, json=START))
        configured_skill.write_text(configured_skill.read_text() + "\nApproved synthetic revision.\n")
        newer = active_rules(monkeypatch, configured_skill, version="test-v2")

        edited = success(await client.put(path + "/target", headers=ALICE, json={
            **START, "target": {"amount": "282.54", "gst_basis": "excluding_gst"},
        }))
        assert edited["revision"] == 3

    async with service() as client:
        revision_three = {"expected_revision": 3}
        assert success(await client.get(path + "/rules", headers=ALICE))["primary"]["sha256"] == first
        for stage in ("workflow", "extraction", "reconciliation"):
            success(await client.post(path + "/" + stage, headers=ALICE, json=revision_three))
        still_old = success(await client.post(path + "/classification", headers=ALICE, json=revision_three))
        assert still_old["report"]["rule_context"]["primary"]["sha256"] == first
        allocation = success(await client.post(path + "/allocation", headers=ALICE, json={
            **revision_three, "expected_classification_id": still_old["proposal_id"],
        }))
        workbook = success(await client.post(path + "/workbook", headers=ALICE, json={
            **revision_three, "expected_allocation_id": allocation["allocation_id"],
        }))
        main.app.state.qa_engine = real_engine
        qa = success(await client.post(path + "/delivery/qa", headers=ALICE, json={
            **revision_three, "expected_artifact_id": workbook["artifact_id"],
        }))
        assert qa["status"] == "awaiting_visual_review", qa["checks"]
        approved = success(await client.post(path + "/delivery/qa/visual-review", headers=ALICE, json={
            **visual_request(qa), **revision_three,
        }))
        assert approved["status"] == "ready_for_delivery"
        assert (await client.get(path + "/delivery/download", headers=ALICE)).status_code == 200

        request = {**revision_three, "primary_sha256": newer}
        assert (await client.post(path + "/rules/upgrade", headers=BOB, json=request)).status_code == 404
        assert (await client.post(path + "/rules/upgrade", headers=ALICE, json={
            **request, "primary_sha256": "f" * 64,
        })).status_code == 409
        key = ALICE | {"Idempotency-Key": "approve-new-rules"}
        upgraded = success(await client.post(path + "/rules/upgrade", headers=key, json=request))
        assert upgraded["task"]["revision"] == 4 and upgraded["rules"]["primary"]["sha256"] == newer
        assert success(await client.post(path + "/rules/upgrade", headers=key, json=request)) == upgraded
        assert success(await client.get(path + "/rules?revision=3", headers=ALICE))["primary"]["sha256"] == first
        assert (await client.put(path + "/target", headers=ALICE, json={
            **revision_three, "target": None,
        })).status_code == 409
        assert (await client.get(path + "/allocation", headers=ALICE)).status_code == 409
        assert (await client.get(path + "/workbook", headers=ALICE)).status_code == 409
        assert (await client.get(path + "/delivery/download", headers=ALICE)).status_code == 409
        assert (await client.post(path + "/delivery/qa/visual-review", headers=ALICE, json={
            **visual_request(qa), **revision_three,
        })).status_code == 409
        assert (await client.get(path + "/source", headers=ALICE)).content == source

        revision_four = {"expected_revision": 4}
        for stage in ("workflow", "extraction", "reconciliation"):
            success(await client.post(path + "/" + stage, headers=ALICE, json=revision_four))
        classified = success(await client.post(path + "/classification", headers=ALICE, json=revision_four))
        assert classified["report"]["rule_context"]["primary"]["sha256"] == newer
        assert classified["proposal_id"] != still_old["proposal_id"]


@pytest.mark.anyio
@pytest.mark.parametrize("case", ["missing", "mismatch"])
async def test_missing_or_changed_pinned_primary_blocks_new_classification(
    task_runtime, configured_skill, monkeypatch, workbook_with, case,
):
    primary = configured_skill.read_bytes()
    digest = active_rules(monkeypatch, configured_skill)
    monkeypatch.setattr(classification_model, "propose", doors_model)
    await setup_checkpoints(settings)

    async with service() as client:
        task_id, _ = await ready_task(client, doors_source(workbook_with))
        path = f"/tasks/{task_id}"
        success(await client.post(path + "/classification", headers=ALICE, json=START))
        success(await client.put(path + "/target", headers=ALICE, json={
            **START, "target": {"amount": "155", "gst_basis": "excluding_gst"},
        }))

    if case == "missing":
        configured_skill.unlink()
        reason = "primary_specification_unavailable"
    else:
        configured_skill.write_bytes(primary + b"\nDifferent version.\n")
        reason = "primary_specification_mismatch"

    async def no_model(*_args):
        pytest.fail("Classification cannot call the model without its pinned primary rules")

    monkeypatch.setattr(classification_model, "propose", no_model)
    revision = {"expected_revision": 3}
    async with service() as client:
        for stage in ("workflow", "extraction", "reconciliation"):
            success(await client.post(path + "/" + stage, headers=ALICE, json=revision))
        blocked = await client.post(path + "/classification", headers=ALICE, json=revision)
        assert blocked.status_code == 503 and blocked.json()["detail"]["code"] == reason
        report = success(await client.get(path + "/classification", headers=ALICE))["report"]
        assert report["rule_context"]["primary"]["sha256"] == digest
        assert report["rule_context"]["primary_verified"] is False
        assert report["rule_context"]["retrieval"]["status"] == "not_performed"

        configured_skill.write_bytes(primary)
        monkeypatch.setattr(classification_model, "propose", doors_model)
        recovered = success(await client.post(path + "/classification", headers=ALICE, json=revision))
        assert recovered["report"]["rule_context"]["primary_verified"] is True
        assert recovered["report"]["rule_context"]["primary"]["sha256"] == digest


@pytest.mark.anyio
@pytest.mark.parametrize("status", ["no_match", "unavailable", "version_mismatch"])
async def test_optional_search_failure_uses_full_verified_primary_without_mismatched_supplement(
    task_runtime, configured_skill, monkeypatch, tmp_path, workbook_with, status,
):
    digest = active_rules(monkeypatch, configured_skill)
    mismatched = tmp_path / "Invoice.Breakdown.Codex.md"
    mismatched.write_text("Unapproved supplemental guidance", encoding="utf-8")
    monkeypatch.setattr(settings, "classification_codex_path", mismatched)
    search = rule_retrieval.search_rules
    if status == "unavailable":
        def search_failed(*_args):
            raise RuntimeError("Synthetic optional lookup failure")
        monkeypatch.setattr(rule_retrieval, "search_rules", search_failed)
    elif status == "version_mismatch":
        def outdated(text, _digest, approved, inputs):
            return search(text, "0" * 64, approved, inputs)
        monkeypatch.setattr(rule_retrieval, "search_rules", outdated)

    async def model(config, specification, inputs):
        assert specification.text == configured_skill.read_text()
        assert specification.sha256 == digest and not specification.retrieved_rules
        assert specification.supplementary.text is None
        return model_answer(inputs, {row["row"]: "Other" for row in inputs["rows"]})

    monkeypatch.setattr(classification_model, "propose", model)
    source = workbook_with({WORKSHEET: source_sheet([
        ("Temporary fencing", "42.00", None), ("Project Total ex GST", "42.00", "SUM(D4:D4)"),
    ])})
    await setup_checkpoints(settings)
    async with service() as client:
        task_id, _ = await ready_task(client, source)
        result = success(await client.post(f"/tasks/{task_id}/classification", headers=ALICE, json=START))
        assert result["status"] == "ready_for_allocation"
        report = result["report"]
        assert report["classified_total"] == "42.00"
        assert report["supplementary_rules"]["status"] == "checksum_mismatch"
        assert report["rule_context"]["supplementary"] is None
        assert report["rule_context"]["retrieval"]["status"] == status
        assert report["rule_context"]["retrieval"]["passages"] == []


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["retrieval", "supplementary"])
async def test_classification_execution_fingerprint_includes_rule_configuration(
    task_runtime, configured_skill, monkeypatch, tmp_path, workbook_with, change,
):
    active_rules(monkeypatch, configured_skill)
    codex = write_codex(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "classification_codex_path", codex)
    monkeypatch.setattr(settings, "execution_max_attempts", 1)
    calls = []

    async def failing(_config, specification, _inputs):
        calls.append(specification.supplementary.provenance())
        raise classification_model.ClassificationModelError("model_unavailable")

    monkeypatch.setattr(classification_model, "propose", failing)
    await setup_checkpoints(settings)
    async with service() as client:
        task_id, _ = await ready_task(client, doors_source(workbook_with))
        path = f"/tasks/{task_id}/classification"
        first = await client.post(path, headers=ALICE, json=START)
        assert first.status_code == 503
        if change == "retrieval":
            monkeypatch.setattr(rule_retrieval, "RETRIEVAL_VERSION", "keyword-test-v2")
        else:
            monkeypatch.setattr(settings, "classification_codex_path", None)
        second = await client.post(path, headers=ALICE, json=START)
        assert second.status_code == 503
        assert first.headers["X-Execution-Id"] != second.headers["X-Execution-Id"]
        assert len(calls) == 2 and calls[0]["status"] == "applied"
        context = success(await client.get(path, headers=ALICE))["report"]["rule_context"]
        if change == "retrieval":
            assert context["retrieval_configuration"] == "keyword-test-v2"
            assert context["supplementary"]["sha256"] == calls[0]["sha256"]
        else:
            assert context["supplementary"] is None and calls[1]["status"] == "not_configured"


@pytest.mark.anyio
async def test_retrieved_rule_does_not_resolve_a_pending_trade_question(
    task_runtime, configured_skill, monkeypatch, workbook_with,
):
    active_rules(monkeypatch, configured_skill)

    async def uncertain(_config, _specification, inputs):
        return model_answer(inputs, {row["row"]: None for row in inputs["rows"]})

    monkeypatch.setattr(classification_model, "propose", uncertain)
    await setup_checkpoints(settings)
    async with service() as client:
        task_id, _ = await ready_task(client, doors_source(workbook_with))
        path = f"/tasks/{task_id}/classification"
        draft = success(await client.post(path, headers=ALICE, json=START))
        assert draft["status"] == "blocked"
        passage, = draft["report"]["rule_context"]["retrieval"]["passages"]
        assert passage["section"] == "6"
        question, = draft["report"]["questions"]
        assert question["kind"] == "trade" and question["evidence"] == ["Trade Summary!A4"]
        assert draft["report"]["assignments"] == []
        assert (await client.post(f"/tasks/{task_id}/allocation", headers=ALICE, json={
            **START, "expected_classification_id": draft["proposal_id"],
        })).status_code == 409
