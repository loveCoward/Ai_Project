"""Pure parsers; all input is data and no links are fetched."""

import io
import json
import re
import zipfile
from xml.etree import ElementTree

from pypdf import PdfReader

from presales.materials.models import Location, ParsedUnit, ParseFailure


def pdf(data: bytes) -> list[ParsedUnit]:
    if not data.startswith(b"%PDF-"):
        raise ParseFailure("content_type_mismatch")
    reader = PdfReader(io.BytesIO(data), strict=True)
    if reader.is_encrypted:
        raise ParseFailure("encrypted_file")
    units = []
    for number, page in enumerate(reader.pages, 1):
        text = page.extract_text() or ""
        if not text.strip():
            raise ParseFailure("scanned_or_empty_pdf")
        units.append(ParsedUnit(text=text, location=Location(kind="pdf_page", page=number)))
    return units


def docx(data: bytes) -> list[ParsedUnit]:
    if not data.startswith(b"PK\x03\x04"):
        raise ParseFailure("content_type_mismatch")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries = archive.infolist()
        if len(entries) > 2048 or sum(e.file_size for e in entries) > 50 * 1024 * 1024:
            raise ParseFailure("docx_expansion_limit")
        if len({e.filename for e in entries}) != len(entries):
            raise ParseFailure("corrupt_file")
        if any(e.flag_bits & 1 for e in entries):
            raise ParseFailure("encrypted_file")
        if any(e.filename.startswith("/") or ".." in e.filename.split("/") for e in entries):
            raise ParseFailure("corrupt_file")
        if "[Content_Types].xml" not in archive.namelist():
            raise ParseFailure("content_type_mismatch")
        for entry in entries:
            if entry.filename.endswith((".xml", ".rels")):
                content = archive.read(entry)
                if b"<!DOCTYPE" in content.upper() or b"<!ENTITY" in content.upper():
                    raise ParseFailure("unsafe_xml")
        document = ElementTree.fromstring(archive.read("word/document.xml"))
    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    body = document.find(f"{ns}body")
    if body is None:
        raise ParseFailure("corrupt_file")
    units = []
    paragraph, table = 0, 0
    for element in body:
        if element.tag == f"{ns}p":
            paragraph += 1
            text = "".join(t.text or "" for t in element.iter(f"{ns}t"))
            location = Location(kind="docx_paragraph", paragraph=paragraph)
            if text.strip():
                units.append(ParsedUnit(text=text, location=location))
        elif element.tag == f"{ns}tbl":
            table += 1
            for row, tr in enumerate(element.findall(f"{ns}tr"), 1):
                for column, cell in enumerate(tr.findall(f"{ns}tc"), 1):
                    text = "\n".join(
                        "".join(t.text or "" for t in p.iter(f"{ns}t"))
                        for p in cell.findall(f"{ns}p")
                    )
                    if text.strip():
                        units.append(
                            ParsedUnit(
                                text=text,
                                location=Location(
                                    kind="docx_cell", table=table, row=row, column=column
                                ),
                            )
                        )
    return units


def markdown(data: bytes) -> list[ParsedUnit]:
    text = data.decode("utf-8-sig")
    headings: list[str] = []
    units = []
    paragraph: list[str] = []
    number = 0

    def flush() -> None:
        nonlocal number
        if paragraph:
            number += 1
            units.append(
                ParsedUnit(
                    text="\n".join(paragraph),
                    location=Location(
                        kind="markdown_paragraph", paragraph=number, heading=tuple(headings)
                    ),
                )
            )
            paragraph.clear()

    fence = None
    for line in text.splitlines():
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        if marker:
            fence = None if fence == marker[1][0] else marker[1][0]
        heading = re.match(r"^(#{1,6})[ \t]+(.+)$", line) if not fence else None
        if heading:
            flush()
            depth = len(heading[1])
            headings[:] = headings[: depth - 1]
            headings.append(heading[2].strip())
            paragraph.append(line)
            flush()
        elif not line.strip() and not fence:
            flush()
        else:
            paragraph.append(line)
    flush()
    return units


def structured_json(data: bytes) -> list[ParsedUnit]:
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ParseFailure("duplicate_json_key")
            result[key] = value
        return result

    def invalid_constant(value):
        raise ParseFailure("corrupt_file")

    content = json.loads(
        data.decode("utf-8-sig"), object_pairs_hook=pairs, parse_constant=invalid_constant
    )
    units = []

    def walk(value, path: str) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                walk(item, path + "/" + key.replace("~", "~0").replace("/", "~1"))
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, path + f"/{index}")
        else:
            text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            units.append(ParsedUnit(text=text, location=Location(kind="json_path", path=path)))

    walk(content, "")
    return units
