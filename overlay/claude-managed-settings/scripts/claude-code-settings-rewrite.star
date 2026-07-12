# Rewrites the server-managed settings document Claude Code fetches at startup and
# hourly: GET /api/claude_code/settings on api.anthropic.com (api-staging.anthropic.com
# on staging). The document has no integrity check -- its uuid/checksum fields are
# required strings but never verified against the body -- so any body the client's
# schema accepts is applied. The sibling GET /api/claude_code/policy_limits
# ({"restrictions": {<key>: {"allowed": bool}}}, no uuid/checksum) is a separate
# endpoint, not handled here.

SETTINGS = "/api/claude_code/settings"

# Applied when a rule sets no `settings_patch` param. EMPTY on purpose: this lib is a
# generic mechanism with NO policy of its own -- a profile supplies the real merge-patch
# via the rule's `settings_patch` param (see the profile's [[rule]]). With the empty
# default the rule still forces a fresh fetch but rewrites nothing, so a bare
# `pack add` is inert until you set the param.
# (Claude Code rejects the whole document -- falling back to its on-disk cache -- if a
# KNOWN settings key holds a malformed value; unknown keys are tolerated.)
DEFAULT_PATCH = {}


def merge_patch(target, patch):
    # RFC 7386: null deletes a key, a dict merges recursively, anything else replaces.
    if type(patch) != "dict":
        return patch
    base = target if type(target) == "dict" else {}
    out = {}
    for k in base:
        if k not in patch:
            out[k] = base[k]
        elif patch[k] != None:
            out[k] = merge_patch(base[k], patch[k])
    for k in patch:
        if k not in base and patch[k] != None:
            out[k] = merge_patch(None, patch[k])
    return out


def settings_patch():
    # The override is a JSON string, not a TOML table: merge-patch marks a deletion
    # with null and TOML has no null.
    raw = param("settings_patch", "")
    if raw == None or raw == "":
        return DEFAULT_PATCH
    return json_decode(raw)


def on_request():
    if req_path().split("?")[0] != SETTINGS:
        return
    # A warm client sends If-None-Match computed from its cached copy; upstream would
    # answer 304 with no body to rewrite. There is no header-delete primitive, so
    # overwrite it with a value that cannot match a real ETag.
    req_set_header("If-None-Match", "\"credproxy-force-200\"")


def on_response():
    # The rule is path-scoped; this guard stops a host-only misconfiguration from
    # rewriting other api.anthropic.com responses.
    if req_path().split("?")[0] != SETTINGS:
        return
    if resp_status() != 200:
        return                             # only a 200 carries a document to patch
    doc = resp_json()
    if type(doc) != "dict":
        return
    doc["settings"] = merge_patch(doc.get("settings", {}), settings_patch())
    resp_set_body(json_encode(doc))        # uuid/checksum pass through unverified
