"""The base overlay's neutral packs, resolved off the mounted overlay chain
and expanded the same way `pack add` / `create` expand them (`build_pack`),
plus the static definition (`get_pack`) for the option/requires halves that
don't expand into a workspace TOML. (The opinionated `claude-managed-settings`
pack lives in the `50-example` overlay, tested there.)
"""
from credproxy_cli.core.model.packs import build_pack, get_pack, load_pack_sources


def test_base_packs_resolve_from_the_base_overlay():
    src = load_pack_sources()
    for name in ("proxy-ca", "toolchain", "cache", "claude-code", "codex", "github-auth", "git-signing", "gcloud"):
        assert src.get(name) == "overlay:base", f"{name} resolved from {src.get(name)!r}"


def test_proxy_ca_is_a_root_bootstrap_step():
    exp = build_pack("proxy-ca")
    assert exp.bindings == () and exp.rules == () and exp.mounts == ()
    (setup,) = exp.setup
    assert setup["user"] == "root" and setup["order"] == 0
    assert "bootstrap.sh" in setup["run"]


def test_github_auth_two_parts_share_one_placeholder():
    # build_pack propagates the caller-resolved provider/secret to every part;
    # the pack's own defaults (gh-cli / github.com) live on the spec (below).
    exp = build_pack("github-auth", provider="gh-cli", secret="github.com")
    api, git = exp.bindings
    assert (api.name, api.injector, api.hosts, api.env) == \
        ("github-auth-api", "bearer", ("api.github.com",), "GITHUB_TOKEN")
    assert (git.name, git.injector, git.hosts) == \
        ("github-auth-git", "basic", ("github.com",))
    # The shared placeholder is what lets gh hand api's token to git for github.com.
    assert api.placeholder == git.placeholder
    assert api.placeholder.startswith("ghp_") and len(api.placeholder) == 40
    assert api.provider == git.provider == "gh-cli"
    assert api.secret == git.secret == "github.com"
    assert get_pack("github-auth").default_provider == "gh-cli"
    assert get_pack("github-auth").default_secret == "github.com"


def test_github_auth_declares_host_prereqs():
    spec = get_pack("github-auth")
    kinds = {r.kind for r in spec.requires}
    assert kinds == {"command", "provider"}
    prov = next(r for r in spec.requires if r.kind == "provider")
    assert prov.fetch is True


def test_gcloud_injects_a_host_minted_access_token():
    # Host-minted access token + bearer swap: the `gcloud` provider prints a
    # short-lived token on the host, and this binding swaps it on *.googleapis.com.
    exp = build_pack("gcloud", provider="gcloud", secret="default")
    (api,) = exp.bindings
    assert (api.name, api.injector, api.hosts, api.env) == \
        ("gcloud-api", "bearer", ("*.googleapis.com",), "CLOUDSDK_AUTH_ACCESS_TOKEN")
    assert api.provider == "gcloud" and api.secret == "default"
    # ya29.* so client-side format sniffs pass; the proxy swaps it in transit.
    assert api.placeholder.startswith("ya29.credproxy-") and len(api.placeholder) == 64
    # gcloud ignores SSL_CERT_FILE/REQUESTS_CA_BUNDLE — it needs its own CA knob.
    assert dict(exp.env)["CLOUDSDK_CORE_CUSTOM_CA_CERTS_FILE"] == "/tmp/proxy-ca.crt"
    # Flagless off the host's active gcloud login.
    spec = get_pack("gcloud")
    assert spec.default_provider == "gcloud" and spec.default_secret == "default"


def test_gcloud_declares_host_prereqs():
    spec = get_pack("gcloud")
    kinds = {r.kind for r in spec.requires}
    assert kinds == {"command", "provider"}
    prov = next(r for r in spec.requires if r.kind == "provider")
    assert prov.fetch is True


