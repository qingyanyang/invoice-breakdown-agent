"""Search allowlisted guidance from the verified primary rules already in memory."""

import re
import unicodedata
from dataclasses import asdict, dataclass

from app.classification_evidence import COMBINED_FLOORS
from app.request_receipts import fingerprint

RETRIEVAL_VERSION = "keyword-v2"
MAX_MATCHES = 6
MAX_PASSAGE_LENGTH = 640
MAX_ROW_REFERENCES = 12
MAX_QUERY_ROWS = 300
MAX_QUERY_CHARS = 640
SECTION = re.compile(r"^##\s+(\d+)\.\s+(.+)$", re.MULTILINE)
PASSAGE_BOUNDARY = re.compile(r"\n(?=(?:[-*]|\d+\.)\s)|\n[ \t]*\n")
HISTORY = re.compile(
    r"\b(?:historical|example|unrelated|sample|past project|previous project|prior project|earlier project)\b", re.IGNORECASE,
)
ILLUSTRATIVE_VALUE = re.compile(r"[$€£¥]|\b\d")


@dataclass(frozen=True)
class RuleDocument:
    document: str
    version: str
    sha256: str


@dataclass(frozen=True)
class RuleTopic:
    name: str
    section: str
    terms: tuple[str, ...]
    anchors: tuple[str, ...]


# The section numbers and starts identify guidance in the approved workflow.
# Matching terms only locate passages. They never assign trades or amounts.
TOPICS = (
    RuleTopic("doors", "6", ("ffe", "ff&e", "masterkey", "master key", "door hardware"), (
        "classify FF&E and Masterkey under Doors",
        "FF&E maps to Doors",
    )),
    RuleTopic("access_panel", "6", ("access panel", "access panels", "access hatch"), (
        "classify Access Panel separately from Ceiling",
        "include confirmed types according to the reference",
        "distinguish amounts already included in the control total",
        "do not invent a value when Notes exclude Access Panel",
        "investigate a conflict between exclusion Notes and priced detail",
    )),
    RuleTopic("glazing", "6", ("glazing", "glazing frames", "glass", "mullions"), (
        "separate glass, glazing frames, and mullions from Partition",
    )),
    RuleTopic("make_good", "6", ("make good", "repair", "patching"), (
        "classify genuine repair and patching work under Make Good",
    )),
    RuleTopic("other", "6", ("hoarding", "temporary fencing", "miscellaneous work", "other items"), (
        "use Other only for items that cannot reasonably fit another category",
    )),
    RuleTopic("combined_area", "7", ("combined areas", "combined floors", "combined amounts"), (
        "do not invent floor allocations or divide combined amounts evenly",
        "preserve combined areas",
        "where a floor is shown without independent amounts",
    )),
    RuleTopic("additional_items", "7", ("unallocated items", "additional items"), (
        "place unallocated items in Additional Items",
    )),
    RuleTopic("scope_adjustments", "5", ("option", "removal", "deletion", "base scope"), (
        "determine whether Option, Removal, and Deletion items are included",
        "if the user requests base scope only",
        "preserve their original signs",
        "bridge the original source Total",
    )),
)


@dataclass(frozen=True)
class RetrievedRule:
    passage_id: str
    document: str
    version: str
    sha256: str
    section: str
    heading: str
    text: str
    row_refs: tuple[str, ...]


@dataclass(frozen=True)
class RetrievalResult:
    status: str
    passages: tuple[RetrievedRule, ...] = ()

    def report(self) -> dict:
        return {
            "status": self.status,
            "configuration": RETRIEVAL_VERSION,
            "passages": [asdict(passage) for passage in self.passages],
        }


def tokens(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    normalized = re.sub(r"\bff\s*&\s*e\b", "ffe", normalized)
    return " ".join(re.findall(r"[^\W_]+", normalized))


def approved_passages(text: str) -> dict[str, tuple[str, str]]:
    """Extract complete matching rule clauses, omitting unknown and historical text."""
    boundaries = list(SECTION.finditer(text))
    sections = {}
    for index, heading in enumerate(boundaries):
        stop = boundaries[index + 1].start() if index + 1 < len(boundaries) else len(text)
        label, title = heading.groups()
        if label in sections:
            return {}
        sections[label] = (title.strip(), text[heading.end():stop])

    passages = {}
    for topic in TOPICS:
        if topic.section not in sections:
            continue
        heading, body = sections[topic.section]
        selected, length = [], 0
        anchors = tuple(tokens(anchor) + " " for anchor in topic.anchors)
        for part in PASSAGE_BOUNDARY.split(body):
            clause = re.sub(r"^\s*(?:[-*]|\d+\.)\s+", "", part.strip())
            clause = " ".join(clause.split())
            if HISTORY.search(clause) or ILLUSTRATIVE_VALUE.search(clause):
                continue
            if (tokens(clause) + " ").startswith(anchors) and length + len(clause) + 1 <= MAX_PASSAGE_LENGTH:
                selected.append(clause)
                length += len(clause) + 1
        if selected:
            passages[topic.name] = (heading, "\n".join(selected))
    return passages


def search_rules(text: str, source_sha256: str, approved: RuleDocument, inputs: dict) -> RetrievalResult:
    if source_sha256 != approved.sha256:
        return RetrievalResult("version_mismatch")
    passages = approved_passages(text)
    ranked = []
    rows = inputs["rows"][:MAX_QUERY_ROWS]
    for topic in TOPICS:
        if topic.name not in passages:
            continue
        terms = tuple((" " + tokens(term) + " ", len(tokens(term).split())) for term in topic.terms)
        row_refs, score = [], 0
        cues = rows
        if topic.name == "scope_adjustments":
            cues = [*rows, *inputs.get("scope_rows", [])[:MAX_QUERY_ROWS]]
        for row in cues:
            areas = list(row.get("area_candidates", {}))[:8]
            cue = " ".join((row["description"][:MAX_QUERY_CHARS], *areas))[:MAX_QUERY_CHARS]
            query = " " + tokens(cue) + " "
            weight = max((strength for term, strength in terms if term in query), default=0)
            if topic.name == "combined_area" and (
                any(COMBINED_FLOORS.fullmatch(area[:MAX_QUERY_CHARS]) for area in areas)
                or COMBINED_FLOORS.search(row["description"][:MAX_QUERY_CHARS])
            ):
                weight = max(weight, 2)
            if weight:
                score = max(score, weight)
                if row["row"] not in row_refs:
                    row_refs.append(row["row"])
        if not row_refs:
            continue
        heading, clause = passages[topic.name]
        hit = RetrievedRule(
            passage_id=fingerprint([approved.document, approved.sha256, topic.section, topic.name, clause]),
            document=approved.document, version=approved.version, sha256=approved.sha256,
            section=topic.section, heading=heading, text=clause, row_refs=tuple(row_refs[:MAX_ROW_REFERENCES]),
        )
        ranked.append((-score, topic.name, hit))
    ranked.sort()
    matches = tuple(hit for _, _, hit in ranked[:MAX_MATCHES])
    return RetrievalResult("matched" if matches else "no_match", matches)
