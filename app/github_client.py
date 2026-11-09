import base64
import binascii
import re
from dataclasses import dataclass

import httpx

from app.comment import MARKER
from app.schemas import PullRequestSubject

# A closing keyword must sit immediately before the reference. A bare "#160"
# in the same description is a mention, not the issue this pull request closes.
_CLOSING_REFERENCE = re.compile(
    r"\b(?:fix(?:es|ed)?|close[sd]?|resolve[sd]?)\s*:?\s*"
    r"(?:\[)?(?:#(?P<hash>\d+)"
    r"|https?://github\.com/(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)/issues/(?P<url_number>\d+)"
    r"|(?P<qowner>[A-Za-z0-9_.-]+)/(?P<qrepo>[A-Za-z0-9_.-]+)#(?P<qualified>\d+))",
    re.IGNORECASE,
)

PATCH_FILE_CAP = 8000
PATCH_TOTAL_CAP = 60000
_TEXT_CAP = 20000

_GUIDE_PATHS = (
    ".github/CONTRIBUTING.md",
    "CONTRIBUTING.md",
    "docs/CONTRIBUTING.md",
)
_TEMPLATE_PATHS = (
    ".github/pull_request_template.md",
    ".github/PULL_REQUEST_TEMPLATE.md",
)


@dataclass(frozen=True)
class CheckResult:
    name: str
    conclusion: str | None

    @property
    def failing(self) -> bool:
        return (self.conclusion or "").lower() in {"failure", "cancelled", "timed_out", "action_required", "error"}


@dataclass(frozen=True)
class FilePatch:
    filename: str
    status: str
    additions: int
    deletions: int
    patch: str
    truncated: bool


@dataclass(frozen=True)
class Snapshot:
    fetched: bool
    issue_number: int | None
    issue_title: str
    issue_body: str
    files: tuple[str, ...]
    mergeable: bool | None
    checks: tuple[CheckResult, ...]
    issue_state: str = ""
    issue_url: str = ""
    head_sha: str = ""
    base_sha: str = ""
    patches: tuple[FilePatch, ...] = ()
    files_skipped: tuple[str, ...] = ()
    contributing_guide: str = ""
    guide_sha: str = ""
    pull_request_template: str = ""

    def truncated_names(self) -> tuple[str, ...]:
        return tuple(item.filename for item in self.patches if item.truncated)


def linked_issue_number(repository: str, body: str) -> int | None:
    """Return the issue a closing keyword points at.

    GitHub treats `Fixes #136.` as a link. The period, a markdown bracket,
    and a full issue URL all stay attached to that reference in real
    descriptions, so the number is read from the digits only. A mention such
    as "the fix from #160" has no closing keyword and is left alone.
    """
    expected = repository.strip().lower()
    for match in _CLOSING_REFERENCE.finditer(body):
        if match.group("hash"):
            return int(match.group("hash"))
        owner = (match.group("owner") or match.group("qowner") or "").lower()
        repo = (match.group("repo") or match.group("qrepo") or "").lower()
        number = match.group("url_number") or match.group("qualified")
        if number and f"{owner}/{repo}" == expected:
            return int(number)
    return None


def select_patches(items: list | tuple | None) -> tuple[tuple[FilePatch, ...], tuple[str, ...]]:
    from app.rubric import redact

    kept: list[FilePatch] = []
    skipped: list[str] = []
    total = 0
    for item in items or []:
        if not isinstance(item, dict):
            continue
        filename = str(item.get("filename") or "")
        if not filename:
            continue
        raw = item.get("patch")
        text = redact(raw if isinstance(raw, str) else "")
        if total >= PATCH_TOTAL_CAP and text:
            skipped.append(filename)
            continue
        room = min(PATCH_FILE_CAP, PATCH_TOTAL_CAP - total) if text else 0
        truncated = bool(text) and len(text) > room
        patch = text[:room] if truncated else text
        total += len(patch)
        additions = item.get("additions")
        deletions = item.get("deletions")
        kept.append(
            FilePatch(
                filename=filename,
                status=str(item.get("status") or ""),
                additions=additions if isinstance(additions, int) else 0,
                deletions=deletions if isinstance(deletions, int) else 0,
                patch=patch,
                truncated=truncated,
            )
        )
    return tuple(kept), tuple(skipped)


