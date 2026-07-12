"""Host option markers in preset `[[part]]`/`[[rule]]` hosts (#72): a whole-element
`{ option = "id" }` marker lets a generic pack parameterize the per-org hostname
of a self-hosted service (GitLab, Artifactory, …) instead of forking the pack.

Covers the parse (marker recorded, dummy substituted), definition errors, the
expansion substitution (part + rule, mixed literal/marker), `preset list`
rendering, the CLI add + `[preset.options]` stamp, missing-option fail, refresh
read-back, and atomic failure on a junk pattern.
"""
from __future__ import annotations

import json
import textwrap

import pytest

from test_porcelain import _run


def _write_preset(name: str, toml: str):
    from credproxy_cli.core.paths import config_dir
    d = config_dir() / "presets"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.toml").write_text(textwrap.dedent(toml))


def _make_ws(name: str):
    from credproxy_cli.core.paths import workspaces_config_dir
    wd = workspaces_config_dir()
    wd.mkdir(parents=True, exist_ok=True)
    (wd / f"{name}.toml").write_text('image = "python:3.12-slim"\n')


def _config_text(name: str) -> str:
    from credproxy_cli.core.paths import workspaces_config_dir
    return (workspaces_config_dir() / f"{name}.toml").read_text()


def _load(name):
    from credproxy_cli.core.model.presets import get_preset
    return get_preset(name)


# A generic self-hosted pack: the bearer part AND a readonly rule both point at
# one `gitlab_host` option; the rule also names a literal (mixed array).
_GITLAB = """
    [[option]]
    id = "gitlab_host"
    type = "string"
    description = "your GitLab instance's hostname"

    [placeholder]
    prefix = "glpat_"
    length = 26
    charset = "alnumeric"

    [[part]]
    suffix = "api"
    injector = "bearer"
    hosts = [{ option = "gitlab_host" }]
    env = "GITLAB_TOKEN"

    [[rule]]
    suffix = "readonly"
    hosts = [{ option = "gitlab_host" }, "registry.example.com"]
    action = "block"
    methods = ["DELETE"]
"""


# ---- parse -------------------------------------------------------------------


def test_host_markers_recorded_with_dummy(xdg):
    from credproxy_cli.core.model.presets import _HOST_OPTION_DUMMY
    _write_preset("gitlab", _GITLAB)
    spec = _load("gitlab")
    assert spec.parts[0].host_options == ((0, "gitlab_host"),)
    assert spec.parts[0].hosts == (_HOST_OPTION_DUMMY,)
    # A mixed rule array: marker at index 0, literal at index 1.
    assert spec.rules[0].host_options == ((0, "gitlab_host"),)
    assert spec.rules[0].rule.hosts == (_HOST_OPTION_DUMMY, "registry.example.com")
    # The option is referenced (by the hosts), so not flagged unreferenced.
    from credproxy_cli.core.model.presets import _unreferenced_option_ids
    assert _unreferenced_option_ids(spec) == []


def test_host_marker_undefined_option_rejected(xdg):
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.presets import load_presets
    _write_preset("p", """
        [placeholder]
        prefix = "p_"
        length = 20
        charset = "hex"
        [[part]]
        suffix = "api"
        injector = "bearer"
        hosts = [{ option = "nope" }]
    """)
    with pytest.raises(ConfigError, match="undefined option 'nope'"):
        load_presets()


def test_host_marker_bool_option_rejected(xdg):
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.presets import load_presets
    _write_preset("p", """
        [[option]]
        id = "flag"
        type = "bool"
        default = true
        [placeholder]
        prefix = "p_"
        length = 20
        charset = "hex"
        [[part]]
        suffix = "api"
        injector = "bearer"
        hosts = [{ option = "flag" }]
    """)
    with pytest.raises(ConfigError, match="'bool' option"):
        load_presets()


def test_host_marker_malformed_rejected(xdg):
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.presets import load_presets
    _write_preset("p", """
        [[option]]
        id = "h"
        type = "string"
        default = "x.example.com"
        [placeholder]
        prefix = "p_"
        length = 20
        charset = "hex"
        [[part]]
        suffix = "api"
        injector = "bearer"
        hosts = [{ option = "h", extra = "boom" }]
    """)
    with pytest.raises(ConfigError, match="unexpected extra key"):
        load_presets()


def test_rule_host_marker_in_rule_only_pack(xdg):
    """A pure-rule policy pack for a self-hosted service has the identical gap;
    the rule host marker must parse there too."""
    _write_preset("policy", """
        [[option]]
        id = "host"
        type = "string"
        description = "the service host"
        [[rule]]
        suffix = "guard"
        hosts = [{ option = "host" }]
        action = "block"
        methods = ["DELETE"]
    """)
    spec = _load("policy")
    assert spec.rules[0].host_options == ((0, "host"),)


# ---- expansion ---------------------------------------------------------------


def test_expansion_substitutes_part_and_rule_hosts(xdg):
    from credproxy_cli.core.model.presets import build_preset
    _write_preset("gitlab", _GITLAB)
    exp = build_preset("gitlab", "env", "GITLAB_TOKEN",
                       options={"gitlab_host": "gitlab.acme.internal"})
    assert exp.bindings[0].hosts == ("gitlab.acme.internal",)
    # The rule keeps its literal and gets the substituted marker (order preserved).
    assert exp.rules[0].hosts == ("gitlab.acme.internal", "registry.example.com")


