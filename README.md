# resume_builderV3

JSON inbox → PostgreSQL queue → variable-only LLM generation → Python-rendered
Word documents using V2 templates → private Supabase Storage.

FastAPI, importer, and worker share one codebase. **No scraping, CSV ingestion,
Hermes, OpenClaw, LangGraph, Redis, or provider memory.** The Chrome extension owns
capture. Prompts and authoritative career evidence are version-controlled files.

## Implemented and deliberately deferred

- Strict schema-version-2 intake via HTTP or watched JSON directory; per-job
  validation, durable errors, idempotent imports, and changed-content revisions.
- Dedicated PostgreSQL workflow tables, polling workers, execution leases,
  bounded attempts, durable generated JSON, resumable rendering/upload.
- Mock provider (default, no external services), OpenAI-compatible structured JSON
  provider, concise tailored prose with source citations, immutable fixed facts,
  usage/config hashes.
- Generated JSON checkpoint in PostgreSQL; resume and cover-letter DOCX artifacts
  rendered entirely in memory and uploaded to private Supabase Storage.
  There is no local final-output fallback. Supabase is **not** the workflow database.
- Authenticated operator endpoints and non-root Docker services.
- **Not implemented:** PDF export, browser UI, signed download URLs,
  approval/regeneration UI (operator regeneration API is available), CSV processing,
  cost dashboard, automatic migrations
  for future schema revisions. Back up PostgreSQL and artifact storage before upgrades.

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

The default suite uses isolated SQLite files and mocked external providers.
For PostgreSQL locking/concurrency coverage, export `CORE_TEST_DATABASE_URL`
(a PostgreSQL SQLAlchemy URL) and `CORE_TEST_DATABASE_PASSWORD` for a disposable test
database, then run `python -m pytest`. Tests create and remove isolated schemas;
the test role needs CREATE SCHEMA permission. Never point tests at an unrelated
application database.

## Shared PostgreSQL setup

Use the existing `postgres` service on the external `ai_platform` Docker network
from `WanMusa/ai-platform_infra`. No infra changes are included here.
Automated deployment provisions the dedicated `resume_builder` LOGIN role and
database, with no SUPERUSER, CREATEDB, or CREATEROLE privileges.
The role should own only its app database/schema and be able to create the app's
tables, indexes and constraints there. Restrict unrelated database access.
The app never receives infrastructure administrator credentials. Deployment uses
local `psql` admin access inside the existing `postgres` container; it creates no
new PostgreSQL server and never changes the Gmail sorter role/database.

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

## Automatic VPS deployment

The **Test and deploy** Actions workflow tests Python 3.12 with `.[test]`, checks
the deployment shell/Compose file and builds the Docker image on pull requests.
CI starts an isolated PostgreSQL 17 service with disposable test-only credentials.
It tests role/database provisioning, repeated schema bootstrap preserving existing
data, and connectivity/permissions using the app's non-admin login. Generation
tests use the example fixture and mocked HTTP responses, never your private
resume or a live OpenAI key. No CI test makes paid LLM calls.
Only a push/merge to the default branch (`main`) can then deploy, after that
revision's CI passes. PRs never receive deployment secrets. If the repository's
default branch is renamed, update the workflow's `push.branches` filter too.
In **Actions → Test and deploy → Run workflow**, select **main** for a manual
deployment with fresh CI. Other refs cannot deploy. A rerun tests/deploys its
original SHA, not whichever commit is newest at SSH time. Stale runs cannot
downgrade an already-newer checkout; divergent local history is also refused.
Deployments are serialized in Actions and with a VPS file lock.

### One-time prerequisites (operator, not Actions)

The workflow reuses exactly these SSH secrets:

- `VPS_HOST`: hostname or IPv4 address, SSH port 22.
- `VPS_USER`: dedicated deployment account.
- `VPS_SSH_KEY`: that account's private SSH key.

