import asyncio
import base64
import json

import httpx
from fastapi.testclient import TestClient

from app.ai.findings import (
    CriterionFinding,
    FileVerdict,
    GuidelineCheck,
    GuidelineChecklist,
    GuidelineItem,
    IssueExtraction,
    JudgeFindings,
    ReviewNote,
    default_checklist,
)
from app.ai.parse import parse_model_response
from app.ai.pipeline import analyze, path_is_allowed
from app.ai.protocol import ModelTurn, ToolCall
from app.comment import MARKER, render_comment
from app.github_client import (
    PATCH_FILE_CAP,
    CheckResult,
    FilePatch,
    GitHubClient,
    Snapshot,
    select_patches,
)
from app.main import app, build_model, github_client
from app.schemas import AnalysisConfig, PullRequestSubject, ReviewRequest
from app.scoring import category_for, score_review
from app.store import MemoryGuidelineCache, document_id, review_payload
from app.webhook import signature_header, verify_signature
from tests.test_review import subject


class ScriptedModel:
    def __init__(self, turns: list[ModelTurn]):
        self.turns = list(turns)
        self.calls: list[dict] = []

    async def complete(self, **kwargs):
        self.calls.append(kwargs)
        if not self.turns:
            raise AssertionError(kwargs.get("purpose"))
        return self.turns.pop(0)


def _pull(**overrides) -> PullRequestSubject:
    return subject(**overrides)


def _snapshot(**overrides) -> Snapshot:
    payload = dict(
        fetched=True,
        issue_number=None,
        issue_title="",
        issue_body="",
        files=(),
        mergeable=True,
        checks=(),
    )
    payload.update(overrides)
    return Snapshot(**payload)


def _findings(**overrides) -> JudgeFindings:
    payload = {"criteria": [], "files": [], "guidelines": [], "notes": []}
    payload.update(overrides)
    return JudgeFindings.model_validate(payload)


def _score(snapshot: Snapshot, findings: JudgeFindings, **pull_overrides):
    return score_review(
        _pull(**pull_overrides),
        snapshot,
        AnalysisConfig(),
        default_checklist(),
        IssueExtraction(),
        findings,
    )


def test_category_bands_and_caps():
    assert category_for(4.99, linked=True, failing_check=False) == ("bad", "Bad PR")
    assert category_for(5, linked=True, failing_check=False) == ("good", "Good PR")
    assert category_for(7.99, linked=True, failing_check=False) == ("good", "Good PR")
    assert category_for(8, linked=True, failing_check=False) == ("very_good", "Very Good PR")
    assert category_for(9.4, linked=False, failing_check=False) == ("good", "Good PR")
    assert category_for(9.4, linked=True, failing_check=True) == ("good", "Good PR")


def test_scores_follow_the_formula_and_a_failing_check_blocks_very_good():
    covered = _snapshot(
        issue_number=14,
        issue_title="Encode NEL",
        issue_body="Update the encoder.",
        files=("src/Encoder.java",),
        patches=(FilePatch("src/Encoder.java", "modified", 4, 1, "encodes NEL", False),),
        checks=(CheckResult("unit-tests", "success"),),
    )
    strong = _findings(
        criteria=[CriterionFinding(text="encode NEL", status="met", evidence="encodes NEL")],
        files=[FileVerdict(path="src/Encoder.java", relation="required", reason="named by the issue")],
        guidelines=[GuidelineCheck(rule="Add tests", status="pass")],
    )
    review = _score(covered, strong, title="Encode NEL", body="x" * 80)
    assert review.report is not None
    assert review.report.issueFit == 10
    assert review.report.changeFit == 9
    assert review.report.guidelines == 6
    assert review.report.evidence == 9
    assert review.category == "very_good"
    assert review.suggestedScores is not None
    assert review.suggestedScores.scope == review.report.changeFit
    assert review.suggestedScores.outcome == int(round(review.mergeEstimate.value / 10))
    assert review.mergeEstimate.value == 90
    assert review.mergeEstimate.band == "high"

    failing = _snapshot(
        issue_number=14,
        issue_title="Encode NEL",
        issue_body="Update the encoder.",
        files=("src/Encoder.java",),
        patches=(FilePatch("src/Encoder.java", "modified", 4, 1, "encodes NEL", False),),
        checks=(CheckResult("unit-tests", "failure"),),
    )
    blocked = _score(failing, strong, title="Encode NEL", body="x" * 80)
    assert blocked.report.evidence == 6
    assert blocked.report.overall >= 8
    assert blocked.category == "good"
    assert blocked.mergeEstimate.value == 45
    assert any(factor.code == "checks_failing" for factor in blocked.mergeEstimate.factors)


