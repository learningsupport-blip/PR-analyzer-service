import json
import re
from collections.abc import Awaitable, Callable

from pydantic import BaseModel, ValidationError

from app.ai.findings import (
    DiffExtraction,
    DiffFileNote,
    GuidelineChecklist,
    GuidelineDraft,
    GuidelineItem,
    IssueDraft,
    IssueExtraction,
    JudgeFindings,
    default_checklist,
)
from app.ai.prompts import SYSTEM, diff_prompt, guideline_prompt, issue_prompt, judge_prompt
from app.ai.protocol import ChatModel, ModelTurn
from app.github_client import Snapshot
from app.rubric import redact
from app.schemas import AnalysisConfig, PullRequestSubject, ReviewResponse
from app.scoring import score_review
from app.settings import gemini_judge_model, gemini_model
from app.store import GuidelineCache, default_guideline_cache

ReadFile = Callable[[str], Awaitable[str]]
_READ_BUDGET = 3


def path_is_allowed(path: str, snapshot: Snapshot) -> bool:
    cleaned = path.strip().replace("\\", "/").lstrip("/")
    if not cleaned or ".." in cleaned.split("/"):
        return False
    if cleaned in snapshot.files:
        return True
    haystack = "\n".join(item.patch for item in snapshot.patches)
    if cleaned in haystack:
        return True
    base = cleaned.split("/")[-1]
    return len(base) >= 4 and base in haystack


def _error_text(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        parts = []
        for err in exc.errors()[:6]:
            loc = ".".join(str(item) for item in err.get("loc", []))
            parts.append(f"{loc}: {err.get('msg', 'invalid')}")
        return "; ".join(parts) or "invalid json"
    return redact(str(exc))[:300]


def _coerce(turn: ModelTurn) -> dict:
    if isinstance(turn.data, dict):
        return turn.data
    raw = (turn.raw or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.S).strip()
    if not raw:
        raise ValueError("model returned no json")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, flags=re.S)
        if not match:
            raise ValueError("model returned no json") from None
        value = json.loads(match.group(0))
    if not isinstance(value, dict):
        raise ValueError("model json was not an object")
    return value


async def _validated(
    model: ChatModel,
    *,
    purpose: str,
    model_name: str,
    user: str,
    response_model: type[BaseModel],
    enable_read_file: bool,
) -> BaseModel:
    prompt = user
    error: Exception | None = None
    for attempt in range(2):
        turn = await model.complete(
            purpose=purpose,
            model=model_name,
            system=SYSTEM,
            user=prompt,
            response_model=response_model,
            enable_read_file=enable_read_file,
        )
        try:
            return response_model.model_validate(_coerce(turn))
        except (ValidationError, ValueError, json.JSONDecodeError) as exc:
            error = exc
            if attempt == 0:
                prompt = (
                    f"{user}\n\nThe previous response failed validation: {_error_text(exc)}. "
                    "Return only the corrected JSON object."
                )
    assert error is not None
    raise error


def _complete_diff(diff: DiffExtraction, snapshot: Snapshot) -> DiffExtraction:
    by_path = {item.path: item for item in diff.files if item.path in snapshot.files}
    notes: list[DiffFileNote] = []
    truncated = set(snapshot.truncated_names())
    skipped = set(snapshot.files_skipped)
    for name in snapshot.files:
        if name in by_path:
            notes.append(by_path[name])
        elif name in skipped:
            notes.append(DiffFileNote(path=name, summary="The patch was not sent for review."))
        elif name in truncated:
            notes.append(DiffFileNote(path=name, summary="The patch was truncated before review."))
        else:
            notes.append(DiffFileNote(path=name, summary="No description was returned for this file."))
    return DiffExtraction(files=notes)


async def _read_supplement(calls: tuple, snapshot: Snapshot, read_file: ReadFile) -> str:
    parts: list[str] = []
    used = 0
    for call in calls:
        path = str(getattr(call, "path", "") or "")
        name = str(getattr(call, "name", "") or "")
        if name != "read_file":
            parts.append(f"{path or name}: ignored")
            continue
        if used >= _READ_BUDGET:
            parts.append(f"{path}: read budget exhausted")
            continue
        if not path_is_allowed(path, snapshot):
            parts.append(f"{path}: not a changed file or a file imported by one")
            continue
        used += 1
        parts.append(f"FILE {path}\n{await read_file(path)}")
    return "\n\n".join(parts)


