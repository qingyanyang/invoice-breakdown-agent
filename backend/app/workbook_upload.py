from io import BytesIO
from pathlib import PurePath
from posixpath import normpath
from urllib.parse import unquote, urlsplit
from xml.etree.ElementTree import ParseError
from xml.sax import SAXException
from xml.sax.handler import ContentHandler
from zipfile import ZIP_DEFLATED, ZIP_STORED, BadZipFile, ZipFile
from zlib import error as ZipDataError

from defusedxml import sax
from defusedxml.ElementTree import fromstring
from defusedxml.common import DefusedXmlException
from fastapi import HTTPException, Request
from starlette.requests import ClientDisconnect

XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_EXPANDED_BYTES = 64 * 1024 * 1024
MAX_XML_BYTES = 1024 * 1024
MAX_ENTRIES = 2048
PACKAGE_RELATIONSHIPS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
SHEET_RELATIONSHIP_IDS = (
    "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id",
    "{http://purl.oclc.org/ooxml/officeDocument/relationships}id",
)
WORKBOOK_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"


def validate_filename(filename: str) -> None:
    if not filename or len(filename) > 255 or any(
        char in "/\\" or ord(char) < 32 or ord(char) == 127 for char in filename
    ):
        raise HTTPException(400, "Use a plain workbook filename")
    extension = PurePath(filename).suffix.lower()
    if extension != ".xlsx":
        raise HTTPException(415, "Only .xlsx is supported. Legacy .xls import is deferred.")


async def read_upload(request: Request) -> bytes:
    body = bytearray()
    try:
        async for chunk in request.stream():
            if len(body) + len(chunk) > MAX_UPLOAD_BYTES:
                raise HTTPException(413, "Workbook exceeds the 10 MiB upload limit")
            body.extend(chunk)
    except ClientDisconnect:
        raise HTTPException(400, "Workbook upload was interrupted") from None
    return bytes(body)


def read_package_xml(archive: ZipFile, name: str):
    if archive.getinfo(name).file_size > MAX_XML_BYTES:
        raise BadZipFile
    return fromstring(archive.read(name), forbid_dtd=True)


def relationships(archive: ZipFile, name: str):
    root = read_package_xml(archive, name)
    if root.tag != PACKAGE_RELATIONSHIPS + "Relationships":
        raise BadZipFile
    return root.findall(PACKAGE_RELATIONSHIPS + "Relationship")


def internal_target(relationship, directory: str) -> str:
    target = unquote(relationship.get("Target", ""))
    url = urlsplit(target)
    if not target or url.scheme or url.netloc or "\\" in target:
        raise BadZipFile
    if relationship.get("TargetMode", "").lower() == "external":
        raise BadZipFile
    return normpath(directory + "/" + target if not target.startswith("/") else target).lstrip("/")


def validate_workbook(data: bytes) -> None:
    try:
        with ZipFile(BytesIO(data)) as archive:
            entries = archive.infolist()
            names = {entry.filename.lower() for entry in entries}
            if len(entries) != len(names) or len(entries) > MAX_ENTRIES:
                raise BadZipFile
            if any(entry.flag_bits & 0x41 for entry in entries):
                raise HTTPException(415, "Encrypted workbooks are unsupported")
            if any(entry.compress_type not in {ZIP_STORED, ZIP_DEFLATED} for entry in entries):
                raise BadZipFile
            if sum(entry.file_size for entry in entries) > MAX_EXPANDED_BYTES:
                raise HTTPException(413, "Workbook exceeds the 64 MiB expanded limit")
            if any(
                "vbaproject" in name or name.startswith(("xl/macrosheets/", "xl/intlmacrosheets/"))
                for name in names
            ):
                raise HTTPException(415, "Macro-enabled workbooks are unsupported")
            content_types = read_package_xml(archive, "[Content_Types].xml")
            if content_types.tag != "{http://schemas.openxmlformats.org/package/2006/content-types}Types":
                raise BadZipFile
            for declaration in content_types:
                kind = declaration.get("ContentType", "").lower()
                if any(marker in kind for marker in ("macroenabled", "macrosheet", "vba")):
                    raise HTTPException(415, "Macro-enabled workbooks are unsupported")
            override = next(
                (item.get("ContentType") for item in content_types
                 if item.get("PartName") == "/xl/workbook.xml"),
                None,
            )
            default = next(
                (item.get("ContentType") for item in content_types
                 if item.get("Extension", "").lower() == "xml"),
                None,
            )
            if (override or default) != WORKBOOK_TYPE:
                raise BadZipFile
            package_documents = [
                item for item in relationships(archive, "_rels/.rels")
                if item.get("Type", "").endswith("/officeDocument")
            ]
            if len(package_documents) != 1 or internal_target(package_documents[0], "") != "xl/workbook.xml":
                raise BadZipFile
            workbook = read_package_xml(archive, "xl/workbook.xml")
            if workbook.tag not in {
                "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}workbook",
                "{http://purl.oclc.org/ooxml/spreadsheetml/main}workbook",
            }:
                raise BadZipFile
            sheets = workbook.find(workbook.tag.replace("}workbook", "}sheets"))
            if sheets is None or not len(sheets):
                raise BadZipFile
            sheet_relationships = {}
            for item in relationships(archive, "xl/_rels/workbook.xml.rels"):
                kind = item.get("Type", "").lower()
                if any(marker in kind for marker in ("macrosheet", "vba")):
                    raise HTTPException(415, "Macro-enabled workbooks are unsupported")
                identifier = item.get("Id")
                if not identifier or identifier in sheet_relationships:
                    raise BadZipFile
                sheet_relationships[identifier] = item
            for sheet in sheets:
                identifier = next((sheet.get(key) for key in SHEET_RELATIONSHIP_IDS if key in sheet.attrib), None)
                target = internal_target(sheet_relationships[identifier], "xl")
                if archive.getinfo(target).is_dir():
                    raise BadZipFile

            expanded = 0
            for entry in entries:
                with archive.open(entry) as file:
                    if entry.filename.lower().endswith((".xml", ".rels")):
                        # Check XML syntax without extracting cell values or following links.
                        sax.parse(file, ContentHandler(), forbid_dtd=True)
                        expanded += entry.file_size
                    else:
                        while chunk := file.read(64 * 1024):
                            expanded += len(chunk)
                    if expanded > MAX_EXPANDED_BYTES:
                        raise HTTPException(413, "Workbook exceeds the 64 MiB expanded limit")
    except (
        BadZipFile, KeyError, ParseError, SAXException, DefusedXmlException, RuntimeError,
        EOFError, NotImplementedError, ValueError, ZipDataError,
    ):
        raise HTTPException(422, "Invalid or malformed .xlsx workbook") from None
