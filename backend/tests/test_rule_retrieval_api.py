import hashlib
import json
from uuid import UUID

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select, update

from app import classification_model, main, rule_retrieval
from app.checkpoints import setup_checkpoints
from app.classification_tables import source_classifications
from app.config import settings
from test_allocation_api import fixture_model
from test_classification import write_codex
from test_classification_api import START, configured_skill, model_answer, ready_task
from test_reconciliation import WORKSHEET, source_sheet
from test_reconciliation_api import ALICE, BOB, FIXTURES, service, success
from test_workbook_qa import real_engine, visual_request

HISTORICAL_CASE = json.loads((FIXTURES / "evaluation" / "historical_classification.json").read_text(encoding="utf-8"))
CURRENT_AMOUNTS = [HISTORICAL_CASE["current_amount"], HISTORICAL_CASE["historical_amount"]]


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
@pytest.mark.parametrize("amount", CURRENT_AMOUNTS)
async def test_classification_records_rule_matches_without_importing_historical_amounts(
    task_runtime, configured_skill, monkeypatch, workbook_with, amount,
):
    historical = HISTORICAL_CASE["historical_reference"]
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
        assert historical not in seen[0].text
        assert seen[0].text == configured_skill.read_text()
        assert len(seen[0].retrieved_rules) == 1
        assert success(await client.get(path, headers=ALICE)) == result
        assert (await client.get(f"/tasks/{task_id}/rules", headers=BOB)).status_code == 404


@pytest.mark.anyio
@pytest.mark.parametrize("amount", CURRENT_AMOUNTS)
async def test_provider_cannot_publish_historical_amounts_in_project_rationale(
    task_runtime, configured_skill, monkeypatch, workbook_with, amount,
):
    historical = HISTORICAL_CASE["historical_reference"]
    approved_text = configured_skill.read_text()
    active_rules(monkeypatch, configured_skill)
    client_class = httpx.AsyncClient
    submit_unsupported_reason = True

    def respond(request):
        messages = json.loads(request.content)["messages"]
        assert approved_text in messages[0]["content"]
        assert historical not in messages[0]["content"] and "98,765.43" not in messages[0]["content"]
        inputs = json.loads(messages[1]["content"])
        proposal = model_answer(inputs, {row["row"]: "Doors" for row in inputs["rows"]}).model_dump()
        if submit_unsupported_reason:
            proposal["rows"][0]["reason"] = HISTORICAL_CASE["unsupported_rationale"]
        return httpx.Response(200, json={"choices": [{
            "finish_reason": "stop", "message": {"content": json.dumps(proposal)},
        }]})

    monkeypatch.setattr(settings, "llm_api_key", SecretStr("example-key"))
    monkeypatch.setattr(classification_model.httpx, "AsyncClient", lambda **kwargs: client_class(
        transport=httpx.MockTransport(respond), **kwargs,
    ))
    await setup_checkpoints(settings)
    async with service() as client:
        task_id, _ = await ready_task(client, doors_source(workbook_with, amount))
        path = f"/tasks/{task_id}/classification"
        assert (await client.post(path, headers=ALICE, json=START)).status_code == 503
        failed = success(await client.get(path, headers=ALICE))
        assert failed["report"]["error"] == "invalid_model_proposal"
        assert "98,765.43" not in json.dumps(failed) and failed["report"]["assignments"] == []
        submit_unsupported_reason = False
        classified = success(await client.post(path, headers=ALICE, json=START))
        assert classified["status"] == "ready_for_allocation" and classified["report"]["classified_total"] == amount
        review, = classified["report"]["model_proposals"]
        assert review["reason"] == "The model proposed Doors from Trade Summary!A4: FF&E door hardware"
        assert classified["report"]["rationale_origin"] == "current_workbook"