def test_issue_change_and_guideline_edges():
    loaded = _snapshot(issue_number=3, issue_title="Title", issue_body="Body", files=("a.py",), patches=(FilePatch("a.py", "modified", 1, 0, "alpha", False),))
    mixed = _findings(
        criteria=[
            CriterionFinding(text="one", status="met"),
            CriterionFinding(text="two", status="partial"),
            CriterionFinding(text="three", status="missing"),
            CriterionFinding(text="four", status="unknown"),
        ]
    )
    assert _score(loaded, mixed).report.issueFit == 5
    assert _score(_snapshot(), _findings(criteria=[CriterionFinding(text="invented", status="met")])).report.issueFit == 2
    assert _score(loaded, _findings()).report.issueFit == 5

    named = IssueExtraction(namedFiles=["missing.java"])
    verdicts = _findings(
        files=[
            FileVerdict(path="a.py", relation="required"),
            FileVerdict(path="b.py", relation="unrelated"),
            FileVerdict(path="ghost.py", relation="unrelated"),
        ]
    )
    review = score_review(
        _pull(),
        _snapshot(files=("a.py", "b.py"), patches=(
            FilePatch("a.py", "modified", 1, 0, "alpha", False),
            FilePatch("b.py", "modified", 1, 0, "beta", False),
        )),
        AnalysisConfig(),
        GuidelineChecklist(items=[GuidelineItem(rule="Add tests")], source="repository"),
        named,
        verdicts,
    )
    assert review.report.changeFit == 5
    assert all(item.path != "ghost.py" for item in review.report.files)
    failed = score_review(
        _pull(),
        _snapshot(),
        AnalysisConfig(),
        default_checklist(),
        IssueExtraction(),
        _findings(guidelines=[
            GuidelineCheck(rule="Add tests", status="pass"),
            GuidelineCheck(rule="Stay focused", status="fail"),
        ]),
    )
    assert failed.report.guidelines == 5
    unknown = score_review(
        _pull(),
        _snapshot(),
        AnalysisConfig(),
        GuidelineChecklist(items=[GuidelineItem(rule="Add tests")], source="repository"),
        IssueExtraction(),
        _findings(guidelines=[GuidelineCheck(rule="Add tests", status="unknown")]),
    )
    assert unknown.report.guidelines == 5


def test_merge_estimate_clamps_and_template_adjusts_clarity():
    low = _score(
        _snapshot(issue_number=1, issue_title="T", issue_body="B", mergeable=False, checks=(CheckResult("tests", "failure"),)),
        _findings(),
        draft=True,
    )
    assert low.mergeEstimate.value == 5
    assert low.mergeEstimate.band == "low"
    plain = _score(_snapshot(), _findings())
    assert plain.mergeEstimate.value == 35
    assert plain.mergeEstimate.band == "low"
    template = _snapshot(pull_request_template="## Summary\n")
    present = _score(template, _findings(), body="## Summary\n" + "x" * 20)
    absent = _score(template, _findings(), body="y" * 20)
    assert present.report.clarity == 6
    assert absent.report.clarity == 2


