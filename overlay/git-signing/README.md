# overlay: git-signing (lib)

Lets you sign your git commits from inside the workspace — without the signing key
ever entering the container. A small **dedicated** ssh-agent runs on the host holding
only a throwaway signing key, and its socket is forwarded in; git signs through it.

Why a dedicated key rather than your normal agent:
- **Least privilege** — the container can sign, and nothing else; your auth keys never enter it.
- **Blast radius** — if a workspace misuses the socket, you revoke one key on GitHub; your primary keys are untouched.
- **Attribution** — commits carry a distinct key, so GitHub's "Verified" badge shows which came from a container.

Signing is **dynamic**: `user.signingkey` is left unset and git reads the key from the
agent on every commit. So you can load or restart the agent any time (no recreate), and
a commit made with an empty agent is *refused* — never silently unsigned — with a
`warning:` pointing back at the host helper.

**Contributes:** a `setup.d` step (configures git signing) and a host helper
(`bin/credproxy-signing-agent`).

## Compose from a profile

Forward the agent-socket **directory** (not the socket file — so agent restarts are
transparent to a running container), mount the setup step, and set `SSH_AUTH_SOCK`:
```toml
{ bind    = "~/.ssh/credproxy-agent",  target = "/ssh-agent" },                     # agent socket dir
{ overlay = "setup.d/git-signing.sh",  target = "/opt/setup.d/40-git-signing.sh" }, # configures signing

[env]
SSH_AUTH_SOCK = "/ssh-agent/agent.sock"
```
`setup-runner` runs the step automatically. The socket **directory** must exist before
`start` (credproxy validates bind sources) — the host helper below creates it.

## Configure — host setup

**1. Generate a dedicated signing key**
```sh
ssh-keygen -t ed25519 -f ~/.ssh/credproxy-signing -C "credproxy workspace signing"
```
**2. Register the public key on GitHub as a *signing* key**
```sh
gh ssh-key add ~/.ssh/credproxy-signing.pub --type signing --title "credproxy workspace signing"
```
(`--type signing`, not the default `authentication`.) Or via the web UI at
<https://github.com/settings/ssh/new> → **Key type: Signing Key**. GitHub allows
several signing keys per account, so a workspace key verifies just like your normal one.

**3. Run the dedicated agent** — `bin/credproxy-signing-agent` starts an agent on a
fixed socket holding only that key. Put it on your PATH and run it before `start` (or
wire it into a login item / systemd user service):
```sh
export PATH="$PATH:/path/to/credproxy/overlay/git-signing/bin"
credproxy-signing-agent          # start the agent + load the key (idempotent)
credproxy-signing-agent status   # report state without changing anything
```
Optional env knobs:
- `CREDPROXY_SIGNING_KEY` — key path (default `~/.ssh/credproxy-signing`).
- `CREDPROXY_SIGNING_SOCK` — socket path (default `~/.ssh/credproxy-agent/agent.sock`).
- `CREDPROXY_SIGNING_CONFIRM=1` — load with `ssh-add -c`, so the host prompts for approval on *every* signature.

**Verify** inside the workspace:
```sh
git -C /workdir commit --allow-empty -m "signing smoke test"
git -C /workdir log --show-signature -1      # → Good "git" signature … ED25519
```
Local verification needs an `allowed_signers` file, which the setup step writes
(`~/.config/git/allowed_signers`, `gpg.ssh.allowedSignersFile`) **whenever it sees a
key**. Signing is dynamic but that file isn't, so if you loaded the key *after* `start`
(when setup had no key to record), re-run the step once to enable verification:
```sh
bash /opt/setup.d/*-git-signing.sh      # re-runnable; refreshes signing + allowed_signers
```
(Signing itself still works without it — the commit carries the signature and GitHub
shows **Verified**; the `allowed_signers` file only affects *local* `--show-signature`.)
