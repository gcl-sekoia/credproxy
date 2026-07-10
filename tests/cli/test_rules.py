"""Tests for core/rules.py: parse/validate, auto-name, materialization,
append/remove surgical edits, wire entries, the matcher, and the combined
fingerprint."""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest


def _write_ws(workspaces_dir: Path, name: str, content: str):
    from credproxy_cli.core.model.workspace import Workspace
    p = workspaces_dir / f"{name}.toml"
    p.write_text(textwrap.dedent(content))
    return Workspace(name)


# ---- parse / validate --------------------------------------------------------

def test_parse_block_rule(xdg, workspaces_dir):
    from credproxy_cli.core.model.rules import load_rules
    ws = _write_ws(workspaces_dir, "w", """
        image = "x"
        [[rule]]
        name = "gh"
        hosts = ["api.github.com"]
        methods = ["DELETE"]
        path = "/repos/**"
        action = "block"
    """)
    (r,) = load_rules(ws)
    assert r.name == "gh" and r.action == "block"
    assert r.methods == ("DELETE",) and r.path == "/repos/**"
    assert r.effective_visible is True


def test_rewrite_defaults_hidden(xdg, workspaces_dir):
    from credproxy_cli.core.model.rules import load_rules
    ws = _write_ws(workspaces_dir, "w", """
        image = "x"
        [[rule]]
        name = "rw"
        hosts = ["api.example.com"]
        action = "rewrite"
        set_headers = { X-Env = "sandbox" }
    """)
    (r,) = load_rules(ws)
    assert r.effective_visible is False


def test_unknown_action_rejected(xdg, workspaces_dir):
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.rules import load_rules
    ws = _write_ws(workspaces_dir, "w", """
        image = "x"
        [[rule]]
        hosts = ["h.example.com"]
        action = "nope"
    """)
    with pytest.raises(ConfigError, match="action must be one of"):
        load_rules(ws)


@pytest.mark.parametrize("op", [
    'set_headers = { Host = "evil.com" }',
    'set_headers = { host = "evil.com" }',
    'remove_headers = ["Host"]',
    'set_headers = { ":authority" = "evil.com" }',
])
def test_host_rewrite_rejected(xdg, workspaces_dir, op):
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.rules import load_rules
    ws = _write_ws(workspaces_dir, "w", f"""
        image = "x"
        [[rule]]
        name = "h"
        hosts = ["api.github.com"]
        action = "rewrite"
        {op}
    """)
    with pytest.raises(ConfigError, match="authority|Host"):
        load_rules(ws)


def test_misplaced_field_rejected(xdg, workspaces_dir):
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.rules import load_rules
    ws = _write_ws(workspaces_dir, "w", """
        image = "x"
        [[rule]]
        hosts = ["h.example.com"]
        action = "block"
        body = "nope"
    """)
    with pytest.raises(ConfigError, match="not valid for action"):
        load_rules(ws)


def test_respond_requires_status(xdg, workspaces_dir):
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.rules import load_rules
    ws = _write_ws(workspaces_dir, "w", """
        image = "x"
        [[rule]]
        hosts = ["h.example.com"]
        action = "respond"
    """)
    with pytest.raises(ConfigError, match="status"):
        load_rules(ws)


def test_bad_host_pattern_rejected(xdg, workspaces_dir):
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.rules import load_rules
    ws = _write_ws(workspaces_dir, "w", """
        image = "x"
        [[rule]]
        hosts = ["*.com"]
        action = "block"
    """)
    with pytest.raises(ConfigError):
        load_rules(ws)


def test_empty_methods_rejected(xdg, workspaces_dir):
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.rules import load_rules
    ws = _write_ws(workspaces_dir, "w", """
        image = "x"
        [[rule]]
        hosts = ["h.example.com"]
        action = "block"
        methods = []
    """)
    with pytest.raises(ConfigError, match="methods must be a non-empty"):
        load_rules(ws)


def test_bad_path_rejected(xdg, workspaces_dir):
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.rules import load_rules
    ws = _write_ws(workspaces_dir, "w", """
        image = "x"
        [[rule]]
        hosts = ["h.example.com"]
        action = "block"
        path = "repos"
    """)
    with pytest.raises(ConfigError, match="must start with"):
        load_rules(ws)