@pytest.mark.anyio
async def test_mixed_rules_are_blocked_before_search_or_the_model(
    task_runtime, configured_skill, monkeypatch, workbook_with,
):
    approved_text = configured_skill.read_text()
    configured_skill.write_text(approved_text + "\n" + HISTORICAL_CASE["historical_reference"] + "\n")
    active_rules(monkeypatch, configured_skill)

    def unexpected(*args, **kwargs):
        pytest.fail("Mixed runtime rules must be rejected before search or a model request")

    monkeypatch.setattr(rule_retrieval, "search_rules", unexpected)
    monkeypatch.setattr(classification_model, "propose", unexpected)
    await setup_checkpoints(settings)
    async with service() as client:
        task_id, _ = await ready_task(client, doors_source(workbook_with))
        path = f"/tasks/{task_id}"
        mixed = await client.post(path + "/classification", headers=ALICE, json=START)
        assert mixed.status_code == 503
        assert mixed.json()["detail"]["code"] == "primary_specification_contains_evaluation_data"
        failure = success(await client.get(path + "/classification", headers=ALICE))
        assert failure["report"]["rule_context"]["retrieval"]["status"] == "not_performed"
        assert not failure["report"]["assignments"]
        assert success(await client.get(path + "/rules", headers=ALICE)) is None


@pytest.mark.anyio
async def test_classification_boundary_rejects_mocked_adapter_claim_and_persists_no_proposal(
    task_runtime, configured_skill, monkeypatch, workbook_with,
):
    async def invalid_adapter(config, specification, inputs):
        proposal = (await doors_model(config, specification, inputs)).model_dump()
        proposal["rows"][0]["reason"] = HISTORICAL_CASE["unsupported_rationale"]
        return proposal

    monkeypatch.setattr(classification_model, "propose", invalid_adapter)
    await setup_checkpoints(settings)
    async with service() as client:
        task_id, _ = await ready_task(client, doors_source(workbook_with))
        path = f"/tasks/{task_id}/classification"
        assert (await client.post(path, headers=ALICE, json=START)).status_code == 503
        async with main.app.state.database_engine.connect() as connection:
            saved = (await connection.execute(select(source_classifications).where(
                source_classifications.c.task_id == UUID(task_id),
            ))).mappings().one()
        assert saved["status"] == "failed" and saved["proposal"] is None
        assert saved["report"]["error"] == "invalid_model_proposal"
        assert HISTORICAL_CASE["unsupported_rationale"] not in json.dumps(saved["report"])