def test_notes_must_quote_the_patch_and_are_absent_from_firestore():
    secret = "ghp_abcdefghij1234567890"
    snapshot = _snapshot(
        files=("src/A.java",),
        patches=(FilePatch("src/A.java", "modified", 1, 0, "UNIQUE_PATCH_TEXT [redacted]", False),),
    )
    review = _score(
        snapshot,
        _findings(notes=[
            ReviewNote(path="src/A.java", statement="The hunk says `UNIQUE_PATCH_TEXT`.", confidence="high", severity="medium"),
            ReviewNote(path="src/A.java", statement=f"Token `{secret}` was committed.", confidence="high", severity="high"),
            ReviewNote(path="other.java", statement="The hunk says `UNIQUE_PATCH_TEXT`.", confidence="high", severity="low"),
        ]),
    )
    assert len(review.report.notes) == 1
    assert "UNIQUE_PATCH_TEXT" in review.report.notes[0].statement
    assert secret not in review.model_dump_json()
    payload = review_payload(
        ReviewRequest(pullRequest=_pull()),
        review,
        head_sha="abc",
        reviewed_files=["src/A.java"],
        skipped_files=[],
        truncated_files=[],
        models={"extract": "gemini-2.5-flash", "judge": "gemini-2.5-pro"},
    )
    stored = json.dumps(payload)
    assert "UNIQUE_PATCH_TEXT" not in stored
    assert secret not in stored
    assert "patch" not in payload
    assert payload["reviewedFiles"] == ["src/A.java"]
    assert payload["rubricVersion"] == "2"
    assert payload["metrics"]["issueFit"] == review.report.issueFit
    comment = render_comment(review)
    assert comment.startswith(MARKER)
    assert "UNIQUE_PATCH_TEXT" in comment
    assert secret not in comment
    assert len(review.insights) <= 12


def test_select_patches_redacts_truncates_and_skips():
    secret = "ghp_abcdefghij1234567890"
    kept, skipped = select_patches(
        [
            {"filename": "a.py", "patch": secret + "\n" + ("a" * 9000), "additions": 1, "deletions": 0},
            {"filename": "b.py", "patch": "b" * 100},
        ]
    )
    assert secret not in kept[0].patch
    assert kept[0].patch.startswith("[redacted]")
    assert kept[0].truncated is True
    assert len(kept[0].patch) == PATCH_FILE_CAP
    assert skipped == ()

    packed = [{"filename": f"f{index}.py", "patch": "x" * PATCH_FILE_CAP} for index in range(8)]
    packed.append({"filename": "later.py", "patch": "more"})
    huge, skipped_files = select_patches(packed)
    assert len(huge) == 8
    assert huge[-1].truncated is True
    assert skipped_files == ("later.py",)


def test_document_id_includes_head_sha_not_the_description():
    request = ReviewRequest(pullRequest=subject(body="ghp_abcdefghij1234567890"))
    assert document_id(request, "abc") != document_id(request, "def")
    assert "ghp_" not in document_id(request, "ghp_abcdefghij1234567890")


def test_path_is_allowed_only_for_changed_or_imported_files():
    snapshot = _snapshot(
        files=("src/A.java",),
        patches=(FilePatch("src/A.java", "modified", 1, 0, 'import helper.B;', False),),
    )
    assert path_is_allowed("src/A.java", snapshot) is True
    assert path_is_allowed("helper.B", snapshot) is True
    assert path_is_allowed("../secrets.env", snapshot) is False
    assert path_is_allowed("src/Missing.java", snapshot) is False


def test_github_client_reads_guide_template_and_redacts_patches():
    guide = base64.b64encode(b"# Contributing\n\nTests are required. ghp_abcdefghij1234567890\n").decode()
    template = base64.b64encode(b"## Summary\n").decode()

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/pulls/153"):
            return httpx.Response(
                200,
                json={
                    "title": "Fix XML encoding",
                    "body": "Fixes #14.",
                    "mergeable": True,
                    "head": {"sha": "abc"},
                    "base": {"sha": "base123"},
                },
            )
        if path.endswith("/files"):
            return httpx.Response(200, json=[{"filename": "src/A.java", "patch": "ghp_abcdefghij1234567890"}])
        if path.endswith("/issues/14"):
            return httpx.Response(200, json={"title": "XML", "body": "A.java", "state": "open", "html_url": "https://github.com/owasp/owasp-java-encoder/issues/14"})
        if path.endswith("/check-runs"):
            return httpx.Response(200, json={"check_runs": []})
        if path.endswith("/status"):
            return httpx.Response(200, json={"statuses": []})
        if path.endswith("/.github/CONTRIBUTING.md"):
            return httpx.Response(200, json={"encoding": "base64", "content": guide, "sha": "blob1"})
        if path.endswith("/.github/pull_request_template.md"):
            return httpx.Response(200, json={"encoding": "base64", "content": template, "sha": "blob2"})
        return httpx.Response(404)

    client = GitHubClient("github-token", transport=httpx.MockTransport(handler))
    snapshot = asyncio.run(client.load(subject(body="")))
    assert snapshot.base_sha == "base123"
    assert "Tests are required" in snapshot.contributing_guide
    assert "ghp_" not in snapshot.contributing_guide
    assert snapshot.guide_sha == "blob1"
    assert "Summary" in snapshot.pull_request_template
    assert snapshot.patches[0].patch == "[redacted]"