def test_missing_script_rejected(xdg, workspaces_dir):
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.rules import load_rules
    ws = _write_ws(workspaces_dir, "w", """
        image = "x"
        [[rule]]
        name = "s"
        hosts = ["h.example.com"]
        action = "script"
        script = "does-not-exist-zzz"
    """)
    with pytest.raises(ConfigError, match="not found"):
        load_rules(ws)


# ---- auto-name + name-required ----------------------------------------------

def test_auto_name():
    from credproxy_cli.core.model.rules import Rule, _auto_name
    r = Rule(name=None, hosts=("api.github.com",), action="block")
    assert _auto_name(r, set()) == "block-api-github-com"
    assert _auto_name(r, {"block-api-github-com"}) == "block-api-github-com-2"


def test_missing_name_rejected_with_suggestion(xdg, workspaces_dir):
    """A hand-authored `[[rule]]` without a `name` is rejected with a prescriptive
    fix (the intent TOML is hand-owned; names are no longer auto-written)."""
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.rules import load_rules
    ws = _write_ws(workspaces_dir, "w", """
        image = "x"
        [[rule]]
        hosts = ["api.github.com"]
        action = "block"
    """)
    with pytest.raises(ConfigError, match=r'missing a required `name`.*block-api-github-com'):
        load_rules(ws)


# ---- append / remove ---------------------------------------------------------

def test_append_then_remove(xdg, workspaces_dir):
    from credproxy_cli.core.model.rules import (Rule, append_rule, load_rules,
                                          remove_rule)
    ws = _write_ws(workspaces_dir, "w", 'image = "x"\n')
    append_rule(ws, Rule(name="r1", hosts=("api.github.com",), action="block"))
    append_rule(ws, Rule(name="r2", hosts=("api.example.com",), action="rewrite",
                         set_headers={"X-Env": "s"}))
    assert [r.name for r in load_rules(ws)] == ["r1", "r2"]
    remove_rule(ws, "r1")
    assert [r.name for r in load_rules(ws)] == ["r2"]


def test_append_rule_coexists_with_bindings(xdg, workspaces_dir):
    """A [[rule]] block must not be confused with a [[binding]] block by the
    surgical-edit machinery (both use the shared array-depth block spans)."""
    from credproxy_cli.core.model.bindings import load_bindings
    from credproxy_cli.core.model.rules import Rule, append_rule, load_rules
    ws = _write_ws(workspaces_dir, "w", """
        image = "x"
        [[binding]]
        name = "b"
        injector = "bearer"
        provider = "env"
        secret = "TOK"
        hosts = ["api.github.com"]
        placeholder = "ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    """)
    append_rule(ws, Rule(name="r1", hosts=("api.github.com",), action="block"))
    assert [b.name for b in load_bindings(ws)] == ["b"]
    assert [r.name for r in load_rules(ws)] == ["r1"]


# ---- wire entries + matcher --------------------------------------------------

def test_wire_entries_shape():
    from credproxy_cli.core.model.rules import Rule, rule_wire_entries
    rules = [Rule(name="r", hosts=("api.github.com",), action="respond",
                  status=200, body="{}", headers={"Content-Type": "application/json"},
                  methods=("GET",), path="/v1/models")]
    (e,) = rule_wire_entries(rules)
    assert e == {"name": "r", "hosts": ["api.github.com"], "action": "respond",
                 "methods": ["GET"], "path": "/v1/models", "status": 200,
                 "body": "{}", "headers": {"Content-Type": "application/json"}}


def test_match_rules_first_terminal_wins():
    from credproxy_cli.core.model.rules import Rule, match_rules
    rules = [
        Rule(name="rw", hosts=("api.github.com",), action="rewrite",
             set_headers={"X": "y"}),
        Rule(name="blk", hosts=("api.github.com",), action="block",
             methods=("DELETE",)),
        Rule(name="never", hosts=("api.github.com",), action="block"),
    ]
    m = match_rules(rules, "DELETE", "api.github.com", "/repos/a")
    assert [x.name for x in m] == ["rw", "blk"]         # stops at first terminal
    assert m[-1].terminal is True
    # A GET skips the DELETE-scoped block and falls to the catch-all.
    m2 = match_rules(rules, "GET", "api.github.com", "/x")
    assert [x.name for x in m2] == ["rw", "never"]


