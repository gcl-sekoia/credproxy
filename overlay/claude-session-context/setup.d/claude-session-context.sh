#!/bin/sh
# claude-session-context setup step — mounted into a profile's /opt/setup.d/. Registers
# the SessionStart hook. The runner itself is runtime (mounted at
# /opt/session-context.sh) and runs the hook body when invoked with no args, so it
# can't double as the setup step — hence this tiny fragment.
exec bash "${SESSION_CONTEXT_RUNNER:-/opt/session-context.sh}" --install