def test_parse_model_response_keeps_tool_calls_when_text_raises():
    class Call:
        name = "read_file"
        args = {"path": "src/A.java"}

    class Response:
        function_calls = [Call()]
        parsed = None

        @property
        def text(self):
            raise ValueError("function call only")

    turn = parse_model_response(Response())
    assert turn.tool_calls[0].path == "src/A.java"
    assert turn.data is None

    class JsonResponse:
        function_calls = []
        parsed = None
        text = '{"files": []}'

    assert parse_model_response(JsonResponse()).data == {"files": []}


def test_pipeline_retries_invalid_guidelines_then_caches_them():
    cache = MemoryGuidelineCache()
    model = ScriptedModel(
        [
            ModelTurn(data={"items": []}),
            ModelTurn(data={"items": [{"rule": "Add tests"}]}),
            ModelTurn(data={"problem": "NEL", "criteria": ["encode NEL"], "namedFiles": ["A.java"], "outOfScope": []}),
            ModelTurn(data={"files": [{"path": "src/A.java", "summary": "encodes NEL"}]}),
            ModelTurn(data={"criteria": [{"text": "encode NEL", "status": "met"}], "files": [], "guidelines": [], "notes": []}),
        ]
    )
    snapshot = _snapshot(
        issue_number=14,
        issue_title="Encode NEL",
        issue_body="Fix A.java",
        files=("src/A.java",),
        patches=(FilePatch("src/A.java", "modified", 1, 0, "encodes NEL", False),),
        contributing_guide="Tests are required.",
        guide_sha="blob1",
    )

    async def read_file(path: str) -> str:
        raise AssertionError(path)

    first = asyncio.run(analyze(_pull(), snapshot, AnalysisConfig(), model, read_file, cache))
    assert first.analysisMode == "gemini"
    assert model.calls[1]["user"].count("failed validation") == 1
    assert cache.get("owasp/owasp-java-encoder", "blob1")

    again = ScriptedModel(
        [
            ModelTurn(data={"problem": "NEL", "criteria": ["encode NEL"], "namedFiles": [], "outOfScope": []}),
            ModelTurn(data={"files": [{"path": "src/A.java", "summary": "encodes NEL"}]}),
            ModelTurn(data={"criteria": [], "files": [], "guidelines": [], "notes": []}),
        ]
    )
    asyncio.run(analyze(_pull(), snapshot, AnalysisConfig(), again, read_file, cache))
    assert [call["purpose"] for call in again.calls] == ["issue", "diff", "judge"]


def test_diff_step_stops_after_three_file_reads():
    files = tuple(f"src/{index}.py" for index in range(4))
    reads: list[str] = []

    async def read_file(path: str) -> str:
        reads.append(path)
        return "contents"

    model = ScriptedModel(
        [
            ModelTurn(tool_calls=tuple(ToolCall("read_file", path) for path in files)),
            ModelTurn(data={"files": [{"path": files[0], "summary": "touched"}]}),
            ModelTurn(data={"criteria": [], "files": [], "guidelines": [], "notes": []}),
        ]
    )
    snapshot = _snapshot(
        files=files,
        patches=tuple(FilePatch(path, "modified", 1, 0, "line", False) for path in files),
    )
    asyncio.run(analyze(_pull(), snapshot, AnalysisConfig(), model, read_file, MemoryGuidelineCache()))
    assert reads == list(files[:3])
    assert "read budget exhausted" in model.calls[1]["user"]
    assert model.calls[0]["enable_read_file"] is True
    assert model.calls[1]["enable_read_file"] is False