def test_match_rules_script_never_hides_later_rule():
    # The CLI has no Starlark, so a script's phase is unknown: it is always
    # reported as possibly-terminal (may_terminate) and NEVER stops the dry-run --
    # a definite later block is still shown, flagged conditional on the script.
    # (match_rules no longer reads the .star, so the script name need not resolve.)
    from credproxy_cli.core.model.rules import Rule, match_rules
    rules = [
        Rule(name="scrub", hosts=("api.github.com",), action="script",
             script="scrub-emails", path="/users/**"),
        Rule(name="blk", hosts=("api.github.com",), action="block"),
    ]
    m = match_rules(rules, "GET", "api.github.com", "/users/x")
    assert [x.name for x in m] == ["scrub", "blk"]      # block NOT hidden
    assert m[0].may_terminate is True and m[0].terminal is False
    assert m[1].terminal is True and m[1].conditional is True   # gated on scrub


def test_match_rules_path_and_host_glob():
    from credproxy_cli.core.model.rules import Rule, match_rules
    rules = [Rule(name="r", hosts=("sts.*.amazonaws.com",), action="block",
                  path="/assume/**")]
    assert match_rules(rules, "POST", "sts.us-east-1.amazonaws.com", "/assume/x")
    assert not match_rules(rules, "POST", "sts.us-east-1.amazonaws.com", "/other")
    assert not match_rules(rules, "POST", "s3.amazonaws.com", "/assume/x")


# ---- combined fingerprint ----------------------------------------------------

def test_combined_fingerprint_changes_with_rules():
    from credproxy_cli.core.model.rules import Rule, combined_fingerprint
    a = combined_fingerprint([], [Rule(name="r", hosts=("h.example.com",),
                                       action="block")])
    b = combined_fingerprint([], [Rule(name="r", hosts=("h.example.com",),
                                       action="block", status=404)])
    c = combined_fingerprint([], [])
    assert a != b != c and a != c


def test_fingerprint_changes_on_reorder():
    # Rules evaluate in declaration order, so a reorder is a behavioral change
    # and MUST change the fingerprint (re-push).
    from credproxy_cli.core.model.rules import Rule, combined_fingerprint
    r1 = Rule(name="a", hosts=("h.example.com",), action="block")
    r2 = Rule(name="b", hosts=("g.example.com",), action="block")
    assert combined_fingerprint([], [r1, r2]) != combined_fingerprint([], [r2, r1])


def test_parse_rule_entry_rejects_out_of_range_status():
    # Field-shape validation lives in the ONE per-entry validator now (used by
    # both the load path and `rule add`); a bad status is caught there.
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.rules import _parse_rule_entry
    with pytest.raises(ConfigError, match="status"):
        _parse_rule_entry({"action": "block", "hosts": ["h.example.com"],
                           "status": 999}, "src", "rule")


def test_parse_rule_entry_builds_and_validates():
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.rules import _parse_rule_entry
    r = _parse_rule_entry({"action": "respond", "hosts": ["api.x.com"],
                           "status": 200, "body": "{}",
                           "headers": {"Content-Type": "application/json"}},
                          "src", "rule")
    assert r.action == "respond" and r.status == 200 and r.body == "{}"
    assert r.headers == {"Content-Type": "application/json"}
    # respond without a status is rejected here (not deferred to validate)
    with pytest.raises(ConfigError, match="requires a 'status'"):
        _parse_rule_entry({"action": "respond", "hosts": ["h.x.com"]}, "src", "rule")
    # a field not valid for the action is rejected here
    with pytest.raises(ConfigError, match="not valid for action 'block'"):
        _parse_rule_entry({"action": "block", "hosts": ["h.x.com"], "body": "x"},
                          "src", "rule")


# ---- script rule params (#35) -----------------------------------------------


