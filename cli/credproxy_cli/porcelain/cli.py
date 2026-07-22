"""The porcelain front-end: argument parsing, convenience resolution, and
rendering. Two surfaces over one core.

Surfaces (chosen purely by invocation, never by terminal sniffing):
  - STRICT (`credproxy`): every workspace named explicitly; omitting one is a
    clear error. No default-workspace resolution, no prompts ever, no aliases.
    The scriptable contract.
  - LOOSE (`credproxy --loose`, aliased `credp`): adds default-workspace
    resolution (announced on stderr), short command aliases that resolve to
    canonical commands with no independent behavior, and the confirmation gate
    on destructive-and-implicit actions.

`--json` is orthogonal to the surface: it selects the renderer only.

Grammar (canonical):
    credproxy workspace create NAME
    credproxy workspace use NAME
    credproxy workspace list [FILTER]
    credproxy list [FILTER]                       # canonical survey
    credproxy workspace NAME {enter|start|stop|recreate|delete|apply|inspect|logs}
    credproxy workspace NAME binding {add|remove|list|test} ...
    credproxy injector {scaffold NAME|list}
    credproxy provider {scaffold NAME|list}
    credproxy dev {build|test|reload}

argparse can't express name-before-verb, so the `workspace` noun is dispatched
by a small hand-rolled router (peek the second token: a verb routes directly,
anything else is a workspace name and the third token is the verb). Leaf
commands' flags still go through argparse.

This module is the routing/assembly/help core: argv canonicalization, the
top-level dispatch that picks a per-noun handler, leaf-parser assembly, and
help-text generation. The per-noun `do_*` handlers live in the sibling
`cmd_*` modules; shared resolution/confirmation/image scaffolding is in
`common`.
"""
from __future__ import annotations

import argparse
import sys

from ..core.errors import CredproxyError
from . import render
from .render import fail, say
from .common import Ctx, _LeafParser
from .cmd_binding import (
    do_binding_add, do_binding_list, do_binding_remove, do_binding_test,
    _binding_subparsers,
)
from .cmd_dev import (
    do_dev_build, do_dev_reload, do_dev_test, do_doctor, do_emit_compose,
)
from .cmd_lifecycle import (
    do_apply, do_enter, do_exec, do_logs, do_push, do_push_stateless,
    do_recreate, do_resolve, do_start, do_stop,
)
from .cmd_mount import do_mount_add, _mount_subparsers
from .cmd_pack import do_pack_add, do_pack_refresh, do_pack_remove
from .cmd_registry import (
    do_def_list, do_injector_api, do_injector_check, do_pack_list,
    do_provider_show, do_scaffold, do_scaffold_script, do_script_check,
)
from .cmd_postgres import (
    do_postgres_add, do_postgres_list, do_postgres_remove, do_postgres_test,
    _postgres_subparsers,
)
from .cmd_rule import (
    do_rule_add, do_rule_list, do_rule_remove, do_rule_test, _rule_subparsers,
)
from .cmd_workspace import (
    do_bind_dir, do_config, do_create, do_current, do_delete, do_edit, do_info,
    do_inspect, do_list, do_use, _create_dir, _parse_create,
)


# Workspace-scoped verbs (the `workspace NAME <verb>` tail).
_WS_VERBS = {
    "enter", "exec", "edit", "start", "stop", "recreate", "delete", "apply",
    "inspect", "config", "logs", "binding", "bind-dir", "mount", "rule",
    "postgres", "push", "resolve",
}
# Workspace-level verbs that take a name as their argument, not a subject.
_WS_NOUN_VERBS = {"create", "use", "list"}
# Top-level meta commands: no workspace argument. Every token in the three
# command sets above and here must be in core's RESERVED_NAMES (a workspace
# can't take a colliding name) -- guarded by test_reserved_names_cover_all_cli_verbs.
_META_COMMANDS = {"list", "current", "info", "doctor"}


# ---- argparse leaf parsers ---------------------------------------------------
#
# argparse handles each leaf command's flags. The dispatcher feeds it a
# normalized argv (canonicalized so name-before-verb and aliases collapse to a
# single internal form: `_ws <verb> [NAME] ...`). Each noun's subparser builder
# lives in that noun's module; assembly is here.


def _build_leaf_parser() -> argparse.ArgumentParser:
    """Parser for the verb tail of a workspace-scoped command. The dispatcher
    has already stripped `workspace` and the workspace name; what remains is
    `<verb> [args]`. NAME is threaded separately (resolved by the dispatcher)."""
    parser = _LeafParser(prog="credproxy workspace", add_help=False)
    sub = parser.add_subparsers(dest="verb", required=True)

    p_enter = sub.add_parser("enter")
    # One-session override of the config `user` (e.g. `enter --user root` for a
    # debug shell in a non-root workspace) without editing the config file.
    p_enter.add_argument("--user", dest="enter_user", default=None)
    # Force a config re-push (re-resolve secrets) even if the proxy already has
    # the current config -- e.g. after rotating a secret in place. Default skips
    # the push when the proxy's config fingerprint already matches.
    p_enter.add_argument("--push", dest="enter_push", action="store_true")
    p_exec = sub.add_parser("exec")
    # Default sources the CA-trust env (like `enter -- CMD`). `--login` upgrades to
    # a full bash login shell (/etc/profile.d + rc, mise shims); `--raw` drops to a
    # direct execve (no shell, for minimal images) -- the two are exclusive.
    p_exec.add_argument("--login", dest="exec_login", action="store_true")
    p_exec.add_argument("--raw", dest="exec_raw", action="store_true")
    # One-off `-u` override, beating config `user` for this call (parity with enter).
    p_exec.add_argument("--user", dest="exec_user", default=None)
    p_exec.add_argument("--push", dest="exec_push", action="store_true")
    sub.add_parser("edit")
    sub.add_parser("start")
    sub.add_parser("stop")
    p_recreate = sub.add_parser("recreate")
    # Default rebuilds only the workspace container (keeps the running proxy +
    # its CA). `--proxy`/`--all` also recreates the proxy (full re-bootstrap).
    p_recreate.add_argument("--proxy", "--all", dest="recreate_proxy",
                            action="store_true")
    # Also wipe the named managed volume(s) (re-seeded from the image), e.g.
    # `--reset-volume home`. Repeatable. Destroys data, so it's gated like
    # delete; bind/overlay (host-path) mounts are untouched.
    p_recreate.add_argument("--reset-volume", dest="recreate_reset_volumes",
                            action="append", metavar="NAME", default=[])
    p_delete = sub.add_parser("delete")
    # Keep the workspace's managed volumes instead of wiping them (they become
    # orphans unless a same-named workspace is recreated).
    p_delete.add_argument("--keep-volumes", dest="delete_keep_volumes",
                          action="store_true")
    sub.add_parser("apply")
    p_push = sub.add_parser("push")
    # --wait polls the proxy's /health (never /ready) until capture-ready before
    # pushing -- for an attached proxy that Compose/CI just started.
    p_push.add_argument("--wait", dest="push_wait", action="store_true")
    p_push.add_argument("--timeout", dest="push_timeout", type=float, default=120.0,
                        metavar="SECS")
    p_resolve = sub.add_parser("resolve")
    # Exactly one of --json (global, blob to stdout) or --out FILE (mode 0600).
    p_resolve.add_argument("--out", dest="resolve_out", default=None, metavar="FILE")
    sub.add_parser("inspect")
    p_config = sub.add_parser("config")
    p_config.add_argument("--declared", action="store_true", dest="config_declared")
    p_logs = sub.add_parser("logs")
    # --audit filters the stream to the structured `[audit]` events (#24):
    # credential-use and rule-hit records, pretty-printed (or raw JSON with
    # --json). Without it, `logs` streams the full proxy log verbatim.
    p_logs.add_argument("--audit", action="store_true", dest="logs_audit")

    p_bind = sub.add_parser("bind-dir")
    # Defaults to the current directory; --dir associates a different one.
    p_bind.add_argument("--dir", dest="bind_directory", default=None, metavar="PATH")

    binding = sub.add_parser("binding")
    bsub = binding.add_subparsers(dest="bindingcmd", required=True)
    _binding_subparsers(bsub)

    mount = sub.add_parser("mount")
    msub = mount.add_subparsers(dest="mountcmd", required=True)
    _mount_subparsers(msub)

    rule = sub.add_parser("rule")
    rsub = rule.add_subparsers(dest="rulecmd", required=True)
    _rule_subparsers(rsub)

    postgres = sub.add_parser("postgres")
    pgsub = postgres.add_subparsers(dest="postgrescmd", required=True)
    _postgres_subparsers(pgsub)

    pack = sub.add_parser("pack")
    psub = pack.add_subparsers(dest="packcmd", required=True)
    pa = psub.add_parser("add")
    pa.add_argument("pack", metavar="PACK")
    # Optional: a binding-bearing pack may carry a default provider/secret; a
    # pure-rule pack needs neither. Enforced (conditionally) in the handler.
    pa.add_argument("--provider", default=None)
    pa.add_argument("--secret", action="append", metavar="REF|SLOT=REF")
    # Pack `[[option]]` values, whole-field host-half parameters (#59). Repeatable
    # `--opt id=value`; unresolved required options prompt on loose+TTY, else fail
    # with the structured missing error.
    pa.add_argument("--opt", action="append", metavar="ID=VALUE", default=None)
    pr = psub.add_parser("refresh")
    # Optional PACK: re-expand just that referenced pack (else every one).
    pr.add_argument("pack", metavar="PACK", nargs="?", default=None)
    # Preview the diff without writing (also the CI "is anything stale" probe).
    pr.add_argument("--check", action="store_true")
    prm = psub.add_parser("remove")
    prm.add_argument("pack", metavar="PACK")

    return parser


