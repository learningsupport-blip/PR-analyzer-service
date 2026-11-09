import re

from app.ai.findings import GuidelineChecklist, IssueExtraction, JudgeFindings, ReviewNote
from app.github_client import Snapshot
from app.rubric import _overlap, redact
from app.schemas import (
    AnalysisConfig,
    AnalysisReport,
    Insight,
    MergeEstimate,
    MergeFactor,
    PullRequestSubject,
    ReportCriterion,
    ReportFile,
    ReportGuideline,
    ReportNote,
    ReviewResponse,
    SuggestedScores,
)


def _clamp(value: float) -> int:
    if value >= 10:
        return 10
    if value <= 0:
        return 0
    return int(round(value))


def _short(value: str, limit: int) -> str:
    text = redact(re.sub(r"\s+", " ", value or "")).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def category_for(overall: float, *, linked: bool, failing_check: bool) -> tuple[str, str]:
    if overall < 5:
        key = "bad"
    elif overall < 8:
        key = "good"
    else:
        key = "very_good"
    if key == "very_good" and (not linked or failing_check):
        key = "good"
    labels = {"bad": "Bad PR", "good": "Good PR", "very_good": "Very Good PR"}
    return key, labels[key]


def evidence_score(snapshot: Snapshot, use_checks: bool) -> int:
    if not use_checks or not snapshot.fetched or not snapshot.checks:
        return 5
    failing = [check for check in snapshot.checks if check.failing]
    if not failing:
        return 9
    return _clamp(8 - 2 * len(failing))


def template_adjustment(body: str, template: str) -> int:
    headings = [item.strip() for item in re.findall(r"(?m)^#{1,6}\s+(.+?)\s*$", template or "") if item.strip()]
    if not headings:
        return 0
    lowered = body.lower()
    if all(heading.lower() in lowered for heading in headings):
        return 2
    return -2


def clarity_score(pull_request: PullRequestSubject, snapshot: Snapshot) -> int:
    overlap = _overlap(snapshot.issue_title, pull_request.title) if snapshot.issue_title else 0
    clarity = 2
    body = pull_request.body.strip()
    if len(body) >= 80:
        clarity += 4
    elif len(body) >= 20:
        clarity += 2
    if snapshot.issue_number:
        clarity += 2
    if overlap >= 0.34:
        clarity += 2
    clarity += template_adjustment(pull_request.body, snapshot.pull_request_template)
    return _clamp(clarity)


def _file_present(named: str, files: tuple[str, ...]) -> bool:
    target = named.strip().replace("\\", "/").lstrip("./").lower()
    if not target:
        return True
    base = target.split("/")[-1]
    for name in files:
        lowered = name.lower()
        if lowered == target or lowered.endswith("/" + target) or lowered.split("/")[-1] == base:
            return True
    return False


def _issue_loaded(snapshot: Snapshot) -> bool:
    return bool(snapshot.issue_title or snapshot.issue_body)


def issue_fit_score(snapshot: Snapshot, findings: JudgeFindings) -> int:
    if not snapshot.issue_number or not _issue_loaded(snapshot):
        return 2
    points: list[float] = []
    for item in findings.criteria:
        if item.status == "met":
            points.append(1)
        elif item.status == "partial":
            points.append(0.5)
        elif item.status == "missing":
            points.append(0)
    if not points:
        return 5
    return _clamp(10 * sum(points) / len(points))


def change_fit_score(snapshot: Snapshot, issue: IssueExtraction, findings: JudgeFindings) -> int:
    score = 8
    reviewed = {item.filename for item in snapshot.patches}
    for verdict in findings.files:
        if verdict.path not in reviewed:
            continue
        if verdict.relation == "unrelated":
            score -= 2
        elif verdict.relation == "required":
            score += 1
    for named in issue.namedFiles:
        if named.strip() and not _file_present(named, snapshot.files):
            score -= 2
    return _clamp(score)


def guideline_score(checklist: GuidelineChecklist, findings: JudgeFindings) -> int:
    failed = any(item.status == "fail" for item in findings.guidelines)
    scored = [item for item in findings.guidelines if item.status in {"pass", "fail"}]
    if checklist.source == "default" and not failed:
        return 6
    if not scored:
        return 5 if checklist.source == "repository" else 6
    passed = sum(1 for item in scored if item.status == "pass")
    return _clamp(10 * passed / len(scored))


