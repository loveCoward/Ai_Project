import json
import os
import subprocess
import sys
from pathlib import Path

from presales.materials import parsers
from presales.materials.models import (
    MAX_FILE_BYTES,
    MAX_FILE_CHARS,
    ParsedDocument,
    ParseFailure,
)


def parse_bytes(filename: str, data: bytes) -> ParsedDocument:
    if len(data) > MAX_FILE_BYTES:
        raise ParseFailure("file_byte_limit")
    extension = Path(filename).suffix.lower()
    parser = {
        ".pdf": parsers.pdf,
        ".docx": parsers.docx,
        ".md": parsers.markdown,
        ".json": parsers.structured_json,
    }.get(extension)
    if parser is None:
        raise ParseFailure("unsupported_extension")
    try:
        units = parser(data)
    except ParseFailure:
        raise
    except Exception:
        raise ParseFailure("corrupt_file") from None
    count = sum(len(unit.text) for unit in units)
    if count > MAX_FILE_CHARS:
        raise ParseFailure("file_character_limit")
    if not units or not any(u.text.strip() for u in units):
        raise ParseFailure("no_extractable_text")
    return ParsedDocument(units=tuple(units), character_count=count)


class ControlledParser:
    def __init__(self, timeout_seconds: float = 60) -> None:
        if not 0 < timeout_seconds <= 60:
            raise ValueError("Parser timeout must be within 60 seconds")
        self.timeout_seconds = timeout_seconds

    def parse(self, filename: str, data: bytes) -> ParsedDocument:
        if len(data) > MAX_FILE_BYTES:
            raise ParseFailure("file_byte_limit")
        inherited = {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATH"}
        environment = {k: v for k, v in os.environ.items() if k.upper() in inherited}
        environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        with subprocess.Popen(
            [sys.executable, "-m", "presales.materials.worker", Path(filename).suffix.lower()],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=environment,
            creationflags=flags,
        ) as process:
            try:
                output, _ = process.communicate(data, timeout=self.timeout_seconds)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
                raise ParseFailure("parse_timeout") from None
        if process.returncode != 0 or len(output) > 4 * 1024 * 1024:
            raise ParseFailure("parser_failed")
        try:
            result = json.loads(output)
            if "error" in result:
                raise ParseFailure(result["error"])
            return ParsedDocument.model_validate(result)
        except ParseFailure:
            raise
        except Exception:
            raise ParseFailure("parser_failed") from None
