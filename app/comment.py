import re

from app.schemas import ReviewResponse

MARKER = "<!-- pr-analyzer -->"


def render_comment(review: ReviewResponse) -> str:
    parts = [MARKER]
    report = review.report
    if review.categoryLabel:
        parts.append(f"**{review.categoryLabel}**")
    if report is not None:
        parts.append(
            f"Issue fit {report.issueFit}/10. Change fit {report.changeFit}/10. "
            f"Guidelines {report.guidelines}/10. Evidence {report.evidence}/10. "
            f"Clarity {report.clarity}/10."
        )
    if review.mergeEstimate is not None:
        factors = "; ".join(factor.detail for factor in review.mergeEstimate.factors) or "No extra merge factors"
        parts.append(
            f"Merge estimate {review.mergeEstimate.value} ({review.mergeEstimate.band}). {factors}."
        )
    if report is not None:
        gaps = [item for item in report.criteria if item.status in {"missing", "partial"}]
        if gaps:
            lines = "\n".join(f"- {item.text} ({item.status})" for item in gaps)
            parts.append(f"Issue criteria still open:\n{lines}")
        unrelated = [item.path for item in report.files if item.relation == "unrelated"]
        if unrelated:
            parts.append("Unrelated files:\n" + "\n".join(f"- {path}" for path in unrelated))
        if report.failingChecks:
            parts.append("Failing checks:\n" + "\n".join(f"- {name}" for name in report.failingChecks))
        if report.nextSteps:
            steps = "\n".join(f"{index}. {step}" for index, step in enumerate(report.nextSteps, start=1))
            parts.append(f"Next steps:\n{steps}")
        notes = _comment_notes(review)
        if notes:
            parts.append("Review notes:\n" + "\n".join(f"- {line}" for line in notes))
    elif review.summary:
        parts.append(review.summary)
    return "\n\n".join(parts)


def _comment_notes(review: ReviewResponse) -> list[str]:
    report = review.report
    if report is None:
        return []
    lines = []
    for note in report.notes:
        if note.confidence != "high" or not note.path:
            continue
        statement = re.sub(r"\s+", " ", note.statement).strip()
        if not statement:
            continue
        lines.append(f"{note.path}: {statement}")
    return lines[:3]