**Additional required host verification secret:** `VPS_KNOWN_HOSTS`, containing
the complete OpenSSH known-hosts line(s) for `VPS_HOST`, e.g.
`your-host.example ssh-ed25519 <public-host-key>`. Obtain/verify the public host
key or fingerprint through the VPS provider console or another independently
trusted channel. An unverified `ssh-keyscan` result is not verification. Hashed
known-hosts entries also work. Host key changes fail closed; independently
verify rotation before updating the secret. Actions uses strict checking.

Add these runtime secrets in the repository's Actions settings:

| Secret | Purpose |
| --- | --- |
| `DATABASE_PASSWORD` | Password assigned to the dedicated `resume_builder` role |
| `API_TOKEN` | Bearer token accepted by the inbox API |
| `JOB_RUNS_API_TOKEN` | Optional separate token for the Chrome extension's limited Job Runs API |
| `JOB_RUNS_API_TOKEN` | Optional separate bearer token limited to the Chrome extension's Job Runs routes |
| `OPENAI_API_KEY` | Runtime generation key, only needed when using `PROVIDER=openai` |
| `DB_USER` | Optional existing PostgreSQL admin login used for local provisioning; defaults to the container's `POSTGRES_USER` |
| `SUPABASE_URL` | HTTPS URL of the output storage project |
| `SUPABASE_KEY` | Server-only credential with bucket metadata and upload/upsert permission |
| `SUPABASE_SECRET_KEY` | V2-compatible alternative to `SUPABASE_KEY`; `SUPABASE_KEY` takes precedence |
| `SUPABASE_BUCKET` | Optional bucket override; new deployments default to `generated-documents` |

Secrets travel in the encrypted SSH input stream, not command arguments or logs.
Deployment creates `.env` from `.env.example` if missing, then atomically merges
nonempty runtime secrets with mode `0600`. Existing non-secret settings and omitted
secrets are retained. Deployment explicitly upgrades `STORAGE_BACKEND` to `supabase`;
old `local` settings are not retained. First deployment requires `DATABASE_PASSWORD`
and `API_TOKEN`, and preflight requires valid Supabase settings and a private bucket.
`DB_USER` is not the application's login and is not written into its runtime file.
The existing PostgreSQL container must allow its admin login over the local Unix
socket; no database admin password is sent to the app. A supplied database password
is applied only to `resume_builder`; use distinct passwords for different apps.
Set a unique random `JOB_RUNS_API_TOKEN` Actions secret to enable the extension's
Job Runs list, retry/regenerate actions, and signed resume-link endpoint. It is
separate from `API_TOKEN`; without it those routes remain disabled.

On the VPS, install Python 3, Bash, Git, `flock` (util-linux), GNU `timeout` (coreutils),
Docker Engine and Compose v2 supporting `up --wait` (2.20+ recommended).
The deploy account must be able to run Docker noninteractively (Docker access
is effectively root access; use a trusted account). It needs outbound HTTPS to
GitHub and the image/package registries. Provision the existing shared
`ai_platform` network and the existing `postgres` container.
Deployment creates the app role/database if absent and synchronizes its password
on redeployment. It refuses an existing privileged app role or a database owned
by another role instead of adopting them. It does **not** create infrastructure
or another PostgreSQL container. The app role must own its dedicated database,
have USAGE/CREATE on its schema, and own/have read/write access to its app tables.
Do not use an infra admin role or a database belonging to another app.

The fixed checkout directory is **`$HOME/projects/resume_builderV3`**, relative
to `VPS_USER`'s home. The fixed Compose project is **`resume_builder_v3`**.
Its only accepted origin is
**`https://github.com/WanMusa/resume_builderV3.git`** (public, no Git credentials).
Deployment creates a missing checkout directory, runtime file and runtime
directories automatically. An existing checkout must use the documented origin.
Bootstrap refuses other
files; it also safely resumes an interrupted Git initialization with no HEAD.
Do not place it inside another checkout or behind symlinks.

The existing Actions workflow invokes `scripts/deploy.sh`, which creates missing
`data/`, `inbox/`, `inbox/json/`, and `inbox/csv/` and rejects symlinks/files in
their place. After building, a short-lived setup container sets only those four
directory owners to UID 10001, their group to the deployment account's primary
GID, and their mode to `2770` (setgid, owner/group access, no other-user access).
The helper runs as root with only the added CHOWN, FOWNER and DAC_READ_SEARCH
capabilities; normal services keep their unprivileged user and dropped caps.
No interactive sudo or ACL package is needed for a fresh deployment. Existing
files are not recursively chowned/chmodded. Runtime secret merging preserves
other `.env` settings. Setup failure prevents service shutdown/startup.