# ---- top-level dispatch ------------------------------------------------------


def _print_help(loose: bool = False) -> None:
    say(_LOOSE_HELP if loose else _STRICT_HELP)


_STRICT_HELP = (
    "credproxy -- workspace manager for the credential-injecting proxy.\n"
    "\n"
    "Strict surface: name every workspace explicitly, no default resolution,\n"
    "no prompts. The scriptable contract. `credp` is the human alias\n"
    "(`credproxy --loose`): default-workspace resolution, short aliases, and a\n"
    "confirmation gate on destructive actions -- run `credp --help` for that.\n"
    "\n"
    "Workspaces:\n"
    "  credproxy workspace create NAME\n"
    "  credproxy workspace list [FILTER]   (or: credproxy list [FILTER])\n"
    "  credproxy info                      (global config & state: paths, overlays, registries)\n"
    "  credproxy doctor [NAME] [--fetch]   (preflight: docker/image/config/bindings)\n"
    "  credproxy version                   (or: credproxy --version)\n"
    "  credproxy workspace NAME enter|edit|start|stop|recreate|delete|apply|inspect|logs\n"
    "  credproxy workspace NAME push [--wait]   (resolve + POST config to the proxy)\n"
    "  credproxy workspace NAME resolve --json|--out FILE   (build wire config, no proxy)\n"
    "  credproxy workspace create NAME --attach SELECTOR   (attached: externally-run containers)\n"
    "  credproxy push --admin URL --config FILE --token FILE   (stateless push; no workspace)\n"
    "  credproxy emit-compose [NAME] [--image TAG]   (Docker Compose proxy-sidecar fragment)\n"
    "  credproxy workspace NAME exec [--login|--raw] -- CMD...   (one-shot; scriptable)\n"
    "  credproxy workspace NAME bind-dir [--dir PATH]   (associate with a directory)\n"
    "  credproxy workspace NAME mount add --volume NAME --target PATH [--ro] [--preserve] [--user-owned]\n"
    "  credproxy workspace NAME binding add|remove|list|test ...\n"
    "  credproxy workspace NAME rule add|remove|list|test ...   (traffic guardrails)\n"
    "  credproxy workspace NAME pack add PACK   (service pack: bindings + rules)\n"
    "  credproxy workspace binding test --provider P --secret REF [--injector I]\n"
    "      (ad-hoc: test a definition before binding it; no workspace needed)\n"
    "Definitions:\n"
    "  credproxy injector scaffold NAME [--script] | list | check NAME | api\n"
    "  credproxy provider scaffold NAME | provider list | show NAME\n"
    "  credproxy script check [NAME]       (compile .star scripts before push)\n"
    "  credproxy pack list               (service setup packs: bindings + guardrails)\n"
    "Dev harness:\n"
    "  credproxy dev build|test|reload\n"
    "\n"
    "Global flags: --loose (human surface; use the `credp` alias), --json,\n"
    "  --yes (bypass confirmation)."
)


_LOOSE_HELP = (
    "credp -- human surface for credproxy (credproxy --loose).\n"
    "\n"
    "An omitted workspace resolves to the current default (announced on\n"
    "stderr); destructive actions on the default workspace ask first.\n"
    "\n"
    "Workspaces (omit NAME to resolve by current directory, then the default):\n"
    "  credp use NAME                  set the default workspace\n"
    "  credp current                   show the workspace a bare command targets\n"
    "  credp create [NAME] [--here]    (NAME derived from the dir if omitted)\n"
    "  credp bind-dir [NAME] [--dir PATH]  associate a workspace with a directory\n"
    "  credp list [FILTER]\n"
    "  credp info                      global config & state (paths, overlays, registries)\n"
    "  credp enter|edit|start|stop|recreate|delete|apply|inspect|logs [NAME]\n"
    "  credp push [NAME] [--wait]      resolve + POST config to the workspace's proxy\n"
    "  credp resolve [NAME] --json|--out FILE   build wire config (no proxy)\n"
    "  credp create [NAME] --attach SELECTOR    attached workspace (externally-run containers)\n"
    "  credp emit-compose [NAME] [--image TAG]  Docker Compose proxy-sidecar fragment\n"
    "  credp mount add --volume NAME --target PATH [--ro] [--preserve] [--user-owned]\n"
    "  credp binding add|remove|list|test ...   (acts on the default workspace)\n"
    "  credp rule add|remove|list|test ...      (traffic guardrails)\n"
    "  credp pack add PACK                  (service pack: bindings + rules)\n"
    "  credp binding test --provider P --secret REF [--injector I]\n"
    "      (ad-hoc: test a definition before binding it; no workspace needed)\n"
    "Definitions:\n"
    "  credp injector scaffold NAME [--script] | list | check NAME | api\n"
    "  credp provider scaffold NAME | provider list | show NAME\n"
    "  credp script check [NAME]       (compile .star scripts before push)\n"
    "  credp pack list               (service setup packs: bindings + guardrails)\n"
    "Dev harness:\n"
    "  credp dev build|test|reload\n"
    "\n"
    "The canonical `credproxy workspace NAME <verb>` forms work too and are the\n"
    "scriptable contract. Global flags: --json, --yes (bypass confirmation)."
)


