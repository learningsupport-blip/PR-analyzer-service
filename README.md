# PR Analyzer Service

Reviews a GitHub pull request for Contributor Conclave. The Next.js app sends the saved pull request, and this service returns the summary, insights, and suggested scores that the score dialog already accepts.

The service listens on `0.0.0.0` and `$PORT`. Cloud Run sets `PORT`.

## Local

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Set `ANALYSIS_TOKEN` in `.env`. Use the same value as `PR_ANALYSIS_TOKEN` in Contributor Conclave. Set `GITHUB_TOKEN` when check results, the linked issue, and the diff should be read from GitHub.

`ANALYSIS_MODE=rubric` is the default and does not call a model. `ANALYSIS_MODE=gemini` sends the redacted diff, the linked issue, and the repo contributing guide to Vertex AI. Python then assigns the scores. If Vertex is unavailable or returns JSON that fails validation, the response falls back to the rubric and `analysisMode` is `rubric`. Local Gemini uses application-default credentials for `GOOGLE_CLOUD_PROJECT`. Leave the mode on `rubric` when those credentials are not available.

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

In Contributor Conclave `.env`:

```bash
PR_ANALYSIS_URL=http://127.0.0.1:8080/review
PR_ANALYSIS_TOKEN=<the same ANALYSIS_TOKEN>
```

Restart `npm run dev`, then use AI Analysis on a mentor pull request. Once `ANALYSIS_MODE=gemini`, that app's HTTP client must allow up to 180 seconds.

`POST /` and `POST /review` accept the same JSON. `GET /health` is open so Cloud Run can probe it. Review requests must send `Authorization: Bearer <ANALYSIS_TOKEN>`.

The request may include `config`. When it is omitted, quality, merge readiness, checks, and coaching all run, and suggested scores are returned.

A Gemini review adds `category` (`bad`, `good`, or `very_good`), `categoryLabel` (`Bad PR`, `Good PR`, or `Very Good PR`), `mergeEstimate`, and `report`. The dialog scores stay on the existing fields: `clarity`, `scope` (change fit), `evidence`, and `outcome` (the merge estimate divided by 10). The category is Bad below 5, Good from 5 to 7.9, and Very Good at 8 or above. A pull request with no linked issue, or with a failing check, cannot be Very Good. The merge estimate starts at 55 and moves by fixed check, conflict, draft, and issue-fit factors. It is clamped between 5 and 92.

The model does not choose those numbers. It returns findings for the issue criteria, the changed files, and the contributing guide. File notes that quote code are kept only when that quote is in a patch that was actually sent. `CONTRIBUTING.md` is read from the pull request base, then `.github/pull_request_template.md`. A repository with no contributing guide uses a short default checklist, and the report says so.

Firestore is off locally. On Cloud Run, `FIRESTORE_ENABLED=true` stores the summary, scores, category, and reviewed or skipped paths in the `prAnalyses` collection, keyed by the head commit. The pull request description and diff are not stored. Guideline checklists are cached separately by repository and blob id. The runtime service account needs Firestore write access. Do not put a Firebase private key in the image or in Cloud Build.

`POST /github/webhook` accepts a signed `pull_request` `opened` or `synchronize` event. It runs the same review and creates or updates one comment marked `<!-- pr-analyzer -->`. The route checks `X-Hub-Signature-256` with `GITHUB_WEBHOOK_SECRET`. It does not use `ANALYSIS_TOKEN`.

## Cloud Run

`cloudbuild.yaml` builds this directory, pushes `pr-analyzer-service` to the existing `contributor-conclave` Artifact Registry repository in `us-central1`, and deploys a separate Cloud Run service. Create these Secret Manager secrets before the first deploy, without putting their values in the build file:

- `pr-analyzer-token`
- `pr-analyzer-github-token`
- `pr-analyzer-webhook-secret`

Create a runtime service account `pr-analyzer-run` that can read those secrets, write to Firestore, and call Vertex AI. Grant `roles/aiplatform.user` on the project:

```bash
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:pr-analyzer-run@$PROJECT_ID.iam.gserviceaccount.com" \
  --role="roles/aiplatform.user"
```

The Cloud Run request timeout is 180 seconds. The trigger for this file should use this directory as its source, so a change here does not rebuild the Next.js app.
