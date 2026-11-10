import re
from dataclasses import dataclass
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator

from presales.domain.contracts import digest
from presales.materials.models import Location, ParsedDocument


class ChunkConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    version: str = "paragraph-600-80-v1"
    target_tokens: int = Field(default=600, ge=1, le=600)
    overlap_tokens: int = Field(default=80, ge=0, le=80)
    tokenizer: str = "unicode-cjk-word-v1"

    @model_validator(mode="after")
    def validate_config(self):
        if self.overlap_tokens >= self.target_tokens or self.tokenizer != "unicode-cjk-word-v1":
            raise ValueError("Invalid chunk or tokenizer configuration")
        return self


@dataclass(frozen=True)
class Chunk:
    id: UUID
    text: str
    location: Location
    start: int
    end: int
    tokens: tuple[str, ...]


def tokens(text: str) -> tuple[str, ...]:
    return tuple(
        m.group().casefold()
        for m in re.finditer(r"[\u3400-\u9fff]|[^\W\u3400-\u9fff]+|[^\w\s]", text, re.UNICODE)
    )


def split_chunks(document: ParsedDocument, parse_id: UUID, config: ChunkConfig) -> list[Chunk]:
    chunks = []
    for unit_index, unit in enumerate(document.units):
        matches = list(re.finditer(r"[\u3400-\u9fff]|[^\W\u3400-\u9fff]+|[^\w\s]", unit.text))
        if not matches:
            continue
        offset = 0
        while offset < len(matches):
            stop = min(offset + config.target_tokens, len(matches))
            start_char = 0 if offset == 0 else matches[offset].start()
            end_char = len(unit.text) if stop == len(matches) else matches[stop].start()
            text = unit.text[start_char:end_char]
            key = digest([config.model_dump(), unit_index, start_char, end_char, text])
            chunks.append(
                Chunk(uuid5(parse_id, key), text, unit.location, start_char, end_char, tokens(text))
            )
            if stop == len(matches):
                break
            offset = stop - config.overlap_tokens
    return chunks