def test_parse_rule_entry_accepts_script_params():
    from credproxy_cli.core.model.rules import _parse_rule_entry
    r = _parse_rule_entry(
        {"action": "script", "hosts": ["api.x.com"], "script": "guard",
         "params": {"allow_prefixes": ["/a", "/b"], "status": 418,
                    "nested": {"k": True}}},
        "src", "rule")
    assert r.params == {"allow_prefixes": ["/a", "/b"], "status": 418,
                        "nested": {"k": True}}


def test_parse_rule_entry_params_only_on_script_action():
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.rules import _parse_rule_entry
    # params is a script-only field: on block/respond/rewrite it's an extra field.
    with pytest.raises(ConfigError, match="not valid for action 'block'"):
        _parse_rule_entry({"action": "block", "hosts": ["h.x.com"],
                           "params": {"x": 1}}, "src", "rule")


def test_parse_rule_entry_rejects_non_json_clean_params():
    """A TOML-only value JSON can't carry (a datetime) is rejected with its
    path, so it fails at `rule add`/load, not silently at the wire POST."""
    import datetime
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.rules import _parse_rule_entry
    with pytest.raises(ConfigError, match="unsupported value type|JSON-clean"):
        _parse_rule_entry(
            {"action": "script", "hosts": ["api.x.com"], "script": "g",
             "params": {"when": datetime.datetime(2026, 1, 1)}}, "src", "rule")


def test_parse_rule_entry_rejects_non_table_params():
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.rules import _parse_rule_entry
    with pytest.raises(ConfigError, match="params must be a table"):
        _parse_rule_entry({"action": "script", "hosts": ["api.x.com"],
                           "script": "g", "params": [1, 2]}, "src", "rule")


def test_wire_entry_includes_script_params():
    """params ride the wire next to script/api; a rule without params omits the
    key (so zero-config rules keep a byte-identical wire entry)."""
    from credproxy_cli.core.model.rules import Rule, rule_wire_entries
    with_params = Rule(name="g", hosts=("api.github.com",), action="script",
                       script="scrub-emails", params={"a": [1, 2]})
    (e,) = rule_wire_entries([with_params])
    assert e["params"] == {"a": [1, 2]}
    assert e["script"] == "scrub-emails" and "script_source" in e

    without = Rule(name="g2", hosts=("api.github.com",), action="script",
                   script="scrub-emails")
    (e2,) = rule_wire_entries([without])
    assert "params" not in e2


def test_parse_rule_entry_rejects_non_finite_float_params():
    """nan/inf are valid TOML floats but not JSON -- reject them (else json.dumps
    emits bare NaN/Infinity that strict parsers, incl. a future Go CLI, refuse)."""
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.rules import _parse_rule_entry
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ConfigError, match="finite"):
            _parse_rule_entry({"action": "script", "hosts": ["api.x.com"],
                               "script": "g", "params": {"x": bad}}, "src", "rule")
    # a finite float is fine (regression: don't over-reject).
    r = _parse_rule_entry({"action": "script", "hosts": ["api.x.com"],
                           "script": "g", "params": {"ratio": 0.5}}, "src", "rule")
    assert r.params == {"ratio": 0.5}


def test_rule_row_includes_params():
    """`rule list`/`inspect`/`rule_added` rows carry params (operator-plaintext),
    so the docs' "shown in rule list" claim is true -- #36 review."""
    from credproxy_cli.core.model.rules import Rule
    from credproxy_cli.porcelain.cmd_rule import _rule_row
    row = _rule_row(Rule(name="g", hosts=("h",), action="script", script="s",
                         params={"allow": ["/a"]}))
    assert row["params"] == {"allow": ["/a"]}
    # a params-less rule still has the key, null (present-but-empty).
    assert _rule_row(Rule(name="b", hosts=("h",), action="block"))["params"] is None


