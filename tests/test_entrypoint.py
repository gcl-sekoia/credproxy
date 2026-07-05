"""Guard: entrypoint.sh installs iptables rules BEFORE exec'ing python.

That ordering is what lets `/health` (which observes only that the listeners
accept connections) imply capture-readiness -- a listener cannot come up before
the netfilter rules that redirect traffic to it. If someone reorders the script
so python starts before the rules are in, `/health` would go green during a
window where egress isn't actually being captured. Crude line-number check, but
a silent regression here is a real security gap.
"""
from pathlib import Path

ENTRYPOINT = Path(__file__).resolve().parent.parent / "proxy" / "entrypoint.sh"


def test_iptables_rules_installed_before_python_exec():
    lines = ENTRYPOINT.read_text().splitlines()

    iptables_lines = [
        i for i, ln in enumerate(lines)
        # `iptables`/`ip6tables` invocations, not the comment prose about them.
        if ln.lstrip().startswith(("iptables", "ip6tables"))
    ]
    # The `exec ... python` statement spans continuation lines (`python` lands on
    # a later physical line); anchor on the `exec ` line, which is the earliest
    # part of that statement -- a conservative bound (python runs strictly after).
    exec_lines = [
        i for i, ln in enumerate(lines) if ln.lstrip().startswith("exec ")
    ]
    python_lines = [i for i, ln in enumerate(lines) if "python" in ln]

    assert iptables_lines, "no iptables invocation found in entrypoint.sh"
    assert exec_lines, "no `exec ...` line found in entrypoint.sh"
    assert python_lines, "no python line found in entrypoint.sh"

    last_iptables = max(iptables_lines)
    python_exec = min(exec_lines)
    assert last_iptables < python_exec, (
        f"iptables rules (last at line {last_iptables + 1}) must be installed "
        f"before the python exec (line {python_exec + 1}) -- see the ordering "
        f"comment in entrypoint.sh; /health green must imply capture active"
    )
    # And python really is what's exec'd (a `python` token on/after the exec, not
    # only the header-comment prose that also mentions python).
    assert any(i >= python_exec for i in python_lines), \
        "expected a python invocation at or after the exec line"
