"""Tests for core/bindings.py: parse/validate, auto-name generation, the
name-required (hand-authored) contract, append/remove surgical edits, multi-slot
secrets, and wire_config shape."""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest


# ---- helpers -----------------------------------------------------------------


def _write_ws(workspaces_dir: Path, name: str, content: str):
    """Write a workspace TOML and return a Workspace."""
    from credproxy_cli.core.model.workspace import Workspace
    p = workspaces_dir / f"{name}.toml"
    p.write_text(textwrap.dedent(content))
    return Workspace(name)


# ---- auto-name generation ----------------------------------------------------


def test_auto_name_no_collision():
    from credproxy_cli.core.model.bindings import _auto_name

    assert _auto_name("bearer", "env", set()) == "bearer-env"


def test_auto_name_first_collision():
    from credproxy_cli.core.model.bindings import _auto_name

    taken = {"bearer-env"}
    assert _auto_name("bearer", "env", taken) == "bearer-env-2"


def test_auto_name_multi_collision():
    from credproxy_cli.core.model.bindings import _auto_name

    taken = {"bearer-env", "bearer-env-2", "bearer-env-3"}
    assert _auto_name("bearer", "env", taken) == "bearer-env-4"


def test_auto_name_no_prefix_suffix_cross_collision():
    """bearer-env-2 existing should not prevent bearer-env from being used."""
    from credproxy_cli.core.model.bindings import _auto_name

    taken = {"bearer-env-2"}
    assert _auto_name("bearer", "env", taken) == "bearer-env"


# ---- secret_refs normalization -----------------------------------------------


def test_secret_refs_bare_string_is_value_slot():
    from credproxy_cli.core.model.bindings import Binding, secret_refs

    b = Binding(name="b", injector="bearer", provider="env", secret="TOK",
                hosts=("h.io",), placeholder="p", env=None)
    assert secret_refs(b) == {"value": "TOK"}


def test_secret_refs_table_passthrough():
    from credproxy_cli.core.model.bindings import Binding, secret_refs

    b = Binding(name="b", injector="bearer", provider="env",
                secret={"a": "R1", "b": "R2"}, hosts=("h.io",),
                placeholder="p", env=None)
    assert secret_refs(b) == {"a": "R1", "b": "R2"}


# ---- parse / validate --------------------------------------------------------


def test_parse_missing_injector(xdg, workspaces_dir):
    from credproxy_cli.core.model.bindings import _parse_bindings
    from credproxy_cli.core.errors import ConfigError

    raw = {"binding": [{"provider": "env", "secret": "X", "hosts": ["h.io"]}]}
    with pytest.raises(ConfigError, match="injector is required"):
        _parse_bindings(raw, "test")


def test_parse_missing_provider(xdg, workspaces_dir):
    from credproxy_cli.core.model.bindings import _parse_bindings
    from credproxy_cli.core.errors import ConfigError

    raw = {"binding": [{"injector": "bearer", "secret": "X", "hosts": ["h.io"]}]}
    with pytest.raises(ConfigError, match="provider is required"):
        _parse_bindings(raw, "test")


def test_parse_missing_secret(xdg, workspaces_dir):
    from credproxy_cli.core.model.bindings import _parse_bindings
    from credproxy_cli.core.errors import ConfigError

    raw = {"binding": [{"injector": "bearer", "provider": "env", "hosts": ["h.io"]}]}
    with pytest.raises(ConfigError, match="secret is required"):
        _parse_bindings(raw, "test")


def test_parse_secret_table(xdg, workspaces_dir):
    """A `secret` table parses into a slot->ref dict."""
    from credproxy_cli.core.model.bindings import _parse_bindings

    raw = {"binding": [{
        "injector": "bearer", "provider": "env",
        "secret": {"access_key_id": "AKID", "secret_access_key": "SAK"},
        "hosts": ["h.io"],
    }]}
    bindings = _parse_bindings(raw, "test")
    assert bindings[0].secret == {"access_key_id": "AKID", "secret_access_key": "SAK"}


def test_parse_secret_table_empty_rejected(xdg, workspaces_dir):
    from credproxy_cli.core.model.bindings import _parse_bindings
    from credproxy_cli.core.errors import ConfigError

    raw = {"binding": [{"injector": "bearer", "provider": "env",
                        "secret": {}, "hosts": ["h.io"]}]}
    with pytest.raises(ConfigError, match="slot"):
        _parse_bindings(raw, "test")


def test_parse_empty_hosts(xdg, workspaces_dir):
    from credproxy_cli.core.model.bindings import _parse_bindings
    from credproxy_cli.core.errors import ConfigError

    raw = {"binding": [{"injector": "bearer", "provider": "env", "secret": "X", "hosts": []}]}
    with pytest.raises(ConfigError, match="hosts is required"):
        _parse_bindings(raw, "test")


def test_parse_hosts_not_array(xdg, workspaces_dir):
    from credproxy_cli.core.model.bindings import _parse_bindings
    from credproxy_cli.core.errors import ConfigError

    raw = {"binding": [{"injector": "bearer", "provider": "env", "secret": "X", "hosts": "api.github.com"}]}
    with pytest.raises(ConfigError, match="hosts is required"):
        _parse_bindings(raw, "test")


def test_parse_empty_name_rejected(xdg):
    from credproxy_cli.core.model.bindings import _parse_bindings
    from credproxy_cli.core.errors import ConfigError

    raw = {"binding": [{
        "injector": "bearer", "provider": "env", "secret": "X",
        "hosts": ["h.io"], "name": ""
    }]}
    with pytest.raises(ConfigError, match="name must be a non-empty string"):
        _parse_bindings(raw, "test")


def test_validate_accepts_glob_pattern(xdg, workspaces_dir):
    """A well-formed glob host (e.g. `*.amazonaws.com`) validates."""
    from credproxy_cli.core.model.bindings import Binding, validate

    b = Binding(name="aws", injector="sigv4", provider="env",
                secret={"access_key_id": "A", "secret_access_key": "B"},
                hosts=("*.amazonaws.com",), placeholder=None, env=None)
    validate([b], "test")  # does not raise


def test_validate_rejects_overbroad_pattern(xdg, workspaces_dir):
    from credproxy_cli.core.model.bindings import Binding, validate
    from credproxy_cli.core.errors import ConfigError

    b = Binding(name="aws", injector="sigv4", provider="env",
                secret={"access_key_id": "A", "secret_access_key": "B"},
                hosts=("*.com",), placeholder=None, env=None)
    with pytest.raises(ConfigError, match="too broad"):
        validate([b], "test")


