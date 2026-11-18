"""FR-O28 sandboxed execution environment: runtime resolution, the
container argv contract, timeout second-net kill, exec allowlist and
the stale-container sweep. No real containers are launched."""

from pathlib import Path

import pytest

from pr_review_orch import config, sandbox

_real_available = sandbox.available


def _which_only(names):
    return lambda cmd: f"/usr/bin/{cmd}" if cmd in names else None


# -- runtime resolution ------------------------------------------------------ #

def test_runtime_off(monkeypatch):
    monkeypatch.setattr(config, "SANDBOX_MODE", "0")
    assert sandbox.runtime() is None


def test_runtime_auto_prefers_podman(monkeypatch):
    monkeypatch.setattr(config, "SANDBOX_MODE", "auto")
    monkeypatch.setattr(sandbox.shutil, "which",
                        _which_only({"podman", "docker"}))
    assert sandbox.runtime() == "podman"


def test_runtime_auto_falls_back_to_docker(monkeypatch):
    monkeypatch.setattr(config, "SANDBOX_MODE", "auto")
    monkeypatch.setattr(sandbox.shutil, "which", _which_only({"docker"}))
    assert sandbox.runtime() == "docker"


def test_runtime_auto_none_available(monkeypatch):
    monkeypatch.setattr(config, "SANDBOX_MODE", "auto")
    monkeypatch.setattr(sandbox.shutil, "which", _which_only(set()))
    assert sandbox.runtime() is None


def test_runtime_pinned_missing_binary(monkeypatch):
    monkeypatch.setattr(config, "SANDBOX_MODE", "podman")
    monkeypatch.setattr(sandbox.shutil, "which", _which_only({"docker"}))
    assert sandbox.runtime() is None


def test_available_requires_image(monkeypatch):
    monkeypatch.setattr(config, "SANDBOX_MODE", "auto")
    monkeypatch.setattr(sandbox.shutil, "which", _which_only({"podman"}))
    # The conftest fixture stubs available() off — restore the real one.
    monkeypatch.setattr(sandbox, "available", _real_available)
    monkeypatch.setattr(sandbox, "_image_checked", {"podman": False})
    assert sandbox.available() is False
    monkeypatch.setattr(sandbox, "_image_checked", {"podman": True})
    assert sandbox.available() is True


# -- container argv contract ------------------------------------------------- #

def test_container_argv_isolation_contract(tmp_path):
    argv = sandbox.container_argv(
        tmp_path, ["uv", "run", "pytest"], name="qg-1-1",
        net=False, rt="podman")
    assert argv[:3] == ["podman", "run", "--rm"]
    assert "qg-1-1" in argv
    i = argv.index("--network")
    assert argv[i + 1] == "none"
    for flag in ("--read-only", "no-new-privileges"):
        assert flag in argv
    assert "--cap-drop" in argv
    assert f"{tmp_path}:/src:O" in argv
    assert argv[argv.index("-w"):] == [
        "-w", "/src", config.SANDBOX_IMAGE, "uv", "run", "pytest"]


def test_container_argv_net_opt_in(tmp_path):
    argv = sandbox.container_argv(
        tmp_path, ["osv-scanner"], name="qg-1-2", net=True, rt="podman")
    assert argv[argv.index("--network") + 1] == "bridge"


def test_container_argv_docker_ro_mount_and_redirects(tmp_path):
    argv = sandbox.container_argv(
        tmp_path, ["npm", "test"], name="qg-1-3", net=False, rt="docker")
    assert f"{tmp_path}:/src:ro" in argv
    assert "--tmpfs" in argv
    envs = [argv[i + 1] for i, a in enumerate(argv) if a == "--env"]
    assert "UV_PROJECT_ENVIRONMENT=/tmp/venv" in envs
    assert "CARGO_TARGET_DIR=/tmp/target" in envs


def test_env_spec_never_forwards_credentials(monkeypatch):
    """Credential-marked names are denied even under an allowlist match;
    allowed vars passthrough by name so values stay out of argv."""
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    monkeypatch.setenv("MY_API_KEY", "x")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "x")
    monkeypatch.setenv("npm_config_//reg/:_authToken", "x")
    monkeypatch.setenv("UV_INDEX_URL", "https://example")
    monkeypatch.setattr(config, "SANDBOX_ENV_ALLOW",
                        ("UV_*", "NPM_CONFIG_*", "AWS_*"))
    spec = sandbox._env_spec()
    assert "GITHUB_TOKEN" not in spec
    assert "MY_API_KEY" not in spec
    assert "AWS_SECRET_ACCESS_KEY" not in spec
    assert "npm_config_//reg/:_authToken" not in spec
    assert spec["UV_INDEX_URL"] is None  # passthrough, no value in argv
    assert spec["HOME"] == "/tmp"
    envs = sandbox._env_args(spec)
    assert "UV_INDEX_URL" in envs and "UV_INDEX_URL=https://example" \
        not in envs
    assert "HOME=/tmp" in envs