@pytest.mark.anyio
async def test_cached_legacy_classification_does_not_serve_unsupported_model_facts(
    task_runtime, configured_skill, monkeypatch, workbook_with,
):
    monkeypatch.setattr(classification_model, "propose", doors_model)
    await setup_checkpoints(settings)
    async with service() as client:
        task_id, _ = await ready_task(client, doors_source(workbook_with))
        path = f"/tasks/{task_id}/classification"
        current = success(await client.post(path, headers=ALICE, json=START))
        legacy_report = current["report"]
        legacy_report.pop("rationale_origin")
        legacy_report["model_proposals"][0]["reason"] = HISTORICAL_CASE["unsupported_rationale"]
        async with main.app.state.database_engine.begin() as connection:
            await connection.execute(update(source_classifications).where(
                source_classifications.c.task_id == UUID(task_id),
            ).values(report=legacy_report))

    async with service() as client:
        cached = success(await client.get(path, headers=ALICE))
        assert cached == success(await client.post(path, headers=ALICE, json=START))
        assert cached["proposal_id"] == current["proposal_id"]
        assert cached["report"]["classified_total"] == "150.00"
        assert "reason" not in cached["report"]["model_proposals"][0]
        assert HISTORICAL_CASE["unsupported_rationale"] not in json.dumps(cached)


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
@pytest.mark.parametrize("recovery", ["archive", "explicit_primary_only"])
async def test_pinned_supplement_blocks_classification_until_restored_or_explicitly_removed(
    task_runtime, configured_skill, monkeypatch, tmp_path, workbook_with, recovery,
):
    primary = active_rules(monkeypatch, configured_skill)
    codex = write_codex(tmp_path, monkeypatch)
    original = codex.read_bytes()
    digest = hashlib.sha256(original).hexdigest()
    archive = tmp_path / "approved-rules"
    monkeypatch.setattr(settings, "classification_codex_path", codex)
    monkeypatch.setattr(settings, "classification_rules_archive", archive)
    calls = []

    async def model(config, specification, inputs):
        calls.append(specification.supplementary)
        return await doors_model(config, specification, inputs)

    monkeypatch.setattr(classification_model, "propose", model)
    await setup_checkpoints(settings)
    async with service() as client:
        task_id, _ = await ready_task(client, doors_source(workbook_with))
        path = f"/tasks/{task_id}"
        before = success(await client.post(path + "/classification", headers=ALICE, json=START))
        assert before["report"]["rule_context"]["supplementary"]["sha256"] == digest
        success(await client.put(path + "/target", headers=ALICE, json={
            **START, "target": {"amount": "160.00", "gst_basis": "excluding_gst"},
        }))
        codex.unlink()

    async with service() as client:
        revision_three = {"expected_revision": 3}
        assert success(await client.get(path + "/rules", headers=ALICE))["supplementary"]["sha256"] == digest
        for stage in ("workflow", "extraction", "reconciliation"):
            success(await client.post(path + "/" + stage, headers=ALICE, json=revision_three))
        missing = await client.post(path + "/classification", headers=ALICE, json=revision_three)
        assert missing.status_code == 503 and missing.json()["detail"]["code"] == "pinned_supplementary_unavailable"
        failed = success(await client.get(path + "/classification", headers=ALICE))
        assert failed["status"] == "failed"
        context = failed["report"]["rule_context"]
        assert context["primary_verified"] and context["supplementary"] is None
        assert context["pinned_supplementary"]["sha256"] == digest
        assert len(calls) == 1
        upgrade = {**revision_three, "primary_sha256": primary}
        assert (await client.post(path + "/rules/upgrade", headers=ALICE, json=upgrade)).status_code == 409

        if recovery == "archive":
            codex.write_text("Changed default version, with no archived copy yet.", encoding="utf-8")
            stale = await client.post(path + "/classification", headers=ALICE, json=revision_three)
            assert stale.status_code == 503 and stale.json()["detail"]["code"] == "pinned_supplementary_mismatch"
            assert len(calls) == 1
            archived = archive / digest / codex.name
            archived.parent.mkdir(parents=True)
            archived.write_bytes(original)
            # The new default registry entry does not authorize a substitution for this task.
            monkeypatch.setattr(classification_model, "APPROVED_CODEX_VERSION", "new-default-version")
            monkeypatch.setattr(classification_model, "APPROVED_CODEX_SHA256", hashlib.sha256(codex.read_bytes()).hexdigest())
            restored = success(await client.post(path + "/classification", headers=ALICE, json=revision_three))
            assert restored["status"] == "ready_for_allocation"
            assert restored["report"]["rule_context"]["supplementary"] == before["report"]["rule_context"]["supplementary"]
            assert calls[0] == calls[1]
        else:
            change = {**upgrade, "supplementary_sha256": None}
            assert (await client.post(path + "/rules/upgrade", headers=BOB, json=change)).status_code == 404
            key = ALICE | {"Idempotency-Key": "explicit-primary-only"}
            changed = success(await client.post(path + "/rules/upgrade", headers=key, json=change))
            assert changed["task"]["revision"] == 4 and changed["rules"]["supplementary"] is None
            assert success(await client.post(path + "/rules/upgrade", headers=key, json=change)) == changed
            assert (await client.post(path + "/rules/upgrade", headers=key, json=upgrade)).status_code == 409
            assert success(await client.get(path + "/rules?revision=3", headers=ALICE))["supplementary"]["sha256"] == digest
            codex.write_bytes(original)
            revision_four = {"expected_revision": 4}
            for stage in ("workflow", "extraction", "reconciliation"):
                success(await client.post(path + "/" + stage, headers=ALICE, json=revision_four))
            restored = success(await client.post(path + "/classification", headers=ALICE, json=revision_four))
            assert restored["status"] == "ready_for_allocation"
            assert len(calls) == 2 and calls[1].text is None


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
        assert calls[0]["status"] == "applied"
        context = success(await client.get(path, headers=ALICE))["report"]["rule_context"]
        if change == "retrieval":
            assert len(calls) == 2
            assert context["retrieval_configuration"] == "keyword-test-v2"
            assert context["supplementary"]["sha256"] == calls[0]["sha256"]
        else:
            assert len(calls) == 1 and second.json()["detail"]["code"] == "pinned_supplementary_unavailable"
            assert context["supplementary"] is None
            assert context["pinned_supplementary"]["sha256"] == calls[0]["sha256"]


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
