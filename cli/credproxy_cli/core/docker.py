"""Thin wrappers over the `docker` CLI.

These do not print; on failure they raise DockerError. `stream=True`
sends docker's output straight to the terminal (used by `dev build`),
which is the one place docker output is allowed to reach the user
directly -- porcelain owns that decision by passing the flag through.
"""
from __future__ import annotations

import subprocess

from .errors import DockerError


def docker(args: list[str], stream: bool = False) -> None:
    """Run `docker <args>`; raise DockerError on error. With stream=True,
    docker's output goes straight to the terminal."""
    if stream:
        if subprocess.run(["docker", *args], check=False).returncode != 0:
            raise DockerError(f"docker {args[0]} failed")
        return
    r = subprocess.run(
        ["docker", *args],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if r.returncode != 0:
        raise DockerError(f"docker {args[0]} failed: {r.stderr.strip()}")


def docker_quiet(args: list[str]) -> None:
    """Run `docker <args>`, ignoring failures (best-effort cleanup)."""
    subprocess.run(
        ["docker", *args],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def docker_output(args: list[str]) -> str:
    """Run `docker <args>` and return stdout; raise DockerError on error."""
    r = subprocess.run(
        ["docker", *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if r.returncode != 0:
        raise DockerError(f"docker {args[0]} failed: {r.stderr.strip()}")
    return r.stdout


def inspect(ref: str, fmt: str) -> str | None:
    """`docker inspect -f <fmt> <ref>`; None if the object is absent."""
    r = subprocess.run(
        ["docker", "inspect", "-f", fmt, ref],
        capture_output=True,
        text=True,
        check=False,
    )
    return r.stdout.strip() if r.returncode == 0 else None


def container_status(name: str) -> str | None:
    """running / exited / created / ... ; None if the container is absent."""
    return inspect(name, "{{.State.Status}}")


def logs_tail(name: str, n: int = 20) -> str:
    """Last `n` log lines of a container, stdout+stderr MERGED (tracebacks land
    on the container's stderr), best-effort -- '' if unavailable. Used to
    surface a crashed proxy's reason inline rather than via a separate command."""
    r = subprocess.run(
        ["docker", "logs", "--tail", str(n), name],
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode != 0:
        return ""
    return r.stdout + r.stderr


def seed_volume_from_container(
    container: str, src_path: str, volume: str, helper_image: str,
    userns_flags: list[str] | None = None,
) -> None:
    """Stream the CONTENTS of `container:src_path` into the named `volume`, via
    `docker cp ... - | docker run -i ... tar -x`, preserving ownership.

    `docker cp` reads the container's merged filesystem (image + writable layer),
    so this captures data that was never on a volume -- the whole point of
    "preserve". `src_path/.` copies the directory's contents (not the directory
    itself), so they land at the volume root, which mounts AT `src_path` in the
    recreated container.

    The extract helper runs with the SAME userns mapping as the workspace
    container (`userns_flags`, from lifecycle._host_user_run_flags) so file
    ownership round-trips identically on rootless podman, and as `--user 0`
    (namespace-root) so it can restore ownership across the whole mapped uid
    range. On rootful Docker / a root workspace `userns_flags` is empty and it
    runs as real root -- uids are 1:1 either way.

    Both pipe stages' exit codes are checked, so a partial/failed capture raises
    rather than silently leaving a half-seeded volume."""
    userns_flags = userns_flags or []
    cp = subprocess.Popen(
        ["docker", "cp", "-a", f"{container}:{src_path}/.", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    extract = subprocess.Popen(
        ["docker", "run", "--rm", "-i", *userns_flags, "--user", "0",
         "-v", f"{volume}:/dst", helper_image, "tar", "-xpf", "-", "-C", "/dst"],
        stdin=cp.stdout, stderr=subprocess.PIPE,
    )
    # Close our copy of the read end so `cp` gets SIGPIPE if the helper dies.
    assert cp.stdout is not None
    cp.stdout.close()
    _, extract_err = extract.communicate()
    cp.wait()
    cp_err = cp.stderr.read() if cp.stderr else b""
    if cp.stderr:
        cp.stderr.close()
    if cp.returncode != 0:
        raise DockerError(
            f"capturing {container}:{src_path} failed: "
            f"{cp_err.decode(errors='replace').strip()}"
        )
    if extract.returncode != 0:
        raise DockerError(
            f"seeding volume {volume} failed: "
            f"{extract_err.decode(errors='replace').strip()}"
        )


def resolve_host_port(container_name: str, container_port: int) -> int:
    """Return the host port Docker mapped to *container_port* for *container_name*.

    Uses `docker port <container> <port>/tcp`, which is self-healing: it
    queries Docker's live NetworkSettings each time, so a container restart
    (which may reassign an ephemeral port) is always reflected correctly.

    Raises DockerError if the container is not running or the port is not
    published."""
    r = subprocess.run(
        ["docker", "port", container_name, f"{container_port}/tcp"],
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode != 0 or not r.stdout.strip():
        raise DockerError(
            f"cannot resolve host port for {container_name}:{container_port}/tcp"
            f" — is the container running? ({r.stderr.strip()})"
        )
    # Output is one or more lines like "127.0.0.1:54321"; take the first
    # 127.0.0.1 binding.
    for line in r.stdout.splitlines():
        line = line.strip()
        if line.startswith("127.0.0.1:"):
            _, port_str = line.rsplit(":", 1)
            return int(port_str)
    # Fallback: parse the first line regardless of address
    first = r.stdout.splitlines()[0].strip()
    _, port_str = first.rsplit(":", 1)
    return int(port_str)