# Per-command help. The leaf parsers are deliberately `add_help=False` (we don't
# want raw argparse usage spew), so `--help` is honored by the hand-rolled
# dispatch via these prose blocks instead.
_BINDING_ADD_HELP = (
    "credproxy workspace NAME binding add --injector INJ -- bind a credential\n"
    "into requests for one or more hosts. (Coordinated multi-binding sets +\n"
    "guardrails are the `pack` noun: `workspace NAME pack add PACK`.)\n"
    "\n"
    "  --injector INJ    how the credential is shaped into the request (bearer,\n"
    "                    basic, body, sigv4, ...). See `credproxy injector list`.\n"
    "  --provider PROV   where the value comes from. Required.\n"
    "                    See `credproxy provider list`.\n"
    "  --secret REF      the reference the provider resolves. For the `env`\n"
    "                    provider REF is the host env var NAME (not the value).\n"
    "                    Repeat as SLOT=REF for a multi-slot secret.\n"
    "  --host HOST       host this binding applies to; repeatable. Required.\n"
    "  --name NAME       binding name (auto: <injector>-<provider>[-N]).\n"
    "  --placeholder PH  inert sentinel swapped for the real value at egress\n"
    "                    (auto-generated for substitute schemes).\n"
    "  --env VAR         env var name exposed to the workspace via /setup and\n"
    "                    /exports.sh (defaults to the injector's suggested env).\n"
    "  --no-env          suppress the injector's suggested env (writes\n"
    "                    `env = false`); mutually exclusive with --env.\n"
)

_PACK_ADD_HELP = (
    "credproxy workspace NAME pack add PACK -- apply a service setup pack:\n"
    "append a durable `[[pack]]` REFERENCE (with the resolved provider/secret/\n"
    "options written explicitly) to the workspace TOML. The resolver expands it --\n"
    "its `[[binding]]` set, `[[rule]]` guardrails, AND container half\n"
    "(`[[mount]]`/`[env]`/`[[setup]]`) -- at resolve time and snapshots the\n"
    "expansion in the lock. `credproxy pack list` shows every pack.\n"
    "\n"
    "  --provider PROV   where binding values come from (a binding-bearing pack\n"
    "                    may supply a default; a rule/container-only pack needs none).\n"
    "  --secret REF      the reference the provider resolves (see `binding add`);\n"
    "                    may be defaulted by a pack for its default provider.\n"
    "\n"
    "A reference, not a stamp: the expansion lives in the lock, never the TOML; a\n"
    "changed definition is inert until `pack refresh`, but editing the block's\n"
    "own inputs (provider/secret/options/disable/override) re-expands on the next\n"
    "resolve. A binding/rule on a host with no prior binding flips it to\n"
    "TLS-intercepted; the container half changes the workspace spec (restart to\n"
    "apply if the container exists) -- `pack add` announces both. An attached\n"
    "workspace refuses a container-half pack. Re-referencing the same pack is\n"
    "refused (a `[[pack]]` block already names it).\n"
    "\n"
    "Sibling verbs:\n"
    "  pack refresh [PACK] [--check]   re-expand from the current definition,\n"
    "                    diffing the locked snapshot (`--check` previews, no write).\n"
    "  pack remove PACK   drop the `[[pack]]` block + its lock snapshot.\n"
)

_BINDING_TEST_HELP = (
    "credproxy workspace NAME binding test [BINDING] -- dry-run resolve binding\n"
    "secrets via their providers. Reports ok/length per binding (never the\n"
    "secret value); exits 1 if any fail.\n"
    "\n"
    "  BINDING                       test only this binding (default: all).\n"
    "  --provider P --secret REF [--injector I]\n"
    "                                ad-hoc: test a definition before it is\n"
    "                                bound (no workspace needed).\n"
)

_MOUNT_ADD_HELP = (
    "credproxy workspace NAME mount add -- add a managed-volume mount to the\n"
    "workspace (a named, persistent, per-workspace Docker volume).\n"
    "\n"
    "  --volume NAME   the volume's name (letters/digits/_.-). `home` writes the\n"
    "                  `home = ...` sugar instead of a mounts entry.\n"
    "  --target PATH   absolute path the volume mounts at inside the workspace.\n"
    "  --ro            mount read-only.\n"
    "  --preserve      seed the new volume with the CURRENT container's data at\n"
    "                  --target before applying. A mount can't attach to a live\n"
    "                  container, so this stops + recreates the workspace (which\n"
    "                  carries the data across); without it the volume starts\n"
    "                  empty/image-seeded and the change is deferred to `start`.\n"
    "                  On a running workspace with live `enter` sessions it asks\n"
    "                  first (loose) / needs --yes (strict), since they're killed.\n"
    "  --user-owned    chown the volume to the workspace `user` (after setup) so a\n"
    "                  non-root user can write it -- needed when it mounts at a path\n"
    "                  the image doesn't populate (else root-owned). Requires a\n"
    "                  non-root `user`; not valid for the `home` sugar.\n"
)

_RULE_ADD_HELP = (
    "credproxy workspace NAME rule add ACTION --host HOST [--host HOST...] ...\n"
    "  Govern traffic on an intercepted host (no credential involved). ACTION is a\n"
    "  SUBCOMMAND -- one of block|respond|rewrite|script (not a flag). Adding a rule\n"
    "  to a passthrough host makes it TLS-intercepted (workspace must bootstrap CA).\n"
    "\n"
    "  Scoping (every action):\n"
    "    --host HOST|GLOB   literal or glob (`*.amazonaws.com`); repeatable, required\n"
    "    --method METHOD    restrict to these methods (repeatable; default all)\n"
    "    --path GLOB        path glob: `*` within a segment, `**` across\n"
    "                       (e.g. /repos/**); default all paths\n"
    "    --name NAME        rule name (auto-generated if omitted)\n"
    "    --visible/--hidden override the per-family enumeration+attribution default\n"
    "                       (block/respond visible; rewrite/script hidden)\n"
    "\n"
    "  Per action (each owns exactly these flags):\n"
    "    block    [--status N]                              refuse (default 403)\n"
    "    respond  --status N [--body TEXT] [--header K=V...] stub a response\n"
    "    rewrite  [--header K=V] [--remove-header NAME]      request headers\n"
    "             [--resp-header K=V] [--resp-remove-header NAME]  response headers\n"
    "    script   --script NAME                             a .star rule script\n"
    "\n"
    "  Examples:\n"
    "    rule add block --host api.github.com --method DELETE --path '/repos/**'\n"
    "    rule add respond --host api.openai.com --path /v1/models --status 200 --body '{}'\n"
    "    rule add script --host api.github.com --path '/users/**' --script scrub-emails\n"
    "\n"
    "  A visible block self-identifies (X-Credproxy-Rule header + a credproxy JSON\n"
    "  body); a hidden block is a bare status. Hidden rules are excluded from\n"
    "  /setup but ALWAYS logged/audited for the operator. See docs/reference/rules.md."
)

