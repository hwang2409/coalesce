from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Cardinality(str, Enum):
    ONE = "one"
    ONE_OR_MORE = "one_or_more"
    EXACTLY = "exactly"

class Status(str, Enum):
    OK = "OK"
    SPLIT = "SPLIT"
    MERGED = "MERGED"
    AMBIGUOUS = "AMBIGUOUS"
    EMPTY = "EMPTY"
    ORDER = "ORDER"

@dataclass(frozen=True)
class ElementKey:
    feature: str
    role: str
    source: str | None = None

@dataclass
class Candidate:
    key: ElementKey
    entity: Any = field(default=None, repr=False, compare=False)
    descriptor: dict[str, Any] = field(default_factory=dict)

@dataclass
class Reference:
    feature: str
    role: str
    cardinality: Cardinality = Cardinality.ONE
    count: int | None = None
    source: str | None = None
    def to_dict(self) -> dict[str, Any]:
        return {"feature": self.feature, "role": self.role, "cardinality": self.cardinality.value, "count": self.count, "source": self.source}

@dataclass
class Resolution:
    status: Status
    candidates: list[Candidate] = field(default_factory=list)
    error: dict[str, Any] | None = None
