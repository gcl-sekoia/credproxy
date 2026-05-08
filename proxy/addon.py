"""Proxy mitmproxy addon: terminate configured hosts, inject headers.

For SNIs in `creds.intercept_hosts()`, mitmproxy terminates TLS using its
CA; the `request` hook then injects any configured headers and the flow
continues to the upstream. For everything else, `ignore_connection =
True` puts the flow into byte-passthrough so we only see the SNI.

The sentinel-IP path is handled by bootstrap.py on a separate listener
(:39998), so this addon never sees those flows.
"""
from mitmproxy import http, tls

from config import Credentials


class HostnameLogger:
    def __init__(self, creds: Credentials):
        self._creds = creds

    def tls_clienthello(self, data: tls.ClientHelloData) -> None:
        sni = data.client_hello.sni
        if sni in self._creds.intercept_hosts():
            print(f"[sni] {sni} (intercept)", flush=True)
            return
        print(f"[sni] {sni or '<no-sni>'} (passthrough)", flush=True)
        data.ignore_connection = True

    def request(self, flow: http.HTTPFlow) -> None:
        req = flow.request
        host = req.pretty_host
        injected = []
        for header, value in self._creds.headers_for(host).items():
            req.headers[header] = value
            injected.append(header)
        marker = f" (+{','.join(injected)})" if injected else ""
        print(f"[http] {req.method} {host}{req.path}{marker}", flush=True)
