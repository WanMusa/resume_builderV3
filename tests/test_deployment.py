import os
import shutil
import subprocess
from pathlib import Path

import pytest

from app import deployment
from app.config import Settings


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/deploy.sh"
ORIGIN = "https://github.com/WanMusa/resume_builderV3.git"


def git(directory, *args):
    return subprocess.check_output(["git", "-C", str(directory), *args], text=True).strip()


@pytest.fixture
def server(tmp_path):
    """Real local Git history, fake Docker/clock; never contacts a VPS or provider."""
    remote = tmp_path / "remote"
    remote.mkdir()
    git(remote, "init", "-b", "main")
    git(remote, "config", "user.email", "test@example.invalid")
    git(remote, "config", "user.name", "Deployment test")
    (remote / "app").mkdir()
    (remote / "app/version.py").write_text("version = 1\n")
    for name in ("json", "csv"):
        folder = remote / "inbox" / name
        folder.mkdir(parents=True)
        (folder / ".gitkeep").touch()
    (remote / ".gitignore").write_text(".env\ndata/\ninbox/*/*\n!inbox/*/.gitkeep\n")
    (remote / "docker-compose.yml").write_text("services: {}\n")
    git(remote, "add", ".")
    git(remote, "commit", "-qm", "first")
    first = git(remote, "rev-parse", "HEAD")
    (remote / "app/version.py").write_text("version = 2\n")
    (remote / "app/new.py").write_text("new = True\n")
    git(remote, "add", ".")
    git(remote, "commit", "-qm", "second")
    second = git(remote, "rev-parse", "HEAD")

    home = tmp_path / "home"
    checkout = home / "projects/resume_builderV3"
    checkout.mkdir(parents=True)
    (checkout / ".env").write_text("PRIVATE_SETTING=sentinel-not-a-real-secret\n")
    for path in ("data", "inbox/json", "inbox/csv", "inbox/json/archive", "inbox/json/quarantine"):
        (checkout / path).mkdir(parents=True, exist_ok=True)
        (checkout / path / "preserve.txt").write_text("keep me")
    gitconfig = home / ".gitconfig"
    gitconfig.write_text(f'[url "{remote}"]\n\tinsteadOf = {ORIGIN}\n')
    # remote get-url expands insteadOf; the wrapper only hides that expansion
    # from the origin assertion, while all actual fetches remain local.
    binaries = tmp_path / "bin"
    binaries.mkdir()
    real_git = shutil.which("git")
    (binaries / "git").write_text(
        f'#!/bin/bash\nif [[ "$*" == "remote get-url --all origin" ]]; then\n'
        f'  echo "{ORIGIN}"\nelse\n  exec "{real_git}" "$@"\nfi\n'
    )
    (binaries / "docker").write_text("""#!/bin/bash
printf '%s\\n' "$*" >> "$DOCKER_CALLS"
if [[ "$*" == *"$FAIL_DOCKER"* && -n "$FAIL_DOCKER" ]]; then
    echo "sentinel-not-a-real-secret" >&2
    exit 1
fi
if [[ "$1" == inspect ]]; then
    echo "${CONTAINER_STATE:-running 0}"
elif [[ "$*" == *" ps -q "* ]]; then
    echo container-id
fi
""")
    (binaries / "sleep").write_text("#!/bin/bash\nexit 0\n")
    # Keep real timeout bounds, but speed up the shell's sustained-health clock.
    script = tmp_path / "deploy.sh"
    script.write_text(SCRIPT.read_text()
                      .replace("SECONDS - stable_since >= 15", "SECONDS - stable_since >= 0")
                      .replace("deadline=$((SECONDS + 90))", "deadline=$((SECONDS + 1))"))
    for path in binaries.iterdir():
        path.chmod(0o755)
    env = {**os.environ, "HOME": str(home), "PATH": f"{binaries}:{os.environ['PATH']}",
           "GIT_CONFIG_GLOBAL": str(gitconfig), "DOCKER_CALLS": str(tmp_path / "docker-calls"),
           "FAIL_DOCKER": ""}

    def deploy(sha=second, failure="", state="running 0"):
        return subprocess.run(
            ["bash", str(script), sha, "main"],
            env={**env, "FAIL_DOCKER": failure, "CONTAINER_STATE": state},
            text=True, capture_output=True, timeout=15,
        )

    return checkout, remote, first, second, deploy, Path(env["DOCKER_CALLS"])


def test_bootstrap_exact_sha_and_preserve_runtime(server):
    checkout, _, first, second, deploy, calls = server
    result = deploy(first)
    assert result.returncode == 0, result.stderr
    assert git(checkout, "rev-parse", "HEAD") == first  # remote main is already newer
    assert "sentinel-not-a-real-secret" in (checkout / ".env").read_text()
    assert all(path.read_text() == "keep me" for path in checkout.rglob("preserve.txt"))
    assert "stop --timeout 300 worker importer" in calls.read_text()
    assert "resume-builder init-db" in calls.read_text()
    assert calls.read_text().index("build api") < calls.read_text().index("stop --timeout")
    assert calls.read_text().index("init-db") < calls.read_text().index("up -d")
    assert deploy(second).returncode == 0
    assert deploy(second).returncode == 0  # reruns of the same revision are safe
    stale = deploy(first)
    assert stale.returncode != 0
    assert "Stale deployment" in stale.stderr
    assert git(checkout, "rev-parse", "HEAD") == second


