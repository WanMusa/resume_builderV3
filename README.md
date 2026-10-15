# resume_builderV3

JSON inbox → PostgreSQL queue → Python generation → deterministic JSON/Markdown
rendering → private Supabase Storage (or local development storage).

FastAPI, importer, and worker share one codebase. **No scraping, CSV ingestion,
Hermes, OpenClaw, LangGraph, Redis, or provider memory.** The Chrome extension owns
capture. Prompts and authoritative career evidence are version-controlled files.

## Implemented and deliberately deferred

- Strict schema-version-2 intake via HTTP or watched JSON directory; per-job
  validation, durable errors, idempotent imports, and changed-content revisions.
- Dedicated PostgreSQL workflow tables, polling workers, execution leases,
  bounded attempts, durable generated JSON, resumable rendering/upload.
- Mock provider (default, no external services), OpenAI-compatible structured JSON
  provider, evidence selection, immutable fixed sections, usage/config hashes.
- JSON and readable Markdown artifacts; local storage and real Supabase REST
  storage adapter. Supabase is **not** the workflow database.
- Authenticated operator endpoints and non-root Docker services.
- **Not implemented:** DOCX/PDF templates, browser UI, signed download URLs,
  approval/regeneration UI, CSV processing, cost dashboard, automatic migrations
  for future schema revisions. The renderer is a small replaceable module, not
  fake DOCX support. Back up PostgreSQL and artifact storage before upgrades.

The committed career data is **fictional example evidence**, not a real resume.
Mock artifacts are visibly marked NOT FOR SUBMISSION. Replace the example with
verified facts before real use. Do not put private contact details or confidential
career data into a public repository; use a private repository or read-only
deployment asset override for those facts.

## Local development

Python 3.12+ and PostgreSQL 17 are recommended. Run from the checkout root so
default relative asset paths resolve correctly:

```bash
export APP_DIR=/absolute/path/to/resume_builderV3
cd "$APP_DIR"
python -m venv "$APP_DIR/.venv"
. "$APP_DIR/.venv/bin/activate"
python -m pip install -e '.[test]'
cp "$APP_DIR/.env.example" "$APP_DIR/.env"
```

Edit `.env`: set `DATABASE_URL` to your **dedicated app role/database**, supply
`DATABASE_PASSWORD`, set a long random `API_TOKEN`, and replace container paths
with local absolute paths:

```dotenv
DATA_DIR=/absolute/path/to/resume_builderV3/data
INBOX_DIR=/absolute/path/to/resume_builderV3/inbox/json
ASSETS_DIR=/absolute/path/to/resume_builderV3/assets
PROMPTS_DIR=/absolute/path/to/resume_builderV3/prompts
```

Then:

```bash
resume-builder init-db
uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000
# Separate terminals, same virtualenv and checkout:
resume-builder import-inbox --watch
resume-builder worker
```

`init-db` is an explicit initial `create_all` schema bootstrap, not a migration
engine. Run it before starting services; do not run it on every request.
For an entirely offline smoke run, a file-backed
`DATABASE_URL=sqlite:////absolute/path/to/dev.db` is supported for development and
tests only. PostgreSQL is required for production multi-worker locking.

```bash
# No live provider/storage calls or credentials are required:
python -m pytest
# One-shot operator commands:
resume-builder import-inbox
resume-builder worker --once
```

## Shared PostgreSQL setup

Use the existing `postgres` service on the external `ai_platform` Docker network
from `WanMusa/ai-platform_infra`. No infra changes are included here.
Ask the database operator to provision a dedicated LOGIN role and database,
for example `resume_builder`, with no SUPERUSER, CREATEDB, or CREATEROLE privileges.
The role should own only its app database/schema and be able to create the app's
tables, indexes and constraints there. Restrict unrelated database access.
The app never needs infrastructure administrator credentials.

Set a URL of the form:

```text
postgresql+psycopg://resume_builder@postgres:5432/resume_builder
```

Supply the dedicated role's password in `DATABASE_PASSWORD`; it is passed to the
driver separately, so special characters do not need URL encoding.

For non-Docker local development use the appropriate local database hostname
(the infra service publishes PostgreSQL on VPS loopback, not publicly).
Keep PostgreSQL private. Use TLS when connecting across an untrusted network.

Tables separate batch intake, job execution, normalized input snapshots
(`job_inputs`), generated packages (`generated_documents`), and artifact metadata
(`artifacts`). JSON content uses PostgreSQL JSONB. Snapshots preserve the source
description; normalization is used only for deduplication hashes.

## Inbox contract and flow

The current extension exports one schema-version-2 envelope per job; multiple
jobs in one envelope also work:

```json
{
  "schema_version": 2,
  "jobs": [{
    "id": "capture-id",
    "source_url": "https://example.com/jobs/123",
    "site": "other",
    "captured_at": "2026-10-14T12:00:00Z",
    "description_text": "Full captured advert text",
    "title": "Business Analyst",
    "company": null
  }]
}
```