_CREATE_HELP = (
    "credproxy workspace create NAME -- scaffold a workspace config file and\n"
    "auth token (does not start anything). The scaffold sets a concrete image;\n"
    "edit the generated <name>.toml to change it (its comments show what to\n"
    "adjust), or override the template in an overlay (see docs/advanced/overlays.md).\n"
    "\n"
    "  --here        associate the workspace with the current directory, so a\n"
    "                loose `credp <verb>` run from at/under it resolves here.\n"
    "  --dir PATH    associate with PATH instead of the current directory.\n"
    "  --attach SEL  scaffold an ATTACHED workspace (containers managed externally;\n"
    "                credproxy only pushes credentials). SEL is compose-project=P |\n"
    "                container=X | admin-url=URL | discover=k=v[,k=v]. --here/--dir\n"
    "                still apply. Use `push` (not start/enter) on it.\n"
    "\n"
    "On the loose surface (`credp`), NAME may be omitted with --here/--dir: it is\n"
    "derived from the directory's basename (sanitized to the name charset, and\n"
    "deduped with a numeric suffix). Strict `credproxy` always requires NAME.\n"
)


_PUSH_STATELESS_HELP = (
    "credproxy push --admin URL --config FILE --token FILE [--wait] [--timeout SECS]\n"
    "  Stateless escape hatch: push a config to an arbitrary proxy with no\n"
    "  workspace and no state (for Compose/devcontainers/CI).\n"
    "\n"
    "  --admin URL    the proxy's admin base URL. MUST be loopback (127.0.0.0/8 or\n"
    "                 localhost) -- the wire carries resolved secrets over plain HTTP.\n"
    "  --config FILE  a workspace-TOML SUBSET: only [[binding]] + [[rule]] (any\n"
    "                 container/attach key is rejected). Build one with `resolve`.\n"
    "  --token FILE   file holding the proxy's bearer token.\n"
    "  --wait         poll /health until capture-ready before pushing.\n"
    "  --timeout SECS --wait timeout (default 120).\n"
    "\n"
    "To push a workspace instead, use `credproxy workspace NAME push`."
)


def _wants_help(argv: list[str]) -> bool:
    """True if argv contains a help flag. Used by the hand-rolled dispatch to
    honor `-h`/`--help` on subcommands (the leaf argparse parsers suppress it)."""
    return any(t in ("-h", "--help") for t in argv)


def _scaffold_help(kind: str) -> str:
    s = (
        f"credproxy {kind} scaffold NAME -- copy the builtin {kind} template "
        f"into\nyour registry as NAME, to author from. NAME must not start "
        f"with '-'."
    )
    if kind == "injector":
        s += (
            "\n\n--script [sign|substitute]  instead emit a SCRIPTED (custom) "
            "injector\n  (a manifest + a .star with the primitive-API reference "
            "inline) -- use\n  this when no built-in scheme fits. Pick the family:\n"
            "    sign        compute auth material on every request (e.g. an HMAC\n"
            "                signature); no placeholder. [default]\n"
            "    substitute  swap an inert placeholder the workspace holds for the\n"
            "                real secret value.\n"
            "  See `injector api` for the full reference; check it with "
            "`injector check NAME`."
        )
    if kind == "provider":
        s += (
            "\n\n--lang [python|sh]  template language (default python; "
            "sh = POSIX shell + jq).\n\n"
            "A provider is ANY executable -- a script in any language, or a "
            "compiled\nbinary -- that speaks the JSON stdin/stdout protocol "
            "(docs/reference/providers.md);\nit can also be a directory with an executable "
            "`run`."
        )
    s += f"\nThen `credproxy {kind} list` shows it."
    return s