def test_validate_rejects_bare_star(xdg, workspaces_dir):
    from credproxy_cli.core.model.bindings import Binding, validate
    from credproxy_cli.core.errors import ConfigError

    b = Binding(name="aws", injector="sigv4", provider="env",
                secret={"access_key_id": "A", "secret_access_key": "B"},
                hosts=("*",), placeholder=None, env=None)
    with pytest.raises(ConfigError, match="too broad"):
        validate([b], "test")


def test_validate_duplicate_name(xdg, workspaces_dir):
    from credproxy_cli.core.model.bindings import Binding, validate
    from credproxy_cli.core.errors import ConfigError

    b = Binding(name="dup", injector="bearer", provider="env",
                secret="X", hosts=("api.github.com",), placeholder="p", env=None)
    with pytest.raises(ConfigError, match="duplicate binding name"):
        validate([b, b], "test")


def test_validate_duplicate_host_location(xdg, workspaces_dir):
    """Two bindings writing the same header on the same host with the SAME
    placeholder can't be told apart -> fail."""
    from credproxy_cli.core.model.bindings import Binding, validate
    from credproxy_cli.core.errors import ConfigError

    b1 = Binding(name="b1", injector="bearer", provider="env",
                 secret="X", hosts=("api.github.com",), placeholder="p", env=None)
    b2 = Binding(name="b2", injector="bearer", provider="env",
                 secret="Y", hosts=("api.github.com",), placeholder="p", env=None)
    with pytest.raises(ConfigError, match="both write header"):
        validate([b1, b2], "test")


def test_validate_distinct_placeholders_share_location(xdg, workspaces_dir):
    """Distinct placeholders disambiguate, so two bindings may share a header on
    one host -- the rule that lets several re-seal bindings share a token
    endpoint."""
    from credproxy_cli.core.model.bindings import Binding, validate

    b1 = Binding(name="b1", injector="bearer", provider="env",
                 secret="X", hosts=("api.github.com",), placeholder="p1", env=None)
    b2 = Binding(name="b2", injector="bearer", provider="env",
                 secret="Y", hosts=("api.github.com",), placeholder="p2", env=None)
    validate([b1, b2], "test")   # no raise


def test_validate_unconditional_writers_collide(xdg, workspaces_dir):
    """Two sign-family (no-placeholder) bindings on the same header collide --
    nothing disambiguates them."""
    from credproxy_cli.core.model.bindings import Binding, validate
    from credproxy_cli.core.errors import ConfigError

    sec = {"access_key_id": "AK", "secret_access_key": "SK"}
    b1 = Binding(name="s1", injector="sigv4", provider="env",
                 secret=sec, hosts=("aws.example.com",), placeholder=None, env=None)
    b2 = Binding(name="s2", injector="sigv4", provider="env",
                 secret=sec, hosts=("aws.example.com",), placeholder=None, env=None)
    with pytest.raises(ConfigError, match="no placeholder"):
        validate([b1, b2], "test")


def test_validate_slot_mismatch(xdg, workspaces_dir):
    """A substitute scheme wants the single `value` slot; an extra slot fails."""
    from credproxy_cli.core.model.bindings import Binding, validate
    from credproxy_cli.core.errors import ConfigError

    b = Binding(name="b", injector="bearer", provider="env",
                secret={"value": "X", "extra": "Y"}, hosts=("h.io",),
                placeholder="p", env=None)
    with pytest.raises(ConfigError, match="needs secret slot"):
        validate([b], "test")


def test_validate_unknown_injector(xdg, workspaces_dir):
    """validate() raises InjectorError if injector name is not found."""
    from credproxy_cli.core.model.bindings import Binding, validate
    from credproxy_cli.core.errors import InjectorError

    b = Binding(name="b", injector="nonexistent_zzz", provider="env",
                secret="X", hosts=("h.io",), placeholder="p", env=None)
    with pytest.raises(InjectorError):
        validate([b], "test")


def test_validate_unknown_provider(xdg, workspaces_dir):
    """validate() raises ProviderError if provider name is not found."""
    from credproxy_cli.core.model.bindings import Binding, validate
    from credproxy_cli.core.errors import ProviderError

    b = Binding(name="b", injector="bearer", provider="nonexistent_zzz",
                secret="X", hosts=("api.github.com",), placeholder="p", env=None)
    with pytest.raises(ProviderError):
        validate([b], "test")


def _write_injector(xdg_unused, name: str, body: str):
    from credproxy_cli.core.paths import injectors_config_dir
    d = injectors_config_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.toml").write_text(body)


def test_validate_rejects_oauth2_reseal_without_api_hosts(xdg, workspaces_dir):
    """oauth2-reseal's api_hosts is required: missing it is a fail-OPEN at the
    proxy, so the CLI must reject the binding at add/apply (parity with the proxy)."""
    from credproxy_cli.core.model.bindings import Binding, validate
    from credproxy_cli.core.errors import ConfigError
    _write_injector(xdg, "reseal-noapi",
                    'scheme = "oauth2-reseal"\n[params]\ntoken_field = "access_token"\n')
    b = Binding(name="r", injector="reseal-noapi", provider="env",
                secret="CS", hosts=("login.example.com",), placeholder="cs_PH", env=None)
    with pytest.raises(ConfigError, match="api_hosts"):
        validate([b], "test")


def test_validate_accepts_oauth2_reseal_with_api_hosts(xdg, workspaces_dir):
    from credproxy_cli.core.model.bindings import Binding, validate
    _write_injector(xdg, "reseal-ok",
                    'scheme = "oauth2-reseal"\n[params]\napi_hosts = ["api.example.com"]\n')
    b = Binding(name="r", injector="reseal-ok", provider="env",
                secret="CS", hosts=("login.example.com",), placeholder="cs_PH", env=None)
    validate([b], "test")        # no raise


def test_validate_rejects_case_differing_host_collision(xdg, workspaces_dir):
    """DNS is case-insensitive: `API.x` and `api.x` writing one header with the
    same placeholder collide -- caught at validate (parity with the proxy)."""
    from credproxy_cli.core.model.bindings import Binding, validate
    from credproxy_cli.core.errors import ConfigError
    a = Binding(name="a", injector="bearer", provider="env", secret="X",
                hosts=("API.example.com",), placeholder="PH", env=None)
    b = Binding(name="b", injector="bearer", provider="env", secret="X",
                hosts=("api.example.com",), placeholder="PH", env=None)
    with pytest.raises(ConfigError, match="both write"):
        validate([a, b], "test")


