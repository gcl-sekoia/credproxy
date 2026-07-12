"""Typed exceptions raised by the core.

The core never calls sys.exit; it raises these. Porcelain catches
CredproxyError and renders it via _fail (exit 1 with the `[credproxy] `
prefix). Subclasses exist so later waves (and `--json` rendering) can
distinguish failure kinds without string-matching; today porcelain
treats them uniformly.
"""
from __future__ import annotations


class CredproxyError(Exception):
    """Base for all core-raised, user-facing errors. `str(e)` is the
    message porcelain renders."""


class ConfigError(CredproxyError):
    """A problem with a workspace's config TOML (missing, malformed, or
    failing validation), or a missing host-env secret referenced by it."""


class WorkspaceError(CredproxyError):
    """A workspace is missing, already exists, or has an invalid name."""


class ImageError(CredproxyError):
    """The proxy image is missing or does not declare the env contract."""


class DockerError(CredproxyError):
    """A `docker` invocation failed."""


class ProxyError(CredproxyError):
    """The proxy did not become ready, rejected the token, or returned an
    error from /admin/config."""


class DependencyError(CredproxyError):
    """A host-side dependency is missing."""


class ProviderError(CredproxyError):
    """A provider could not be found, failed to execute, returned a
    malformed response, or reported that the secret does not exist. The
    message names the provider, the secret id, and a tail of the
    provider's stderr where available."""


class InjectorError(CredproxyError):
    """An injector definition could not be found or is malformed
    (missing/invalid `header`, bad `[placeholder]` charset, etc.)."""


class PackTemplateError(ConfigError):
    """A template-declared `[[pack]]` entry (#57) can't be expanded at `create`
    time because a required field (provider/secret) is missing and the pack has no
    default for it. Carries `pack` + `missing` so `--json` can serialize the
    structured `{pack, missing}` shape (surfaced via `json_fields`)."""

    def __init__(self, pack: str, missing: list[str]):
        self.pack = pack
        self.missing = list(missing)
        joined = " and ".join(f"`{m}`" for m in self.missing)
        add_flags = " ".join(f"--{m} ..." for m in self.missing)
        super().__init__(
            f"template pack '{pack}' is missing {joined} -- fill it in the "
            f"`[[pack]]` entry (name/provider/secret) in your "
            f"workspace.template.toml, or drop the entry and run "
            f"`credproxy workspace NAME pack add {pack} {add_flags}` after "
            f"create")

    @property
    def json_fields(self) -> dict:
        return {"pack": self.pack, "missing": self.missing}


class PackOptionsError(ConfigError):
    """A pack expansion (`pack add` / template `[[pack]]`) can't resolve one
    or more required pack `[[option]]`s (#59): no explicit `--opt`/`[pack.options]`
    value, no default, and no prompt (strict, or loose without a TTY). Carries
    `pack` + `missing` -- a list of `{id, type, description, enum?}` dicts -- so
    `--json` serializes the structured `{pack, missing}` shape an agent re-invokes
    against (`--opt id=value` per entry)."""

    def __init__(self, pack: str, missing: list[dict]):
        self.pack = pack
        self.missing = list(missing)
        ids = ", ".join(m["id"] for m in self.missing)
        flags = " ".join(f"--opt {m['id']}=..." for m in self.missing)
        super().__init__(
            f"pack '{pack}' needs option value(s): {ids} -- supply {flags} "
            f"(or a default in the pack, or run on the loose surface in a terminal "
            f"to be prompted)")

    @property
    def json_fields(self) -> dict:
        return {"pack": self.pack, "missing": self.missing}
