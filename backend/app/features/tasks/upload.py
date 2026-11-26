from io import BytesIO
from pathlib import PurePath
from posixpath import normpath
from urllib.parse import unquote, urlsplit
from xml.etree.ElementTree import ParseError
from xml.sax import SAXException
from xml.sax.handler import ContentHandler, feature_namespaces
from zipfile import ZIP_DEFLATED, ZIP_STORED, BadZipFile, ZipFile
from zlib import error as ZipDataError

from defusedxml import sax
from defusedxml.ElementTree import fromstring
from defusedxml.common import DefusedXmlException
from app.outcomes import FailureKind, UseCaseError

XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_EXPANDED_BYTES = 64 * 1024 * 1024
MAX_XML_BYTES = 1024 * 1024
MAX_ENTRIES = 2048
PACKAGE_RELATIONSHIPS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
DOCUMENT_RELATIONSHIP_NAMESPACES = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "http://purl.oclc.org/ooxml/officeDocument/relationships",
)
SHEET_KINDS = {"worksheet", "chartsheet", "dialogsheet"}
SHEET_TYPES = {
    f"{namespace}/{kind}": kind
    for namespace in DOCUMENT_RELATIONSHIP_NAMESPACES
    for kind in SHEET_KINDS
}
SPREADSHEET_CONTENT_TYPE_PREFIX = "application/vnd.openxmlformats-officedocument.spreadsheetml."
WORKBOOK_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"


def validate_filename(filename: str) -> None:
    if not filename or len(filename) > 255 or any(
        char in "/\\" or ord(char) < 32 or ord(char) == 127 for char in filename
    ):
        raise UseCaseError(FailureKind.INVALID_REQUEST, "Use a plain workbook filename")
    extension = PurePath(filename).suffix.lower()
    if extension != ".xlsx":
        raise UseCaseError(FailureKind.UNSUPPORTED_FORMAT, "Only .xlsx is supported. Legacy .xls import is deferred.")


async def read_upload(chunks) -> bytes:
    body = bytearray()
    async for chunk in chunks:
        if len(body) + len(chunk) > MAX_UPLOAD_BYTES:
            raise UseCaseError(FailureKind.TOO_LARGE, "Workbook exceeds the 10 MiB upload limit")
        body.extend(chunk)
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


class SheetRoot(ContentHandler):
    def __init__(self, expected: tuple[str, str]):
        self.expected = expected

    def startElementNS(self, name, _qualified_name, _attributes):
        if self.expected is not None:
            if name != self.expected:
                raise BadZipFile
            self.expected = None


def validate_xml(file, expected_root: tuple[str, str] | None) -> None:
    parser = sax.make_parser()
    parser.forbid_dtd = True
    parser.setFeature(feature_namespaces, True)
    parser.setContentHandler(SheetRoot(expected_root) if expected_root else ContentHandler())
    parser.parse(file)


def validate_workbook(data: bytes) -> None:
    try:
        with ZipFile(BytesIO(data)) as archive:
            entries = archive.infolist()
            names = {entry.filename.lower() for entry in entries}
            if len(entries) != len(names) or len(entries) > MAX_ENTRIES:
                raise BadZipFile
            if any(entry.flag_bits & 0x41 for entry in entries):
                raise UseCaseError(FailureKind.UNSUPPORTED_FORMAT, "Encrypted workbooks are unsupported")
            if any(entry.compress_type not in {ZIP_STORED, ZIP_DEFLATED} for entry in entries):
                raise BadZipFile
            if sum(entry.file_size for entry in entries) > MAX_EXPANDED_BYTES:
                raise UseCaseError(FailureKind.TOO_LARGE, "Workbook exceeds the 64 MiB expanded limit")
            if any(
                "vbaproject" in name or name.startswith(("xl/macrosheets/", "xl/intlmacrosheets/"))
                for name in names
            ):
                raise UseCaseError(FailureKind.UNSUPPORTED_FORMAT, "Macro-enabled workbooks are unsupported")
            content_types = read_package_xml(archive, "[Content_Types].xml")
            if content_types.tag != "{http://schemas.openxmlformats.org/package/2006/content-types}Types":
                raise BadZipFile
            overrides = {}
            defaults = {}
            for declaration in content_types:
                kind = declaration.get("ContentType", "")
                if any(marker in kind.lower() for marker in ("macroenabled", "macrosheet", "vba")):
                    raise UseCaseError(FailureKind.UNSUPPORTED_FORMAT, "Macro-enabled workbooks are unsupported")
                if path := declaration.get("PartName"):
                    overrides[path] = kind
                if extension := declaration.get("Extension"):
                    defaults[extension.lower()] = kind

            def content_type(part: str) -> str | None:
                extension = PurePath(part).suffix.lstrip(".").lower()
                return overrides.get("/" + part, defaults.get(extension))

            if content_type("xl/workbook.xml") != WORKBOOK_TYPE:
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
                    raise UseCaseError(FailureKind.UNSUPPORTED_FORMAT, "Macro-enabled workbooks are unsupported")
                identifier = item.get("Id")
                if not identifier or identifier in sheet_relationships:
                    raise BadZipFile
                sheet_relationships[identifier] = item
            expected_roots = {}
            spreadsheet_namespace = workbook.tag[1:].split("}", 1)[0]
            for sheet in sheets:
                if sheet.tag != f"{{{spreadsheet_namespace}}}sheet":
                    raise BadZipFile
                identifier = next(
                    (sheet.get(f"{{{namespace}}}id") for namespace in DOCUMENT_RELATIONSHIP_NAMESPACES
                     if f"{{{namespace}}}id" in sheet.attrib),
                    None,
                )
                relationship = sheet_relationships[identifier]
                sheet_kind = SHEET_TYPES[relationship.get("Type")]
                target = internal_target(relationship, "xl")
                expected_type = f"{SPREADSHEET_CONTENT_TYPE_PREFIX}{sheet_kind}+xml"
                if archive.getinfo(target).is_dir() or content_type(target) != expected_type:
                    raise BadZipFile
                expected_roots[target] = (spreadsheet_namespace, sheet_kind)

            expanded = 0
            for entry in entries:
                with archive.open(entry) as file:
                    expected_root = expected_roots.get(entry.filename)
                    if expected_root or entry.filename.lower().endswith((".xml", ".rels")):
                        # Check XML syntax and sheet roots without reading cell values or following links.
                        validate_xml(file, expected_root)
                        expanded += entry.file_size
                    else:
                        while chunk := file.read(64 * 1024):
                            expanded += len(chunk)
                    if expanded > MAX_EXPANDED_BYTES:
                        raise UseCaseError(FailureKind.TOO_LARGE, "Workbook exceeds the 64 MiB expanded limit")
    except (
        BadZipFile, LookupError, ParseError, SAXException, DefusedXmlException, RuntimeError,
        EOFError, NotImplementedError, ValueError, ZipDataError,
    ):
        raise UseCaseError(FailureKind.INVALID_DATA, "Invalid or malformed .xlsx workbook") from None
