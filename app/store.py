import hashlib
import logging

from app.schemas import ReviewRequest, ReviewResponse
from app.settings import firestore_collection, firestore_enabled, firestore_project

logger = logging.getLogger("pr-analyzer")
_GUIDELINE_COLLECTION = "prGuidelineCache"


def document_id(request: ReviewRequest, head_sha: str = "") -> str:
    pull_request = request.pullRequest
    identity = "|".join(
        [
            pull_request.repository.lower(),
            str(pull_request.number),
            pull_request.state.lower(),
            str(pull_request.merged),
            str(pull_request.draft),
            str(pull_request.additions),
            str(pull_request.deletions),
            str(pull_request.changedFiles),
            str(pull_request.commits),
            head_sha,
        ]
    )
    return hashlib.sha256(identity.encode()).hexdigest()[:40]


def review_payload(
    request: ReviewRequest,
    review: ReviewResponse,
    *,
    head_sha: str,
    reviewed_files: list[str],
    skipped_files: list[str],
    truncated_files: list[str],
    models: dict | None,
) -> dict:
    metrics = None
    if review.report is not None:
        metrics = {
            "issueFit": review.report.issueFit,
            "changeFit": review.report.changeFit,
            "guidelines": review.report.guidelines,
            "evidence": review.report.evidence,
            "clarity": review.report.clarity,
            "overall": review.report.overall,
        }
    return {
        "repository": request.pullRequest.repository,
        "number": request.pullRequest.number,
        "headSha": head_sha,
        "summary": review.summary,
        "insights": [item.model_dump() for item in review.insights],
        "suggestedScores": review.suggestedScores.model_dump() if review.suggestedScores else None,
        "category": review.category,
        "categoryLabel": review.categoryLabel,
        "mergeEstimate": review.mergeEstimate.model_dump() if review.mergeEstimate else None,
        "analysisMode": review.analysisMode,
        "metrics": metrics,
        "reviewedFiles": list(reviewed_files),
        "skippedFiles": list(skipped_files),
        "truncatedFiles": list(truncated_files),
        "models": models or {},
        "rubricVersion": "2" if review.analysisMode == "gemini" else "1",
    }


def save_review(
    request: ReviewRequest,
    review: ReviewResponse,
    *,
    head_sha: str = "",
    reviewed_files: list[str] | None = None,
    skipped_files: list[str] | None = None,
    truncated_files: list[str] | None = None,
    models: dict | None = None,
) -> None:
    if not firestore_enabled():
        return
    payload = review_payload(
        request,
        review,
        head_sha=head_sha,
        reviewed_files=reviewed_files or [],
        skipped_files=skipped_files or [],
        truncated_files=truncated_files or [],
        models=models,
    )
    try:
        from google.cloud import firestore

        client = firestore.Client(project=firestore_project() or None)
        client.collection(firestore_collection()).document(document_id(request, head_sha)).set(payload)
    except Exception:
        logger.warning("firestore_write_failed")


class GuidelineCache:
    def get(self, repository: str, blob_sha: str) -> list | None:
        raise NotImplementedError

    def put(self, repository: str, blob_sha: str, items: list[dict]) -> None:
        raise NotImplementedError


class MemoryGuidelineCache(GuidelineCache):
    def __init__(self) -> None:
        self.data: dict[tuple[str, str], list] = {}

    def get(self, repository: str, blob_sha: str) -> list | None:
        if not blob_sha:
            return None
        return self.data.get((repository.lower(), blob_sha))

    def put(self, repository: str, blob_sha: str, items: list[dict]) -> None:
        if blob_sha:
            self.data[(repository.lower(), blob_sha)] = items


class FirestoreGuidelineCache(GuidelineCache):
    def _document(self, repository: str, blob_sha: str):
        from google.cloud import firestore

        client = firestore.Client(project=firestore_project() or None)
        doc_id = hashlib.sha256(f"{repository.lower()}|{blob_sha}".encode()).hexdigest()[:40]
        return client.collection(_GUIDELINE_COLLECTION).document(doc_id)

    def get(self, repository: str, blob_sha: str) -> list | None:
        if not blob_sha:
            return None
        try:
            snapshot = self._document(repository, blob_sha).get()
            if not snapshot.exists:
                return None
            items = (snapshot.to_dict() or {}).get("items")
            if isinstance(items, list):
                return items
        except Exception:
            logger.warning("guideline_cache_read_failed")
        return None

    def put(self, repository: str, blob_sha: str, items: list[dict]) -> None:
        if not blob_sha:
            return
        try:
            self._document(repository, blob_sha).set(
                {"repository": repository.lower(), "blobSha": blob_sha, "items": items}
            )
        except Exception:
            logger.warning("guideline_cache_write_failed")


memory_guideline_cache = MemoryGuidelineCache()


def default_guideline_cache() -> GuidelineCache:
    if firestore_enabled():
        return FirestoreGuidelineCache()
    return memory_guideline_cache
