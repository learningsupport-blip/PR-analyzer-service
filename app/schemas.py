from typing import Literal

from pydantic import BaseModel, Field


class PullRequestSubject(BaseModel):
    url: str
    repository: str
    number: int = Field(ge=1)
    title: str = ""
    body: str = ""
    state: str = ""
    merged: bool = False
    draft: bool = False
    authorLogin: str = ""
    additions: int | None = None
    deletions: int | None = None
    changedFiles: int | None = None
    commits: int | None = None


class AnalysisAspects(BaseModel):
    quality: bool = True
    mergeReadiness: bool = True
    checks: bool = True
    coaching: bool = True


class AnalysisWeights(BaseModel):
    issueMatch: int = Field(default=4, ge=0, le=10)
    fileMatch: int = Field(default=4, ge=0, le=10)
    description: int = Field(default=2, ge=0, le=10)
    checks: int = Field(default=6, ge=0, le=10)
    size: int = Field(default=4, ge=0, le=10)


class AnalysisConfig(BaseModel):
    aspects: AnalysisAspects = Field(default_factory=AnalysisAspects)
    suggestScores: bool = True
    weights: AnalysisWeights = Field(default_factory=AnalysisWeights)


class ReviewRequest(BaseModel):
    pullRequest: PullRequestSubject
    config: AnalysisConfig = Field(default_factory=AnalysisConfig)


class Insight(BaseModel):
    metric: Literal["clarity", "scope", "evidence", "outcome", "general"]
    title: str
    detail: str


class SuggestedScores(BaseModel):
    clarity: int = Field(ge=0, le=10)
    scope: int = Field(ge=0, le=10)
    evidence: int = Field(ge=0, le=10)
    outcome: int = Field(ge=0, le=10)


class MergeFactor(BaseModel):
    code: str
    delta: int
    detail: str


class MergeEstimate(BaseModel):
    value: int = Field(ge=5, le=92)
    band: Literal["low", "medium", "high"]
    factors: list[MergeFactor] = Field(default_factory=list)


class ReportCriterion(BaseModel):
    text: str
    status: Literal["met", "partial", "missing", "unknown"]
    evidence: str = ""


class ReportFile(BaseModel):
    path: str
    relation: Literal["required", "related", "unrelated", "unknown"]
    reason: str = ""


class ReportGuideline(BaseModel):
    rule: str
    status: Literal["pass", "fail", "unknown", "not_applicable"]
    evidence: str = ""


class ReportNote(BaseModel):
    severity: Literal["low", "medium", "high"]
    path: str
    statement: str
    confidence: Literal["low", "medium", "high"]


class AnalysisReport(BaseModel):
    issueFit: int = Field(ge=0, le=10)
    changeFit: int = Field(ge=0, le=10)
    guidelines: int = Field(ge=0, le=10)
    evidence: int = Field(ge=0, le=10)
    clarity: int = Field(ge=0, le=10)
    overall: float
    missingGuide: bool = False
    criteria: list[ReportCriterion] = Field(default_factory=list)
    files: list[ReportFile] = Field(default_factory=list)
    guidelineChecks: list[ReportGuideline] = Field(default_factory=list)
    notes: list[ReportNote] = Field(default_factory=list)
    skippedFiles: list[str] = Field(default_factory=list)
    reviewedFiles: list[str] = Field(default_factory=list)
    truncatedFiles: list[str] = Field(default_factory=list)
    failingChecks: list[str] = Field(default_factory=list)
    nextSteps: list[str] = Field(default_factory=list)


class ReviewResponse(BaseModel):
    summary: str
    insights: list[Insight]
    suggestedScores: SuggestedScores | None = None
    category: Literal["bad", "good", "very_good"] | None = None
    categoryLabel: str | None = None
    mergeEstimate: MergeEstimate | None = None
    report: AnalysisReport | None = None
    analysisMode: Literal["rubric", "gemini"] | None = None