def test_validate_rejects_overlapping_placeholders(xdg, workspaces_dir):
    """Overlapping placeholders (`ph` substring of `ph2`) on a shared location are
    rejected (mirrors the proxy): sequential substitution would corrupt one."""
    from credproxy_cli.core.model.bindings import Binding, validate
    from credproxy_cli.core.errors import ConfigError
    a = Binding(name="a", injector="bearer", provider="env", secret="X",
                hosts=("h.io",), placeholder="ph", env=None)
    b = Binding(name="b", injector="bearer", provider="env", secret="X",
                hosts=("h.io",), placeholder="ph2", env=None)
    with pytest.raises(ConfigError, match="overlap"):
        validate([a, b], "test")


def test_validate_rejects_case_differing_header_collision(xdg, workspaces_dir):
    """Header names are case-insensitive: `Authorization` vs `authorization` on
    one host with the same placeholder collide."""
    from credproxy_cli.core.model.bindings import Binding, validate
    from credproxy_cli.core.errors import ConfigError
    _write_injector(xdg, "auth-up", 'scheme = "bearer"\n[params]\nheader = "Authorization"\n')
    _write_injector(xdg, "auth-lo", 'scheme = "bearer"\n[params]\nheader = "authorization"\n')
    a = Binding(name="a", injector="auth-up", provider="env", secret="X",
                hosts=("x.com",), placeholder="PH", env=None)
    b = Binding(name="b", injector="auth-lo", provider="env", secret="X",
                hosts=("x.com",), placeholder="PH", env=None)
    with pytest.raises(ConfigError, match="both write"):
        validate([a, b], "test")


def test_fingerprint_includes_effective_injector_env(xdg, workspaces_dir):
    """The fingerprint must use the EFFECTIVE env (binding override else injector
    suggestion) -- the same value wire_config pushes -- so editing the injector's
    env re-pushes instead of silently serving stale config. Two injectors that
    differ ONLY in env (everything else, incl. the injector NAME, absent from the
    hash) must yield different fingerprints."""
    from credproxy_cli.core.model.bindings import Binding, config_fingerprint
    _write_injector(xdg, "inj-a", 'scheme = "bearer"\nenv = "A_TOKEN"\n')
    _write_injector(xdg, "inj-b", 'scheme = "bearer"\nenv = "B_TOKEN"\n')
    a = Binding(name="b", injector="inj-a", provider="env", secret="X",
                hosts=("h",), placeholder="PH", env=None)
    b = Binding(name="b", injector="inj-b", provider="env", secret="X",
                hosts=("h",), placeholder="PH", env=None)
    assert config_fingerprint([a]) != config_fingerprint([b])


def test_fingerprint_includes_scripted_compile_metadata(xdg, workspaces_dir):
    """A scripted injector's compile metadata (here location_kind) is pushed and
    the proxy compiles with it, so it must be in the fingerprint -- editing it
    must re-push. Same script source, differing only in location_kind."""
    from credproxy_cli.core.model.bindings import Binding, config_fingerprint
    from credproxy_cli.core.paths import scripts_config_dir
    sd = scripts_config_dir()
    sd.mkdir(parents=True, exist_ok=True)
    (sd / "s.star").write_text("def on_request():\n    return True\n")
    base = 'scheme = "script"\nscript = "s"\napi = 1\nfamily = "sign"\nslots = ["key"]\n'
    _write_injector(xdg, "s-hdr", base + 'location_kind = "header"\n')
    _write_injector(xdg, "s-body", base + 'location_kind = "body"\n')
    a = Binding(name="b", injector="s-hdr", provider="env", secret={"key": "X"},
                hosts=("h",), placeholder=None, env=None)
    b = Binding(name="b", injector="s-body", provider="env", secret={"key": "X"},
                hosts=("h",), placeholder=None, env=None)
    assert config_fingerprint([a]) != config_fingerprint([b])


# ---- _block_spans (surgical-edit boundary scan) ------------------------------
#
# The spans must be 1:1 with the [[binding]] tables tomllib parses, in order --
# materialize/remove index into them by binding position, so a miscount or a
# wrong boundary edits/deletes the wrong block and corrupts the source-of-truth
# file. These pin the two regressions C4 plus the everyday multi-line array.


def _spans(text):
    from credproxy_cli.core.model.bindings import _block_spans
    return _block_spans(text)


def test_block_spans_tolerates_header_comment():
    """`[[binding]]  # note` is a valid header; it used to yield zero spans, so
    materialize did spans[0] -> IndexError."""
    import tomllib
    t = '[[binding]]  # note\nname = "a"\nhosts = ["x.com"]\n'
    assert len(_spans(t)) == len(tomllib.loads(t)["binding"]) == 1
    assert _spans(t) == [(0, 3)]


def test_block_spans_not_split_by_bracket_led_array_line():
    """A '['-led line inside a multi-line array value must not be read as a table
    header (which used to split the block and misplace edits)."""
    import tomllib
    t = ('[[binding]]\nname = "a"\nmatrix = [\n  [1, 2],\n  [3, 4],\n]\n'
         'hosts = ["x"]\n\n[[binding]]\nname = "b"\nhosts = ["y"]\n')
    spans = _spans(t)
    assert len(spans) == len(tomllib.loads(t)["binding"]) == 2
    lines = t.splitlines(keepends=True)
    # block 0 must reach its own hosts line, not stop at `matrix = [`.
    assert "hosts" in "".join(lines[spans[0][0]:spans[0][1]])


def test_block_spans_ignores_brackets_and_hashes_in_strings():
    """Brackets/`#` inside string values aren't array depth or comments."""
    import tomllib
    t = ('[[binding]]\nname = "a"\nhosts = ["api].com"]\nenv = "A#B"\n'
         '\n[[binding]]\nname = "b"\nhosts = ["y"]\n')
    assert len(_spans(t)) == len(tomllib.loads(t)["binding"]) == 2


# ---- name-required (hand-authored intent) -----------------------------------


