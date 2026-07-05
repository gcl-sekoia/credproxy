[index](../README.md) · [02 · Your first workspace](02-first-workspace.md) →

# 01 · Install

credproxy runs from a clone of its repository. There is no package to install
and nothing to add to your system Python. The `bin/credproxy` and `bin/credp`
commands run the tool directly from the checkout. This keeps the install simple
and the code easy to inspect.

## Prerequisites

**A container engine.** credproxy is built from two containers, so you need one
of:

- **Docker** (Docker Desktop on macOS or Windows, Docker Engine on Linux), or
- **rootless Podman** (Fedora, RHEL, and other Linux hosts).

**Python 3.11 or newer.** The host command is written in Python and uses the
standard library only — no `pip install` step. Check your version:

```console
$ python3 --version
Python 3.11.7
```

## Get the code

Clone the repository and enter it:

```sh
git clone https://github.com/gregclermont/credproxy.git
cd credproxy
```

## Put the commands on your PATH

credproxy has two commands, both in the `bin/` directory:

- `credproxy` — the strict command, for scripts.
- `credp` — the human command, for daily use.

The simplest way to reach both is to add `bin/` to your PATH:

```sh
export PATH="$PWD/bin:$PATH"
```

Add that line (with the real absolute path) to your shell profile to make it
permanent. If you prefer symlinks into a directory already on your PATH, link
**both** commands so they stay together:

```sh
ln -s "$PWD/bin/credproxy" ~/.local/bin/credproxy
ln -s "$PWD/bin/credp"     ~/.local/bin/credp
```

> [!NOTE]
> `credp` and `credproxy` are the same program with different defaults. You will
> use `credp` almost everywhere. The [daily workflow](03-daily-workflow.md) page
> explains the split. For now, install both.

## Verify

Run the built-in preflight check. It inspects your environment and reports every
problem it finds at once:

```console
$ credproxy doctor
✓ docker daemon reachable
✗ image credproxy:dev not found; run `credproxy dev build` first  → run `credproxy dev build`
1 of 2 checks FAILED
```

A missing proxy image is expected on a fresh install — you build it once, on
your first `start`, in the next chapter. As long as `doctor` reports
`docker daemon reachable`, you are ready.

> [!TIP]
> Seeing "docker daemon unreachable"? Start Docker Desktop, or start the Podman
> socket, then run `credproxy doctor` again. Want the full list of checks and
> what each means? → [Troubleshooting](../troubleshooting.md). Otherwise,
> continue.

---

**Next:** [02 · Your first workspace](02-first-workspace.md) — create, build,
and enter a workspace.
