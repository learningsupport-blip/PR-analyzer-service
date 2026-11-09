import os

from dotenv import load_dotenv

load_dotenv()


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes"}


def analysis_token() -> str:
    return os.environ.get("ANALYSIS_TOKEN", "").strip()


def github_token() -> str:
    return os.environ.get("GITHUB_TOKEN", "").strip()


def firestore_enabled() -> bool:
    return _flag("FIRESTORE_ENABLED")


def firestore_project() -> str:
    return os.environ.get("FIRESTORE_PROJECT", "").strip()


def firestore_collection() -> str:
    return os.environ.get("FIRESTORE_COLLECTION", "prAnalyses").strip() or "prAnalyses"


def analysis_mode() -> str:
    mode = os.environ.get("ANALYSIS_MODE", "rubric").strip().lower()
    if mode in {"rubric", "gemini"}:
        return mode
    return "rubric"


def gemini_model() -> str:
    return os.environ.get("GEMINI_MODEL", "gemini-2.5-flash").strip() or "gemini-2.5-flash"


def gemini_judge_model() -> str:
    return os.environ.get("GEMINI_JUDGE_MODEL", "gemini-2.5-pro").strip() or "gemini-2.5-pro"


def vertex_location() -> str:
    return os.environ.get("VERTEX_LOCATION", "us-central1").strip() or "us-central1"


def google_cloud_project() -> str:
    return (
        firestore_project()
        or os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()
        or os.environ.get("GCLOUD_PROJECT", "").strip()
    )


def github_webhook_secret() -> str:
    return os.environ.get("GITHUB_WEBHOOK_SECRET", "").strip()