def test_block_spans_tolerate_commented_block_header(xdg, workspaces_dir):
    """A `[[binding]]  # note` header still parses/loads (span machinery must
    match it, or remove/load would miscount)."""
    from credproxy_cli.core.model.bindings import load_bindings
    ws = _write_ws(workspaces_dir, "cmthdr", """\
        image = "x"

        [[binding]]  # github
        name     = "gh"
        injector = "bearer"
        provider = "env"
        secret   = "GITHUB_TOKEN"
        hosts    = ["api.github.com"]
    """)
    assert load_bindings(ws)[0].name == "gh"
    assert "# github" in ws.config_path.read_text()


def test_missing_name_rejected_with_suggestion(xdg, workspaces_dir):
    """A hand-authored `[[binding]]` without a `name` is rejected with a
    prescriptive fix suggesting `<injector>-<provider>` -- the load path no longer
    auto-names + writes back."""
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.bindings import load_bindings
    ws = _write_ws(workspaces_dir, "noname", """\
        image = "x"

        [[binding]]
        injector = "bearer"
        provider = "env"
        secret   = "GITHUB_TOKEN"
        hosts    = ["api.github.com"]
    """)
    with pytest.raises(ConfigError, match=r'missing a required `name`.*bearer-env'):
        load_bindings(ws)


# ---- append_binding / remove_binding ----------------------------------------


def test_append_binding_round_trip(xdg, workspaces_dir):
    """append_binding writes a valid block; remove_binding removes it."""
    ws = _write_ws(workspaces_dir, "ar", 'image = "x"\n')
    from credproxy_cli.core.model.bindings import Binding, append_binding, remove_binding
    import tomllib

    b = Binding(
        name="mygh", injector="bearer", provider="env",
        secret="TOK", hosts=("api.github.com",), placeholder="ghp_xxx", env="GITHUB_TOKEN",
    )
    append_binding(ws, b)

    raw = tomllib.loads(ws.config_path.read_text())
    assert len(raw.get("binding", [])) == 1
    on_disk = raw["binding"][0]
    assert on_disk["name"] == "mygh"
    assert on_disk["placeholder"] == "ghp_xxx"
    assert on_disk["env"] == "GITHUB_TOKEN"

    remove_binding(ws, "mygh")
    raw2 = tomllib.loads(ws.config_path.read_text())
    assert len(raw2.get("binding", [])) == 0


def test_remove_binding_with_multiline_hosts_neighbor(xdg, workspaces_dir):
    """remove_binding deletes exactly the target block even when a sibling has a
    multi-line `hosts` array -- the depth-aware span scan must not cut that array
    short or delete the wrong lines."""
    ws = _write_ws(workspaces_dir, "rmmulti", """\
        image = "x"

        [[binding]]
        name     = "keep"
        injector = "bearer"
        provider = "env"
        secret   = "A"
        hosts    = [
          "a.example.com",
          "b.example.com",
        ]
        placeholder = "ph_keep"

        [[binding]]
        name     = "drop"
        injector = "bearer"
        provider = "env"
        secret   = "B"
        hosts    = ["c.example.com"]
        placeholder = "ph_drop"
    """)
    from credproxy_cli.core.model.bindings import remove_binding
    import tomllib

    remove_binding(ws, "drop")
    raw = tomllib.loads(ws.config_path.read_text())
    assert [b["name"] for b in raw["binding"]] == ["keep"]
    assert raw["binding"][0]["hosts"] == ["a.example.com", "b.example.com"]


def test_remove_binding_with_child_table_first(xdg, workspaces_dir):
    """A hand-written `[binding.params]` child sub-table must be removed WITH its
    parent `[[binding]]`, not orphaned -- even when the child-bearing binding is
    FIRST (its child must not end its span early and re-attach to nothing/leak)."""
    from credproxy_cli.core.model.bindings import load_bindings, remove_binding
    import tomllib

    ws = _write_ws(workspaces_dir, "childfirst", """\
        image = "x"

        [[binding]]
        name     = "aws"
        injector = "sigv4"
        provider = "env"
        secret   = { access_key_id = "AKID", secret_access_key = "SAK" }
        hosts    = ["s3.amazonaws.com"]
        [binding.params]
        region  = "us-east-1"
        service = "s3"

        [[binding]]
        name     = "keep"
        injector = "sigv4"
        provider = "env"
        secret   = { access_key_id = "AK2", secret_access_key = "SK2" }
        hosts    = ["ec2.amazonaws.com"]
        [binding.params]
        region  = "us-west-2"
        service = "ec2"
    """)
    assert [b.name for b in load_bindings(ws)] == ["aws", "keep"]

    # The survivor's block (from its header to EOF) must be byte-identical after
    # removing the FIRST binding.
    before = ws.config_path.read_text()
    survivor_block = before[before.index('name     = "keep"'):]

    remove_binding(ws, "aws")
    after = ws.config_path.read_text()
    raw = tomllib.loads(after)                             # file still parses
    assert [b["name"] for b in raw["binding"]] == ["keep"]
    assert raw["binding"][0]["params"] == {"region": "us-west-2", "service": "ec2"}
    assert "region  = \"us-east-1\"" not in after          # child went with parent
    assert after.endswith(survivor_block)                 # survivor byte-identical


def test_remove_binding_with_child_table_last(xdg, workspaces_dir):
    """Same, but the child-bearing binding is LAST: removing it must not leave an
    orphaned `[binding.params]` and must leave the survivor untouched."""
    from credproxy_cli.core.model.bindings import load_bindings, remove_binding
    import tomllib

    ws = _write_ws(workspaces_dir, "childlast", """\
        image = "x"

        [[binding]]
        name     = "keep"
        injector = "sigv4"
        provider = "env"
        secret   = { access_key_id = "AK2", secret_access_key = "SK2" }
        hosts    = ["ec2.amazonaws.com"]
        [binding.params]
        region  = "us-west-2"
        service = "ec2"

        [[binding]]
        name     = "aws"
        injector = "sigv4"
        provider = "env"
        secret   = { access_key_id = "AKID", secret_access_key = "SAK" }
        hosts    = ["s3.amazonaws.com"]
        [binding.params]
        region  = "us-east-1"
        service = "s3"
    """)
    assert [b.name for b in load_bindings(ws)] == ["keep", "aws"]

    before = ws.config_path.read_text()
    survivor_block = before[:before.index('[[binding]]\nname     = "aws"')]

    remove_binding(ws, "aws")
    after = ws.config_path.read_text()
    raw = tomllib.loads(after)                             # file still parses
    assert [b["name"] for b in raw["binding"]] == ["keep"]
    assert raw["binding"][0]["params"] == {"region": "us-west-2", "service": "ec2"}
    assert "region  = \"us-east-1\"" not in after          # child went with parent
    # Everything outside the removed block is byte-identical.
    assert after == survivor_block.rstrip("\n") + "\n"