# What each workspace-scoped verb does, surfaced on `... NAME <verb> --help`.
# Kept terse but descriptive: the blind-agent rounds showed a bare `usage:`
# line for the lifecycle verbs read as "is this command even doing anything?".
_VERB_HELP = {
    "enter": (
        "credproxy workspace NAME enter [--user USER] [--push] [-- CMD...] -- open\n"
        "a shell (default bash, or run CMD) in the workspace, starting it if needed.\n"
        "  --user USER   run as USER for this session (overrides config `user`).\n"
        "  --push        force a config re-push (re-resolve secrets) even if the\n"
        "                proxy already has the current config -- e.g. after\n"
        "                rotating a secret in place. Default skips the redundant push."
    ),
    "exec": (
        "credproxy workspace NAME exec [--login|--raw] [--user U] [--push] -- CMD...\n"
        "-- run a one-shot command in the workspace (starting it if needed) and\n"
        "propagate its exit code. The non-interactive sibling of `enter`: it never\n"
        "INITIATES an auto-stop, so it's safe to fire many times from a script/agent,\n"
        "and it takes a fast path when the workspace is already running.\n"
        "  (default) source the CA-trust env, like `enter -- CMD` (so `exec -- curl\n"
        "           https://…` works against the proxy). Honours `enter_prelude`.\n"
        "  --raw    direct execve: no shell wrapper, no CA-trust env (minimal images).\n"
        "  --login  a bash login shell (/etc/profile.d + rc, mise shims); needs bash.\n"
        "  --user U run as U for this call (overrides config `user`).\n"
        "  --push   force a config re-push (as `enter --push`); also the full start.\n"
        "(`--json` does not apply -- exec streams the command's output verbatim.)"
    ),
    "start": (
        "credproxy workspace NAME start -- (re)start the proxy, wait for health,\n"
        "push the resolved bindings, then (re)start the workspace. Creates the\n"
        "containers if missing; recreates one whose spec (image/mounts/env/...)\n"
        "has drifted. Safe to re-run."
    ),
    "stop": (
        "credproxy workspace NAME stop -- stop both containers (kept, not removed).\n"
        "Config and state survive; a later `start`/`enter` resumes."
    ),
    "recreate": (
        "credproxy workspace NAME recreate [--proxy] [--reset-volume NAME ...] --\n"
        "rebuild the workspace container from a clean slate (re-runs setup), then\n"
        "start it. Keeps all managed volumes, config, auth token, and state -- only\n"
        "the container is replaced (unlike `delete`). `--proxy` (alias `--all`) also\n"
        "recreates the proxy container, regenerating its CA (full re-bootstrap).\n"
        "`--reset-volume NAME` (repeatable) ALSO wipes that managed volume,\n"
        "re-seeded from the image (e.g. `--reset-volume home`) -- bind/overlay\n"
        "host-path mounts are untouched, and config/token/state survive. It\n"
        "destroys data, so on the loose surface it prompts for an implicit default\n"
        "workspace (pass --yes to bypass)."
    ),
    "delete": (
        "credproxy workspace NAME delete [--keep-volumes] -- remove both\n"
        "containers, the workspace's managed volumes, the config file, and the\n"
        "state dir. `--keep-volumes` preserves the volumes (they orphan unless a\n"
        "same-named workspace is recreated). Not reversible. (On the loose surface,\n"
        "deleting the default workspace prompts first.)"
    ),
    "apply": (
        "credproxy workspace NAME apply -- reconcile a running workspace with its\n"
        "config: binding changes are re-pushed live; container-spec changes\n"
        "(image/home/mounts/env/setup) are deferred with a `start` hint. Reports\n"
        "what was applied vs deferred. On an ATTACHED workspace, `apply` == `push`."
    ),
    "push": (
        "credproxy workspace NAME push [--wait] [--timeout SECS] -- resolve every\n"
        "binding's secret and POST the full wire config (bindings + rules) to the\n"
        "workspace's proxy. For a managed workspace this targets its running\n"
        "proxy's published port (start it first); for an ATTACHED workspace it\n"
        "resolves the `attach` selector (compose/container/discover/admin_url).\n"
        "  --wait          poll the proxy's /health until capture-ready first\n"
        "                  (never /ready -- that gates on this very push).\n"
        "  --timeout SECS  --wait timeout (default 120)."
    ),
    "resolve": (
        "credproxy workspace NAME resolve (--json | --out FILE) -- build the full\n"
        "wire config (with RESOLVED secret values) WITHOUT contacting any proxy.\n"
        "  --json      write the config blob to stdout.\n"
        "  --out FILE  write it to FILE, mode 0600 (it holds real secrets; a path\n"
        "              outside the workspace state dir warns). Exactly one required.\n"
        "Feeds the stateless `credproxy push --config FILE`."
    ),
    "inspect": (
        "credproxy workspace NAME inspect -- show config, running state, host port,\n"
        "binding summary, and any drift against what was last applied."
    ),
    "config": (
        "credproxy workspace NAME config [--declared] -- dump the container-side\n"
        "config. Default `effective`: every field with its in-effect value, all\n"
        "defaults filled (so you see what applies even when it's not in the file).\n"
        "`--declared` shows only what's literally in the .toml. `--json` for both."
    ),
    "edit": (
        "credproxy workspace NAME edit -- open the config in $VISUAL/$EDITOR\n"
        "(default vi), then validate it. Sugar over editing the .toml directly;\n"
        "hints `apply`/`start` afterward."
    ),
    "logs": (
        "credproxy workspace NAME logs -- follow the proxy container's logs\n"
        "(docker logs -f)."
    ),
    "bind-dir": (
        "credproxy workspace NAME bind-dir [--dir PATH] -- associate the workspace\n"
        "with a host directory (default: the current directory), so a loose\n"
        "`credp <verb>` run from at or under it resolves to NAME. Sugar over\n"
        "editing the `directory` field in the .toml (the source of truth)."
    ),
}


def _verb_help(verb_argv: list[str]) -> str:
    """Contextual help for a workspace-scoped verb (`--help` on the leaf)."""
    verb = verb_argv[0] if verb_argv else ""
    if verb == "binding":
        sub = verb_argv[1] if len(verb_argv) > 1 and not verb_argv[1].startswith("-") else ""
        if sub == "add":
            return _BINDING_ADD_HELP
        if sub == "test":
            return _BINDING_TEST_HELP
        return ("credproxy workspace NAME binding {add|remove|list|test} ...\n"
                "Run `binding add --help` or `binding test --help` for details.")
    if verb == "mount":
        sub = verb_argv[1] if len(verb_argv) > 1 and not verb_argv[1].startswith("-") else ""
        if sub == "add":
            return _MOUNT_ADD_HELP
        return ("credproxy workspace NAME mount add ...\n"
                "Run `mount add --help` for details.")
    if verb == "rule":
        sub = verb_argv[1] if len(verb_argv) > 1 and not verb_argv[1].startswith("-") else ""
        if sub == "add":
            return _RULE_ADD_HELP
        return ("credproxy workspace NAME rule {add|remove|list|test} ...\n"
                "Rules govern traffic on intercepted hosts (block/respond/rewrite/\n"
                "script), credential-free. `rule test METHOD URL` dry-runs the\n"
                "matcher. Run `rule add --help` for details.")
    if verb == "postgres":
        return ("credproxy workspace NAME postgres {add|remove|list|test} ...\n"
                "PostgreSQL broker upstreams: the workspace dials\n"
                "postgresql://<name>@proxy.local:5432/<db> and the proxy\n"
                "re-originates to the real database with an injected credential.\n"
                "`postgres add --provider P --secret username=REF --secret\n"
                "password=REF --host H --dbname D [--sslmode M] [--env VAR]`.")
    if verb == "pack":
        return _PACK_ADD_HELP
    if verb in _VERB_HELP:
        return _VERB_HELP[verb]
    return f"usage: credproxy workspace NAME {verb}"


def _split_trailing(argv: list[str]) -> tuple[list[str], list[str]]:
    """Split off a `-- CMD...` tail (for `enter` and `dev test`)."""
    if "--" in argv:
        i = argv.index("--")
        return argv[:i], argv[i + 1:]
    return argv, []


def _pop_global_flags(argv: list[str]) -> tuple[list[str], bool, bool, bool]:
    """Pull the order-independent global flags (--loose/--json/--yes) out of
    argv wherever they appear, returning the remainder and the flag values."""
    loose = as_json = assume_yes = False
    rest = []
    for tok in argv:
        if tok == "--loose":
            loose = True
        elif tok == "--json":
            as_json = True
        elif tok in ("--yes", "-y"):
            assume_yes = True
        else:
            rest.append(tok)
    return rest, loose, as_json, assume_yes


