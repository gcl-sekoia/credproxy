← [02 · Your first workspace](02-first-workspace.md) · [index](../README.md) · [04 · Your first credential](04-first-credential.md) →

# 03 · Daily workflow

You now have a workspace. This chapter covers the commands you use every day and
the one idea that ties them together: which workspace a command acts on.

## Two commands: `credp` and `credproxy`

credproxy ships two commands that do the same work with different defaults:

- **`credproxy`** is strict. You name the workspace every time
  (`credproxy workspace myproject enter`). It never guesses and never prompts.
  Use it in scripts.
- **`credp`** is for humans. It fills in a default workspace, adds short verbs
  (`credp enter`), and asks before destructive actions. Use it day to day.

This guide uses `credp`. Every `credp` command has an explicit
`credproxy workspace NAME ...` form behind it.

> [!NOTE]
> `--json` works on both commands for machine-readable output, and `--yes`
> skips confirmation prompts.

## The default workspace

When you omit the workspace name, `credp` needs to know which one you mean. It
picks the **default workspace**. Set it once:

```console
$ credp use myproject
default workspace is now 'myproject'
```

Check which workspace a bare command will target:

```console
$ credp current
myproject
```

## Addressing by directory

Naming the default is one way. Another is to let the current directory decide.
If you created the workspace with `--here` (or ran `credp bind-dir`), then
running `credp` from that directory — or any subfolder — targets that workspace,
even if a different one is the default.

```sh
credp create myproject --here      # link at creation time
credp bind-dir --dir ~/work/api    # or link an existing workspace to a path
```

This is the `cd project && credp enter` convenience. When two ways of resolving
disagree, the directory match wins, and `credp` prints which workspace it chose.

> [!TIP]
> Working across several projects at once? Directory addressing means you never
> have to switch a global default. Want the resolution rules in full? →
> [Configuration reference](../reference/configuration.md). Otherwise, continue.

## The start / enter / stop rhythm

Day to day you cycle through a few verbs, all on the default (or directory-
matched) workspace:

```sh
credp start      # bring up both containers (safe to re-run)
credp enter      # open a shell inside the workspace
credp stop       # stop both containers; config and data are kept
```

`enter` starts the workspace automatically if it is stopped, so most days you
just run `credp enter`.

## One-off commands with `exec`

To run a single command in the workspace without opening a shell, use `exec`.
It starts the workspace if needed, runs the command, and returns its exit code —
ideal for scripts and agents:

```console
$ credp exec -- curl -s https://example.com -o /dev/null -w '%{http_code}\n'
200
```

Everything after `--` is the command to run. Unlike `enter`, `exec` never
triggers an automatic stop, so you can call it many times in a row.

## Watching traffic with `logs`

The proxy records what it does. `credp logs` streams that record, one readable
line per event:

```console
$ credp logs
2026-07-05T18:22:01Z  sni    api.github.com (intercept)
2026-07-05T18:22:01Z  http   GET api.github.com/user
```

Add `--audit` to see only credential and rule events. You will use this in the
next chapter to watch a credential swap happen.

---

**Next:** [04 · Your first credential](04-first-credential.md) — make a real
token work inside the container.