async def _guideline_step(
    model: ChatModel,
    pull_request: PullRequestSubject,
    snapshot: Snapshot,
    cache: GuidelineCache,
) -> GuidelineChecklist:
    guide = snapshot.contributing_guide.strip()
    if not guide:
        return default_checklist()
    if snapshot.guide_sha:
        cached = cache.get(pull_request.repository, snapshot.guide_sha)
        if cached:
            items = [GuidelineItem.model_validate(item) for item in cached if isinstance(item, dict)]
            if items:
                return GuidelineChecklist(items=items[:12], source="repository")
    draft = await _validated(
        model,
        purpose="guideline",
        model_name=gemini_model(),
        user=guideline_prompt(guide),
        response_model=GuidelineDraft,
        enable_read_file=False,
    )
    assert isinstance(draft, GuidelineDraft)
    items = [item for item in draft.items if item.rule.strip()][:12]
    if not items:
        raise ValueError("guideline checklist was empty")
    checklist = GuidelineChecklist(items=items, source="repository")
    if snapshot.guide_sha:
        cache.put(pull_request.repository, snapshot.guide_sha, [item.model_dump() for item in items])
    return checklist


async def _issue_step(model: ChatModel, snapshot: Snapshot) -> IssueExtraction:
    if not snapshot.issue_number or not (snapshot.issue_title or snapshot.issue_body):
        return IssueExtraction(missingIssue=snapshot.issue_number is None)
    draft = await _validated(
        model,
        purpose="issue",
        model_name=gemini_model(),
        user=issue_prompt(snapshot),
        response_model=IssueDraft,
        enable_read_file=False,
    )
    assert isinstance(draft, IssueDraft)
    return IssueExtraction(
        problem=draft.problem,
        criteria=[item for item in draft.criteria if item.strip()][:12],
        namedFiles=[item for item in draft.namedFiles if item.strip()][:20],
        outOfScope=[item for item in draft.outOfScope if item.strip()][:8],
        missingIssue=False,
    )


async def _diff_step(
    model: ChatModel,
    pull_request: PullRequestSubject,
    snapshot: Snapshot,
    read_file: ReadFile,
) -> DiffExtraction:
    prompt = diff_prompt(pull_request, snapshot)
    first = await model.complete(
        purpose="diff",
        model=gemini_model(),
        system=SYSTEM,
        user=prompt,
        response_model=None,
        enable_read_file=True,
    )
    if first.tool_calls:
        supplement = await _read_supplement(first.tool_calls, snapshot, read_file)
        draft = await _validated(
            model,
            purpose="diff",
            model_name=gemini_model(),
            user=f"{prompt}\n\nAdditional files:\n{supplement}\nReturn the diff JSON only.",
            response_model=DiffExtraction,
            enable_read_file=False,
        )
    else:
        try:
            draft = DiffExtraction.model_validate(_coerce(first))
        except (ValidationError, ValueError, json.JSONDecodeError):
            draft = await _validated(
                model,
                purpose="diff",
                model_name=gemini_model(),
                user=prompt,
                response_model=DiffExtraction,
                enable_read_file=False,
            )
    assert isinstance(draft, DiffExtraction)
    return _complete_diff(draft, snapshot)


async def analyze(
    pull_request: PullRequestSubject,
    snapshot: Snapshot,
    config: AnalysisConfig,
    model: ChatModel,
    read_file: ReadFile,
    cache: GuidelineCache | None = None,
) -> ReviewResponse:
    store = cache if cache is not None else default_guideline_cache()
    checklist = await _guideline_step(model, pull_request, snapshot, store)
    issue = await _issue_step(model, snapshot)
    diff = await _diff_step(model, pull_request, snapshot, read_file)
    judged = await _validated(
        model,
        purpose="judge",
        model_name=gemini_judge_model(),
        user=judge_prompt(checklist, issue, diff, snapshot.files),
        response_model=JudgeFindings,
        enable_read_file=False,
    )
    assert isinstance(judged, JudgeFindings)
    return score_review(pull_request, snapshot, config, checklist, issue, judged)
