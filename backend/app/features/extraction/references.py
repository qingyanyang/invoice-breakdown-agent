"""Source coordinates and validated source labels."""

from typing import Annotated, NamedTuple

from pydantic import StringConstraints


class Reference(NamedTuple):
    sheet: str
    cell: str

    @property
    def key(self) -> str:
        return f"{self.sheet}!{self.cell}"

    @classmethod
    def from_dict(cls, value: dict) -> "Reference":
        return cls(value["sheet"], value["cell"])

    def evidence(self) -> dict:
        return {"sheet": self.sheet, "cell": self.cell}


SourceKey = Annotated[str, StringConstraints(min_length=1, max_length=255)]
