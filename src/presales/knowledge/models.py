from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Applicability(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    products: tuple[str, ...] = Field(min_length=1, max_length=100)
    deployments: tuple[str, ...] = Field(min_length=1, max_length=100)

    @field_validator("products", "deployments")
    @classmethod
    def normalized(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not v.strip() or len(v) > 128 for v in values):
            raise ValueError("Scope values must be nonempty and bounded")
        values = tuple(v.strip() for v in values)
        if "*" in values and len(values) != 1:
            raise ValueError("Wildcard must stand alone")
        return tuple(sorted(set(v.strip() for v in values)))

    def overlaps(self, other: "Applicability") -> bool:
        return all(
            "*" in left or "*" in right or bool(set(left) & set(right))
            for left, right in (
                (self.products, other.products),
                (self.deployments, other.deployments),
            )
        )


KnowledgeAction = Literal["publish", "replace", "retire"]
CATALOG_LOCK = 3
MAX_VERSIONS = 100
MAX_CHUNKS = 5000