def test_remove_binding_inline_array_form_refused(xdg, workspaces_dir):
    """The inline-array form (`binding = [ { ... } ]`) parses to entries but has
    NO removable block span -- `remove_binding` must refuse prescriptively rather
    than IndexError / edit the wrong block."""
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.bindings import remove_binding

    ws = _write_ws(workspaces_dir, "inlinearr", """\
        image = "x"
        binding = [
          { name = "a", injector = "bearer", provider = "env", secret = "A", hosts = ["a.io"] },
        ]
    """)
    with pytest.raises(ConfigError, match="isn't a removable `\\[\\[binding\\]\\]` block"):
        remove_binding(ws, "a")


def test_remove_binding_duplicate_name_refused(xdg, workspaces_dir):
    """Two `[[binding]]` blocks with the same name: `remove` must refuse as
    ambiguous rather than silently drop the first."""
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.bindings import remove_binding

    ws = _write_ws(workspaces_dir, "dupname", """\
        image = "x"

        [[binding]]
        name     = "dup"
        injector = "bearer"
        provider = "env"
        secret   = "A"
        hosts    = ["a.io"]

        [[binding]]
        name     = "dup"
        injector = "bearer"
        provider = "env"
        secret   = "B"
        hosts    = ["b.io"]
    """)
    with pytest.raises(ConfigError, match="defined more than once"):
        remove_binding(ws, "dup")


def test_append_binding_multi_slot_inline_table(xdg, workspaces_dir):
    """A multi-slot secret round-trips through an inline table."""
    ws = _write_ws(workspaces_dir, "ms", 'image = "x"\n')
    from credproxy_cli.core.model.bindings import Binding, append_binding
    import tomllib

    b = Binding(
        name="aws", injector="bearer", provider="env",
        secret={"access_key_id": "AKID", "secret_access_key": "SAK"},
        hosts=("h.io",), placeholder=None, env=None,
    )
    append_binding(ws, b)
    raw = tomllib.loads(ws.config_path.read_text())
    assert raw["binding"][0]["secret"] == {
        "access_key_id": "AKID", "secret_access_key": "SAK"
    }


def test_append_binding_escapes_special_chars(xdg, workspaces_dir):
    """A ref/host/env/placeholder with quotes or backslashes round-trips
    instead of corrupting the TOML."""
    ws = _write_ws(workspaces_dir, "esc", 'image = "x"\n')
    from credproxy_cli.core.model.bindings import Binding, append_binding
    import tomllib

    nasty = 'op://v/it"em\\x'
    b = Binding(name="b1", injector="bearer", provider="env",
                secret=nasty, hosts=('h".io',), placeholder='p"h', env='E"V')
    append_binding(ws, b)
    raw = tomllib.loads(ws.config_path.read_text())  # must not raise
    od = raw["binding"][0]
    assert od["secret"] == nasty
    assert od["hosts"] == ['h".io']
    assert od["placeholder"] == 'p"h'
    assert od["env"] == 'E"V'


def test_append_binding_multi_slot_escapes(xdg, workspaces_dir):
    ws = _write_ws(workspaces_dir, "escms", 'image = "x"\n')
    from credproxy_cli.core.model.bindings import Binding, append_binding
    import tomllib

    b = Binding(name="aws", injector="sigv4", provider="env",
                secret={"access_key_id": 'a"b', "secret_access_key": "c\\d"},
                hosts=("h",), placeholder=None, env=None)
    append_binding(ws, b)
    raw = tomllib.loads(ws.config_path.read_text())  # must not raise
    assert raw["binding"][0]["secret"] == {
        "access_key_id": 'a"b', "secret_access_key": "c\\d"}


def test_test_binding_shared_ref_counted_once(xdg, workspaces_dir):
    """value_len sums distinct fetched values, so a ref shared by two slots is
    counted once."""
    from credproxy_cli.core.model.bindings import Binding, test_binding

    b = Binding(name="x", injector="sigv4", provider="env",
                secret={"access_key_id": "SAME", "secret_access_key": "SAME"},
                hosts=("h",), placeholder=None, env=None)
    r = test_binding(b, fetch_many=lambda p, refs: {ref: "ABCD" for ref in refs})
    assert r.ok and r.value_len == 4  # not 8


def test_append_then_remove_preserves_comments_byte_for_byte(xdg, workspaces_dir):
    """The intent TOML is hand-owned: appending a binding then removing it leaves
    every existing byte (comments everywhere included) untouched -- machine edits
    only append a whole block / delete a whole block."""
    from credproxy_cli.core.model.bindings import (
        Binding, append_binding, remove_binding)
    original = (
        "# top-of-file comment\n"
        'image = "x"   # inline on image\n'
        "\n"
        "# a hand-authored binding, with comments\n"
        "[[binding]]\n"
        'name     = "keep"   # do not touch\n'
        'injector = "bearer"\n'
        'provider = "env"\n'
        'secret   = "TOK"\n'
        'hosts    = ["api.github.com"]   # scope\n'
        "# trailing comment at EOF\n"
    )
    ws = _write_ws(workspaces_dir, "cmts", original)
    ws.ensure_state_dir()

    append_binding(ws, Binding(
        name="added", injector="bearer", provider="env", secret="TOK2",
        hosts=("api.example.com",), placeholder=None, env=None))
    after_add = ws.config_path.read_text()
    # The original text is preserved verbatim as a prefix; only a new block is
    # appended at EOF.
    assert after_add.startswith(original)

    remove_binding(ws, "added")
    # Back to byte-identical (the appended block, and only it, is gone).
    assert ws.config_path.read_text() == original


def test_remove_binding_not_found(xdg, workspaces_dir):
    ws = _write_ws(workspaces_dir, "rm_ghost", 'image = "x"\n')
    from credproxy_cli.core.model.bindings import remove_binding
    from credproxy_cli.core.errors import ConfigError

    with pytest.raises(ConfigError, match="not found"):
        remove_binding(ws, "nosuchbinding")


