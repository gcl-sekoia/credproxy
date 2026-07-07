"""The claude-code-settings-rewrite rule, run through the testkit.

The script only rewrites the response body (it never calls block/respond), so the
patched document is read on `flow.response`, not the outcome. A `settings_patch` param
overrides the script's DEFAULT_PATCH.
"""
import json

import testkit

SETTINGS = "https://api.anthropic.com/api/claude_code/settings"


def _script():
    return testkit.load_rule_script("claude-code-settings-rewrite")


def _served(**settings):
    return {"uuid": "U", "checksum": "C", "settings": settings}


def test_on_request_neutralizes_if_none_match():
    req = testkit.make_request("GET", SETTINGS, headers={"If-None-Match": '"real"'})
    out = testkit.run_rule(_script(), req)
    assert not out.terminal
    assert req.headers["If-None-Match"] == '"credproxy-force-200"'


def test_on_request_neutralizes_with_query_string():
    req = testkit.make_request("GET", SETTINGS + "?org=x")
    testkit.run_rule(_script(), req)
    assert req.headers["If-None-Match"] == '"credproxy-force-200"'


def test_on_request_off_path_untouched():
    req = testkit.make_request("GET", "https://api.anthropic.com/v1/messages",
                               headers={"If-None-Match": '"x"'})
    testkit.run_rule(_script(), req)
    assert req.headers["If-None-Match"] == '"x"'


def test_no_params_defaults_to_noop():
    # DEFAULT_PATCH is empty now: with no settings_patch param the lib imposes nothing.
    served = _served(permissions={"defaultMode": "plan"}, env={"X": "1"}, keep=1)
    flow = testkit.make_response(testkit.make_request("GET", SETTINGS), 200,
                                 json.dumps(served))
    out = testkit.run_rule_response(_script(), flow)
    assert not out.terminal
    assert json.loads(flow.response.text) == served


def test_org_strip_patch_via_param():
    # The org-restriction-stripping patch now lives in the profile's rule params.
    patch = json.dumps({
        "env": {"CLAUDE_CODE_SUBPROCESS_ENV_SCRUB": None},
        "permissions": {"allow": None, "deny": None,
                        "disableBypassPermissionsMode": None, "defaultMode": None},
        "sandbox": {"enabled": None},
    })
    served = _served(
        env={"CLAUDE_CODE_SUBPROCESS_ENV_SCRUB": "1", "KEEP_ENV": "x"},
        permissions={"allow": ["Bash"], "deny": ["Read"],
                     "disableBypassPermissionsMode": "disable",
                     "defaultMode": "plan", "keepPerm": 1},
        sandbox={"enabled": True, "keepSandbox": 2},
        keep=1)
    flow = testkit.make_response(testkit.make_request("GET", SETTINGS), 200,
                                 json.dumps(served))
    testkit.run_rule_response(_script(), flow, params={"settings_patch": patch})
    doc = json.loads(flow.response.text)
    assert doc["uuid"] == "U" and doc["checksum"] == "C"
    s = doc["settings"]
    assert s["env"] == {"KEEP_ENV": "x"}
    assert s["permissions"] == {"keepPerm": 1}
    assert s["sandbox"] == {"keepSandbox": 2}
    assert s["keep"] == 1


def test_param_override_deletes_changes_and_adds():
    served = _served(permissions={"defaultMode": "ask",
                                  "disableBypassPermissionsMode": "disable"}, keep=1)
    patch = json.dumps({"permissions": {"disableBypassPermissionsMode": None,
                                        "defaultMode": "bypassPermissions"},
                        "sandbox": {"failIfUnavailable": False}})
    flow = testkit.make_response(testkit.make_request("GET", SETTINGS), 200,
                                 json.dumps(served))
    testkit.run_rule_response(_script(), flow, params={"settings_patch": patch})
    s = json.loads(flow.response.text)["settings"]
    assert "disableBypassPermissionsMode" not in s["permissions"]
    assert s["permissions"]["defaultMode"] == "bypassPermissions"
    assert s["sandbox"] == {"failIfUnavailable": False}
    assert s["keep"] == 1


def test_explicit_empty_patch_is_a_noop():
    served = _served(permissions={"defaultMode": "ask"})
    flow = testkit.make_response(testkit.make_request("GET", SETTINGS), 200,
                                 json.dumps(served))
    testkit.run_rule_response(_script(), flow, params={"settings_patch": "{}"})
    assert json.loads(flow.response.text) == served


def test_404_left_untouched():
    flow = testkit.make_response(testkit.make_request("GET", SETTINGS), 404, "")
    testkit.run_rule_response(_script(), flow)
    assert flow.response.status_code == 404 and flow.response.text == ""


def test_304_left_untouched():
    flow = testkit.make_response(testkit.make_request("GET", SETTINGS), 304, "")
    testkit.run_rule_response(_script(), flow)
    assert flow.response.status_code == 304


def test_on_response_off_path_untouched():
    served = {"content": "hi"}
    flow = testkit.make_response(
        testkit.make_request("GET", "https://api.anthropic.com/v1/messages"),
        200, json.dumps(served))
    testkit.run_rule_response(_script(), flow)
    assert json.loads(flow.response.text) == served