def test_invalid_gemini_json_falls_back_to_the_rubric(monkeypatch):
    monkeypatch.setenv("ANALYSIS_MODE", "gemini")
    monkeypatch.setenv("ANALYSIS_TOKEN", "shared-secret")
    monkeypatch.setenv("FIRESTORE_ENABLED", "false")

    class BadModel:
        async def complete(self, **kwargs):
            raise ValueError("invalid json")

    class Stub(GitHubClient):
        async def load(self, pull_request):
            return Snapshot(True, 14, "XML", "XmlEncoder.java", ("src/XmlEncoder.java",), True, ())

    app.dependency_overrides[github_client] = lambda: Stub("token")
    app.dependency_overrides[build_model] = lambda: BadModel()
    try:
        response = TestClient(app).post(
            "/review",
            headers={"Authorization": "Bearer shared-secret"},
            json={"pullRequest": subject().model_dump()},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["analysisMode"] == "rubric"
        assert "suggestedScores" in body
        assert body["suggestedScores"]["outcome"] <= 10
    finally:
        app.dependency_overrides.clear()


def _webhook_body(action: str = "opened") -> dict:
    return {
        "action": action,
        "repository": {"full_name": "octo/hello"},
        "pull_request": {
            "html_url": "https://github.com/octo/hello/pull/3",
            "number": 3,
            "title": "Fix encoding",
            "body": "Fixes #3 in the encoder.",
            "state": "open",
            "merged": False,
            "draft": False,
            "user": {"login": "contributor"},
            "additions": 10,
            "deletions": 1,
            "changed_files": 1,
            "commits": 1,
        },
    }


def test_webhook_rejects_a_bad_signature_and_updates_one_comment(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "hook-secret")
    monkeypatch.setenv("ANALYSIS_MODE", "rubric")
    monkeypatch.setenv("FIRESTORE_ENABLED", "false")
    seen = {"loaded": 0, "comments": []}

    class Stub(GitHubClient):
        async def load(self, pull_request):
            seen["loaded"] += 1
            return Snapshot(True, None, "", "", (), True, ())

        async def upsert_comment(self, repository, number, body):
            seen["comments"].append((repository, number, body))

    app.dependency_overrides[github_client] = lambda: Stub("token")
    try:
        client = TestClient(app)
        raw = json.dumps(_webhook_body()).encode()
        denied = client.post(
            "/github/webhook",
            content=raw,
            headers={
                "X-GitHub-Event": "pull_request",
                "X-Hub-Signature-256": signature_header("wrong", raw),
                "Content-Type": "application/json",
            },
        )
        assert denied.status_code == 401
        assert seen["loaded"] == 0

        accepted = client.post(
            "/github/webhook",
            content=raw,
            headers={
                "X-GitHub-Event": "pull_request",
                "X-Hub-Signature-256": signature_header("hook-secret", raw),
                "Content-Type": "application/json",
            },
        )
        assert accepted.status_code == 202
        assert seen["comments"][0][0] == "octo/hello"
        assert seen["comments"][0][1] == 3
        assert MARKER in seen["comments"][0][2]
        closed = json.dumps(_webhook_body("closed")).encode()
        ignored = client.post(
            "/github/webhook",
            content=closed,
            headers={
                "X-GitHub-Event": "pull_request",
                "X-Hub-Signature-256": signature_header("hook-secret", closed),
                "Content-Type": "application/json",
            },
        )
        assert ignored.status_code == 204
        assert seen["loaded"] == 1
    finally:
        app.dependency_overrides.clear()

    assert verify_signature("hook-secret", raw, signature_header("hook-secret", raw)) is True
    assert verify_signature("hook-secret", raw, "sha256=abc") is False


def test_upsert_comment_patches_the_existing_marker():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=[{"id": 9, "body": "<!-- pr-analyzer -->\nold"}])
        if request.method == "PATCH":
            seen["url"] = str(request.url)
            seen["body"] = json.loads(request.content.decode())
            return httpx.Response(200, json={"id": 9})
        return httpx.Response(404)

    client = GitHubClient("github-token", transport=httpx.MockTransport(handler))
    asyncio.run(client.upsert_comment("octo/hello", 3, "<!-- pr-analyzer -->\nGood PR"))
    assert seen["url"].endswith("/issues/comments/9")
    assert seen["body"]["body"].startswith(MARKER)
