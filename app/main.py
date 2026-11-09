import json
import logging

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, Response

from app.auth import require_bearer
from app.comment import render_comment
from app.github_client import GitHubClient
from app.review_service import build_model, perform_review
from app.schemas import ReviewRequest, ReviewResponse
from app.settings import github_token, github_webhook_secret
from app.webhook import subject_from_webhook, verify_signature

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
logger = logging.getLogger("pr-analyzer")

app = FastAPI(title="PR Analyzer Service", docs_url=None, redoc_url=None)


def github_client() -> GitHubClient:
    return GitHubClient(github_token())


@app.get("/", include_in_schema=False)
@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/", response_model=ReviewResponse, response_model_exclude_none=True)
@app.post("/review", response_model=ReviewResponse, response_model_exclude_none=True)
async def review(
    request: ReviewRequest,
    _: None = Depends(require_bearer),
    github: GitHubClient = Depends(github_client),
    model=Depends(build_model),
) -> ReviewResponse | JSONResponse:
    try:
        return await perform_review(request, github, model)
    except Exception:
        logger.warning("review_failed")
        return JSONResponse(status_code=503, content={"detail": "The pull request could not be analyzed."})


@app.post("/github/webhook")
async def github_webhook(
    request: Request,
    github: GitHubClient = Depends(github_client),
    model=Depends(build_model),
):
    secret = github_webhook_secret()
    if not secret:
        return JSONResponse(status_code=503, content={"detail": "Webhook secret is not configured."})
    raw = await request.body()
    if not verify_signature(secret, raw, request.headers.get("X-Hub-Signature-256")):
        return JSONResponse(status_code=401, content={"detail": "Invalid webhook signature."})
    event = request.headers.get("X-GitHub-Event", "")
    if event != "pull_request":
        return Response(status_code=204)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return JSONResponse(status_code=400, content={"detail": "Webhook body must be JSON."})
    if not isinstance(payload, dict) or payload.get("action") not in {"opened", "synchronize"}:
        return Response(status_code=204)
    subject = subject_from_webhook(payload)
    if subject is None:
        return JSONResponse(status_code=400, content={"detail": "Webhook payload is missing the pull request."})
    try:
        result = await perform_review(ReviewRequest(pullRequest=subject), github, model)
    except Exception:
        logger.warning("webhook_failed")
        return JSONResponse(status_code=503, content={"detail": "The pull request could not be analyzed."})
    try:
        await github.upsert_comment(subject.repository, subject.number, render_comment(result))
    except Exception:
        logger.warning("comment_failed")
        return JSONResponse(status_code=502, content={"detail": "The review was not posted."})
    return JSONResponse(
        status_code=202,
        content={"analysisMode": result.analysisMode, "category": result.category, "summary": result.summary},
    )
