from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

PARSER_VERSION = "located-1.0"
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_FILE_CHARS = 100_000
MAX_TASK_CHARS = 300_000


class ParseFailure(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class Location(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    kind: Literal["pdf_page", "docx_paragraph", "docx_cell", "markdown_paragraph", "json_path"]
    page: int | None = Field(default=None, ge=1)
    paragraph: int | None = Field(default=None, ge=1)
    table: int | None = Field(default=None, ge=1)
    row: int | None = Field(default=None, ge=1)
    column: int | None = Field(default=None, ge=1)
    heading: tuple[str, ...] = ()
    path: str | None = None

    @model_validator(mode="after")
    def required_locator(self):
        required = {
            "pdf_page": ("page",),
            "docx_paragraph": ("paragraph",),
            "docx_cell": ("table", "row", "column"),
            "markdown_paragraph": ("paragraph",),
            "json_path": ("path",),
        }[self.kind]
        if any(getattr(self, name) is None for name in required):
            raise ValueError("Required source coordinates missing")
        return self


class ParsedUnit(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    text: str
    location: Location


class ParsedDocument(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    parser_version: Literal["located-1.0"] = PARSER_VERSION
    units: tuple[ParsedUnit, ...]
    character_count: int = Field(ge=0, le=MAX_FILE_CHARS)

    @model_validator(mode="after")
    def actual_count(self):
        if not self.units or self.character_count != sum(len(u.text) for u in self.units):
            raise ValueError("Parsed text count mismatch")
        return self


class ManifestItem(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    material_version_id: UUID
    necessity: Literal["required", "optional", "unclassified"]