If migrating existing input/output files, back them up, then grant the
Dockerfile's **UID 10001** read/write/traverse access to all runtime
directories and read access to existing files, including archives/quarantine
and nested generated data. Existing input/output files and Git's `.gitkeep`
placeholders do not need container write permission; input moves use directory
permissions. New final documents are uploaded from memory, not written to these directories.
Use narrowly scoped ownership/ACL changes, **not chmod 777**. Docker bind mounts
hide the image's directory ownership. Existing runtime paths must also remain
accessible to the deployment account for Git checkout before the setup helper
runs. The script checks the resulting runtime access inside the image as UID
10001; incompatible permissions on existing nested files still need an explicit
migration plan rather than recursive deployment-time ownership changes.
Keep assets and prompts readable by UID 10001 but not writable by the container.

The generated `.env` defaults to the dedicated app `DATABASE_URL` and
`ALLOW_UNAUTHENTICATED=false`; Actions supplies the password and API token.
Automatic provisioning supports only `resume_builder@postgres:5432/resume_builder`,
not arbitrary admin/application URLs. Keep container paths
`DATA_DIR=/app/data`, `INBOX_DIR=/app/inbox/json`, `ASSETS_DIR=/app/assets`,
`PROMPTS_DIR=/app/prompts`. Supabase credentials and an existing private bucket are
required. Preflight checks bucket metadata and rejects public buckets; it does not
upload a test object. `PROVIDER=mock` remains mock unless **you** change the runtime file.
Deployment does not require an OpenAI key or non-example evidence to start.
Those checks happen only when a live generation job actually runs. Live generation
still requires your real, validated non-example career evidence and a runtime key;
deployment never switches providers or makes paid inference calls.

API binding stays **127.0.0.1:8000**. If another service uses that port, set
`RESUME_BUILDER_PORT=8001` (or another free port) in the VPS `.env`; it remains
loopback-only and the container port remains 8000. Do not expose the API
unauthenticated or publish PostgreSQL.

### Shared HTTPS hostname and extension inbox

The API registers with the existing `ai-platform_infra` nginx-proxy 1.7 and
ACME companion on `ai_platform`, using `APP_DOMAIN` (default
`wanagents.duckdns.org`) and only the **`/resume/`** path. `VIRTUAL_DEST=/`
strips that prefix: public `/resume/batches` reaches internal `/batches`.
Only `api` has proxy registration settings; worker/importer stay private.
One-off deployment setup/preflight/database containers clear the proxy and ACME
host variables so they cannot join the public route during deployment.
Existing Gmail sorter and local-LLM routes/configuration are not modified.

The proxy uses the hostname's existing certificate; its ACME companion maintains
certificates using infra's configured `DEFAULT_EMAIL`. No additional hostname,
certificate email, proxy-volume edits, or public application port is required
for this shared-host setup. DNS and the existing proxy/companion must already be
working for the hostname. Do not set this API to the root path `/`, which could
mix it with another service using the same host/path.

After deploying the updated revision, verify on the VPS or your laptop:

```bash
curl --fail --show-error https://wanagents.duckdns.org/resume/health
curl --silent --output /dev/null --write-out '%{http_code}\n' \
  https://wanagents.duckdns.org/resume/batches
```

Health should return 200 and `{"status":"ok"}`; the unauthenticated batches
request should return **401**, not 404. These checks do not enqueue generation.
If needed, run `docker exec reverse-proxy nginx -t` and inspect the relevant
host/path block privately with `nginx -T`; do not paste full proxy configuration
or credentials into logs/chat. Existing custom VPS proxy overrides must not
conflict with `/resume/`. Recheck your existing Gmail/LLM URLs after deployment.

In the capture extension Settings, enable explicit inbox sending and use:

