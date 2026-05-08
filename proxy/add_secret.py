"""Add or replace one secret in /run/secrets/secrets.json.

Invoked via `docker exec -i --user 31337 credproxy python /opt/proxy/add_secret.py NAME`
with the value on stdin. Atomic write: tempfile + os.replace, mode 0600.

The supervisor pipes this file into each python spawn's stdin, so a
subsequent `reload.sh` is enough for the new value to take effect — no
container restart, no workspace restart.

Tolerates a missing or corrupt existing file (treats as empty). Refuses
empty values and badly-shaped names.
"""
import json
import os
import re
import sys

SECRETS_PATH = "/run/secrets/secrets.json"
NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("usage: add_secret.py NAME (value via stdin)")
    name = sys.argv[1]
    if not NAME_RE.match(name):
        sys.exit(f"invalid secret name: {name!r} (must match {NAME_RE.pattern})")

    value = sys.stdin.read()
    if not value:
        sys.exit("empty value on stdin; refusing to write")

    try:
        with open(SECRETS_PATH) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}

    data[name] = value

    tmp = SECRETS_PATH + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.replace(tmp, SECRETS_PATH)
    print(f"added/updated secret: {name}")


if __name__ == "__main__":
    main()
