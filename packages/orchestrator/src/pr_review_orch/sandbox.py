"""FR-O28 sandboxed execution environment.

Runs PR-controlled commands inside a container instead of on the host.
One isolation contract serves every consumer — QAAgent validators, the
SecurityAgent dependency audit, the functional stage and the bounded
``sandbox_exec`` seam.

Isolation per sandboxed command: ``--network none`` unless the consumer
opts into egress, read-only rootfs, the worktree bind-mounted read-only
(podman gets a copy-on-write ``:O`` overlay so tools can write build
artifacts; docker falls back to tmpfs + cache redirects), a non-root
image user, ``no-new-privileges``, ``--cap-drop ALL`` and
memory/cpu/pids caps. The in-container env is a whitelist — secrets
never enter the sandbox; the host-side client spawns under
``scrub_env`` like any other validator (FR-P11).

Every sandbox is named ``qg-*`` so the timeout kill second-net and the
stale sweep apply uniformly.
"""

from __future__ import annotations

import itertools
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from quality_gate_mcp import git_ops

from . import config

logger = logging.getLogger("pr-review-orch")

_RUNTIMES = ("podman", "docker")
_seq = itertools.count(1)
_image_checked: dict[str, bool] = {}

#: Minimal env injected into every container; host values are never
#: forwarded implicitly.
_BASE_ENV = {
    "PATH": "/usr/local/bin:/usr/local/sbin:/usr/bin:/usr/sbin:/bin:/sbin",
    "HOME": "/tmp",
    "LANG": "C.UTF-8",
    # Marker so test suites can detect they run inside the sandbox —
    # POSIX-signal semantics differ under a container runtime.
    "QG_IN_SANDBOX": "1",
}

#: Toolchain cache/env redirects into the tmpfs scratch — deterministic
#: in both mount modes and immune to a stale host ``.venv`` showing
#: through the overlay.
_RO_REDIRECTS = {
    "UV_PROJECT_ENVIRONMENT": "/tmp/venv",
    "UV_CACHE_DIR": "/tmp/uv-cache",
    "PIP_CACHE_DIR": "/tmp/pip-cache",
    "CARGO_TARGET_DIR": "/tmp/target",
    "GOCACHE": "/tmp/gocache",
    "GOMODCACHE": "/tmp/gomodcache",
    "npm_config_cache": "/tmp/npm-cache",
}


def runtime() -> str | None:
    """Resolve the container runtime per ``QG_SANDBOX`` (FR-O28)."""
    mode = config.SANDBOX_MODE
    if mode in ("0", "off", "false", "no", ""):
        return None
    if mode in _RUNTIMES:
        return mode if shutil.which(mode) else None
    # auto / 1 / on: first available runtime wins.
    for rt in _RUNTIMES:
        if shutil.which(rt):
            return rt
    return None


def _image_present(rt: str) -> bool:
    """Check the sandbox image exists; cached per process. Degrades
    gracefully — any inspection error means 'not usable'."""
    if rt not in _image_checked:
        try:
            res = subprocess.run(
                [rt, "image", "inspect", config.SANDBOX_IMAGE],
                capture_output=True, timeout=30,
            )
            _image_checked[rt] = res.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            _image_checked[rt] = False
    return _image_checked[rt]


def available() -> bool:
    """True when a runtime AND the sandbox image are usable."""
    rt = runtime()
    return rt is not None and _image_present(rt)


#: Credential markers — a forwarded name containing any of these
#: (case-insensitive) is denied, even when its prefix is allowlisted.
#: Substring match because credential var names are irregular:
#: ``npm_config_//reg/:_authToken``, ``AWS_SECRET_ACCESS_KEY``,
#: ``*_PASSWORD`` don't share a single suffix.
_DENY_MARKERS = ("SECRET", "TOKEN", "PASSWORD", "PASSWD",
                 "CREDENTIAL", "PRIVATE", "AUTH")


def _secret_name(name: str) -> bool:
    up = name.upper()
    return up.endswith("_KEY") or any(m in up for m in _DENY_MARKERS)


def _env_spec() -> dict[str, str | None]:
    """In-container env whitelist as {name: value|None} — ``None`` means
    passthrough (``--env NAME``, the runtime copies the value from the
    client's own scrubbed env so secrets never appear in argv).

    Whitelisted host vars are forwarded only when their name carries no
    credential marker (``_secret_name``); literal base vars keep
    ``--env K=V`` (no secrets among them)."""
    env: dict[str, str | None] = dict(_BASE_ENV)
    for name in os.environ:
        if _secret_name(name):
            continue
        # Trailing ``*`` = prefix match; otherwise exact name.
        if any(name.startswith(p[:-1]) if p.endswith("*")
               else name == p for p in config.SANDBOX_ENV_ALLOW):
            env[name] = None  # passthrough — value stays out of argv
    return env


def _env_args(spec: dict[str, str | None]) -> list[str]:
    return [a for k, v in spec.items()
            for a in ("--env", k if v is None else f"{k}={v}")]


def _client_env(spec: dict[str, str | None]) -> dict[str, str]:
    """Env for the host-side container client: the FR-P11 scrubbed set
    plus the forwarded (already credential-filtered) tool vars the
    ``--env NAME`` passthroughs resolve from."""
    env = git_ops.scrub_env()
    for k, v in spec.items():
        if v is None and k in os.environ:
            env[k] = os.environ[k]
    return env