| Setting | Value |
| --- | --- |
| Inbox HTTPS base URL | `https://wanagents.duckdns.org` |
| Inbox path | `/resume/batches` |
| Inbox bearer token | The resume builder VPS `API_TOKEN`, without `Bearer` |

Save settings and grant Chrome access when prompted, then select jobs and use
**Send selected**. This posts the schema-version-2 envelope directly to the
authenticated queue API, not to Supabase or the model backend. Changing
`APP_DOMAIN` requires using that hostname in the checks and extension settings.

### Deployment behavior and recovery

`scripts/deploy.sh TESTED_FULL_SHA main` is the reusable entry point, streamed
over verified SSH from the tested checkout. Docker/maintenance commands receive
`/dev/null` as stdin so they cannot consume the remaining streamed script, except
the admin `psql` command whose stdin is the generated app-only bootstrap SQL. Do not
run it with an untested SHA. A completed deployment prints
`Deployment healthy at tested revision <SHA>` after checking all three services;
a green CI job alone does not establish that the deploy job ran.
It fetches default-branch history but checks out only the requested revision,
refuses dirty/staged tracked changes, local divergence, untracked collisions,
and additional untracked source/assets/prompts. It never force-resets or cleans.
`assets/base_resume.json` is private, ignored runtime data. Deployment preserves
it byte-for-byte, including the transition from older revisions that tracked it.
Only when the target revision no longer tracks the resume does deployment move
the private file into a restricted temporary directory outside the checkout,
remove its Git index entry, and restore the original file after checkout.
An exit trap also restores it on failure; restoration refuses to overwrite any
unexpected file and reports the retained backup location instead. Unrelated
tracked edits (including prompts) still block deployment. Fresh deployments copy
the example to the private runtime path and explicitly report that it must be
replaced before live generation. Docker images exclude the private resume and
contain example data only; Compose mounts the VPS assets at runtime.
Do not commit private career data to this public repository. Ignoring/untracking
a file does not remove its contents from previous commits.
Ignored `.env` settings are retained except for supplied runtime secret updates.
Generated `data/`, inbox JSON/CSV, archive and quarantine contents are retained.
Only the existing tracked inbox `.gitkeep` placeholders are allowed
under runtime paths. There is no `down`, volume deletion or system pruning.

Preflight/build failure leaves running services untouched. Builds and commands
that might include env values in errors have their output suppressed in Actions;
failures give a stage-specific, secret-free message. Diagnose privately on the
VPS; **do not paste `.env`, expanded Compose config, or raw app/build logs into
Actions/issues**. Only this project's `api`, `worker`, and `importer` are managed;
existing containers from another directory with the same project name are refused.

After building, merging runtime secrets, provisioning the app database and checking
auth/configuration, bind permissions and real DB connectivity/role readiness,
deployment stops this app's importer/worker, runs
the supported `resume-builder init-db`, then recreates these three services.
`init-db` is idempotent `create_all`, **not a migration system**. Future schema
changes need a separately reviewed migration/backup plan. No unrelated DB is
initialized, stopped, dropped or migrated.

The CLI catches SIGINT, so Compose uses it with a bounded five-minute stop grace
period. **This is not a drain-to-completion implementation**: an in-flight request
can be interrupted. Existing leases/checkpoints recover work after expiry, but
a provider response not yet checkpointed can incur another paid call on retry.
For cost-sensitive updates, pause intake and wait for active jobs to finish
before merging/deploying. Do not run extra importer instances.

Success requires sustained running containers with no restart, a DB/schema check
and internal `/health` readiness. These checks do not import jobs, generate resumes
or test live inference/storage. Failure after stopping/startup is reported as
failure, not success; workers may remain stopped and operator recovery is needed.
There is no automatic rollback or guarantee of zero downtime.

**Rollback:** record the previous successful tested SHA from Actions, back up
the DB/data, and review schema compatibility first. Prefer reverting the bad
change in a new PR and merging it: the normal CI/deploy path then moves forward
without bypassing downgrade protection. An emergency older-image/source rollback
is an operator-only maintenance procedure, not a workflow ref input; stop only
this app's services, preserve `.env`/runtime files and use a previously tested
revision with a compatible schema. Never restore/drop the shared infra database,
run destructive migrations, or silently reset a divergent checkout.

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
Job details include generated content and Supabase bucket/object keys
(legacy local artifacts may still have local paths),
not public URLs. Health checks database connectivity without exposing credentials.
New outputs are Word documents, not Markdown.