@pytest.mark.parametrize("failure", ["network inspect", "build api", "python -m app.deployment"])
def test_preflight_failure_keeps_services_running_and_hides_output(server, failure):
    _, _, _, _, deploy, calls = server
    result = deploy(failure=failure)
    assert result.returncode != 0
    assert "sentinel-not-a-real-secret" not in result.stdout + result.stderr
    assert "stop --timeout" not in calls.read_text()
    assert "up -d" not in calls.read_text()


@pytest.mark.parametrize("failure", ["resume-builder init-db", "up -d", "exec -T api"])
def test_failed_bootstrap_start_or_readiness_never_reports_success(server, failure):
    _, _, _, _, deploy, _ = server
    result = deploy(failure=failure)
    assert result.returncode != 0
    assert "Deployment healthy" not in result.stdout


def test_bootstrap_rejects_unrelated_files_and_handles_no_head_retry(server):
    checkout, _, _, _, deploy, _ = server
    (checkout / "unrelated.txt").write_text("preserve")
    assert deploy().returncode != 0
    assert not (checkout / ".git").exists()
    (checkout / "unrelated.txt").unlink()
    git(checkout, "init")
    git(checkout, "remote", "add", "origin", ORIGIN)
    assert deploy().returncode == 0


def test_dirty_tracked_collision_and_divergence_refused(server):
    checkout, _, first, second, deploy, _ = server
    assert deploy(first).returncode == 0
    (checkout / "app/version.py").write_text("local edit")
    assert "tracked edits" in deploy(second).stderr
    assert (checkout / "app/version.py").read_text() == "local edit"
    git(checkout, "checkout", "--", "app/version.py")
    (checkout / "app/untracked.py").write_text("local addition")
    assert "Untracked source" in deploy(second).stderr
    (checkout / "app/untracked.py").unlink()
    git(checkout, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
        "commit", "--allow-empty", "-qm", "local divergence")
    assert "divergent" in deploy(second).stderr


def test_missing_env_and_wrong_origin_refused(server):
    checkout, _, first, _, deploy, calls = server
    (checkout / ".env").unlink()
    assert "readable runtime .env" in deploy().stderr
    assert "build api" not in calls.read_text()
    assert "stop --timeout" not in calls.read_text()
    (checkout / ".env").touch()
    assert deploy(first).returncode == 0
    git(checkout, "config", "remote.origin.url", "https://example.invalid/other.git")
    assert "origin configuration" in deploy().stderr


def test_untracked_target_collision_preserved(server):
    checkout, _, first, second, deploy, _ = server
    assert deploy(first).returncode == 0
    (checkout / "app/new.py").write_text("local private source")
    result = deploy(second)
    assert result.returncode != 0
    assert "collides" in result.stderr
    assert (checkout / "app/new.py").read_text() == "local private source"
    assert git(checkout, "rev-parse", "HEAD") == first


@pytest.mark.parametrize("state", ["exited 0", "running 1", "restarting 2"])
def test_service_health_requires_all_running_without_restarts(server, state):
    _, _, _, _, deploy, _ = server
    result = deploy(state=state)
    assert result.returncode != 0
    assert "Deployment healthy" not in result.stdout


def test_configuration_production_guards():
    settings = Settings(_env_file=None, api_token="fixture-token", database_password="fixture-password",
                        data_dir="/app/data", inbox_dir="/app/inbox/json",
                        assets_dir="/app/assets", prompts_dir="/app/prompts")
    deployment.check_configuration(settings)
    for changes in ({"api_token": None}, {"allow_unauthenticated": True},
                    {"database_url": "sqlite://"}, {"database_password": None},
                    {"provider": "openai"}, {"storage_backend": "supabase"},
                    {"data_dir": Path("/tmp/unpersisted")}):
        with pytest.raises(ValueError):
            deployment.check_configuration(settings.model_copy(update=changes))


def test_secret_free_check_failure(monkeypatch, capsys):
    def fail():
        raise ValueError("PRIVATE_SETTING=sentinel-not-a-real-secret")

    monkeypatch.setattr(deployment, "Settings", fail)
    assert deployment.main() == 1
    assert "sentinel-not-a-real-secret" not in capsys.readouterr().err


def test_files_checked_without_generation(tmp_path):
    settings = Settings(_env_file=None, assets_dir=ROOT / "assets", prompts_dir=ROOT / "prompts",
                        data_dir=tmp_path / "data", inbox_dir=tmp_path / "inbox/json")
    settings.data_dir.mkdir()
    settings.inbox_dir.mkdir(parents=True)
    deployment.check_files(settings)
    (settings.data_dir / "unsafe").symlink_to(tmp_path)
    with pytest.raises(ValueError, match="symlinks"):
        deployment.check_files(settings)


def test_workflow_guards_and_shell_syntax():
    workflow = (ROOT / ".github/workflows/deploy.yml").read_text()
    assert "needs: ci" in workflow
    assert "github.event_name != 'pull_request'" in workflow
    assert "github.event.repository.default_branch" in workflow
    assert "ref: ${{ github.sha }}" in workflow
    assert "StrictHostKeyChecking=yes" in workflow
    assert "cancel-in-progress: false" in workflow
    assert "contents: read" in workflow
    assert "ssh-keyscan" not in workflow
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)
