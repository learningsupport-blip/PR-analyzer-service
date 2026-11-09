import httpx
from fastapi.testclient import TestClient

from app.github_client import CheckResult, GitHubClient, Snapshot, linked_issue_number
from app.main import app, github_client
from app.rubric import build_review
from app.schemas import AnalysisAspects, AnalysisConfig, PullRequestSubject, ReviewRequest
from app.store import document_id


def subject(**overrides) -> PullRequestSubject:
    payload = {
        "url": "https://github.com/owasp/owasp-java-encoder/pull/153",
        "repository": "owasp/owasp-java-encoder",
        "number": 153,
        "title": "Fix XML encoding",
        "body": "Fixes #14 by encoding NEL characters in the XML writer.",
        "state": "open",
        "merged": False,
        "draft": False,
        "authorLogin": "contributor",
        "additions": 12,
        "deletions": 3,
        "changedFiles": 1,
        "commits": 1,
    }
    payload.update(overrides)
    return PullRequestSubject(**payload)


REAL_DESCRIPTION = """HTML unquoted attributes previously encoded NEL.

- Integrate current main and the XML 1.1 test discovery fix from #160.

Fixes #136.
"""


def test_linked_issue_number_reads_a_closing_reference():
    assert linked_issue_number("owasp/owasp-java-encoder", "Fixes #14 in the writer.") == 14
    assert linked_issue_number("owasp/owasp-java-encoder", "See the discussion.") is None
    assert linked_issue_number("owasp/owasp-java-encoder", REAL_DESCRIPTION) == 136
    assert linked_issue_number("owasp/owasp-java-encoder", "Fixes: #136") == 136
    assert linked_issue_number(
        "owasp/owasp-java-encoder",
        "Fixes [#136](https://github.com/OWASP/owasp-java-encoder/issues/136).",
    ) == 136
    assert linked_issue_number(
        "OWASP/owasp-java-encoder",
        "Resolves https://github.com/OWASP/owasp-java-encoder/issues/136.",
    ) == 136
    assert linked_issue_number("owasp/owasp-java-encoder", "The fix from #160 is separate.") is None


def test_quality_and_checks_follow_the_app_contract():
    review = build_review(
        subject(),
        Snapshot(
            fetched=True,
            issue_number=14,
            issue_title="XML encoding drops NEL",
            issue_body="Update XmlEncoder.java",
            files=("src/XmlEncoder.java",),
            mergeable=True,
            checks=(CheckResult("unit-tests", "failure"),),
        ),
        AnalysisConfig(),
    )
    payload = review.model_dump()
    assert 1 <= len(payload["summary"]) <= 2000
    assert len(payload["insights"]) <= 12
    assert any(item["detail"].startswith("Tests must pass") for item in payload["insights"])
    assert payload["suggestedScores"]["outcome"] < 10
    for score in payload["suggestedScores"].values():
        assert isinstance(score, int) and 0 <= score <= 10


def test_linked_issue_is_quoted_instead_of_reported_missing():
    review = build_review(
        subject(body=REAL_DESCRIPTION, title="Fix XML 1.1 and HTML NEL encoding", merged=True, state="closed"),
        Snapshot(
            fetched=True,
            issue_number=136,
            issue_title="Fix NEL in forHtmlUnquotedAttribute",
            issue_body=(
                "## Problem\n\n"
                "HTMLEncoder.java encodes U+0085 as &#133;, which HTML parsers treat as an ellipsis. "
                "XML 1.1 leaves NEL raw.\n\n"
                "## Acceptance criteria\n\n"
                "- [ ] forHtmlUnquotedAttribute treats U+0085 like the other C1 controls.\n"
                "- [ ] XML 1.1 emits &#x85; and &#x2028;.\n"
            ),
            files=("core/src/main/java/org/owasp/encoder/HTMLEncoder.java",),
            mergeable=True,
            checks=(),
            issue_state="closed",
            issue_url="https://github.com/OWASP/owasp-java-encoder/issues/136",
        ),
        AnalysisConfig(),
    )
    clarity = next(item for item in review.insights if item.title.startswith("Linked issue"))
    assert "No linked issue" not in clarity.detail
    assert "https://github.com/OWASP/owasp-java-encoder/issues/136" in clarity.detail
    assert "Fix NEL in forHtmlUnquotedAttribute" in clarity.detail
    assert "ellipsis" in clarity.detail
    assert "HTMLEncoder.java" in clarity.detail
    assert "forHtmlUnquotedAttribute treats U+0085" in clarity.detail
    assert "Linked issue #136" in review.summary