def _mount_args(rt: str, repo: Path) -> list[str]:
    """Worktree mount: podman ``:O`` gives a copy-on-write writable view
    (host worktree untouched); docker mounts strictly read-only plus a
    tmpfs scratch redirected to by ``_RO_REDIRECTS``."""
    tmp = ["--tmpfs", "/tmp"]  # HOME and caches live here (read-only rootfs)
    if rt == "podman":
        return ["-v", f"{repo}:/src:O", *tmp]
    return ["-v", f"{repo}:/src:ro", *tmp]


def container_argv(repo: Path, argv: list[str], *, name: str,
                   net: bool, rt: str) -> list[str]:
    """Full ``container run`` argv implementing the FR-O28 contract."""
    args = [
        rt, "run", "--rm",
        "--init",  # proper PID 1 — reaps zombies reparented by PR code
        "--name", name,
        "--network", "bridge" if (net or config.SANDBOX_ALLOW_NET) else "none",
        "--read-only",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--memory", config.SANDBOX_MEMORY,
        "--cpus", config.SANDBOX_CPUS,
        "--pids-limit", config.SANDBOX_PIDS,
        *_mount_args(rt, repo),
        *_env_args(_env_spec()),
    ]
    for k, v in _RO_REDIRECTS.items():
        args += ["--env", f"{k}={v}"]
    args += ["-w", "/src", config.SANDBOX_IMAGE, *argv]
    return args


def _kill_container(rt: str, name: str) -> None:
    """Second-net kill for a timed-out sandbox. Best-effort, bounded."""
    try:
        subprocess.run([rt, "kill", name], capture_output=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        pass


def run(repo: Path | str, argv: list[str], timeout: int | None = None,
        *, net: bool = False) -> dict[str, Any]:
    """Run ``argv`` inside the sandbox over ``repo``.

    Returns the ``spawn_process`` dict plus ``sandboxed``/``container``
    keys. The host-side client process group is pgid-tracked as usual;
    on timeout the named container gets an explicit ``container kill``
    as a second net.
    """
    rt = runtime()
    if rt is None:
        raise RuntimeError("sandbox.run called with no runtime available")
    repo = Path(repo)
    name = f"qg-{os.getpid()}-{next(_seq)}"
    cargv = container_argv(repo, argv, name=name, net=net, rt=rt)
    res = git_ops.spawn_tracked(
        str(repo), cargv[0], cargv[1:], timeout=timeout,
        env=_client_env(_env_spec()))
    if res["timed_out"]:
        _kill_container(rt, name)
    res["sandboxed"] = True
    res["container"] = name
    return res


def exec_allowed(argv: list[str]) -> bool:
    """``sandbox_exec`` argv allowlist — basename of argv[0] must be in
    ``QG_SANDBOX_EXEC_ALLOW``."""
    return bool(argv) and Path(argv[0]).name in config.SANDBOX_EXEC_ALLOW


class ExecBudget:
    """Bounded sandbox_exec calls per run (``QG_SANDBOX_EXEC_MAX_CALLS``)."""

    def __init__(self, max_calls: int = config.SANDBOX_EXEC_MAX_CALLS):
        self.max_calls = max_calls
        self.calls = 0

    def exec(self, repo: Path | str, argv: list[str],
             timeout: int | None = None, *, net: bool = False
             ) -> dict[str, Any]:
        if self.calls >= self.max_calls:
            return {"pgid": -1, "exit_code": 126, "stdout": "",
                    "stderr": "sandbox_exec budget exhausted",
                    "timed_out": False, "command": argv, "duration_s": 0.0,
                    "sandboxed": True}
        if not exec_allowed(argv):
            return {"pgid": -1, "exit_code": 126, "stdout": "",
                    "stderr": f"argv[0] not in QG_SANDBOX_EXEC_ALLOW: "
                              f"{argv[0] if argv else ''}",
                    "timed_out": False, "command": argv, "duration_s": 0.0,
                    "sandboxed": True}
        self.calls += 1
        return run(repo, argv, timeout, net=net)


def sweep() -> list[str]:
    """Remove stale ``qg-*`` sandboxes left by crashed runs (INV-2 ext).

    Only non-running containers (``created``/``exited``) are touched —
    in-flight sandboxes of concurrent runs are never killed. ``--rm``
    runs self-clean on exit, so leftovers mean a daemon/host crash.
    Returns the removed container names; best-effort on any failure.
    """
    rt = runtime()
    if rt is None:
        return []
    try:
        listed = subprocess.run(
            [rt, "ps", "-a", "--filter", "name=qg-",
             "--filter", "status=created", "--filter", "status=exited",
             "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=30,
        )
        names = [n.strip() for n in listed.stdout.splitlines()
                 if n.strip().startswith("qg-")]
        removed = []
        for name in names:
            rm = subprocess.run([rt, "rm", "-f", name],
                                capture_output=True, timeout=30)
            if rm.returncode == 0:
                removed.append(name)
        return removed
    except (OSError, subprocess.TimeoutExpired):
        return []