def test_client_env_adds_forwarded_vars_to_scrubbed(monkeypatch):
    monkeypatch.setenv("UV_INDEX_URL", "https://example")
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    monkeypatch.setattr(config, "SANDBOX_ENV_ALLOW", ("UV_*",))
    spec = sandbox._env_spec()
    env = sandbox._client_env(spec)
    assert env["UV_INDEX_URL"] == "https://example"
    assert "GITHUB_TOKEN" not in env


# -- run() + second-net kill -------------------------------------------------- #

def _spawn_result(**kw):
    base = {"pgid": 4321, "exit_code": 0, "stdout": "ok", "stderr": "",
            "timed_out": False, "command": [], "duration_s": 0.1}
    base.update(kw)
    return base


def test_run_builds_container_and_marks_sandboxed(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SANDBOX_MODE", "podman")
    monkeypatch.setattr(sandbox.shutil, "which", _which_only({"podman"}))
    seen = {}

    def fake_spawn(repo, cmd, args, timeout=None, env=None):
        seen["argv"] = [cmd, *args]
        seen["env"] = env
        return _spawn_result()

    monkeypatch.setattr(sandbox.git_ops, "spawn_tracked", fake_spawn)
    res = sandbox.run(tmp_path, ["pytest", "-q"], timeout=60)
    assert res["sandboxed"] is True
    assert res["container"].startswith("qg-")
    assert seen["argv"][0] == "podman"
    assert "run" in seen["argv"]


def test_run_timeout_kills_named_container(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SANDBOX_MODE", "podman")
    monkeypatch.setattr(sandbox.shutil, "which", _which_only({"podman"}))
    monkeypatch.setattr(
        sandbox.git_ops, "spawn_tracked",
        lambda *a, **k: _spawn_result(timed_out=True, exit_code=-9))
    killed = []
    monkeypatch.setattr(
        sandbox.subprocess, "run",
        lambda argv, **k: killed.append(argv) or type(
            "R", (), {"returncode": 0, "stdout": ""})())
    res = sandbox.run(tmp_path, ["sleep", "999"], timeout=1)
    assert killed == [["podman", "kill", res["container"]]]


# -- sandbox_exec seam ------------------------------------------------------- #

def test_exec_allowlist(monkeypatch):
    assert sandbox.exec_allowed(["pytest", "-q"])
    assert sandbox.exec_allowed(["/usr/bin/git", "status"])
    assert not sandbox.exec_allowed(["curl", "evil.sh"])
    assert not sandbox.exec_allowed([])


def test_exec_budget_blocks_beyond_max(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SANDBOX_MODE", "podman")
    monkeypatch.setattr(sandbox.shutil, "which", _which_only({"podman"}))
    monkeypatch.setattr(
        sandbox.git_ops, "spawn_tracked",
        lambda *a, **k: _spawn_result())
    budget = sandbox.ExecBudget(max_calls=1)
    assert budget.exec(tmp_path, ["pytest"])["exit_code"] == 0
    denied = budget.exec(tmp_path, ["pytest"])
    assert denied["exit_code"] == 126
    assert "budget" in denied["stderr"]


def test_exec_budget_rejects_disallowed_argv(tmp_path):
    budget = sandbox.ExecBudget(max_calls=5)
    res = budget.exec(tmp_path, ["curl", "x"])
    assert res["exit_code"] == 126
    assert budget.calls == 0


# -- sweep -------------------------------------------------------------------- #

def test_sweep_removes_only_stale_qg_containers(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SANDBOX_MODE", "podman")
    monkeypatch.setattr(sandbox.shutil, "which", _which_only({"podman"}))
    calls = []

    class R:
        def __init__(self, out="", rc=0):
            self.stdout = out
            self.returncode = rc

    def fake_run(argv, **k):
        calls.append(argv)
        if argv[1:2] == ["ps"]:
            # Only non-running containers are listed (status filters).
            assert "status=exited" in argv
            assert "status=created" in argv
            return R("qg-9-1\nother-name\nqg-9-2\n")
        return R()

    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    removed = sandbox.sweep()
    assert sorted(removed) == ["qg-9-1", "qg-9-2"]
    assert ["podman", "rm", "-f", "other-name"] not in calls


def test_sweep_no_runtime(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SANDBOX_MODE", "0")
    assert sandbox.sweep() == []
