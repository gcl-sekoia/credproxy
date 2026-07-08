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


class PresetTemplateError(ConfigError):
    """A template-declared `[[preset]]` entry (#57) can't be expanded at `create`
    time because a required field (provider/secret) is missing and the pack has no
    default for it. Carries `preset` + `missing` so `--json` can serialize the
    structured `{preset, missing}` shape (surfaced via `json_fields`)."""

    def __init__(self, preset: str, missing: list[str]):
        self.preset = preset
        self.missing = list(missing)
        joined = " and ".join(f"`{m}`" for m in self.missing)
        add_flags = " ".join(f"--{m} ..." for m in self.missing)
        super().__init__(
            f"template preset '{preset}' is missing {joined} -- fill it in the "
            f"`[[preset]]` entry (name/provider/secret) in your "
            f"workspace.template.toml, or drop the entry and run "
            f"`credproxy workspace NAME preset add {preset} {add_flags}` after "
            f"create")

    @property
    def json_fields(self) -> dict:
        return {"preset": self.preset, "missing": self.missing}