def test_remove_rule_with_child_table(xdg, workspaces_dir):
    # A hand-written `[rule.headers]` child sub-table must be removed WITH its
    # parent rule, not orphaned (which would corrupt the TOML).
    from credproxy_cli.core.model.rules import load_rules, remove_rule
    ws = _write_ws(workspaces_dir, "w", """
        image = "x"

        [[rule]]
        name = "r1"
        hosts = ["api.openai.com"]
        path = "/v1/models"
        action = "respond"
        status = 200

        [rule.headers]
        Content-Type = "application/json"

        [[rule]]
        name = "r2"
        hosts = ["api.github.com"]
        action = "block"
    """)
    rules = load_rules(ws)
    assert [r.name for r in rules] == ["r1", "r2"]
    assert rules[0].headers == {"Content-Type": "application/json"}
    remove_rule(ws, "r1")
    assert [r.name for r in load_rules(ws)] == ["r2"]      # file still valid
    assert "[rule.headers]" not in ws.config_path.read_text()


def test_remove_rule_with_params_child_table(xdg, workspaces_dir):
    # A `[rule.params]` sub-table (the natural way to write a list/table param)
    # must load and be removed WITH its parent, not orphaned (#35).
    from credproxy_cli.core.model.rules import load_rules, remove_rule
    ws = _write_ws(workspaces_dir, "w", """
        image = "x"

        [[rule]]
        name = "guard"
        hosts = ["api.github.com"]
        action = "script"
        script = "scrub-emails"

        [rule.params]
        allow_prefixes = ["/repos/scratch-", "/user/repos"]
        message = "scratch repos only"

        [[rule]]
        name = "r2"
        hosts = ["api.github.com"]
        action = "block"
    """)
    rules = load_rules(ws)
    assert [r.name for r in rules] == ["guard", "r2"]
    assert rules[0].params == {"allow_prefixes": ["/repos/scratch-", "/user/repos"],
                               "message": "scratch repos only"}
    remove_rule(ws, "guard")
    assert [r.name for r in load_rules(ws)] == ["r2"]      # file still valid
    assert "[rule.params]" not in ws.config_path.read_text()


def test_remove_rule_inline_array_form_refused(xdg, workspaces_dir):
    """The inline-array form (`rule = [ { ... } ]`) parses to entries but has NO
    removable block span -- `remove_rule` must refuse prescriptively, not
    IndexError / mis-edit."""
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.rules import remove_rule
    ws = _write_ws(workspaces_dir, "inlinearr", """\
        image = "x"
        rule = [
          { name = "a", hosts = ["a.io"], action = "block" },
        ]
    """)
    with pytest.raises(ConfigError, match="isn't a removable `\\[\\[rule\\]\\]` block"):
        remove_rule(ws, "a")


def test_remove_rule_duplicate_name_refused(xdg, workspaces_dir):
    """Two `[[rule]]` blocks with the same name: `remove` refuses as ambiguous
    rather than silently dropping the first."""
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.rules import remove_rule
    ws = _write_ws(workspaces_dir, "dupname", """\
        image = "x"

        [[rule]]
        name = "dup"
        hosts = ["a.io"]
        action = "block"

        [[rule]]
        name = "dup"
        hosts = ["b.io"]
        action = "block"
    """)
    with pytest.raises(ConfigError, match="defined more than once"):
        remove_rule(ws, "dup")


def test_rewrite_empty_container_rejected(xdg, workspaces_dir):
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.rules import load_rules
    ws = _write_ws(workspaces_dir, "w", """
        image = "x"
        [[rule]]
        name = "r"
        hosts = ["api.example.com"]
        action = "rewrite"
        remove_headers = []
    """)
    with pytest.raises(ConfigError, match="NON-EMPTY|at least one"):
        load_rules(ws)


def test_render_rule_block_roundtrips_nested_params(xdg, workspaces_dir):
    """A stamped `[rule.params]` (incl. nested tables + numbers/bools) reloads
    byte-for-value identical -- _render_rule_block <-> _parse_rule_entry (#37)."""
    from credproxy_cli.core.model.rules import Rule, append_rules, load_rules
    ws = _write_ws(workspaces_dir, "w", 'image = "x"\n')
    params = {"allow": ["/a", "/b"], "limit": 5, "strict": True,
              "nested": {"k": "v", "n": 2}}
    append_rules(ws, [Rule(name="g", hosts=("api.github.com",), action="script",
                           script="scrub-emails", params=params)])
    (r,) = load_rules(ws)
    assert r.params == params
    assert "[rule.params]" in ws.config_path.read_text()