def merge_estimate(
    pull_request: PullRequestSubject,
    snapshot: Snapshot,
    issue_fit: int,
    *,
    use_checks: bool,
) -> MergeEstimate:
    value = 55
    factors: list[MergeFactor] = []
    failing = [check for check in snapshot.checks if check.failing] if use_checks and snapshot.fetched else []
    if use_checks and snapshot.fetched and snapshot.checks and not failing:
        value += 20
        factors.append(MergeFactor(code="checks_green", delta=20, detail="Required checks passed"))
    if failing:
        value -= 25
        factors.append(MergeFactor(code="checks_failing", delta=-25, detail="A required check is failing"))
    if snapshot.mergeable is False:
        value -= 20
        factors.append(MergeFactor(code="conflicts", delta=-20, detail="The pull request has merge conflicts"))
    if pull_request.draft:
        value -= 15
        factors.append(MergeFactor(code="draft", delta=-15, detail="The pull request is a draft"))
    if issue_fit >= 8:
        value += 15
        factors.append(MergeFactor(code="issue_fit_high", delta=15, detail="Issue fit is strong"))
    if issue_fit <= 3:
        value -= 20
        factors.append(MergeFactor(code="issue_fit_low", delta=-20, detail="Issue fit is low"))
    value = max(5, min(92, value))
    if value < 40:
        band = "low"
    elif value < 70:
        band = "medium"
    else:
        band = "high"
    return MergeEstimate(value=value, band=band, factors=factors)


def _grounded(note: ReviewNote, snapshot: Snapshot) -> bool:
    patch = next((item for item in snapshot.patches if item.filename == note.path and not item.truncated), None)
    if patch is None or not note.statement.strip():
        return False
    snippets = re.findall(r"`([^`]{4,})`", note.statement)
    return all(snippet in patch.patch for snippet in snippets)


def _next_steps(
    pull_request: PullRequestSubject,
    snapshot: Snapshot,
    findings: JudgeFindings,
    failing: list[str],
) -> list[str]:
    steps: list[str] = []
    if not snapshot.issue_number:
        steps.append("Link the issue with Fixes #number so the change can be checked against it.")
    for criterion in findings.criteria:
        if criterion.status in {"missing", "partial"} and snapshot.issue_number:
            steps.append(f"Cover the issue criterion: {_short(criterion.text, 140)}")
    for verdict in findings.files:
        if verdict.relation == "unrelated" and verdict.path in snapshot.files:
            steps.append(f"Split unrelated file {verdict.path} out of this pull request.")
            break
    if failing:
        steps.append(f"Fix the failing check {failing[0]} before asking for review.")
    elif (pull_request.additions or 0) > 400:
        steps.append("Split unrelated files into a smaller pull request.")
    for check in findings.guidelines:
        if check.status == "fail":
            steps.append(f"Follow the contributing guide: {_short(check.rule, 140)}")
            break
    unique: list[str] = []
    for step in steps:
        if step not in unique:
            unique.append(step)
    return unique[:3]


def _insight(metric: str, title: str, detail: str) -> Insight:
    return Insight(metric=metric, title=_short(title, 120), detail=_short(detail, 1000))  # type: ignore[arg-type]


def score_review(
    pull_request: PullRequestSubject,
    snapshot: Snapshot,
    config: AnalysisConfig,
    checklist: GuidelineChecklist,
    issue: IssueExtraction,
    findings: JudgeFindings,
) -> ReviewResponse:
    allowed = set(snapshot.files)
    findings = findings.model_copy(
        update={
            "files": [item for item in findings.files if item.path in allowed],
            "notes": [item for item in findings.notes if _grounded(item, snapshot)],
        }
    )
    use_checks = config.aspects.checks
    issue_fit = issue_fit_score(snapshot, findings)
    change_fit = change_fit_score(snapshot, issue, findings)
    guidelines = guideline_score(checklist, findings)
    evidence = evidence_score(snapshot, use_checks)
    clarity = clarity_score(pull_request, snapshot)
    overall = round(
        0.30 * issue_fit + 0.25 * change_fit + 0.20 * guidelines + 0.15 * evidence + 0.10 * clarity,
        2,
    )
    failing = [check.name for check in snapshot.checks if check.failing] if use_checks and snapshot.fetched else []
    category, label = category_for(overall, linked=snapshot.issue_number is not None, failing_check=bool(failing))
    estimate = merge_estimate(pull_request, snapshot, issue_fit, use_checks=use_checks)
    steps = _next_steps(pull_request, snapshot, findings, failing)
    reviewed = [item.filename for item in snapshot.patches]
    truncated = list(snapshot.truncated_names())
    report = AnalysisReport(
        issueFit=issue_fit,
        changeFit=change_fit,
        guidelines=guidelines,
        evidence=evidence,
        clarity=clarity,
        overall=overall,
        missingGuide=checklist.source == "default",
        criteria=[
            ReportCriterion(text=_short(item.text, 240), status=item.status, evidence=_short(item.evidence, 240))
            for item in findings.criteria[:12]
        ],
        files=[
            ReportFile(path=item.path, relation=item.relation, reason=_short(item.reason, 240))
            for item in findings.files[:20]
        ],
        guidelineChecks=[
            ReportGuideline(rule=_short(item.rule, 240), status=item.status, evidence=_short(item.evidence, 240))
            for item in findings.guidelines[:12]
        ],
        notes=[
            ReportNote(
                severity=item.severity,
                path=item.path,
                statement=_short(item.statement, 300),
                confidence=item.confidence,
            )
            for item in findings.notes[:6]
        ],
        skippedFiles=list(snapshot.files_skipped),
        reviewedFiles=reviewed,
        truncatedFiles=truncated,
        failingChecks=failing,
        nextSteps=steps,
    )
    insights = _insights(
        pull_request,
        snapshot,
        config,
        checklist,
        findings,
        label,
        estimate,
        issue_fit,
        change_fit,
        failing,
        steps,
    )
    summary = (
        f"{label}. Issue fit {issue_fit}/10. Change fit {change_fit}/10. "
        f"Guidelines {guidelines}/10. Evidence {evidence}/10. Clarity {clarity}/10. "
        f"Merge estimate {estimate.value} ({estimate.band})."
    )
    if not snapshot.issue_number:
        summary += " No linked issue."
    if checklist.source == "default":
        summary += " No contributing guide was found."
    if snapshot.files_skipped:
        summary += f" {len(snapshot.files_skipped)} file diffs were not reviewed."
    suggested = None
    if config.suggestScores:
        suggested = SuggestedScores(
            clarity=clarity,
            scope=change_fit,
            evidence=evidence,
            outcome=_clamp(round(estimate.value / 10)),
        )
    return ReviewResponse(
        summary=_short(summary, 2000),
        insights=insights[:12],
        suggestedScores=suggested,
        category=category,  # type: ignore[arg-type]
        categoryLabel=label,
        mergeEstimate=estimate,
        report=report,
        analysisMode="gemini",
    )


