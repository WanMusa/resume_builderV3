import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from app import deployment
from app.config import Settings
from app.provision import bootstrap_sql


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
    (remote / "scripts").mkdir()
    shutil.copyfile(ROOT / "scripts/configure_runtime.py", remote / "scripts/configure_runtime.py")
    shutil.copyfile(ROOT / ".env.example", remote / ".env.example")
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
        '#!/bin/bash\n'
        'if [[ "$1" == checkout && "${TEST_FAIL_CHECKOUT:-}" == 1 ]]; then exit 1; fi\n'
        'if [[ "$*" == "remote get-url --all origin" ]]; then\n'
        f'  echo "{ORIGIN}"\nelse\n  exec "{real_git}" "$@"\nfi\n'
    )
    (binaries / "docker").write_text("""#!/bin/bash
printf '%s\\n' "$*" >> "$DOCKER_CALLS"
if [[ "$*" == *"$FAIL_DOCKER"* && -n "$FAIL_DOCKER" ]]; then
    echo "sentinel-not-a-real-secret" >&2
    exit 1
fi
if [[ ( "$*" == *" run "* || "$*" == *" exec -T "* ) && "${DRAIN_DOCKER_STDIN:-}" == 1 ]]; then
    cat >/dev/null
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

    def deploy(sha=second, failure="", state="running 0", drain_stdin=False, secrets=None):
        return subprocess.run(
            ["bash", "-s", "--", sha, "main"],
            input=script.read_text(),
            env={**env, "FAIL_DOCKER": failure, "CONTAINER_STATE": state,
                 "DRAIN_DOCKER_STDIN": "1" if drain_stdin else "0", **(secrets or {})},
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
    assert calls.read_text().index("build api") < calls.read_text().index("--user 0:")
    assert calls.read_text().index("--user 0:") < calls.read_text().index("python -m app.deployment")
    maintenance = [line for line in calls.read_text().splitlines() if " run --rm " in line]
    assert len(maintenance) == 4
    assert all("-e VIRTUAL_HOST= -e LETSENCRYPT_HOST=" in command for command in maintenance)
    assert calls.read_text().index("init-db") < calls.read_text().index("up -d")
    assert calls.read_text().index("app.provision") < calls.read_text().index("app.deployment")
    assert "psql -X" in calls.read_text()
    assert deploy(second).returncode == 0
    assert deploy(second).returncode == 0  # reruns of the same revision are safe
    stale = deploy(first)
    assert stale.returncode != 0
    assert "Stale deployment" in stale.stderr
    assert git(checkout, "rev-parse", "HEAD") == second


def test_streamed_script_reaches_readiness_when_docker_reads_stdin(server):
    _, _, _, second, deploy, calls = server
    result = deploy(drain_stdin=True)
    assert result.returncode == 0, result.stderr
    assert f"Deployment healthy at tested revision {second}" in result.stdout
    assert "resume-builder init-db" in calls.read_text()
    assert "up -d --no-deps --force-recreate" in calls.read_text()


def test_runtime_directories_created_and_permission_helper_is_scoped(server):
    checkout, _, _, _, deploy, calls = server
    shutil.rmtree(checkout / "data")
    shutil.rmtree(checkout / "inbox")
    result = deploy()
    assert result.returncode == 0, result.stderr
    for path in ("data", "inbox", "inbox/json", "inbox/csv"):
        assert (checkout / path).is_dir()
    commands = calls.read_text()
    assert "--cap-add CHOWN --cap-add FOWNER --cap-add DAC_READ_SEARCH" in commands
    assert "--entrypoint python api" in commands
    assert "os.fchown(descriptor, 10001, group)" in commands
    assert "os.fchmod(descriptor, 0o2770)" in commands
    assert "os.O_NOFOLLOW" in commands
    assert "chown -R" not in commands
    assert "chmod -R" not in commands


@pytest.mark.parametrize("path", ["data", "inbox", "inbox/json", "inbox/csv"])
def test_runtime_symlinks_refused_before_permission_changes(server, path):
    checkout, _, _, _, deploy, calls = server
    target = checkout.parent / "external"
    target.mkdir()
    (target / "preserve.txt").write_text("keep me")
    shutil.rmtree(checkout / path)
    (checkout / path).symlink_to(target, target_is_directory=True)
    result = deploy()
    assert result.returncode != 0
    assert "real directories" in result.stderr
    assert (target / "preserve.txt").read_text() == "keep me"
    assert "--user 0:" not in calls.read_text()


def test_first_deployment_creates_checkout_and_env_from_secrets(server):
    checkout, _, _, _, deploy, calls = server
    shutil.rmtree(checkout)
    result = deploy(secrets={"DEPLOY_DATABASE_PASSWORD": "fixture-db",
                             "DEPLOY_API_TOKEN": "fixture-token",
                             "DEPLOY_OPENAI_API_KEY": "fixture-key"})
    assert result.returncode == 0, result.stderr
    assert checkout.is_dir()
    settings = Settings(_env_file=checkout / ".env")
    assert settings.database_password == "fixture-db"
    assert settings.api_token == "fixture-token"
    assert settings.openai_api_key == "fixture-key"
    assert "fixture-key" not in result.stdout + result.stderr + calls.read_text()


@pytest.mark.parametrize("failure", ["network inspect", "build api", "--user 0:",
                                    "python -m app.provision", "exec -i",
                                    "python -m app.deployment"])
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


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
def test_hidden_tracked_edits_refused(server, flag):
    checkout, _, first, second, deploy, _ = server
    assert deploy(first).returncode == 0
    git(checkout, "update-index", flag, "app/version.py")
    (checkout / "app/version.py").write_text("hidden local edit")
    assert "review tracked edits" in deploy(second).stderr
    assert (checkout / "app/version.py").read_text() == "hidden local edit"


def test_missing_env_and_wrong_origin_refused(server):
    checkout, _, _, second, deploy, calls = server
    (checkout / ".env").unlink()
    assert "Runtime settings update failed" in deploy().stderr
    assert "build api" not in calls.read_text()
    assert "stop --timeout" not in calls.read_text()
    (checkout / ".env").touch()
    assert deploy(second).returncode == 0
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
                        supabase_url="https://storage.example", supabase_key="fixture-key",
                        supabase_bucket="generated-documents",
                        data_dir="/app/data", inbox_dir="/app/inbox/json",
                        assets_dir="/app/assets", prompts_dir="/app/prompts")
    deployment.check_configuration(settings)
    for changes in ({"api_token": None}, {"allow_unauthenticated": True},
                    {"database_url": "sqlite://"}, {"database_password": None},
                    {"storage_backend": "local"}, {"supabase_key": None},
                    {"data_dir": Path("/tmp/unpersisted")}):
        with pytest.raises(ValueError):
            deployment.check_configuration(settings.model_copy(update=changes))
    deployment.check_configuration(settings.model_copy(update={"provider": "openai",
                                                               "openai_api_key": None}))


def test_secret_free_check_failure(monkeypatch, capsys):
    def fail():
        raise ValueError("PRIVATE_SETTING=sentinel-not-a-real-secret")

    monkeypatch.setattr(deployment, "Settings", fail)
    assert deployment.main() == 1
    assert "sentinel-not-a-real-secret" not in capsys.readouterr().err


def test_files_checked_without_generation(tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    shutil.copyfile(ROOT / "assets/base_resume_example.json", assets / "base_resume.json")
    shutil.copytree(ROOT / "assets/templates", assets / "templates")
    settings = Settings(_env_file=None, assets_dir=assets, prompts_dir=ROOT / "prompts",
                        data_dir=tmp_path / "data", inbox_dir=tmp_path / "inbox/json")
    settings.data_dir.mkdir()
    settings.inbox_dir.mkdir(parents=True)
    deployment.check_files(settings)
    (settings.data_dir / "unsafe").symlink_to(tmp_path)
    with pytest.raises(ValueError, match="symlinks"):
        deployment.check_files(settings)


def test_runtime_files_need_read_not_write_access(tmp_path, monkeypatch):
    assets = tmp_path / "assets"
    assets.mkdir()
    shutil.copyfile(ROOT / "assets/base_resume_example.json", assets / "base_resume.json")
    shutil.copytree(ROOT / "assets/templates", assets / "templates")
    settings = Settings(_env_file=None, assets_dir=assets, prompts_dir=ROOT / "prompts",
                        data_dir=tmp_path / "data", inbox_dir=tmp_path / "inbox/json")
    settings.data_dir.mkdir()
    settings.inbox_dir.mkdir(parents=True)
    for name in (".gitkeep", "uploaded.json"):
        (settings.inbox_dir / name).touch()
    real_access = os.access

    def container_access(path, mode):
        if Path(path).is_file():
            return not mode & os.W_OK
        return real_access(path, mode)

    monkeypatch.setattr(deployment.os, "access", container_access)
    deployment.check_files(settings)
    monkeypatch.setattr(deployment.os, "access", lambda path, mode: False)
    with pytest.raises(ValueError):
        deployment.check_files(settings)


def test_private_resume_untracking_preserves_uploaded_bytes(server):
    checkout, remote, _, second, deploy, _ = server
    assert deploy(second).returncode == 0
    assets = remote / "assets"
    assets.mkdir()
    (assets / "base_resume.json").write_text('{"original": true}\n')
    git(remote, "add", "assets/base_resume.json")
    git(remote, "commit", "-qm", "legacy tracked resume")
    legacy = git(remote, "rev-parse", "HEAD")
    assert deploy(legacy).returncode == 0
    private = checkout / "assets/base_resume.json"
    uploaded = b'{"private": "retain every byte"}\r\n'
    private.write_bytes(uploaded)
    git(remote, "rm", "--cached", "assets/base_resume.json")
    with (remote / ".gitignore").open("a") as stream:
        stream.write("assets/base_resume.json\n")
    git(remote, "add", ".gitignore")
    git(remote, "commit", "-qm", "private runtime resume")
    target = git(remote, "rev-parse", "HEAD")
    (checkout / "app/version.py").write_text("unrelated edit")
    assert "tracked edits" in deploy(target).stderr
    assert private.read_bytes() == uploaded
    git(checkout, "restore", "app/version.py")
    failed = deploy(target, secrets={"TEST_FAIL_CHECKOUT": "1"})
    assert "Checkout refused" in failed.stderr
    assert private.read_bytes() == uploaded
    assert not list(checkout.parent.glob(".resume-builder-resume.*"))
    result = deploy(target)
    assert result.returncode == 0, result.stderr
    assert private.read_bytes() == uploaded
    assert git(checkout, "ls-files", "--", "assets/base_resume.json") == ""
    assert git(checkout, "status", "--porcelain") == ""
    assert deploy(target).returncode == 0
    assert private.read_bytes() == uploaded


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
    for key in ("DATABASE_PASSWORD", "OPENAI_API_KEY", "API_TOKEN", "JOB_RUNS_API_TOKEN", "DB_USER"):
        assert f"secrets.{key}" in workflow
    assert "CORE_TEST_DATABASE_URL" in workflow
    assert "PROVISION_TEST_CONTAINER" in workflow
    assert "resume-builder init-db\n          resume-builder init-db" in workflow
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)


def runtime_module():
    spec = importlib.util.spec_from_file_location("configure_runtime", ROOT / "scripts/configure_runtime.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_runtime_secrets_merge_preserves_settings(tmp_path):
    runtime = runtime_module()
    target = tmp_path / ".env"
    target.write_text("# private runtime\nPROVIDER=openai\nDATABASE_PASSWORD=old\nCUSTOM=value\n")
    runtime.configure(tmp_path, {"DEPLOY_DATABASE_PASSWORD": "new-password",
                                "DEPLOY_OPENAI_API_KEY": "fixture-key",
                                "DEPLOY_API_TOKEN": "fixture-token",
                                "DEPLOY_JOB_RUNS_API_TOKEN": "runs-fixture-token"})
    settings = Settings(_env_file=target)
    assert settings.database_password == "new-password"
    assert settings.openai_api_key == "fixture-key"
    assert settings.api_token == "fixture-token"
    assert settings.job_runs_api_token == "runs-fixture-token"
    assert settings.provider == "openai"
    assert "CUSTOM=value" in target.read_text()
    runtime.configure(tmp_path, {})
    assert Settings(_env_file=target).openai_api_key == "fixture-key"
    assert Settings(_env_file=target).job_runs_api_token == "runs-fixture-token"
    if os.name != "nt":
        assert target.stat().st_mode & 0o777 == 0o600


def test_runtime_secret_invalid_input_does_not_overwrite(tmp_path):
    runtime = runtime_module()
    target = tmp_path / ".env"
    target.write_text("API_TOKEN=original\n")
    with pytest.raises(ValueError):
        runtime.configure(tmp_path, {"DEPLOY_API_TOKEN": "first\nSECOND=value"})
    assert target.read_text() == "API_TOKEN=original\n"


def test_runtime_supabase_upgrade_and_v2_secret_alias(tmp_path):
    target = tmp_path / ".env"
    target.write_text("STORAGE_BACKEND=local\nPROVIDER=openai\nSUPABASE_BUCKET=private-docs\n")
    runtime_module().configure(tmp_path, {
        "DEPLOY_SUPABASE_URL": "https://storage.example",
        "DEPLOY_SUPABASE_SECRET_KEY": "v2-server-credential",
    })
    settings = Settings(_env_file=target)
    assert settings.storage_backend == "supabase"
    assert settings.supabase_url == "https://storage.example"
    assert settings.supabase_key == "v2-server-credential"
    assert settings.supabase_bucket == "private-docs"
    runtime_module().configure(tmp_path, {"DEPLOY_SUPABASE_SECRET_KEY": "alias",
                                        "DEPLOY_SUPABASE_KEY": "preferred",
                                        "DEPLOY_SUPABASE_BUCKET": "new-private-docs"})
    settings = Settings(_env_file=target)
    assert settings.supabase_key == "preferred"
    assert settings.supabase_bucket == "new-private-docs"


@pytest.mark.parametrize("password", [
    "fixture ${UNSET_TEST_VARIABLE} $dollar 'quote' \\path #hash",
    "literal\\nnot-a-newline",
    "backslash\\'quote",
    "ends-with-backslash\\",
    'double"quote',
])
def test_compose_preserves_secret_special_characters(tmp_path, password):
    if not shutil.which("docker"):
        pytest.skip("Docker Compose CLI is required to verify runtime env parsing")
    if subprocess.run(["docker", "compose", "version"], capture_output=True).returncode:
        pytest.skip("Docker Compose CLI is not usable in this environment")
    target = tmp_path / ".env"
    target.touch()
    runtime_module().configure(tmp_path, {"DEPLOY_DATABASE_PASSWORD": password})
    compose = tmp_path / "compose.yml"
    compose.write_text("services:\n  app:\n    image: example\n    env_file: .env\n")
    result = subprocess.run(
        ["docker", "compose", "--env-file", str(target), "-f", str(compose),
         "config", "--format", "json"],
        text=True, capture_output=True, check=True,
    )
    config = json.loads(result.stdout)
    # `compose config` re-escapes literal dollar signs for reuse as a Compose file.
    assert config["services"]["app"]["environment"]["DATABASE_PASSWORD"] == password.replace("$", "$$")


def test_runtime_first_deployment_requires_secrets(tmp_path):
    runtime = runtime_module()
    with pytest.raises(ValueError, match="requires"):
        runtime.configure(tmp_path, {})
    assert not (tmp_path / ".env").exists()


@pytest.mark.parametrize("url", [
    "postgresql+psycopg://postgres@postgres/resume_builder",
    "postgresql+psycopg://resume_builder@other/resume_builder",
    "postgresql+psycopg://resume_builder@postgres/gmail_sorter",
    "postgresql+psycopg://resume_builder@postgres:5433/resume_builder",
])
def test_database_bootstrap_refuses_unrelated_targets(url):
    with pytest.raises(ValueError, match="dedicated"):
        bootstrap_sql(Settings(_env_file=None, database_url=url, database_password="fixture"))


def test_database_bootstrap_escapes_password_and_limits_privileges():
    statement = bootstrap_sql(Settings(
        _env_file=None,
        database_url="postgresql+psycopg://resume_builder@postgres/resume_builder",
        database_password="test'; DROP DATABASE other; --",
    ))
    assert "PASSWORD 'test''; DROP DATABASE other; --'" in statement
    assert "NOSUPERUSER NOCREATEDB NOCREATEROLE" in statement
    assert "Refusing to adopt" in statement
    assert "CREATE DATABASE resume_builder OWNER resume_builder" in statement
    assert "DROP DATABASE" not in statement.replace("'test''; DROP DATABASE other; --'", "")


def test_database_bootstrap_requires_password():
    with pytest.raises(ValueError, match="DATABASE_PASSWORD"):
        bootstrap_sql(Settings(
            _env_file=None, database_password=None,
            database_url="postgresql+psycopg://resume_builder@postgres/resume_builder",
        ))


@pytest.mark.skipif(not os.environ.get("PROVISION_TEST_CONTAINER"),
                    reason="Requires the isolated PostgreSQL CI service")
def test_postgres_provisioning_schema_and_connectivity():
    from sqlalchemy import select
    from app.db import init_db, make_engine, make_session_factory
    from app.models import Batch

    settings = Settings(_env_file=None,
                        database_url=os.environ["CORE_TEST_DATABASE_URL"],
                        database_password=os.environ["CORE_TEST_DATABASE_PASSWORD"])
    statement = bootstrap_sql(settings.model_copy(update={
        "database_url": "postgresql+psycopg://resume_builder@postgres/resume_builder"
    }))
    container = os.environ["PROVISION_TEST_CONTAINER"]
    command = ["docker", "exec", "-i", container, "psql", "-X", "-U", "postgres",
               "-d", "postgres", "--set", "ON_ERROR_STOP=1"]
    snapshot_sql = """
        SELECT rolname, rolsuper, rolcreatedb, rolcreaterole, rolcanlogin, rolpassword
        FROM pg_authid WHERE rolname <> 'resume_builder' ORDER BY rolname;
        SELECT datname, pg_get_userbyid(datdba)
        FROM pg_database WHERE datname <> 'resume_builder' ORDER BY datname;
    """
    before = subprocess.run([*command, "-At"], input=snapshot_sql, text=True,
                            capture_output=True, check=True).stdout
    engine = make_engine(settings)
    try:
        init_db(engine)
        factory = make_session_factory(engine)
        with factory() as session:
            batch = Batch(content_hash="migration-test", source="migration-test",
                          status="accepted", errors=[])
            session.add(batch)
            session.commit()
            batch_id = batch.id
        for _ in range(2):
            subprocess.run(command, input=statement, text=True, capture_output=True, check=True)
            init_db(engine)
            deployment.check_database(settings, ready=True)
        after = subprocess.run([*command, "-At"], input=snapshot_sql, text=True,
                               capture_output=True, check=True).stdout
        assert before == after
        with factory() as session:
            batch = session.scalar(select(Batch).where(Batch.id == batch_id))
            assert batch.source == "migration-test"
            session.delete(batch)
            session.commit()
    finally:
        engine.dispose()
