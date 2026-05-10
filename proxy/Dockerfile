FROM python:3.12-slim

# Build arg matches MITMPROXY_UID in proxy/constants.sh; the Makefile
# wires that value through via --build-arg.
ARG MITMPROXY_UID=31337

RUN apt-get update && apt-get install -y --no-install-recommends \
        iptables \
        iproute2 \
        procps \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --uid ${MITMPROXY_UID} --create-home --shell /bin/sh mitmuser

WORKDIR /opt/proxy

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY entrypoint.sh main.py addon.py bootstrap.py \
     admin.py config.py constants.sh constants.py ./
RUN chmod 0755 entrypoint.sh

ENTRYPOINT ["/opt/proxy/entrypoint.sh"]