### Restricted Chrome Job Runs API

The extension's Job Runs view uses a separate `JOB_RUNS_API_TOKEN`, never the
broader inbox/operator `API_TOKEN`. It exposes only the latest 50 job summaries,
retry for failed jobs without a generation checkpoint, explicitly confirmed
regeneration, and resume download-link creation. `GET /job-runs/{id}/resume-url`
returns a five-minute signed URL for a private Supabase object; the bucket stays
private. The public routes are under `/resume/job-runs` through the shared proxy.

Configure a unique random `JOB_RUNS_API_TOKEN` as a GitHub Actions secret and
redeploy. In the extension Settings, set the Job Runs API base URL to
`https://wanagents.duckdns.org` and enter that token in the separate Job Runs API
token field. The extension stores it in local Chrome storage, so use only this
restricted token there. Regeneration makes fresh model calls and may incur cost.

### Re-render an existing checkpoint without LLM calls

For a checkpoint already using the tailored-prose format, use the authenticated
`POST /jobs/JOB_UUID/rerender` endpoint (public prefix: `/resume`). It accepts
`completed`, `needs_review`, or `failed` jobs only when a generated JSON checkpoint
exists. It queues Word rendering and Supabase upload, clears superseded artifact
metadata, and preserves the exact generated database checkpoint. It never
regenerates prose. A second request while queued/processing returns HTTP 409.

```bash
curl -fsS -X POST -H "Authorization: Bearer $TOKEN" \
  https://wanagents.duckdns.org/resume/jobs/JOB_UUID/rerender
```

Old local files are not deleted automatically. No new local document or JSON
output files are written. Missing templates or Supabase configuration fail
explicitly rather than falling back to Markdown or local storage.

### Regenerate old selection-only content or intentionally rewrite prose

Old ID-selection checkpoints cannot become succinct tailored prose merely by
rerendering. The renderer rejects them with an actionable error, rather than
dumping their source evidence or silently making paid calls.

Explicitly request new generation using the **current** source resume, prompts
and provider settings:

```bash
curl -fsS -X POST -H "Authorization: Bearer $TOKEN" "https://wanagents.duckdns.org/resume/jobs/JOB_UUID/regenerate?confirm_paid_generation=true"
```

This endpoint requires authentication, explicit paid-generation confirmation,
a terminal job (`completed`, `needs_review` or `failed`), and an existing checkpoint.
It queues fresh resume and letter generation and clears old artifact metadata.
The original generated content remains in PostgreSQL while generation is pending;
after successful generation it is archived under `previous_versions` in the new
checkpoint. No history is sent to the LLM. Upload/render retries reuse the new
checkpoint without paying again. A failed generation retains the old content and
pending request; use `/retry` to resume it. Automatic generation retries can incur
additional provider charges up to `MAX_ATTEMPTS`.

### State and retries

Normal flow: `queued → generating → rendered → uploaded → completed`.
Live generated drafts finish as `needs_review` rather than claiming semantic
fact verification. Identity and professional experience are fixed; generated
profiles, competencies, key achievements and cover letters still need factual review.

Failed jobs report fixed evidence-validation reasons for unknown evidence IDs,
repeated achievement IDs, omitted critical achievements, or exceeded word budgets. Other exception
details remain redacted to avoid exposing private input or provider responses.
An existing failed job retains its old error until retried; retrying live
generation can make additional paid provider calls.

Workers claim work in short PostgreSQL transactions using `FOR UPDATE SKIP LOCKED`,
increment attempts, and use expiring leases with ownership tokens. Expired work
can be reclaimed after a crash. Checkpoint writes are fenced against stale workers.
PostgreSQL holds the job row lock during each root-filename upload, preventing
a replacement lease from uploading ahead of an in-flight predecessor.
Do not reduce the default lease below the maximum
generation/storage operation duration. No exactly-once model-call guarantee is
possible across a crash between provider response and checkpoint commit.

