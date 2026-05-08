"""Proxy mitmproxy addon: terminate configured hosts, passthrough the rest.

For SNIs in INTERCEPT_HOSTS, mitmproxy terminates TLS using its CA and
the `request` hook fires with decrypted method/host/path. For everything
else, `ignore_connection = True` puts the flow into byte-passthrough so
we only see the SNI.

The sentinel-IP path is handled by bootstrap.py on a separate listener
(:39998), so this addon never sees those flows.
"""
from mitmproxy import http, tls

INTERCEPT_HOSTS = {"api.github.com"}


class HostnameLogger:
    def tls_clienthello(self, data: tls.ClientHelloData) -> None:
        sni = data.client_hello.sni
        if sni in INTERCEPT_HOSTS:
            print(f"[sni] {sni} (intercept)", flush=True)
            return
        print(f"[sni] {sni or '<no-sni>'} (passthrough)", flush=True)
        data.ignore_connection = True

    def request(self, flow: http.HTTPFlow) -> None:
        req = flow.request
        print(f"[http] {req.method} {req.pretty_host}{req.path}", flush=True)