def test_claude_code_is_the_umbrella_enablement_pack():
    # One pack = token + client config + session hook + mise-latest. A caller
    # (template / --provider) supplies the vault; the pack has no default.
    exp = build_pack("claude-code", provider="bw", secret="claude-code-oauth-token")
    (b,) = exp.bindings
    assert b.name == "claude-code-oauth" and b.injector == "bearer"
    assert b.hosts == ("api.anthropic.com",) and b.env == "CLAUDE_CODE_OAUTH_TOKEN"
    assert b.provider == "bw" and b.secret == "claude-code-oauth-token"
    assert b.placeholder.startswith("sk-ant-oat01-") and len(b.placeholder) == 40
    # Neutral: no baked-in vault default.
    spec = get_pack("claude-code")
    assert spec.default_provider is None and spec.default_secret is None
    # Client-config step (onboarding + settings merge) + the SessionStart hook install,
    # in that order; the token↔onboarding coupling is now intra-pack.
    orders = {s["order"]: s["run"] for s in exp.setup}
    assert "claude-code-setup.sh" in orders[20]
    assert "session-context.sh --install" in orders[50]
    # Ships the session runner + its default fragments (credproxy, config-dir, tools).
    targets = {m.target for m in exp.mounts}
    assert {"/opt/session-context.sh", "/opt/session-context.d/10-credproxy.sh",
            "/opt/session-context.d/15-claude-config.sh",
            "/opt/session-context.d/20-tools.sh"} <= targets
    # Keeps the claude CLI on the latest release.
    assert dict(exp.env)["MISE_MINIMUM_RELEASE_AGE_EXCLUDES"] == "claude"


def test_codex_injects_the_openai_api_key():
    # The claude-code sibling, one auth family simpler: OpenAI's long-lived
    # credential is a plain API key, so a single bearer part + the login-seed step.
    exp = build_pack("codex", provider="bw", secret="openai-api-key")
    (b,) = exp.bindings
    assert b.name == "codex-api" and b.injector == "bearer"
    assert b.hosts == ("api.openai.com",) and b.env == "OPENAI_API_KEY"
    assert b.provider == "bw" and b.secret == "openai-api-key"
    # sk- so a client-side format sniff passes; the proxy swaps it in transit.
    assert b.placeholder.startswith("sk-credproxy-") and len(b.placeholder) == 64
    # Neutral: no baked-in vault (no universal OpenAI token source).
    spec = get_pack("codex")
    assert spec.default_provider is None and spec.default_secret is None
    # The auth.json-seeding login step, as the workspace user.
    (setup,) = exp.setup
    assert setup["order"] == 20 and setup["user"] == "workspace"
    assert "codex-setup.sh" in setup["run"]
    (mount,) = exp.mounts
    assert mount.target == "/opt/codex-setup.sh"
    # Carries no [env]: MISE_MINIMUM_RELEASE_AGE_EXCLUDES would collide with the
    # claude-code pack's, so the two packs stay composable.
    assert exp.env == ()
    assert exp.rules == ()


def test_toolchain_is_pure_container_half():
    exp = build_pack("toolchain")
    assert exp.bindings == () and exp.rules == ()
    targets = {m.target for m in exp.mounts}
    assert "/opt/toolchain.sh" in targets
    assert "/opt/toolchain/tools.d/10-base.list" in targets
    (setup,) = exp.setup
    assert setup["order"] == 10 and setup["user"] == "workspace"
    # The pack carries the terminal-env polish (moved off the templates).
    env = dict(exp.env)
    assert env["LANG"] == "C.UTF-8" and env["COLORTERM"] == "truecolor"


def test_cache_is_the_discardable_toolchain_volume():
    exp = build_pack("cache")
    assert exp.bindings == () and exp.rules == ()
    (mount,) = exp.mounts
    assert mount.kind == "volume" and mount.value == "cache" and mount.target == "/cache"
    # Chowned pre-setup (root), before the toolchain pack's order=10 install writes in.
    (setup,) = exp.setup
    assert setup["user"] == "root" and setup["order"] == 5
    assert "/cache" in setup["run"]
    env = dict(exp.env)
    # The generic cache tier for the whole ecosystem...
    assert env["XDG_CACHE_HOME"] == "/cache/xdg"
    # ...plus the DATA-tier install dirs XDG_CACHE_HOME can't reach, so a recreate
    # skips re-installing the toolchain (not just re-downloading).
    assert env["MISE_DATA_DIR"] == "/cache/mise"
    assert env["UV_PYTHON_INSTALL_DIR"] == "/cache/uv/python"
    assert env["UV_TOOL_DIR"] == "/cache/uv/tools"


def test_git_signing_option_feeds_mount_and_requires():
    spec = get_pack("git-signing")
    (opt,) = spec.options
    assert opt.id == "sock_dir" and opt.type == "string"
    assert opt.default == "~/.ssh/credproxy-agent"
    # The one option supplies both the bind source and the requires path (so a
    # refresh can read it back from the stamped mount).
    bind = next(m for m in spec.mounts if m.kind == "bind")
    assert bind.source_option == "sock_dir" and bind.target == "/ssh-agent"
    path_req = next(r for r in spec.requires if r.kind == "path")
    assert path_req.path_option == "sock_dir"
    assert dict(spec.env)["SSH_AUTH_SOCK"] == "/ssh-agent/agent.sock"