def _insights(
    pull_request: PullRequestSubject,
    snapshot: Snapshot,
    config: AnalysisConfig,
    checklist: GuidelineChecklist,
    findings: JudgeFindings,
    label: str,
    estimate: MergeEstimate,
    issue_fit: int,
    change_fit: int,
    failing: list[str],
    steps: list[str],
) -> list[Insight]:
    insights: list[Insight] = []
    aspects = config.aspects
    if aspects.mergeReadiness:
        insights.append(
            _insight(
                "outcome",
                f"{label}",
                f"Merge estimate {estimate.value} ({estimate.band}). "
                + ("; ".join(factor.detail for factor in estimate.factors) or "No extra merge factors")
                + ".",
            )
        )
    if aspects.quality:
        open_items = [item for item in findings.criteria if item.status in {"missing", "partial"}]
        if not snapshot.issue_number:
            detail = "The description does not use Fixes, Closes, or Resolves with an issue number."
        elif open_items:
            shown = "; ".join(_short(item.text, 140) for item in open_items[:4])
            detail = f"Issue fit {issue_fit}/10. {len(open_items)} criteria are not fully covered: {shown}."
        else:
            detail = f"Issue fit {issue_fit}/10. The reviewed criteria are covered."
        insights.append(_insight("clarity", f"Issue fit {issue_fit} / 10", detail))
        unrelated = [item.path for item in findings.files if item.relation == "unrelated"]
        missing = [name for name in findings.files if name.relation == "required"]
        insights.append(
            _insight(
                "scope",
                f"Change fit {change_fit} / 10",
                "Unrelated files: "
                + (", ".join(unrelated) if unrelated else "none")
                + ". Required files: "
                + (", ".join(item.path for item in missing) if missing else "none")
                + ".",
            )
        )
    if checklist.source == "default":
        insights.append(
            _insight(
                "general",
                "No contributing guide",
                "This repository has no CONTRIBUTING.md. The review used the default checklist.",
            )
        )
    failed_rules = [item.rule for item in findings.guidelines if item.status == "fail"]
    if failed_rules:
        insights.append(
            _insight(
                "general",
                "Guideline gaps",
                "; ".join(_short(rule, 160) for rule in failed_rules[:4]),
            )
        )
    if aspects.checks:
        if not snapshot.fetched:
            insights.append(
                _insight(
                    "evidence",
                    "Checks were not loaded",
                    "GitHub checks could not be read. Set GITHUB_TOKEN on the analysis service to report CI.",
                )
            )
        elif not snapshot.checks:
            insights.append(
                _insight(
                    "evidence",
                    "No checks reported",
                    "This repository did not report commit checks or statuses. That is not the same as a failed test.",
                )
            )
        elif failing:
            for name in failing[:4]:
                insights.append(_insight("evidence", f"{name} failed", f"{name} is failing."))
        else:
            insights.append(_insight("evidence", "Checks passed", "The reported checks and statuses succeeded."))
    if aspects.coaching:
        for index, step in enumerate(steps, start=1):
            insights.append(_insight("general", f"Next step {index}", step))
    if (pull_request.additions or 0) > 800 and len(insights) < 12:
        insights.append(_insight("scope", "Large change", "The pull request adds more than 800 lines."))
    return insights