Errors automatically requeue up to `MAX_ATTEMPTS`, then become `failed`.
`POST /jobs/{id}/retry` resets failed job attempts without deleting generated JSON.
Rendering/upload failures **do not repeat generation** once the generated package
is committed. Successfully recorded artifacts are skipped on retry.
Changing prompts or base evidence does not silently rewrite an existing generated
checkpoint. `/regenerate?confirm_paid_generation=true` explicitly requests new
paid generation; `/retry` is for recovering the current work.

## Generation and factual accuracy

Edit `assets/base_resume.json` and the four files under `prompts/`.
Stable evidence IDs link achievements to code-controlled roles, dates, employers,
education, certifications, and identity. The model writes only the career profile,
competency area/description pairs, and selected key-achievement bullets. The
professional experience summaries and highlights are fixed in the base resume;
Python prints them as supplied. Fixed role headers, chronology and qualifications
also remain Python-controlled. Supporting details and platform descriptions can
inform generated competencies and highlights without being copied as a source dump.
The optional fixed `fixed.selected_projects` list is rendered under
`AGENTIC AI & AUTOMATION SOLUTIONS` between core competencies and professional experience.
It supports a section introduction (`selected_projects_intro`), a portfolio URL
(`selected_projects_url`), and project entries with `name`, `description`, and
optional `highlights`; these fields are printed as supplied, not LLM-rewritten.
The complete original resume is preserved as `source_resume` in the checkpoint;
the base file is never modified during generation.
Newly generated checkpoints use package format 3. The renderer continues to
support V2 checkpoints so saved jobs can still be rerendered without generation.

Competencies and key achievements carry internal evidence citations. These IDs are
validated but never printed in either Word document. Each OpenAI request constrains
citation IDs to current-source enums. Python additionally rejects unknown evidence
citations, repeated achievements, omitted critical evidence and invalid JSON.
Fixed experience remains in source order and is never model-editable. Empty source
expertise allows only an empty list.
Semantic correctness of arbitrary prose is **not** guaranteed by JSON
validation; all live drafts require review.

The target is about **2-3 pages**, not a guaranteed layout-engine page count.
Hard limits: profile 140 words; up to 14 competency items, each 26 words including
its label; up to 10 key achievements, each 36 words; all generated resume text at
most 800 words. The prompt targets approximately 600-750 generated words rather
than filling every maximum. Cover letters are capped at 350 words. Actual
pagination depends on fixed sections, fonts and Word's layout engine; inspect the
final document.

Resume requests include the job advert, concise role metadata, and source
achievement/expertise evidence. They omit fixed role prose, platform descriptions,
AI project descriptions, contact details and qualifications, reducing input tokens
while preserving the facts needed for tailoring. Reasonable capability-level
inference is permitted only when supported by multiple concrete source facts;
the model must not invent hands-on tool use, mastery, years or outcomes. Resume
and letter use separate bounded requests with independent instructions and the
same authoritative evidence, not generated claims as facts.
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

`STORAGE_BACKEND=supabase` is the only supported final-output backend and requires an existing **private** bucket,
`SUPABASE_URL` (HTTPS), `SUPABASE_BUCKET`, and a server-only `SUPABASE_KEY` with
upload/upsert permission. A service-role key is powerful: keep it only on the
server, never in the extension. Prefer a scoped server credential where supported.
No bucket is created and no object is made public. Store durable bucket/object
keys; a future authenticated download route can issue short-lived signed URLs.
Supabase calls have timeouts and persist sanitized HTTP errors, not response bodies.
The worker checks the private bucket before any generation call. Python renders
two Word documents using the unchanged V2 templates under
`assets/templates/`, retaining their page setup and applying V2 Lato/blue resume
and Aptos cover-letter formatting: career profile, expertise, professional
experience, education/certifications, and references. Education is printed once
(structured education records take precedence over their duplicate strings).
Bold expertise labels and achievement highlights are paired with normal-weight
descriptions; section and role headings stay with the following paragraph.
Fixed identity, role headers, education, certifications and references come from
the saved package. Other source sections remain context, not an automatic appendix.
Live documents do not include a draft banner; `needs_review` still requires review.
Mock/example documents retain their NOT FOR SUBMISSION banner.