def _dispatch_workspace(ctx: Ctx, rest: list[str], trailing: list[str]) -> None:
    """Hand-rolled router for the `workspace` noun.

    `rest` is everything after `workspace`. Peek the first token:
      - a workspace-level verb (`create`/`use`/`list`) -> handle directly;
      - a workspace-scoped verb (`enter`/.../`binding`) with NO name -> in
        loose mode resolve the default; in strict mode this is an error;
      - otherwise the first token is a workspace NAME and the next is the
        scoped verb.
    """
    if not rest:
        fail("usage: credproxy workspace {create|use|list|NAME <verb>}")

    head = rest[0]

    if head == "create":
        if _wants_help(rest):
            say(_CREATE_HELP)
            return
        a = _parse_create(rest[1:])
        do_create(ctx, a.name, _create_dir(a), a.attach)
        return
    if head == "use":
        # `use` mutates the loose default-workspace pointer, so it's loose-only
        # (strict names every workspace explicitly). Its reader, `current`, is
        # loose-only too -- both sides of the default concept live on loose.
        if not ctx.loose:
            fail("`workspace use` sets the loose default-workspace pointer; it is "
                 "loose-only -- use the `credp` alias (or `credproxy --loose`). "
                 "The strict surface names every workspace explicitly.")
        if len(rest) != 2:
            fail("usage: credp workspace use NAME")
        do_use(ctx, rest[1])
        return
    if head == "list":
        if len(rest) > 2:
            fail("usage: credproxy workspace list [FILTER]")
        do_list(ctx, rest[1] if len(rest) > 1 else None)
        return

    if head in _WS_VERBS:
        # Verb with no explicit name -> default resolution (loose) / error.
        _run_ws_verb(ctx, None, rest, trailing)
        return

    # Otherwise head is a workspace name; rest[1:] is `<verb> ...`.
    name = head
    if len(rest) < 2:
        fail(f"usage: credproxy workspace {name} <verb>")
    _run_ws_verb(ctx, name, rest[1:], trailing)


def _run_ws_verb(
    ctx: Ctx, name: str | None, verb_argv: list[str], trailing: list[str]
) -> None:
    """Parse and run a workspace-scoped verb. `verb_argv` starts with the
    verb. `name` is the (possibly None) explicit workspace name."""
    if _wants_help(verb_argv):
        say(_verb_help(verb_argv))
        return
    a = _build_leaf_parser().parse_args(verb_argv)
    verb = a.verb
    if verb == "enter":
        do_enter(ctx, name, trailing, a.enter_user, a.enter_push)
    elif verb == "exec":
        do_exec(ctx, name, trailing, login=a.exec_login, raw=a.exec_raw,
                push=a.exec_push, user=a.exec_user)
    elif verb == "edit":
        do_edit(ctx, name)
    elif verb == "start":
        do_start(ctx, name)
    elif verb == "stop":
        do_stop(ctx, name)
    elif verb == "delete":
        do_delete(ctx, name, a.delete_keep_volumes)
    elif verb == "apply":
        do_apply(ctx, name)
    elif verb == "push":
        do_push(ctx, name, a.push_wait, a.push_timeout)
    elif verb == "resolve":
        do_resolve(ctx, name, a.resolve_out)
    elif verb == "recreate":
        do_recreate(ctx, name, a.recreate_proxy, a.recreate_reset_volumes)
    elif verb == "inspect":
        do_inspect(ctx, name)
    elif verb == "config":
        do_config(ctx, name, a.config_declared)
    elif verb == "logs":
        do_logs(ctx, name, getattr(a, "logs_audit", False))
    elif verb == "bind-dir":
        do_bind_dir(ctx, name, a.bind_directory)
    elif verb == "binding":
        bc = a.bindingcmd
        if bc == "add":
            do_binding_add(ctx, name, a)
        elif bc == "remove":
            do_binding_remove(ctx, name, a)
        elif bc == "list":
            do_binding_list(ctx, name)
        elif bc == "test":
            do_binding_test(ctx, name, a)
    elif verb == "mount":
        if a.mountcmd == "add":
            do_mount_add(ctx, name, a)
    elif verb == "rule":
        rc = a.rulecmd
        if rc == "add":
            do_rule_add(ctx, name, a)
        elif rc == "remove":
            do_rule_remove(ctx, name, a)
        elif rc == "list":
            do_rule_list(ctx, name)
        elif rc == "test":
            do_rule_test(ctx, name, a)
    elif verb == "postgres":
        pc = a.postgrescmd
        if pc == "add":
            do_postgres_add(ctx, name, a)
        elif pc == "remove":
            do_postgres_remove(ctx, name, a)
        elif pc == "list":
            do_postgres_list(ctx, name)
        elif pc == "test":
            do_postgres_test(ctx, name, a)
    elif verb == "pack":
        if a.packcmd == "add":
            do_pack_add(ctx, name, a)
        elif a.packcmd == "refresh":
            do_pack_refresh(ctx, name, a)
        elif a.packcmd == "remove":
            do_pack_remove(ctx, name, a)


# ---- loose aliases -----------------------------------------------------------
#
# In loose mode, short top-level verbs resolve to canonical commands with NO
# independent behavior. They simply translate to the workspace dispatcher.

_ALIAS_TO_WS_VERB = {
    "enter", "exec", "edit", "start", "stop", "recreate", "delete", "apply",
    "inspect", "config", "logs", "binding", "bind-dir", "mount", "rule",
    "postgres", "resolve",
}


def _dispatch_alias(ctx: Ctx, head: str, rest: list[str], trailing: list[str]) -> None:
    """Loose-only top-level aliases. `head` is the alias verb already consumed;
    `rest` is what follows."""
    if head == "use":
        if len(rest) != 1:
            fail("usage: credp use NAME")
        do_use(ctx, rest[0])
        return
    if head == "create":
        if _wants_help(rest):
            say(_CREATE_HELP)
            return
        a = _parse_create(rest)
        do_create(ctx, a.name, _create_dir(a), a.attach)
        return
    if head == "list":
        do_list(ctx, rest[0] if rest else None)
        return

    if head == "binding":
        # `credp binding <subcmd> ... [NAME]` -> resolve default workspace.
        # NAME is never given on the alias (the alias assumes the default);
        # an explicit workspace uses the canonical `workspace NAME binding`.
        _run_ws_verb(ctx, None, ["binding", *rest], trailing)
        return

    if head == "mount":
        # Like `binding`: the sub-noun's subcommand (`add`) would otherwise be
        # eaten as a NAME by the generic alias path below. The alias always acts
        # on the resolved default workspace; explicit names use the canonical
        # `workspace NAME mount` form.
        _run_ws_verb(ctx, None, ["mount", *rest], trailing)
        return

    if head == "rule":
        # Same as `binding`/`mount`: the sub-noun's subcommand would be eaten as
        # a NAME by the generic path. Always acts on the resolved default
        # workspace; explicit names use `workspace NAME rule`.
        _run_ws_verb(ctx, None, ["rule", *rest], trailing)
        return

    if head == "postgres":
        # Sub-noun (add/remove/list/test) -- special-cased like rule/binding/mount
        # so the subcommand isn't eaten as a NAME. Acts on the resolved default
        # workspace; explicit names use `workspace NAME postgres`.
        _run_ws_verb(ctx, None, ["postgres", *rest], trailing)
        return

    if head in _ALIAS_TO_WS_VERB:
        # A leading non-flag token overrides the default workspace
        # (`credp enter myproj`); flags are forwarded to the verb parser
        # (`credp enter --user root`, optionally `credp enter myproj --user root`).
        name = None
        verb_args = list(rest)
        if verb_args and not verb_args[0].startswith("-"):
            name = verb_args.pop(0)
        _run_ws_verb(ctx, name, [head, *verb_args], trailing)
        return

    fail(f"unknown command '{head}'")