`schema_version` must be the integer `2`; `exported_at` is optional (timezone-aware
ISO timestamp when present). Required job fields are nonempty string `id`, HTTP(S)
`source_url`, nonempty `site`, timezone-aware ISO `captured_at`, and nonempty
`description_text`. Optional nullable strings: `title`, `company`, `location`,
`employment_type`, `salary_text`, `notes`. Older optional
`extraction_mode` accepts `raw` (default) or `llm_refined`;
`requirements`, `responsibilities`, `skills`, and `warnings` default to empty
string arrays. Types are not coerced and unknown fields are rejected. These
legacy fields are accepted but do not trigger extraction or another model call.
See the working sample in `examples/job-envelope.json`.

Copy completed files into `inbox/json`. **Do not copy directly to a `.json`
filename while the importer is watching**: write a `.part` file on the same
filesystem, then atomically rename it:

```bash
cp "$APP_DIR/examples/job-envelope.json" "$APP_DIR/inbox/json/example.part"
mv "$APP_DIR/inbox/json/example.part" "$APP_DIR/inbox/json/example.json"
```

Accepted input moves into `inbox/json/archive` after DB commit. Invalid files
move into `inbox/json/quarantine` with an `.errors.json` sidecar. Valid jobs in a
partially invalid envelope are imported; batch errors identify rejected job
indexes and fields. Fix and resubmit rejected jobs. CSV is ignored; `inbox/csv`
is reserved. Use **one importer instance** per directory; workers may scale
independently.

Repeated identical envelopes return the same batch. Same capture ID and
normalized content deduplicate; changed content creates a new internal job UUID.
Capture time alone is not a new job revision. Do not edit accepted DB snapshots.

## HTTP/operator API

All endpoints except `/health` require an `Authorization` header using the
`Bearer` scheme followed by the configured API token.
Startup refuses a missing token unless `ALLOW_UNAUTHENTICATED=true` is explicitly
set for loopback-only local development. Keep production authentication enabled.
No permissive CORS policy is installed. The extension can post its JSON envelope
to `/batches` using its configured inbox bearer token.

```bash
# Set API_TOKEN in this shell without committing it or placing it in shell history.
curl -fsS http://127.0.0.1:8000/health
curl -fsS http://127.0.0.1:8000/batches \
  -H "Authorization: Bearer"" $API_TOKEN" -H 'Content-Type: application/json' \
  --data-binary "@$APP_DIR/examples/job-envelope.json"
curl -fsS -H "Authorization: Bearer"" $API_TOKEN" http://127.0.0.1:8000/jobs
curl -fsS -H "Authorization: Bearer"" $API_TOKEN" http://127.0.0.1:8000/jobs/JOB_UUID
curl -fsS -X POST -H "Authorization: Bearer"" $API_TOKEN" \
  http://127.0.0.1:8000/jobs/JOB_UUID/retry
```

`POST /batches` accepts a raw JSON envelope (not multipart), maximum 2 MiB;
HTTP 422 returns useful validation errors. Accepted/partial imports return HTTP
201. `GET /batches`, `/batches/{id}`, `/jobs`, `/jobs/{id}` expose statuses and
results. Lists accept `limit` (1–200) and `offset`; jobs also accept `status`.
Job details include generated content and local paths or Supabase object keys,
not public URLs. Health checks database connectivity without exposing credentials.
Do not serve Markdown as trusted HTML without sanitization.

### State and retries

Normal flow: `queued → generating → rendered → uploaded → completed`.
Live generated drafts finish as `needs_review` rather than claiming semantic
fact verification. Source facts remain deterministic, but summaries/letters must
be checked before submission.

Workers claim work in short PostgreSQL transactions using `FOR UPDATE SKIP LOCKED`,
increment attempts, and use expiring leases with ownership tokens. Expired work
can be reclaimed after a crash. Checkpoint writes are fenced against stale workers;
render paths are lease-specific. Do not reduce the default lease below the maximum
generation/storage operation duration. No exactly-once model-call guarantee is
possible across a crash between provider response and checkpoint commit.

Errors automatically requeue up to `MAX_ATTEMPTS`, then become `failed`.
`POST /jobs/{id}/retry` resets failed job attempts without deleting generated JSON.
Rendering/upload failures **do not repeat generation** once the generated package
is committed. Successfully recorded artifacts are skipped on retry.
Changing prompts or base evidence does not silently rewrite an existing generated
checkpoint. Intentional regeneration/version management is a future feature;
retry is for recovery, not paid regeneration.

## Generation and factual accuracy

Edit `assets/base_resume.json` and the four files under `prompts/`.
Stable evidence IDs link achievements to code-controlled roles, dates, employers,
education, certifications, and identity. The model outputs only a summary and
evidence selections for resumes; Python inserts achievement text verbatim.
Cover-letter paragraphs reference supporting evidence IDs. Unknown IDs,
duplicate achievement IDs, omitted critical achievements, and invalid JSON fail
validation. Semantic correctness of arbitrary prose is **not** guaranteed by JSON
validation; all live drafts require review.

