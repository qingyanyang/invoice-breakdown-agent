from pathlib import Path
from zipfile import ZipFile

import pytest
from fastapi import HTTPException
from pydantic import SecretStr
from starlette.requests import Request

from app import workbook_upload
from app.operators import operator_credentials

FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "tests" / "fixtures" / "invoice_breakdown" / "reconciliation.xlsx"
)


def test_supported_workbook_and_filename():
    workbook_upload.validate_filename("Source.XLSX")
    workbook_upload.validate_workbook(FIXTURE.read_bytes())


@pytest.mark.parametrize("filename", ["legacy.xls", "macro.xlsm"])
def test_unsupported_extensions(filename):
    with pytest.raises(HTTPException) as error:
        workbook_upload.validate_filename(filename)
    assert error.value.status_code == 415


def test_zip_expanded_size_is_checked(monkeypatch, workbook_with):
    compressed = workbook_with({"xl/large.bin": b"0" * 100_000})
    monkeypatch.setattr(workbook_upload, "MAX_EXPANDED_BYTES", 50_000)
    assert len(compressed) < 50_000
    with pytest.raises(HTTPException) as error:
        workbook_upload.validate_workbook(compressed)
    assert error.value.status_code == 413


@pytest.mark.anyio
async def test_upload_limit_applies_to_actual_request_chunks(monkeypatch):
    monkeypatch.setattr(workbook_upload, "MAX_UPLOAD_BYTES", 5)
    chunks = iter([
        {"type": "http.request", "body": b"123", "more_body": True},
        {"type": "http.request", "body": b"456", "more_body": False},
    ])

    async def receive():
        return next(chunks)

    request = Request({"type": "http", "headers": []}, receive)
    with pytest.raises(HTTPException) as error:
        await workbook_upload.read_upload(request)
    assert error.value.status_code == 413


def test_encrypted_zip_is_rejected():
    encrypted = bytearray(FIXTURE.read_bytes())
    central_directory = encrypted.index(b"PK\x01\x02")
    encrypted[central_directory + 8] |= 1
    with pytest.raises(HTTPException) as error:
        workbook_upload.validate_workbook(bytes(encrypted))
    assert error.value.status_code == 415
    assert "Encrypted" in error.value.detail


@pytest.mark.parametrize("path", ["xl/vbaProject.bin", "xl/macrosheets/sheet1.xml"])
def test_macro_content_disguised_as_xlsx_is_rejected(path, workbook_with):
    with pytest.raises(HTTPException) as error:
        workbook_upload.validate_workbook(workbook_with({path: b"unexpected macro part"}))
    assert error.value.status_code == 415


def test_malformed_workbook_part_is_rejected(workbook_with):
    malformed = workbook_with({"xl/worksheets/sheet1.xml": b"<worksheet"})
    with pytest.raises(HTTPException) as error:
        workbook_upload.validate_workbook(malformed)
    assert error.value.status_code == 422


def test_missing_referenced_sheet_is_rejected(workbook_with):
    with ZipFile(FIXTURE) as original:
        relationships = original.read("xl/_rels/workbook.xml.rels")
    modified = relationships.replace(b"/xl/worksheets/sheet1.xml", b"/xl/missing.xml")
    assert modified != relationships
    with pytest.raises(HTTPException) as error:
        workbook_upload.validate_workbook(workbook_with({"xl/_rels/workbook.xml.rels": modified}))
    assert error.value.status_code == 422


def test_shared_operator_tokens_are_rejected_without_exposing_them():
    shared = "z" * 40
    secret = SecretStr('{"alice":"' + shared + '","bob":"' + shared + '"}')
    with pytest.raises(ValueError) as error:
        operator_credentials(secret)
    assert shared not in str(error.value)


@pytest.mark.parametrize(
    ("part", "original_text", "replacement"),
    [
        ("xl/_rels/workbook.xml.rels", b"/relationships/worksheet", b"/relationships/styles"),
        ("[Content_Types].xml", b"spreadsheetml.worksheet+xml", b"spreadsheetml.styles+xml"),
    ],
)
def test_sheet_type_and_content_type_must_match(workbook_with, part, original_text, replacement):
    with ZipFile(FIXTURE) as original:
        xml = original.read(part)
    changed = xml.replace(original_text, replacement, 1)
    assert changed != xml
    with pytest.raises(HTTPException) as error:
        workbook_upload.validate_workbook(workbook_with({part: changed}))
    assert error.value.status_code == 422


@pytest.mark.parametrize("part", ["xl/workbook.xml", "xl/worksheets/sheet1.xml"])
def test_unsupported_xml_encoding_returns_422(workbook_with, part):
    with ZipFile(FIXTURE) as original:
        xml = original.read(part)
    changed = xml.replace(b'encoding="utf-8"', b'encoding="invalid-test-encoding"', 1)
    assert changed != xml
    with pytest.raises(HTTPException) as error:
        workbook_upload.validate_workbook(workbook_with({part: changed}))
    assert error.value.status_code == 422