V2 filename convention (invalid filename characters are stripped):

```text
CandidateName-JobTitle-Company-Resume.docx
CandidateName-JobTitle-Company-Cover Letter.docx
```

Both files are uploaded directly to the configured bucket **root**: no job,
lease or timestamp subfolders. Reprocessing the same name/title/company overwrites
the matching files (`upsert=true`), as in V2. Different jobs using identical names
share those output objects; older artifact records are not immutable document
versions, and concurrent jobs' latest successful uploads win. Existing objects in
old subfolders are not automatically deleted.

The JSON package remains in
PostgreSQL; it is not duplicated as an output artifact. Documents are uploaded
from memory with the Word MIME type; `local_path` is null for new artifacts.
Protect Supabase objects, inbox archives, and PostgreSQL backups as personal data.

## Environment reference

| Variable | Default / purpose |
| --- | --- |
| `DATABASE_URL` | Dedicated PostgreSQL SQLAlchemy URL; required deployment configuration |
| `DATABASE_PASSWORD` | Dedicated app role password, passed separately to the driver |
| `API_TOKEN` | Required bearer secret unless explicit local-only opt-out |
| `ALLOW_UNAUTHENTICATED` | `false`; never enable on exposed deployments |
| `PROVIDER` | `mock` or `openai` |
| `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `OPENAI_MODEL` | Live provider credentials, trusted URL, model |
| `STORAGE_BACKEND` | `supabase` only; no local fallback |
| `SUPABASE_URL`, `SUPABASE_KEY`, `SUPABASE_BUCKET` | Private storage configuration |
| `DATA_DIR`, `INBOX_DIR` | Legacy runtime directory and JSON intake path; documents are not written here |
| `ASSETS_DIR`, `PROMPTS_DIR` | Read-only versioned content directories |
| `POLL_SECONDS` | `5`; idle importer/worker polling interval |
| `LEASE_SECONDS` | `600`; execution lease duration |
| `MAX_ATTEMPTS` | `3`; automatic processing attempt limit |

## VPS deployment

1. Provision the dedicated app database/role in existing infra, confirm external
   network `ai_platform` exists, and create the private Supabase output bucket.
2. Check out this repository on the VPS (for example `/opt/resume_builderV3`).
   Create `.env` from `.env.example`, set real app credentials and a strong API
   token, and keep the default `/app/...` paths for containers.
3. Prepare writable host directories for the non-root container UID:

   ```bash
   cd /opt/resume_builderV3
   mkdir -p /opt/resume_builderV3/data /opt/resume_builderV3/inbox/json /opt/resume_builderV3/inbox/csv
   sudo chown -R 10001:10001 /opt/resume_builderV3/data /opt/resume_builderV3/inbox
   sudo chmod 700 /opt/resume_builderV3/data /opt/resume_builderV3/inbox
   chmod 600 /opt/resume_builderV3/.env
   docker compose build
   docker compose run --rm api resume-builder init-db
   docker compose up -d
   docker compose ps
   ```

4. Confirm `/health`, import the example, inspect jobs/artifacts with mock mode,
   then configure actual evidence/provider and test a reviewed real job.
5. API stays bound to VPS loopback port 8000 and registers only `/resume/` with
  the existing shared HTTPS reverse proxy (see the shared-host instructions
  above). Keep bearer authentication enabled, verify TLS and the 401 check,
  and apply appropriate request-size/rate limits at the proxy. Existing DNS,
  proxy/certificate infrastructure and application secrets remain prerequisites.

Containers use a read-only root filesystem, dropped capabilities, no-new-privileges,
and read-only prompt/asset mounts. Never commit `.env`, inbox archives, database
dumps, or generated documents. Back up PostgreSQL, retained input, and output
storage; configure retention and disk monitoring. Default mock generation is for
safe end-to-end verification, not automatic submission of job applications.