def test_append_multiple_then_remove_middle(xdg, workspaces_dir):
    """Removing the second of three bindings leaves the other two intact."""
    ws = _write_ws(workspaces_dir, "mid", 'image = "x"\n')
    from credproxy_cli.core.model.bindings import Binding, append_binding, remove_binding
    import tomllib

    def make(name, host):
        return Binding(name=name, injector="bearer", provider="env",
                       secret="X", hosts=(host,), placeholder="phx", env=None)

    append_binding(ws, make("first", "a.io"))
    append_binding(ws, make("second", "b.io"))
    append_binding(ws, make("third", "c.io"))

    remove_binding(ws, "second")
    raw = tomllib.loads(ws.config_path.read_text())
    names = [b["name"] for b in raw.get("binding", [])]
    assert names == ["first", "third"]


# ---- wire_config shape -------------------------------------------------------


def test_wire_config_shape(xdg, workspaces_dir):
    """wire_config produces the scheme-aware JSON shape (with stub fetch)."""
    from credproxy_cli.core.model.bindings import Binding, wire_config

    b = Binding(
        name="gh", injector="bearer", provider="env",
        secret="GITHUB_TOKEN", hosts=("api.github.com",),
        placeholder="ghp_test_placeholder_val123456789012",
        env="GITHUB_TOKEN",
    )

    def fake_fetch_many(provider, refs):
        return {ref: "real_secret_value" for ref in refs}

    result = wire_config([b], fetch_many=fake_fetch_many)

    assert "bindings" in result
    assert len(result["bindings"]) == 1
    entry = result["bindings"][0]
    assert entry["name"] == "gh"
    assert entry["hosts"] == ["api.github.com"]
    assert entry["scheme"] == "bearer"
    assert entry["params"] == {"header": "Authorization"}
    assert entry["placeholder"] == "ghp_test_placeholder_val123456789012"
    assert entry["secret"] == {"value": "real_secret_value"}
    assert "real" not in entry
    assert entry["env"] == "GITHUB_TOKEN"


def test_wire_config_multi_slot_secret(xdg, workspaces_dir):
    """A multi-slot secret resolves each slot via the batch fetch."""
    from credproxy_cli.core.model.bindings import Binding, wire_config

    b = Binding(
        name="aws", injector="bearer", provider="env",
        secret={"value": "AWS_KEY"}, hosts=("h.io",),
        placeholder="credproxy_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        env=None,
    )
    seen = {}

    def fake_fetch_many(provider, refs):
        seen["refs"] = list(refs)
        return {ref: f"val-of-{ref}" for ref in refs}

    result = wire_config([b], fetch_many=fake_fetch_many)
    assert seen["refs"] == ["AWS_KEY"]
    assert result["bindings"][0]["secret"] == {"value": "val-of-AWS_KEY"}


def test_wire_config_sign_scheme_multi_slot_no_placeholder(xdg, workspaces_dir):
    """A sigv4 binding resolves both slots and carries no placeholder."""
    from credproxy_cli.core.model.bindings import Binding, wire_config

    b = Binding(
        name="aws", injector="sigv4", provider="env",
        secret={"access_key_id": "AKID_REF", "secret_access_key": "SAK_REF"},
        hosts=("sts.amazonaws.com",), placeholder=None, env=None,
    )
    result = wire_config([b], fetch_many=lambda p, refs: {r: f"v-{r}" for r in refs})
    entry = result["bindings"][0]
    assert entry["scheme"] == "sigv4"
    assert entry["secret"] == {"access_key_id": "v-AKID_REF",
                               "secret_access_key": "v-SAK_REF"}
    assert "placeholder" not in entry


def test_validate_sign_scheme_requires_both_slots(xdg, workspaces_dir):
    from credproxy_cli.core.model.bindings import Binding, validate
    from credproxy_cli.core.errors import ConfigError

    b = Binding(name="aws", injector="sigv4", provider="env",
                secret="LONE_REF", hosts=("sts.amazonaws.com",),
                placeholder=None, env=None)
    with pytest.raises(ConfigError, match="needs secret slot"):
        validate([b], "test")


def test_sign_scheme_resolves_no_placeholder(xdg, workspaces_dir):
    """A sign-family binding holds no placeholder -- the resolver leaves it None
    and records nothing in the lock."""
    ws = _write_ws(workspaces_dir, "awsmat", """\
        image = "x"

        [[binding]]
        name     = "aws"
        injector = "sigv4"
        provider = "env"
        secret   = { access_key_id = "AKID", secret_access_key = "SAK" }
        hosts    = ["sts.amazonaws.com"]
    """)
    from credproxy_cli.core.model.resolver import resolve_workspace

    r = resolve_workspace(ws)
    assert r.bindings[0].placeholder is None
    assert r.lock["placeholders"] == {}
    assert "placeholder" not in ws.config_path.read_text()


def test_wire_config_no_env_field_when_absent(xdg, workspaces_dir):
    """wire_config omits `env` key when neither binding nor injector has one."""
    from credproxy_cli.core.model.bindings import Binding, wire_config

    b = Binding(
        name="plain", injector="bearer", provider="env",
        secret="TOK", hosts=("example.com",),
        placeholder="credproxy_testplacholder12345678901",
        env=None,
    )

    result = wire_config([b], fetch_many=lambda p, refs: {r: "val" for r in refs})
    entry = result["bindings"][0]
    assert "env" not in entry


def test_wire_config_binding_env_overrides_injector(xdg, workspaces_dir):
    """Binding-level env overrides the injector's suggested env."""
    from credproxy_cli.core.model.bindings import Binding, wire_config

    b = Binding(
        name="gh", injector="bearer", provider="env",
        secret="X", hosts=("api.github.com",),
        placeholder="ghp_test_placeholder_val123456789012",
        env="MY_CUSTOM_TOKEN",
    )

    result = wire_config([b], fetch_many=lambda p, refs: {r: "v" for r in refs})
    assert result["bindings"][0]["env"] == "MY_CUSTOM_TOKEN"


# ---- env = false (suppress the injector's env hint) --------------------------


def test_parse_env_false_suppresses(xdg):
    from credproxy_cli.core.model.bindings import _parse_bindings

    raw = {"binding": [{"injector": "bearer", "provider": "env", "secret": "X",
                        "hosts": ["h.io"], "env": False}]}
    b = _parse_bindings(raw, "test")[0]
    assert b.env is None
    assert b.env_suppressed is True


