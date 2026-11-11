import math
from collections import Counter
from uuid import UUID

from presales.indexing.chunks import tokens


def bm25(query: str, documents: dict[UUID, tuple[str, ...]]) -> list[tuple[UUID, float]]:
    if not documents:
        return []
    terms = set(tokens(query))
    size = len(documents)
    average = sum(len(value) for value in documents.values()) / size or 1
    frequencies = Counter(term for value in documents.values() for term in set(value))
    ranked = []
    for identifier, document in documents.items():
        counts = Counter(document)
        score = sum(
            math.log(1 + (size - frequencies[term] + 0.5) / (frequencies[term] + 0.5))
            * (counts[term] * 2.2)
            / (counts[term] + 1.2 * (0.25 + 0.75 * len(document) / average))
            for term in terms
            if counts[term]
        )
        if score > 0:
            ranked.append((identifier, score))
    return sorted(ranked, key=lambda item: (-item[1], str(item[0])))


def rrf(routes: list[list[UUID]], constant: int = 60) -> list[tuple[UUID, float]]:
    scores: dict[UUID, float] = {}
    for route in routes:
        for rank, identifier in enumerate(dict.fromkeys(route), 1):
            scores[identifier] = scores.get(identifier, 0.0) + 1 / (constant + rank)
    return sorted(scores.items(), key=lambda item: (-item[1], str(item[0])))
