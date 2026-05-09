"""Proxy mitmproxy addon: terminate configured hosts, substitute placeholders.

For SNIs in `state.creds.intercept_hosts()`, mitmproxy terminates TLS
using its CA; the `request` hook scans configured headers and
substring-replaces the configured placeholder with the real credential
before forwarding. For everything else, `ignore_connection = True`
puts the flow into byte-passthrough so we only see the SNI.

The addon reads `state.creds` fresh on every call (rather than caching
the Credentials at construction) so an in-process config reload --
admin_config swapping `state.creds` under the same AppState -- takes
effect immediately for new flows without a process restart.

The sentinel-IP path is handled by the merged HTTP listener
(admin + bootstrap) on a separate port, so this addon never sees those
flows.
"""
from mitmproxy import http, tls


class HostnameLogger:
    def __init__(self, state):
        # `state` is duck-typed: anything with a `.creds` attribute
        # pointing to a config.Credentials. In production, an
        # admin.AppState; in tests, a SimpleNamespace.
        self._state = state

    def tls_clienthello(self, data: tls.ClientHelloData) -> None:
        creds = self._state.creds
        sni = data.client_hello.sni
        if sni in creds.intercept_hosts():
            print(f"[sni] {sni} (intercept)", flush=True)
            return
        print(f"[sni] {sni or '<no-sni>'} (passthrough)", flush=True)
        data.ignore_connection = True

    def request(self, flow: http.HTTPFlow) -> None:
        creds = self._state.creds
        req = flow.request
        host = req.pretty_host
        substituted: list[str] = []
        for sub in creds.substitutions_for(host):
            value = req.headers.get(sub.header)
            if value is None or sub.placeholder not in value:
                continue
            req.headers[sub.header] = value.replace(sub.placeholder, sub.real)
            substituted.append(sub.header)

        if substituted:
            marker = f" (sub:{','.join(substituted)})"
        elif host in creds.intercept_hosts():
            marker = " (no-sub)"
        else:
            marker = ""
        print(f"[http] {req.method} {host}{req.path}{marker}", flush=True)
