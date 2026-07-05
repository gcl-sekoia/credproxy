"""Tests for the builtin `scrub-emails` rule script.

Drives the RESPONSE phase through `testkit` -- which resolves `scrub-emails.star`
through the layered registry and compiles it under the `kind="rule"` profile
exactly like the proxy does -- via `make_response` + `run_rule_response`. This is
the direct testkit coverage the script lacked (previously only exercised
indirectly through the addon in `tests/test_rules.py`).

The script scrubs `email`/`notification_email` to null out of a JSON object, or
out of every object in a JSON array, and leaves anything else untouched.
"""
import json

import testkit


def _script():
    return testkit.load_rule_script("scrub-emails")


def test_scrubs_single_object():
    script = _script()
    req = testkit.make_request("GET", "https://api.github.com/users/octocat")
    flow = testkit.make_response(req, status=200, body=json.dumps(
        {"login": "octocat", "email": "octo@example.com",
         "notification_email": "noc@example.com"}))
    outcome = testkit.run_rule_response(script, flow)

    assert not outcome.terminal and outcome.response is None   # a rewrite, never terminal
    data = json.loads(flow.response.text)
    assert data["email"] is None                # scrubbed
    assert data["notification_email"] is None   # scrubbed
    assert data["login"] == "octocat"           # untouched


def test_scrubs_each_object_in_list():
    script = _script()
    req = testkit.make_request("GET", "https://api.github.com/users")
    flow = testkit.make_response(req, status=200, body=json.dumps([
        {"login": "a", "email": "a@x.com"},
        {"login": "b", "email": "b@x.com"},
    ]))
    testkit.run_rule_response(script, flow)

    data = json.loads(flow.response.text)
    assert [o["email"] for o in data] == [None, None]
    assert [o["login"] for o in data] == ["a", "b"]


def test_object_without_email_fields_is_left_intact():
    script = _script()
    req = testkit.make_request("GET", "https://api.github.com/users/octocat")
    flow = testkit.make_response(req, status=200,
                                 body=json.dumps({"login": "octocat"}))
    testkit.run_rule_response(script, flow)

    assert json.loads(flow.response.text) == {"login": "octocat"}


def test_non_json_body_is_a_noop():
    """A body that isn't JSON (resp_json() -> None) short-circuits: no rewrite."""
    script = _script()
    req = testkit.make_request("GET", "https://api.github.com/users/octocat")
    flow = testkit.make_response(req, status=200, body="not json at all")
    outcome = testkit.run_rule_response(script, flow)

    assert not outcome.terminal
    assert flow.response.text == "not json at all"   # untouched


def test_json_scalar_body_is_a_noop():
    """A JSON body that is neither an object nor an array (a bare scalar) is left
    alone -- the script only scrubs dicts and lists of dicts."""
    script = _script()
    req = testkit.make_request("GET", "https://api.github.com/users/octocat")
    flow = testkit.make_response(req, status=200, body=json.dumps("just-a-string"))
    testkit.run_rule_response(script, flow)

    assert json.loads(flow.response.text) == "just-a-string"