def _decode_github_file(payload: dict) -> tuple[str, str]:
    from app.rubric import redact

    sha = str(payload.get("sha") or "")
    content = payload.get("content")
    if not isinstance(content, str) or not content.strip():
        return "", sha
    if str(payload.get("encoding") or "") == "base64":
        try:
            text = base64.b64decode(content).decode("utf-8", errors="replace")
        except (ValueError, binascii.Error):
            return "", sha
    else:
        text = content
    return redact(text)[:_TEXT_CAP], sha


class GitHubClient:
    def __init__(self, token: str, transport: httpx.AsyncBaseTransport | None = None):
        self._token = token
        self._transport = transport

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "pr-analyzer-service",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _open(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self._transport, timeout=20.0, headers=self._headers())

    async def load(self, pull_request: PullRequestSubject) -> Snapshot:
        owner_repo = pull_request.repository.split("/", 1)
        submitted = f"{pull_request.title}\n{pull_request.body}"
        issue_number = linked_issue_number(pull_request.repository, submitted)
        if len(owner_repo) != 2:
            return Snapshot(False, issue_number, "", "", (), None, ())

        owner, repo = owner_repo
        try:
            async with self._open() as client:
                pull = await self._json(client, f"https://api.github.com/repos/{owner}/{repo}/pulls/{pull_request.number}")
                if not isinstance(pull, dict):
                    return Snapshot(False, issue_number, "", "", (), None, ())
                live_body = pull.get("body")
                body = live_body if isinstance(live_body, str) and live_body.strip() else pull_request.body
                title = str(pull.get("title") or pull_request.title)
                issue_number = linked_issue_number(pull_request.repository, f"{title}\n{body}") or issue_number
                files = await self._json(
                    client,
                    f"https://api.github.com/repos/{owner}/{repo}/pulls/{pull_request.number}/files?per_page=100",
                )
                issue = None
                if issue_number is not None:
                    issue = await self._json(client, f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_number}")
                head = pull.get("head") if isinstance(pull.get("head"), dict) else {}
                base = pull.get("base") if isinstance(pull.get("base"), dict) else {}
                sha = str(head.get("sha") or "")
                base_sha = str(base.get("sha") or "")
                checks = await self._checks(client, owner, repo, sha) if sha else ()
                guide, guide_sha, template = await self._guides(client, owner, repo, base_sha)
        except httpx.HTTPError:
            return Snapshot(False, issue_number, "", "", (), None, ())

        file_items = files if isinstance(files, list) else []
        patches, skipped = select_patches(file_items)
        filenames = tuple(
            str(item.get("filename"))
            for item in file_items
            if isinstance(item, dict) and item.get("filename")
        )
        issue_data = issue if isinstance(issue, dict) else {}
        return Snapshot(
            fetched=True,
            issue_number=issue_number,
            issue_title=str(issue_data.get("title") or ""),
            issue_body=str(issue_data.get("body") or ""),
            files=filenames,
            mergeable=pull.get("mergeable") if isinstance(pull.get("mergeable"), bool) else None,
            checks=tuple(checks),
            issue_state=str(issue_data.get("state") or ""),
            issue_url=str(issue_data.get("html_url") or ""),
            head_sha=sha,
            base_sha=base_sha,
            patches=patches,
            files_skipped=skipped,
            contributing_guide=guide,
            guide_sha=guide_sha,
            pull_request_template=template,
        )

    async def read_file(self, repository: str, path: str, ref: str) -> str:
        cleaned = path.strip().replace("\\", "/").lstrip("/")
        if not cleaned or not ref or ".." in cleaned.split("/"):
            return "This file cannot be read for the review."
        owner_repo = repository.split("/", 1)
        if len(owner_repo) != 2:
            return "This file cannot be read for the review."
        owner, repo = owner_repo
        try:
            async with self._open() as client:
                text, _sha = await self._text_at(client, owner, repo, cleaned, ref)
        except httpx.HTTPError:
            return "GitHub did not return this file."
        if not text:
            return "GitHub did not return this file."
        return text[:PATCH_FILE_CAP]

    async def upsert_comment(self, repository: str, number: int, body: str) -> None:
        owner_repo = repository.split("/", 1)
        if len(owner_repo) != 2:
            raise ValueError("repository must be owner/name")
        owner, repo = owner_repo
        async with self._open() as client:
            listed = await self._json(
                client,
                f"https://api.github.com/repos/{owner}/{repo}/issues/{number}/comments?per_page=100",
            )
            match_id = None
            if isinstance(listed, list):
                for comment in listed:
                    if isinstance(comment, dict) and MARKER in str(comment.get("body") or ""):
                        match_id = comment.get("id")
                        break
            if match_id:
                response = await client.patch(
                    f"https://api.github.com/repos/{owner}/{repo}/issues/comments/{match_id}",
                    json={"body": body},
                )
            else:
                response = await client.post(
                    f"https://api.github.com/repos/{owner}/{repo}/issues/{number}/comments",
                    json={"body": body},
                )
            response.raise_for_status()

    async def _guides(
        self,
        client: httpx.AsyncClient,
        owner: str,
        repo: str,
        ref: str,
    ) -> tuple[str, str, str]:
        if not ref:
            return "", "", ""
        guide, guide_sha = await self._first_text(client, owner, repo, ref, _GUIDE_PATHS)
        template, _template_sha = await self._first_text(client, owner, repo, ref, _TEMPLATE_PATHS)
        return guide, guide_sha, template

    async def _first_text(
        self,
        client: httpx.AsyncClient,
        owner: str,
        repo: str,
        ref: str,
        paths: tuple[str, ...],
    ) -> tuple[str, str]:
        for path in paths:
            try:
                text, sha = await self._text_at(client, owner, repo, path, ref)
            except httpx.HTTPError:
                continue
            if text.strip():
                return text, sha
        return "", ""

    async def _text_at(self, client: httpx.AsyncClient, owner: str, repo: str, path: str, ref: str) -> tuple[str, str]:
        payload = await self._json(client, f"https://api.github.com/repos/{owner}/{repo}/contents/{path}?ref={ref}")
        if not isinstance(payload, dict):
            return "", ""
        return _decode_github_file(payload)

    async def _checks(self, client: httpx.AsyncClient, owner: str, repo: str, sha: str) -> list[CheckResult]:
        runs = await self._json(client, f"https://api.github.com/repos/{owner}/{repo}/commits/{sha}/check-runs?per_page=100")
        statuses = await self._json(client, f"https://api.github.com/repos/{owner}/{repo}/commits/{sha}/status")
        found: list[CheckResult] = []
        for run in (runs or {}).get("check_runs", []) if isinstance(runs, dict) else []:
            found.append(CheckResult(str(run.get("name") or "check"), run.get("conclusion")))
        for status in (statuses or {}).get("statuses", []) if isinstance(statuses, dict) else []:
            state = str(status.get("state") or "")
            conclusion = "success" if state == "success" else "failure" if state in {"failure", "error"} else state or None
            found.append(CheckResult(str(status.get("context") or "status"), conclusion))
        return found

    async def _json(self, client: httpx.AsyncClient, url: str) -> dict | list | None:
        response = await client.get(url)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        body = response.json()
        if isinstance(body, (dict, list)):
            return body
        return None