def test_parse_env_empty_string_rejected(xdg):
    from credproxy_cli.core.model.bindings import _parse_bindings
    from credproxy_cli.core.errors import ConfigError

    raw = {"binding": [{"injector": "bearer", "provider": "env", "secret": "X",
                        "hosts": ["h.io"], "env": ""}]}
    with pytest.raises(ConfigError, match="non-empty string"):
        _parse_bindings(raw, "test")


def test_parse_env_true_rejected(xdg):
    from credproxy_cli.core.model.bindings import _parse_bindings
    from credproxy_cli.core.errors import ConfigError

    raw = {"binding": [{"injector": "bearer", "provider": "env", "secret": "X",
                        "hosts": ["h.io"], "env": True}]}
    with pytest.raises(ConfigError, match="`true` is not valid"):
        _parse_bindings(raw, "test")


def test_parse_env_non_identifier_rejected(xdg):
    """The env NAME lands unquoted in /exports.sh's `export NAME=...`, so a
    non-identifier (space, dash, leading digit) is rejected at parse."""
    from credproxy_cli.core.model.bindings import _parse_bindings
    from credproxy_cli.core.errors import ConfigError

    # "FOO\n" (expressible in TOML as an escaped string) is the sharp case: a
    # `$`-anchored .match() accepts it, injecting a literal newline into the
    # export line -- only .fullmatch() rejects it.
    for bad in ("MY TOKEN", "MY-TOKEN", "1TOKEN", "FOO\n"):
        raw = {"binding": [{"injector": "bearer", "provider": "env",
                            "secret": "X", "hosts": ["h.io"], "env": bad}]}
        with pytest.raises(ConfigError, match="valid shell/env identifier"):
            _parse_bindings(raw, "test")


def test_validate_env_non_identifier_rejected(xdg, workspaces_dir):
    """validate() re-checks env (the constructed-Binding `binding add --env`
    path), so a bad --env fails at add time, not on the next load."""
    from credproxy_cli.core.model.bindings import Binding, validate
    from credproxy_cli.core.errors import ConfigError

    b = Binding(name="b", injector="bearer", provider="env", secret="X",
                hosts=("h.io",), placeholder="p", env="MY TOKEN")
    with pytest.raises(ConfigError, match="valid shell/env identifier"):
        validate([b], "test")


def test_injector_env_hint_non_identifier_rejected(xdg, workspaces_dir):
    """An injector manifest's `env` hint is held to the same identifier rule."""
    from credproxy_cli.core.model.injectors import find_injector
    from credproxy_cli.core.errors import InjectorError

    _write_injector(xdg, "bad-env", 'scheme = "bearer"\nenv = "MY TOKEN"\n')
    with pytest.raises(InjectorError, match="valid shell/env identifier"):
        find_injector("bad-env")


def test_wire_config_env_false_omits_env_despite_injector_hint(xdg, workspaces_dir):
    """`env = false` suppresses the injector's suggested env: the wire entry has
    no `env` key even though the injector supplies one."""
    from credproxy_cli.core.model.bindings import Binding, wire_config
    _write_injector(xdg, "inj-hint", 'scheme = "bearer"\nenv = "HINT_TOKEN"\n')

    inherit = Binding(name="a", injector="inj-hint", provider="env", secret="X",
                      hosts=("h",), placeholder="PH", env=None)
    assert wire_config([inherit], fetch_many=lambda p, r: {x: "v" for x in r}
                       )["bindings"][0]["env"] == "HINT_TOKEN"

    suppressed = Binding(name="a", injector="inj-hint", provider="env",
                         secret="X", hosts=("h",), placeholder="PH",
                         env=None, env_suppressed=True)
    entry = wire_config([suppressed],
                        fetch_many=lambda p, r: {x: "v" for x in r})["bindings"][0]
    assert "env" not in entry


def test_fingerprint_changes_when_env_suppressed(xdg, workspaces_dir):
    """Toggling `env = false` changes the EFFECTIVE env (hint -> None), so the
    fingerprint changes and the config re-pushes."""
    from credproxy_cli.core.model.bindings import Binding, config_fingerprint
    _write_injector(xdg, "inj-hint", 'scheme = "bearer"\nenv = "HINT_TOKEN"\n')
    inherit = Binding(name="b", injector="inj-hint", provider="env", secret="X",
                      hosts=("h",), placeholder="PH", env=None)
    suppressed = Binding(name="b", injector="inj-hint", provider="env",
                         secret="X", hosts=("h",), placeholder="PH",
                         env=None, env_suppressed=True)
    assert config_fingerprint([inherit]) != config_fingerprint([suppressed])


def test_render_binding_block_writes_env_false(xdg):
    """The TOML writer emits `env = false` for a suppressed binding (round-trips
    back through the parser as suppression)."""
    from credproxy_cli.core.model.bindings import (
        Binding, _render_binding_block, _parse_bindings)
    import tomllib

    b = Binding(name="b", injector="bearer", provider="env", secret="X",
                hosts=("h.io",), placeholder="p", env=None, env_suppressed=True)
    block = _render_binding_block(b)
    assert "env      = false" in block
    parsed = _parse_bindings(tomllib.loads(block), "test")[0]
    assert parsed.env_suppressed is True and parsed.env is None


def test_binding_add_no_env_writes_env_false(xdg, workspaces_dir):
    """`binding add --no-env` records `env = false` in the TOML."""
    from test_porcelain import _run
    _write_ws(workspaces_dir, "m", 'image = "x"\n')
    ec, out, err = _run(["workspace", "m", "binding", "add",
                         "--injector", "bearer", "--provider", "env",
                         "--secret", "T", "--host", "api.github.com", "--no-env"])
    assert ec == 0, err
    text = (workspaces_dir / "m.toml").read_text()
    assert "env      = false" in text
    from credproxy_cli.core.model.bindings import _parse_bindings
    import tomllib
    b = _parse_bindings(tomllib.loads(text), "m")[0]
    assert b.env_suppressed is True and b.env is None


def test_binding_add_env_and_no_env_mutually_exclusive(xdg, workspaces_dir):
    from test_porcelain import _run
    _write_ws(workspaces_dir, "m", 'image = "x"\n')
    ec, out, err = _run(["workspace", "m", "binding", "add",
                         "--injector", "bearer", "--provider", "env",
                         "--secret", "T", "--host", "api.github.com",
                         "--env", "FOO", "--no-env"])
    assert ec != 0
    assert "not allowed with" in (out + err) or "--no-env" in (out + err)


