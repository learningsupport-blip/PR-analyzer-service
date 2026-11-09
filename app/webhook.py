import hashlib
import hmac

from pydantic import ValidationError

from app.schemas import PullRequestSubject


def signature_header(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    if not secret or not header:
        return False
    expected = signature_header(secret, body)
    provided = header.strip()
    if len(provided) != len(expected):
        return False
    return hmac.compare_digest(expected, provided)


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) else None


def subject_from_webhook(payload: dict) -> PullRequestSubject | None:
    pull_request = payload.get("pull_request")
    repository = payload.get("repository") if isinstance(payload.get("repository"), dict) else {}
    full_name = str(repository.get("full_name") or "")
    if not isinstance(pull_request, dict) or not full_name:
        return None
    user = pull_request.get("user") if isinstance(pull_request.get("user"), dict) else {}
    try:
        return PullRequestSubject(
            url=str(pull_request.get("html_url") or ""),
            repository=full_name,
            number=int(pull_request["number"]),
            title=str(pull_request.get("title") or ""),
            body=str(pull_request.get("body") or ""),
            state=str(pull_request.get("state") or ""),
            merged=bool(pull_request.get("merged")),
            draft=bool(pull_request.get("draft")),
            authorLogin=str(user.get("login") or ""),
            additions=_optional_int(pull_request.get("additions")),
            deletions=_optional_int(pull_request.get("deletions")),
            changedFiles=_optional_int(pull_request.get("changed_files")),
            commits=_optional_int(pull_request.get("commits")),
        )
    except (KeyError, TypeError, ValueError, ValidationError):
        return None