Evidence selection ranks whole achievements by keyword overlap with the job.
It always keeps critical achievements and at least one per role, with a soft
target of 12 records; supporting details are never globally stripped or truncated.
Expertise remains compact and complete. Review selection behavior on your real
resume before production; overly large evidence may exceed model limits, and the
bounded output schema permits at most 16 achievements.

Requests omit capture metadata, fixed sections, duplicate extracted requirements,
and prior jobs. Resume and letter use separate bounded requests with independent
instructions and the same authoritative evidence, not generated claims as facts.
The combined validated package is the generation checkpoint; if the second call
fails before that checkpoint, retry repeats both calls. Per-call checkpoints and
combined-call benchmarking can be added later if measured cost warrants it.
Provider/model, base/prompt/input hashes, selected IDs, and usage are recorded.
There are no agent loops, remote tools, or automatic provider SDK retries.
Provider memory is not a token-saving mechanism; relevant evidence is sent on
each request. Prompt caching, if provided, is an optimization, not a guarantee.

For live OpenAI-compatible generation set `PROVIDER=openai`, `OPENAI_API_KEY`,
`OPENAI_MODEL`, and optionally `OPENAI_BASE_URL` (trusted endpoint only; credentials
and career data are sent there). The endpoint must support chat completions with
strict JSON-schema responses. Replace **all** example facts, validate IDs, and set
`is_example=false` in your deployment asset first.

## Storage

`STORAGE_BACKEND=local` writes under `DATA_DIR/storage`; rendered originals remain
under `DATA_DIR`. `STORAGE_BACKEND=supabase` requires an existing **private** bucket,
`SUPABASE_URL` (HTTPS), `SUPABASE_BUCKET`, and a server-only `SUPABASE_KEY` with
upload/upsert permission. A service-role key is powerful: keep it only on the
server, never in the extension. Prefer a scoped server credential where supported.
No bucket is created and no object is made public. Store durable bucket/object
keys; a future authenticated download route can issue short-lived signed URLs.
Supabase calls have timeouts and persist sanitized HTTP errors, not response bodies.

## Environment reference

| Variable | Default / purpose |
| --- | --- |
| `DATABASE_URL` | Dedicated PostgreSQL SQLAlchemy URL; required deployment configuration |
| `DATABASE_PASSWORD` | Dedicated app role password, passed separately to the driver |
| `API_TOKEN` | Required bearer secret unless explicit local-only opt-out |
| `ALLOW_UNAUTHENTICATED` | `false`; never enable on exposed deployments |
| `PROVIDER` | `mock` or `openai` |
| `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `OPENAI_MODEL` | Live provider credentials, trusted URL, model |
| `STORAGE_BACKEND` | `local` or `supabase` |
| `SUPABASE_URL`, `SUPABASE_KEY`, `SUPABASE_BUCKET` | Private storage configuration |
| `DATA_DIR`, `INBOX_DIR` | Writable output and JSON intake paths |
| `ASSETS_DIR`, `PROMPTS_DIR` | Read-only versioned content directories |
| `POLL_SECONDS` | `5`; idle importer/worker polling interval |
| `LEASE_SECONDS` | `600`; execution lease duration |
| `MAX_ATTEMPTS` | `3`; automatic processing attempt limit |

## VPS deployment

1. Provision the dedicated app database/role in existing infra, confirm external
   network `ai_platform` exists, and create the private Supabase bucket if needed.
2. Check out this repository on the VPS (for example `/opt/resume_builderV3`).
   Create `.env` from `.env.example`, set real app credentials and a strong API
   token, and keep the default `/app/...` paths for containers.
3. Prepare writable host directories for the non-root container UID:

   ```bash
   cd /opt/resume_builderV3
   mkdir -p /opt/resume_builderV3/data /opt/resume_builderV3/inbox/json /opt/resume_builderV3/inbox/csv
   sudo chown -R 10001:10001 /opt/resume_builderV3/data /opt/resume_builderV3/inbox
   chmod 600 /opt/resume_builderV3/.env
   docker compose build
   docker compose run --rm api resume-builder init-db
   docker compose up -d
   docker compose ps
   ```

4. Confirm `/health`, import the example, inspect jobs/artifacts with mock mode,
   then configure actual evidence/provider/storage and test a reviewed real job.
5. API is bound to VPS loopback port 8000. Add the existing HTTPS reverse proxy
   deliberately (upstream `api:8000` on `ai_platform`); do not expose this API
   publicly without TLS, bearer authentication, request size/rate limits, and
   appropriate access controls. Proxy host/certificate configuration and secrets
   provisioning remain operator tasks, not assumed deployment automation.

Containers use a read-only root filesystem, dropped capabilities, no-new-privileges,
and read-only prompt/asset mounts. Never commit `.env`, inbox archives, database
dumps, or generated documents. Back up PostgreSQL, retained input, and output
volumes; configure retention and disk monitoring. Default mock/local mode is for
safe end-to-end verification, not automatic submission of job applications.