def test_disabled_checks_are_omitted_and_scores_can_be_hidden():
    review = build_review(
        subject(merged=True, state="closed"),
        Snapshot(True, None, "", "", (), True, (CheckResult("unit-tests", "failure"),)),
        AnalysisConfig(aspects=AnalysisAspects(checks=False, coaching=False), suggestScores=False),
    )
    assert review.suggestedScores is None
    assert review.model_dump(exclude_none=True).get("suggestedScores") is None
    assert all(item.metric != "evidence" for item in review.insights)
    assert "already merged" in review.summary.lower() or any("already merged" in item.detail.lower() for item in review.insights)


def test_document_id_does_not_include_the_description():
    request = ReviewRequest(pullRequest=subject(body="ghp_abcdefghij1234567890"))
    assert "ghp_" not in document_id(request)
    assert document_id(request) == document_id(ReviewRequest(pullRequest=subject(body="different description")))


def test_github_client_reads_files_issue_and_checks():
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/pulls/153"):
            return httpx.Response(
                200,
                json={
                    "title": "Fix XML encoding",
                    "body": "Mention #160 in passing.\n\nFixes #14.",
                    "mergeable": True,
                    "head": {"sha": "abc"},
                },
            )
        if path.endswith("/files"):
            return httpx.Response(200, json=[{"filename": "src/XmlEncoder.java", "patch": "secret diff"}])
        if path.endswith("/issues/14"):
            return httpx.Response(
                200,
                json={
                    "title": "XML encoding",
                    "body": "XmlEncoder.java",
                    "state": "open",
                    "html_url": "https://github.com/owasp/owasp-java-encoder/issues/14",
                },
            )
        if path.endswith("/check-runs"):
            return httpx.Response(200, json={"check_runs": [{"name": "unit-tests", "conclusion": "success"}]})
        if path.endswith("/status"):
            return httpx.Response(200, json={"statuses": []})
        return httpx.Response(404)

    client = GitHubClient("github-token", transport=httpx.MockTransport(handler))
    import asyncio

    snapshot = asyncio.run(client.load(subject(body="")))
    assert snapshot.fetched is True
    assert snapshot.files == ("src/XmlEncoder.java",)
    assert snapshot.issue_number == 14
    assert snapshot.issue_state == "open"
    assert snapshot.issue_url.endswith("/issues/14")
    assert snapshot.checks[0].name == "unit-tests"
    assert snapshot.head_sha == "abc"
    assert snapshot.patches[0].filename == "src/XmlEncoder.java"
    assert snapshot.patches[0].patch == "secret diff"
    assert snapshot.files_skipped == ()


def test_review_endpoint_requires_the_shared_bearer_token(monkeypatch):
    monkeypatch.setenv("ANALYSIS_TOKEN", "shared-secret")
    monkeypatch.setenv("ANALYSIS_MODE", "rubric")
    monkeypatch.setenv("FIRESTORE_ENABLED", "false")

    class StubGitHub(GitHubClient):
        async def load(self, pull_request):
            return Snapshot(False, None, "", "", (), None, ())

    app.dependency_overrides[github_client] = lambda: StubGitHub("")
    try:
        client = TestClient(app)
        denied = client.post("/review", json={"pullRequest": subject().model_dump()})
        assert denied.status_code == 401
        accepted = client.post(
            "/review",
            headers={"Authorization": "Bearer shared-secret"},
            json={"pullRequest": subject().model_dump()},
        )
        assert accepted.status_code == 200
        body = accepted.json()
        assert body["summary"]
        assert "suggestedScores" in body
        assert "shared-secret" not in accepted.text
    finally:
        app.dependency_overrides.clear()


def test_health_stays_open():
    client = TestClient(app)
    assert client.get("/health").json() == {"status": "ok"}
