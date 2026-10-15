#!/usr/bin/env bash
set -euo pipefail
umask 022
export GIT_TERMINAL_PROMPT=0
export GIT_SSH_COMMAND='ssh -o BatchMode=yes -o StrictHostKeyChecking=yes'
export GIT_CONFIG_NOSYSTEM=1

die() { printf 'Deployment refused: %s\n' "$*" >&2; exit 1; }
quiet() { "$@" >/dev/null 2>&1 || die "$failure"; }

sha="${1:-}"
branch="${2:-main}"
[[ "$sha" =~ ^[0-9a-f]{40}$ ]] || die 'Provide the full tested commit SHA.'
[[ "$branch" =~ ^[a-zA-Z0-9._/-]+$ ]] || die 'Invalid default branch.'
for command in git docker flock timeout; do
    command -v "$command" >/dev/null || die "Install required command: $command."
done
git check-ref-format "refs/heads/$branch" >/dev/null || die 'Invalid default branch.'
failure='Docker daemon and Compose v2 must be available without interactive sudo.'
quiet docker info
quiet docker compose version

deploy_dir="$HOME/projects/resume_builderV3"
repo='https://github.com/WanMusa/resume_builderV3.git'
[[ -d "$deploy_dir" && ! -L "$deploy_dir" ]] || die "Create $deploy_dir as a real directory."
[[ -f "$deploy_dir/.env" && -r "$deploy_dir/.env" && ! -L "$deploy_dir/.env" ]] ||
    die 'Create a readable runtime .env on the VPS; Actions never supplies it.'
exec 9>"$HOME/projects/.resume_builderV3.deploy.lock"
flock -n 9 || die 'Another deployment holds the VPS lock; retry later.'
failure='Shared ai_platform network is missing; provision shared infrastructure first.'
quiet docker network inspect ai_platform
cd "$deploy_dir"
[[ "$PWD" == "$(pwd -P)" ]] || die 'Deployment path must not traverse symlinks.'

# Only these untracked runtime paths are permitted in a first-time directory.
bootstrap_contents() {
    local path
    for path in .[^.]* ..?* *; do
        [[ -e "$path" || -L "$path" ]] || continue
        case "$path" in
            .env|.git|data|inbox) ;;
            *) die 'Bootstrap directory may contain only .env, data/ and inbox/.' ;;
        esac
    done
}
if [[ ! -e .git ]]; then
    bootstrap_contents
    git init --quiet
    git remote add origin "$repo"
fi
[[ -d .git && ! -L .git ]] || die 'Expected a standalone Git checkout, not a linked worktree.'
[[ "$(git rev-parse --show-toplevel)" == "$deploy_dir" ]] || die 'Unexpected Git working tree.'
[[ "$(git remote get-url --all origin)" == "$repo" ]] || die 'Origin must be the documented public HTTPS URL.'
[[ "$(git config --get-all remote.origin.url)" == "$repo" ]] || die 'Unexpected origin configuration.'
check_runtime_paths() {
    local path
    while IFS= read -r -d '' path; do
        case "$path" in
            inbox/json/.gitkeep|inbox/csv/.gitkeep) ;;
            *) die 'Runtime paths must never be tracked (except inbox .gitkeep placeholders).' ;;
        esac
    done
}
check_runtime_paths < <(git ls-files -z -- .env data inbox)
while IFS= read -r -d '' entry; do
    case "${entry:0:1}" in
        [a-z]|S) die 'Clear assume-unchanged/skip-worktree flags and review tracked edits before deploying.' ;;
    esac
done < <(git ls-files -v -z)
if git rev-parse --verify HEAD >/dev/null 2>&1; then
    if ! git diff --quiet || ! git diff --cached --quiet; then
        die 'Back up tracked edits (including assets/prompts) before deploying.'
    fi
else
    bootstrap_contents
    [[ -z "$(git ls-files)" ]] || die 'Incomplete bootstrap has staged files.'
fi
for path in data inbox inbox/json inbox/csv; do
    [[ ! -L "$path" && ( ! -e "$path" || -d "$path" ) ]] || die 'Runtime bind paths must be real directories.'
    mkdir -p -- "$path"
done

failure='Cannot fetch the public default branch noninteractively.'
quiet timeout 120 git -c credential.helper= fetch --quiet --no-tags origin \
    "refs/heads/$branch:refs/remotes/origin/$branch"
git cat-file -e "$sha^{commit}" 2>/dev/null || die 'Tested SHA unavailable in default-branch history.'
git merge-base --is-ancestor "$sha" "refs/remotes/origin/$branch" ||
    die 'Tested SHA is not in default-branch history.'
if git rev-parse --verify HEAD >/dev/null 2>&1; then
    git merge-base --is-ancestor HEAD "$sha" ||
        die 'Stale deployment or divergent local history; refusing to downgrade or overwrite.'