# ---- main --------------------------------------------------------------------


def main_loose() -> None:
    """Console-script entry point for the loose surface (`credp`), equivalent to
    `credproxy --loose`. The strict surface uses `main` directly. (The `bin/`
    shims call `main(loose_default=...)` and stay the no-install path.)"""
    main(loose_default=True)


def main(loose_default: bool = False) -> None:
    argv = sys.argv[1:]

    argv, trailing = _split_trailing(argv)
    argv, loose, as_json, assume_yes = _pop_global_flags(argv)
    loose = loose or loose_default

    if not argv or argv[0] in ("-h", "--help", "help"):
        _print_help(loose)
        sys.exit(0)

    if argv[0] in ("version", "--version"):
        import json
        from .. import __version__
        print(json.dumps({"credproxy": __version__}) if as_json
              else f"credproxy {__version__}")
        sys.exit(0)

    render.set_format(as_json)
    ctx = Ctx(loose=loose, as_json=as_json, assume_yes=assume_yes)

    head, rest = argv[0], argv[1:]

    try:
        if head == "workspace":
            _dispatch_workspace(ctx, rest, trailing)
        elif head in _META_COMMANDS:
            _dispatch_meta(ctx, head, rest)
        elif head in ("injector", "provider"):
            _dispatch_def(ctx, head, rest)
        elif head == "pack":
            _dispatch_pack(ctx, rest)
        elif head == "script":
            _dispatch_script(ctx, rest)
        elif head == "dev":
            _dispatch_dev(ctx, rest, trailing)
        elif head == "push":
            _dispatch_push(ctx, rest, trailing)
        elif head == "emit-compose":
            _dispatch_emit_compose(ctx, rest)
        elif loose:
            # Loose surface: top-level aliases.
            _dispatch_alias(ctx, head, rest, trailing)
        else:
            fail(
                f"unknown command '{head}' "
                f"(strict surface; see `credproxy --help`)"
            )
    except CredproxyError as e:
        fail(e)


def _dispatch_meta(ctx: Ctx, head: str, rest: list[str]) -> None:
    """Top-level meta commands (no workspace argument)."""
    if head == "list":
        if len(rest) > 1:
            fail("usage: credproxy list [FILTER]")
        do_list(ctx, rest[0] if rest else None)
    elif head == "current":
        # `current` reports the loose default/cwd-resolved target -- an implicit-
        # targeting concept the strict surface disclaims (like its writer, `use`).
        if not ctx.loose:
            fail("`current` reports the loose default/cwd-resolved workspace; it "
                 "is loose-only -- use the `credp` alias (or `credproxy --loose`). "
                 "The strict surface names every workspace explicitly.")
        if rest:
            fail("usage: credp current (takes no arguments)")
        do_current(ctx)
    elif head == "info":
        if rest:
            fail("usage: credproxy info (takes no arguments)")
        do_info(ctx)
    elif head == "doctor":
        fetch = "--fetch" in rest
        positional = [a for a in rest if not a.startswith("-")]
        unknown = [a for a in rest if a.startswith("-") and a != "--fetch"]
        if unknown or len(positional) > 1:
            fail("usage: credproxy doctor [NAME] [--fetch]")
        do_doctor(ctx, positional[0] if positional else None, fetch)


def _dispatch_def(ctx: Ctx, kind: str, rest: list[str]) -> None:
    sub = rest[0] if rest else None

    if sub == "scaffold":
        args = rest[1:]
        # Honor --help BEFORE treating the next token as a NAME -- otherwise
        # `scaffold --help` would scaffold a file literally named '--help'.
        if _wants_help(args):
            say(_scaffold_help(kind))
            return
        name, script_mode, family, lang = _parse_scaffold_args(kind, args)
        if script_mode:
            if kind != "injector":
                fail("--script is only valid for `injector scaffold`")
            do_scaffold_script(ctx, name, family)
        else:
            do_scaffold(ctx, kind, name, lang)
        return

    if sub == "check" and kind == "injector":
        args = rest[1:]
        if _wants_help(args) or not args:
            say("usage: credproxy injector check NAME [--compile]\n"
                "Validate a scripted injector host-side (manifest parses and the\n"
                "named .star resolves); --compile additionally compiles the .star\n"
                "in the proxy image (needs docker + the built image).")
            return
        names = [a for a in args if not a.startswith("-")]
        flags = [a for a in args if a.startswith("-")]
        bad = [f for f in flags if f != "--compile"]
        if bad or len(names) != 1:
            fail("usage: credproxy injector check NAME [--compile]")
        do_injector_check(ctx, names[0], "--compile" in flags)
        return

    if sub == "api" and kind == "injector":
        if _wants_help(rest[1:]):
            say("usage: credproxy injector api\n"
                "Print the scripted-injector authoring reference (manifest fields\n"
                "+ the Starlark primitive API) without scaffolding anything.")
            return
        do_injector_api(ctx)
        return

    if sub == "show" and kind == "provider":
        args = rest[1:]
        names = [a for a in args if not a.startswith("-")]
        if _wants_help(args) or len(names) != 1:
            say("usage: credproxy provider show NAME\n"
                "Show a provider's source, resolved path, description, and help.")
            return
        do_provider_show(ctx, names[0])
        return

    if sub == "list":
        do_def_list(ctx, kind)
        return

    usage = (
        "usage: credproxy injector {scaffold NAME [--script [sign|substitute]]"
        "|list|check NAME|api}"
        if kind == "injector"
        else "usage: credproxy provider {scaffold NAME|list|show NAME}"
    )
    if _wants_help(rest):
        say(usage)
        return
    if not rest:
        fail(usage)
    fail(f"unknown {kind} command '{rest[0]}'")


def _parse_scaffold_args(kind: str, args: list[str]) -> tuple[str, bool, str, str]:
    """Parse `scaffold` args: a NAME plus optional `--script [sign|substitute]`
    (injector) or `--lang python|sh` (provider)."""
    name: str | None = None
    script_mode = False
    family = "sign"
    lang = "python"
    i = 0
    while i < len(args):
        tok = args[i]
        if tok == "--script":
            script_mode = True
            if i + 1 < len(args) and args[i + 1] in ("sign", "substitute"):
                family = args[i + 1]
                i += 1
        elif tok == "--lang":
            if i + 1 >= len(args) or args[i + 1].startswith("-"):
                fail("--lang needs a value (python or sh)")
            lang = args[i + 1]
            i += 1
        elif tok.startswith("-"):
            fail(f"unknown flag {tok!r}; usage: credproxy {kind} scaffold NAME "
                 f"[--script [sign|substitute]] [--lang python|sh]")
        elif name is None:
            name = tok
        else:
            fail(f"usage: credproxy {kind} scaffold NAME")
        i += 1
    if name is None:
        fail(f"usage: credproxy {kind} scaffold NAME")
    return name, script_mode, family, lang