def test_wire_config_missing_placeholder_raises(xdg):
    from credproxy_cli.core.model.bindings import Binding, wire_config
    from credproxy_cli.core.errors import ConfigError

    b = Binding(
        name="noph", injector="bearer", provider="env",
        secret="X", hosts=("h.io",), placeholder=None, env=None,
    )
    with pytest.raises(ConfigError, match="no placeholder"):
        wire_config([b], fetch_many=lambda p, refs: {r: "v" for r in refs})


# ---- provider batching (one invocation per provider) ------------------------


def _bearer(name, provider, secret, host="h.io"):
    from credproxy_cli.core.model.bindings import Binding
    return Binding(
        name=name, injector="bearer", provider=provider, secret=secret,
        hosts=(host,), placeholder=f"credproxy_{name}_xxxxxxxxxxxxxxxxxxxx",
        env=None,
    )


def test_resolve_secrets_groups_and_dedups(xdg, workspaces_dir):
    """resolve_secrets makes ONE call per distinct provider with the deduped
    union of refs across the bindings that share it."""
    from credproxy_cli.core.model.bindings import resolve_secrets

    calls = []

    def fetch(provider, refs):
        calls.append((provider, list(refs)))
        return {r: f"{provider}:{r}" for r in refs}

    bindings = [
        _bearer("a", "vault", "A"),
        _bearer("b", "vault", "B"),
        _bearer("c", "vault", "A"),   # duplicate ref -> deduped
        _bearer("d", "env", "Z"),
    ]
    resolved = resolve_secrets(bindings, fetch)

    # One call per provider, refs deduped and order-preserving.
    assert calls == [("vault", ["A", "B"]), ("env", ["Z"])]
    assert resolved == {
        "vault": {"A": "vault:A", "B": "vault:B"},
        "env": {"Z": "env:Z"},
    }


def test_wire_config_one_call_per_provider(xdg, workspaces_dir):
    """Several bindings sharing a provider resolve in a single invocation, and
    every binding still gets its own resolved value."""
    from credproxy_cli.core.model.bindings import wire_config

    calls = []

    def fetch(provider, refs):
        calls.append(provider)
        return {r: f"val-{r}" for r in refs}

    bindings = [
        _bearer("a", "vault", "A"),
        _bearer("b", "vault", "B"),
        _bearer("c", "env", "C"),
    ]
    result = wire_config(bindings, fetch_many=fetch)

    assert calls == ["vault", "env"]  # one per provider, not one per binding
    secrets = {e["name"]: e["secret"] for e in result["bindings"]}
    assert secrets == {
        "a": {"value": "val-A"},
        "b": {"value": "val-B"},
        "c": {"value": "val-C"},
    }


def test_wire_config_aborts_before_fetch_on_bad_placeholder(xdg, workspaces_dir):
    """A placeholder config error aborts WITHOUT paying any provider call (so a
    vault is never needlessly unlocked for a config that can't push)."""
    from credproxy_cli.core.model.bindings import Binding, wire_config
    from credproxy_cli.core.errors import ConfigError

    called = []

    def fetch(provider, refs):
        called.append(provider)
        return {r: "v" for r in refs}

    good = _bearer("good", "vault", "A")
    bad = Binding(name="bad", injector="bearer", provider="vault",
                  secret="B", hosts=("h.io",), placeholder=None, env=None)
    with pytest.raises(ConfigError, match="no placeholder"):
        wire_config([good, bad], fetch_many=fetch)
    assert called == []  # nothing fetched


def test_test_bindings_batches_per_provider(xdg, workspaces_dir):
    """test_bindings resolves a shared provider once and reports each binding."""
    from credproxy_cli.core.model.bindings import test_bindings

    calls = []

    def fetch(provider, refs):
        calls.append((provider, list(refs)))
        return {r: "ABCD" for r in refs}

    bindings = [_bearer("a", "vault", "A"), _bearer("b", "vault", "B")]
    results = test_bindings(bindings, fetch_many=fetch)

    assert calls == [("vault", ["A", "B"])]  # one unlock for both
    assert [(r.name, r.ok, r.value_len) for r in results] == [
        ("a", True, 4), ("b", True, 4)
    ]


def test_test_bindings_failure_attributed_per_binding(xdg, workspaces_dir):
    """When a provider's batch fails, test_bindings retries per binding so the
    failure pins to the right binding(s) -- the healthy ones still pass."""
    from credproxy_cli.core.model.bindings import test_bindings
    from credproxy_cli.core.errors import ProviderError

    calls = []

    def fetch(provider, refs):
        calls.append(list(refs))
        if "BAD" in refs:
            raise ProviderError("secret 'BAD' not found")
        return {r: "ABCD" for r in refs}

    bindings = [_bearer("ok", "vault", "GOOD"), _bearer("broken", "vault", "BAD")]
    results = test_bindings(bindings, fetch_many=fetch)

    # batch [GOOD, BAD] fails -> per-binding retry [GOOD] (ok) and [BAD] (fail).
    assert calls == [["GOOD", "BAD"], ["GOOD"], ["BAD"]]
    by_name = {r.name: r for r in results}
    assert by_name["ok"].ok and by_name["ok"].value_len == 4
    assert not by_name["broken"].ok
    assert "not found" in by_name["broken"].error


def test_test_bindings_preserves_order(xdg, workspaces_dir):
    """Results come back in input order even across interleaved providers."""
    from credproxy_cli.core.model.bindings import test_bindings

    bindings = [
        _bearer("a", "vault", "A"),
        _bearer("b", "env", "B"),
        _bearer("c", "vault", "C"),
    ]
    results = test_bindings(bindings, fetch_many=lambda p, refs: {r: "xy" for r in refs})
    assert [r.name for r in results] == ["a", "b", "c"]
    assert all(r.ok for r in results)


def test_atomic_write_text(tmp_path):
    """_atomic_write_text writes correct content and leaves no temp file (the
    workspace TOML is the single source of truth -- a partial write would lose
    it)."""
    from credproxy_cli.core.model.bindings import _atomic_write_text
    p = tmp_path / "x.toml"
    _atomic_write_text(p, "hello")
    assert p.read_text() == "hello"
    _atomic_write_text(p, "world")          # overwrite
    assert p.read_text() == "world"
    assert list(tmp_path.glob("*.tmp")) == []   # no leftover temp