fi
check_runtime_paths < <(git ls-tree -rz --name-only "$sha" -- .env data inbox)
# Git normally overwrites ignored files. Explicitly protect all local collisions.
while IFS= read -r -d '' path; do
    if [[ -e "$path" || -L "$path" ]]; then
        git ls-files --error-unmatch -- "$path" >/dev/null 2>&1 ||
            die 'Untracked/ignored file collides with the target revision; back it up first.'
    fi
done < <(git ls-tree -rz --name-only "$sha")
failure='Checkout refused; preserve local files and resolve collisions manually.'
quiet git checkout --quiet --detach --no-overwrite-ignore "$sha"
[[ "$(git rev-parse HEAD)" == "$sha" ]] || die 'Checkout did not match tested SHA.'
while IFS= read -r -d '' path; do
    case "$path" in
        */__pycache__/*) ;;
        *) die 'Untracked source/assets/prompts would enter the image; back them up outside the checkout.' ;;
    esac
done < <(git ls-files --others -z -- app assets prompts)
for path in data inbox inbox/json inbox/csv; do
    [[ -d "$path" && ! -L "$path" ]] || die 'Runtime bind paths must remain real directories.'
done

compose=(docker compose --project-name resume_builder_v3 --project-directory "$deploy_dir"
    --env-file "$deploy_dir/.env" -f "$deploy_dir/docker-compose.yml")
failure='Invalid Compose settings; review runtime .env privately.'
quiet "${compose[@]}" config --quiet
# Do not adopt containers belonging to another checkout that uses the same project name.
existing_containers="$(docker ps -aq --filter label=com.docker.compose.project=resume_builder_v3)"
for id in $existing_containers; do
    [[ "$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' "$id")" == "$deploy_dir" ]] ||
        die 'Compose project name is already owned by another directory.'
done
failure='Image build failed; running services were not stopped. Build locally to diagnose.'
quiet timeout 900 "${compose[@]}" build api worker importer
failure='Runtime directory permissions setup failed; no services were stopped. Check this app runtime paths.'
quiet timeout 60 "${compose[@]}" run --rm --no-deps -T --user "0:$(id -g)" \
    --cap-add CHOWN --cap-add FOWNER --cap-add DAC_READ_SEARCH --entrypoint python api -c '
import os
import sys

group = int(sys.argv[1])
for path in ("/app/data", "/app/inbox", "/app/inbox/json", "/app/inbox/csv"):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fchown(descriptor, 10001, group)
        os.fchmod(descriptor, 0o2770)
    finally:
        os.close(descriptor)
' "$(id -g)"
failure='Deployment preflight failed: check auth/provider/storage settings, dedicated PostgreSQL role, assets/prompts and UID 10001 bind permissions. No services were stopped.'
quiet timeout 90 "${compose[@]}" run --rm --no-deps -T api timeout 60 python -m app.deployment

# The CLI has no drain handler. SIGINT reaches its KeyboardInterrupt handler;
# allow a bounded grace period, then rely on lease/checkpoint recovery.
failure='Could not stop app workers/importer; inspect this project privately.'
quiet timeout 330 "${compose[@]}" stop --timeout 300 worker importer
failure='App schema bootstrap failed; workers remain stopped. Diagnose privately; no automatic rollback.'
quiet timeout 90 "${compose[@]}" run --rm --no-deps -T api timeout 60 resume-builder init-db
failure='Application startup failed; inspect this Compose project privately. No automatic rollback.'
quiet timeout 180 "${compose[@]}" up -d --no-deps --force-recreate --wait --wait-timeout 120 api worker importer

deadline=$((SECONDS + 90))
while (( SECONDS < deadline )); do
    healthy=true
    for service in api worker importer; do
        id="$("${compose[@]}" ps -q "$service")"
        [[ -n "$id" ]] || { healthy=false; break; }
        [[ "$(docker inspect -f '{{.State.Status}} {{.RestartCount}}' "$id")" == 'running 0' ]] ||
            { healthy=false; break; }
    done
    if [[ "$healthy" == true ]] &&
        timeout 20 "${compose[@]}" exec -T api timeout 15 python -m app.deployment --ready >/dev/null 2>&1 &&
        timeout 10 "${compose[@]}" exec -T api python -c \
            "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)" >/dev/null 2>&1; then
        # Require sustained running status, not a single lucky instant before a crash.
        if [[ "${stable_since:-0}" == 0 ]]; then stable_since=$SECONDS; fi
        if (( SECONDS - stable_since >= 15 )); then
            printf 'Deployment healthy at tested revision %s\n' "$sha"
            exit 0
        fi
    else
        stable_since=0
    fi
    sleep 5
done
die 'Readiness timed out: API, worker, importer and database must all be ready. Diagnose privately; no automatic rollback.'
