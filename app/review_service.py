import logging

from app.ai.protocol import ChatModel
from app.github_client import GitHubClient
from app.rubric import build_review
from app.schemas import ReviewRequest, ReviewResponse
from app.settings import analysis_mode, gemini_judge_model, gemini_model, google_cloud_project, vertex_location
from app.store import save_review

logger = logging.getLogger("pr-analyzer")


def build_model() -> ChatModel | None:
    if analysis_mode() != "gemini":
        return None
    try:
        from app.ai.vertex import VertexChat

        project = google_cloud_project()
        if not project:
            raise RuntimeError("Vertex project is not configured.")
        return VertexChat(project, vertex_location())
    except Exception as exc:
        logger.warning("vertex_unavailable %s", type(exc).__name__)
        return None


async def perform_review(request: ReviewRequest, github: GitHubClient, model: ChatModel | None) -> ReviewResponse:
    snapshot = await github.load(request.pullRequest)

    async def read_file(path: str) -> str:
        return await github.read_file(request.pullRequest.repository, path, snapshot.head_sha)

    result: ReviewResponse
    if analysis_mode() == "gemini" and snapshot.fetched and model is not None:
        try:
            from app.ai.pipeline import analyze

            result = await analyze(request.pullRequest, snapshot, request.config, model, read_file)
        except Exception as exc:
            logger.warning("gemini_failed %s", type(exc).__name__)
            result = build_review(request.pullRequest, snapshot, request.config)
            result.analysisMode = "rubric"
    else:
        result = build_review(request.pullRequest, snapshot, request.config)
        result.analysisMode = "rubric"
    models = None
    if result.analysisMode == "gemini":
        models = {"extract": gemini_model(), "judge": gemini_judge_model()}
    save_review(
        request,
        result,
        head_sha=snapshot.head_sha,
        reviewed_files=[item.filename for item in snapshot.patches],
        skipped_files=list(snapshot.files_skipped),
        truncated_files=list(snapshot.truncated_names()),
        models=models,
    )
    return result