def _dispatch_push(ctx: Ctx, rest: list[str], trailing: list[str]) -> None:
    """The top-level `push`: two forms, distinguished by `--admin`.

    - STATELESS escape hatch: `credproxy push --admin URL --config FILE --token
      FILE [--wait] [--timeout SECS]` -- no workspace, no state (both surfaces).
    - the loose workspace-push alias: `credp push [NAME]` -> the default/cwd
      workspace's `push`. Strict has no bare `push NAME` form (it names workspaces
      explicitly via `workspace NAME push`)."""
    if "--admin" in rest or "--config" in rest or "--token" in rest:
        if _wants_help(rest):
            say(_PUSH_STATELESS_HELP)
            return
        do_push_stateless(ctx, rest)
        return
    if _wants_help(rest):
        say("credproxy push -- push config to a proxy.\n"
            "  credproxy workspace NAME push [--wait] [--timeout SECS]\n"
            "  credproxy push --admin URL --config FILE --token FILE  (stateless)\n"
            "  credp push [NAME]                                       (loose alias)")
        return
    if not ctx.loose:
        fail("`credproxy push` is the stateless escape hatch and needs "
             "--admin URL --config FILE --token FILE; to push a workspace use "
             "`credproxy workspace NAME push`")
    name = None
    verb_args = list(rest)
    if verb_args and not verb_args[0].startswith("-"):
        name = verb_args.pop(0)
    _run_ws_verb(ctx, name, ["push", *verb_args], trailing)


_EMIT_COMPOSE_HELP = (
    "credproxy emit-compose [NAME] [--image TAG] -- print a Docker Compose\n"
    "fragment for a credproxy proxy sidecar to stdout (the one Compose-aware\n"
    "command; everything else is parent-agnostic). Ports and mount paths come\n"
    "from the proxy image's ENV contract, so they can't go stale.\n"
    "\n"
    "  NAME          bake this workspace's real token path (<state>/auth.token)\n"
    "                into the bind mount -- the workspace must exist (pairs with\n"
    "                `create --attach`). Omit NAME to emit a\n"
    "                ${CREDPROXY_STATE:?...}/auth.token reference a Compose .env\n"
    "                can interpolate.\n"
    "  --image TAG   proxy image for `docker inspect` AND the emitted `image:`\n"
    "                line (default the built-in proxy image tag).\n"
    "\n"
    "The healthcheck gates on /ready (creds-ready), not /health, so a Compose\n"
    "`service_healthy` dependency opens only once credentials are pushed. Push\n"
    "after `up`: `credproxy workspace NAME push` (or the stateless `credproxy\n"
    "push`). (`--json` does not apply -- it emits YAML.)"
)


def _dispatch_emit_compose(ctx: Ctx, rest: list[str]) -> None:
    """Top-level `emit-compose [NAME] [--image TAG]`: a Compose fragment to
    stdout. Available on both surfaces (it names no implicit workspace)."""
    if _wants_help(rest):
        say(_EMIT_COMPOSE_HELP)
        return
    # YAML is not credproxy's to structure, so --json has nothing to wrap --
    # refuse it rather than emit non-JSON on a --json call (mirrors `exec`).
    if ctx.json:
        fail("emit-compose prints a YAML Compose fragment; `--json` does not "
             "apply")
    p = _LeafParser(prog="credproxy emit-compose", add_help=False)
    p.add_argument("name", nargs="?", default=None)
    p.add_argument("--image", dest="image", default=None, metavar="TAG")
    a = p.parse_args(rest)
    do_emit_compose(ctx, a.name, a.image)


def _dispatch_script(ctx: Ctx, rest: list[str]) -> None:
    """`script` definition commands. Today just `check [NAME]` -- compile scripts
    before push (sibling of `injector list`/`provider list`/`pack list`)."""
    usage = ("usage: credproxy script check [NAME] [--container]\n"
             "Compile resolvable .star scripts in the proxy runtime (on-host when\n"
             "the Starlark deps import, else in the image). A script referenced by\n"
             "a scripted-injector manifest is compiled under the injector profile\n"
             "paired with that manifest; an unreferenced script is tried under both\n"
             "the injector and rule profiles. Exit 0 iff all compile.")
    if not rest or _wants_help(rest):
        say(usage)
        return
    if rest[0] != "check":
        fail(f"unknown script command '{rest[0]}' ({usage})")
    args = rest[1:]
    names = [a for a in args if not a.startswith("-")]
    flags = [a for a in args if a.startswith("-")]
    force_container = "--container" in flags or "--docker" in flags
    bad = [f for f in flags if f not in ("--container", "--docker")]
    if bad or len(names) > 1:
        fail(usage)
    do_script_check(ctx, names[0] if names else None, force_container)


def _dispatch_pack(ctx: Ctx, rest: list[str]) -> None:
    # `pack` is dual-role: `list` is definitional (no workspace, both surfaces);
    # `add` is workspace-scoped (it references a pack in a workspace TOML). A bare
    # `pack` or `--help` lists, since the listing IS the documentation.
    if not rest or _wants_help(rest) or rest[0] == "list":
        do_pack_list(ctx)
        return
    if rest[0] in ("add", "refresh", "remove"):
        # Top-level `pack {add,refresh,remove}` is the loose implicit-workspace
        # form; strict requires the explicit `workspace NAME pack ...`.
        if not ctx.loose:
            fail(f"`pack {rest[0]}` needs a workspace: "
                 f"`credproxy workspace NAME pack {rest[0]}`")
        _run_ws_verb(ctx, None, ["pack", *rest], [])
        return
    fail(f"unknown pack command '{rest[0]}' (usage: credproxy pack list  |  "
         f"credproxy workspace NAME pack add|refresh|remove ...)")


def _dispatch_dev(ctx: Ctx, rest: list[str], trailing: list[str]) -> None:
    usage = "usage: credproxy dev {build|test|reload}"
    if _wants_help(rest):
        say(usage)
        return
    if not rest:
        fail(usage)
    sub = rest[0]
    if sub == "build":
        do_dev_build(ctx)
    elif sub == "test":
        test_args = rest[1:]  # selection flags (pytest args go after `--`)
        _known = ("--cli", "--proxy", "--container", "--docker")
        unknown = [a for a in test_args if a not in _known]
        if unknown:
            fail(f"dev test: unknown flag(s) {' '.join(unknown)} "
                 f"(use --cli/--proxy/--container; pytest args go after `--`)")
        cli_only = "--cli" in test_args
        proxy_only = "--proxy" in test_args
        force_container = "--container" in test_args or "--docker" in test_args
        if cli_only and proxy_only:
            # Both = "neither" under the old logic, yet the proxy path still ran.
            fail("dev test: --cli and --proxy are mutually exclusive "
                 "(omit both to run both suites)")
        do_dev_test(ctx, trailing, cli_only=cli_only, proxy_only=proxy_only,
                    force_container=force_container)
    elif sub == "reload":
        do_dev_reload(ctx, rest[1] if len(rest) > 1 else None)
    else:
        fail(f"unknown dev command '{sub}'")
