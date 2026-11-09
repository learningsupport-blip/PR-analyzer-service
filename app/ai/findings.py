from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


def _lower(value: object) -> object:
    return value.strip().lower() if isinstance(value, str) else value


class GuidelineItem(BaseModel):
    rule: str
    required: bool = True


class GuidelineDraft(BaseModel):
    items: list[GuidelineItem] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_rules(self) -> "GuidelineDraft":
        if not any(item.rule.strip() for item in self.items):
            raise ValueError("guideline checklist was empty")
        return self


class GuidelineChecklist(BaseModel):
    items: list[GuidelineItem] = Field(default_factory=list)
    source: Literal["repository", "default"] = "default"


class IssueDraft(BaseModel):
    problem: str = ""
    criteria: list[str] = Field(default_factory=list)
    namedFiles: list[str] = Field(default_factory=list)
    outOfScope: list[str] = Field(default_factory=list)


class IssueExtraction(BaseModel):
    problem: str = ""
    criteria: list[str] = Field(default_factory=list)
    namedFiles: list[str] = Field(default_factory=list)
    outOfScope: list[str] = Field(default_factory=list)
    missingIssue: bool = False


class DiffFileNote(BaseModel):
    path: str
    summary: str = ""


class DiffExtraction(BaseModel):
    files: list[DiffFileNote] = Field(default_factory=list)


class CriterionFinding(BaseModel):
    id: str = ""
    text: str
    status: Literal["met", "partial", "missing", "unknown"]
    evidence: str = ""

    @field_validator("status", mode="before")
    @classmethod
    def normalize_status(cls, value: object) -> object:
        return _lower(value)


class FileVerdict(BaseModel):
    path: str
    relation: Literal["required", "related", "unrelated", "unknown"]
    reason: str = ""

    @field_validator("relation", mode="before")
    @classmethod
    def normalize_relation(cls, value: object) -> object:
        return _lower(value)


class GuidelineCheck(BaseModel):
    rule: str
    status: Literal["pass", "fail", "unknown", "not_applicable"]
    evidence: str = ""

    @field_validator("status", mode="before")
    @classmethod
    def normalize_status(cls, value: object) -> object:
        return _lower(value)


class ReviewNote(BaseModel):
    severity: Literal["low", "medium", "high"] = "medium"
    path: str = ""
    statement: str
    confidence: Literal["low", "medium", "high"] = "medium"

    @field_validator("severity", "confidence", mode="before")
    @classmethod
    def normalize_level(cls, value: object) -> object:
        return _lower(value)


class JudgeFindings(BaseModel):
    criteria: list[CriterionFinding]
    files: list[FileVerdict]
    guidelines: list[GuidelineCheck]
    notes: list[ReviewNote]


DEFAULT_RULES = (
    "Link the issue with Fixes, Closes, or Resolves and an issue number.",
    "Describe the change and how it was verified.",
    "Include tests for behavior changes.",
    "Keep the pull request focused on one issue.",
)


def default_checklist() -> GuidelineChecklist:
    return GuidelineChecklist(
        items=[GuidelineItem(rule=rule) for rule in DEFAULT_RULES],
        source="default",
    )