def test_preset_list_renders_option_marker(xdg):
    from credproxy_cli.core.model.presets import describe_presets
    _write_preset("gitlab", _GITLAB)
    row = next(d for d in describe_presets() if d["name"] == "gitlab")
    assert row["bindings"][0]["hosts"] == ["{option=gitlab_host}"]
    assert row["rules"][0]["hosts"] == ["{option=gitlab_host}",
                                        "registry.example.com"]


# ---- CLI add + refresh -------------------------------------------------------


def test_preset_add_host_option_stamps_and_expands(xdg):
    _write_preset("gitlab", _GITLAB)
    _make_ws("w")
    code, out, err = _run([
        "workspace", "w", "preset", "add", "gitlab", "--provider", "env",
        "--secret", "GITLAB_TOKEN", "--opt", "gitlab_host=gitlab.acme.internal",
    ])
    assert code == 0, out + err
    text = _config_text("w")
    assert "[preset.options]" in text and "gitlab.acme.internal" in text
    # The resolved host reaches the expanded binding + rule (never the dummy).
    from credproxy_cli.core.model.resolver import resolve_workspace
    from credproxy_cli.core.model.workspace import Workspace
    r = resolve_workspace(Workspace("w"))
    assert r.bindings[0].hosts == ("gitlab.acme.internal",)
    assert "gitlab.acme.internal" in r.rules[0].hosts
    # The newly-intercepted advisory names the option-supplied host.
    assert "gitlab.acme.internal" in (out + err)


def test_preset_add_missing_host_option_fails(xdg):
    _write_preset("gitlab", _GITLAB)
    _make_ws("w")
    before = _config_text("w")
    code, out, err = _run([
        "--json", "workspace", "w", "preset", "add", "gitlab",
        "--provider", "env", "--secret", "GITLAB_TOKEN",   # no --opt
    ])
    assert code == 1
    obj = json.loads(out)["error"]
    assert obj["type"] == "PresetOptionsError"
    assert obj["missing"][0]["id"] == "gitlab_host"
    assert _config_text("w") == before          # atomic: nothing written


def test_preset_refresh_recovers_host_option(xdg):
    """The host-feeding option's value is read back from `[preset.options]` on a
    refresh -- no re-prompt, same host."""
    _write_preset("gitlab", _GITLAB)
    _make_ws("w")
    assert _run([
        "workspace", "w", "preset", "add", "gitlab", "--provider", "env",
        "--secret", "GITLAB_TOKEN", "--opt", "gitlab_host=gitlab.acme.internal",
    ])[0] == 0
    # A --check refresh reads the pack + the stamped option back; exit 0, host kept.
    code, out, err = _run(["workspace", "w", "preset", "refresh", "--check"])
    assert code == 0, out + err
    from credproxy_cli.core.model.resolver import resolve_workspace
    from credproxy_cli.core.model.workspace import Workspace
    r = resolve_workspace(Workspace("w"))
    assert r.bindings[0].hosts == ("gitlab.acme.internal",)


def test_host_option_dedupes_against_literal(xdg):
    """A marker resolving to a value equal to a literal in the same array (or to
    another marker) is deduped, so the expanded binding/rule doesn't self-collide."""
    from credproxy_cli.core.model.presets import build_preset
    _write_preset("dup", """
        [[option]]
        id = "host"
        type = "string"
        [placeholder]
        prefix = "d_"
        length = 20
        charset = "hex"
        [[part]]
        suffix = "api"
        injector = "bearer"
        hosts = ["gitlab.com", { option = "host" }]
    """)
    exp = build_preset("dup", "env", "T", options={"host": "gitlab.com"})
    assert exp.bindings[0].hosts == ("gitlab.com",)


def test_empty_host_option_rejected(xdg):
    """An empty option value is rejected with an option-framed error (not the
    generic option-blind 'hosts required' from lock read-back)."""
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.presets import build_preset
    _write_preset("e", """
        [[option]]
        id = "host"
        type = "string"
        [placeholder]
        prefix = "e_"
        length = 20
        charset = "hex"
        [[part]]
        suffix = "api"
        injector = "bearer"
        hosts = [{ option = "host" }]
    """)
    with pytest.raises(ConfigError, match="option 'host' supplies an empty host"):
        build_preset("e", "env", "T", options={"host": ""})


def test_preset_add_junk_pattern_host_fails_atomically(xdg):
    """An option value that is an invalid glob pattern (`*.com`) is validated
    through the same hostmatch path as any binding host and fails the whole add."""
    _write_preset("gitlab", _GITLAB)
    _make_ws("w")
    before = _config_text("w")
    code, out, err = _run([
        "workspace", "w", "preset", "add", "gitlab", "--provider", "env",
        "--secret", "GITLAB_TOKEN", "--opt", "gitlab_host=*.com",
    ])
    assert code == 1
    assert _config_text("w") == before          # rolled back
