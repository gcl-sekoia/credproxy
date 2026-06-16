"""The distribution profile: customizable defaults loaded from data, not code.

`profile()` returns the effective `Profile` -- the org overlay's `profile.toml`
(in `profile_dir()`) merged key-by-key over the builtin default. Every
distribution-level constant the CLI used to hardcode (default workspace image,
proxy image tag, the default image's user/home/uid, the default setup commands)
lives here, so a fork customizes one TOML instead of editing Python. The overlay
may set any subset of keys; unset keys fall back to the builtin default.

Read at call time (no caching) so `$CREDPROXY_PROFILE_DIR` -- and tests that set
it -- take effect immediately, matching the call-time semantics of paths.py.
See docs/forking.md.
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass

from .errors import ConfigError
from .paths import builtin_profile_file, profile_dir


@dataclass(frozen=True)
class Profile:
    image_tag: str
    default_image: str
    default_user: str
    default_home: str
    default_uid: int
    generic_home: str
    default_setup: tuple[str, ...]


def _load(path) -> dict:
    if not path.is_file():
        return {}
    try:
        return tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"{path}: invalid profile.toml ({e})")


def profile() -> Profile:
    """The effective distribution profile: overlay merged over the builtin
    default. The builtin file supplies every key; the overlay overrides a
    subset."""
    merged = {**_load(builtin_profile_file()), **_load(profile_dir() / "profile.toml")}

    def _str(key: str) -> str:
        v = merged.get(key)
        if not isinstance(v, str) or not v:
            raise ConfigError(f"profile: '{key}' must be a non-empty string")
        return v

    uid = merged.get("default_uid")
    if not isinstance(uid, int) or isinstance(uid, bool):
        raise ConfigError("profile: 'default_uid' must be an integer")
    setup = merged.get("default_setup", [])
    if not isinstance(setup, list) or not all(isinstance(s, str) and s for s in setup):
        raise ConfigError("profile: 'default_setup' must be an array of strings")

    return Profile(
        image_tag=_str("image_tag"),
        default_image=_str("default_image"),
        default_user=_str("default_user"),
        default_home=_str("default_home"),
        default_uid=uid,
        generic_home=_str("generic_home"),
        default_setup=tuple(setup),
    )
