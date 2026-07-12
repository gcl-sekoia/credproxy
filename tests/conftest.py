# Proxy-suite (tests/) shared setup. Runs both in the image (via `credproxy dev
# test --container`, proxy bind-mounted at /opt/proxy) and ON-HOST (`dev test`'s
# fast path, or a direct `pytest tests/ --ignore=tests/cli`). Two jobs, done
# before any proxy module imports so `import constants` etc. resolve:
#
#   1. Put the proxy source dir on sys.path.
#   2. Supply the CREDPROXY_* constants contract that proxy/constants.py reads
#      with NO defaults (the image is the single source of truth for these).
#
# NOTE: this conftest is an ANCESTOR of tests/cli/ too, so it also loads for the
# CLI suite (`uv run pytest`). That's benign -- the CLI suite reads only
# CREDPROXY_OVERLAY_PATH from this namespace (pinned by tests/cli/conftest.py),
# never the injected contract values.
import os
import re
import sys
from pathlib import Path

# 1. sys.path -> the proxy source. In the image CREDPROXY_SOURCE is set (real
#    ENV) and points at the bind mount; on-host it's unset here (we set it via
#    setdefault below), so fall back to the repo-relative proxy/ dir. Read the
#    env BEFORE the setdefault so on-host doesn't pick up the image path.
_proxy_dir = os.environ.get("CREDPROXY_SOURCE") or str(
    Path(__file__).resolve().parent.parent / "proxy")
if _proxy_dir not in sys.path:
    sys.path.insert(0, _proxy_dir)

# 2. The CREDPROXY_* contract. constants.py reads these with no defaults; in the
#    image they come from proxy/Dockerfile's ENV block (single source of truth),
#    and the host CLI reads them back via `docker inspect`. On-host there's no
#    image, so parse the SAME Dockerfile at rest -- a second READER of the one
#    declaration, not a second source -- and setdefault each (real ENV in-image
#    wins; an explicit override wins). Whole-file regex, not line-based: the ENV
#    block is one backslash-continuation instruction. Lazy: only touched when a
#    key is actually missing, so the in-image path never reads the file.
_CONTRACT_KEYS = (
    "CREDPROXY_MITMPROXY_UID", "CREDPROXY_HTTP_PORT", "CREDPROXY_PROXY_PORT",
    "CREDPROXY_SENTINEL_IP", "CREDPROXY_TMPFS", "CREDPROXY_TOKEN_PATH",
    "CREDPROXY_SOURCE",
)
if any(k not in os.environ for k in _CONTRACT_KEYS):
    _dockerfile = Path(__file__).resolve().parent.parent / "proxy" / "Dockerfile"
    if not _dockerfile.is_file():
        raise RuntimeError(
            f"proxy suite needs the CREDPROXY_* contract but {_dockerfile} is "
            f"missing; set the vars manually (see docs/dev-environment.md) or "
            f"run via `credproxy dev test`")
    for _k, _v in re.findall(r"(CREDPROXY_[A-Z_]+)=(\S+)", _dockerfile.read_text()):
        os.environ.setdefault(_k, _v)
