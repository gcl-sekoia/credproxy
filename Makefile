PROXY_NAME      := credproxy
PROXY_IMAGE     := credproxy:dev
WORKSPACE_IMAGE := python:3.12-slim

# bash, not dash: the `up` recipe relies on bash's behavior of honoring
# `</dev/stdin` on backgrounded jobs. dash redirects backgrounded stdin
# to /dev/null per POSIX even with the explicit redirect.
SHELL := /bin/bash

.DEFAULT_GOAL := help

.PHONY: help build up down restart logs reload shell workspace rebuild test add-secret set-config

help:
	@echo "credproxy dev harness"
	@echo ""
	@echo "  make build      build the proxy image"
	@echo "  make up         start the proxy; reads JSON secrets from stdin"
	@echo "                  e.g. echo '{\"GITHUB_PAT\":\"ghp_...\"}' | make up"
	@echo "                  no secrets needed: make up </dev/null"
	@echo "  make down       stop and remove the proxy container"
	@echo "  make restart    down + up (no rebuild) -- expects secrets on stdin"
	@echo "  make logs       tail proxy logs"
	@echo "  make reload     hot-reload python code (secrets cached in supervisor)"
	@echo "  make shell      open a shell in the proxy (root)"
	@echo "  make workspace  run a workspace container joined to the proxy netns"
	@echo "  make rebuild    down + build + up -- expects secrets on stdin"
	@echo "  make test       run pytest in the proxy image"
	@echo "  make add-secret NAME=X  add/update secret X (value on stdin); reloads"
	@echo "                  e.g. op read 'op://...' | make add-secret NAME=GITHUB_PAT"
	@echo "  make set-config push proxy/config.yaml via admin API after resolving"
	@echo "                  \$${secret:NAME} refs from host env. Reloads proxy."
	@echo "                  e.g. GITHUB_PAT=\$$(op read 'op://...') make set-config"

build:
	docker build -t $(PROXY_IMAGE) proxy/

up:
	@# `docker run -d` closes stdin, defeating the secrets pipeline.
	@# Instead we run in the foreground and background it. POSIX shells
	@# default backgrounded jobs' stdin to /dev/null, so we explicitly
	@# redirect </dev/stdin to keep the pipe attached -- that's how EOF
	@# from the source `<json> | make up` reaches the supervisor's cat.
	@#
	@# Stdin shape: {"auth_token": "...", "secrets": {...}}.
	@# We generate a fresh bearer token here, persist a copy to
	@# .run/auth.token (0600) for the host CLI to reuse, and wrap the
	@# user's secrets JSON. TOKEN is passed to python via env (not argv)
	@# so it doesn't show in ps. The wrapper python tolerates empty stdin.
	@mkdir -p .run
	@TOKEN=$$(openssl rand -hex 16); \
	echo -n "$$TOKEN" > .run/auth.token; \
	chmod 600 .run/auth.token; \
	TOKEN="$$TOKEN" python3 -c 'import json,os,sys; raw=sys.stdin.read().strip(); secrets=json.loads(raw) if raw else {}; print(json.dumps({"auth_token":os.environ["TOKEN"],"secrets":secrets}))' \
		| docker run -i --rm \
			--name $(PROXY_NAME) \
			--cap-add NET_ADMIN \
			--tmpfs /run/secrets:size=64k,uid=31337,mode=0700 \
			-p 127.0.0.1:39997:39997 \
			-v $(CURDIR)/proxy:/opt/proxy \
			$(PROXY_IMAGE) </dev/stdin >/dev/null 2>&1 &
	@sleep 0.5
	@docker ps --filter name=$(PROXY_NAME) --format '{{.Names}}' \
		| grep -q $(PROXY_NAME) \
		&& echo "$(PROXY_NAME) started; token in .run/auth.token" \
		|| (echo "$(PROXY_NAME) failed to start; check 'docker logs'"; exit 1)

down:
	-docker rm -f $(PROXY_NAME) 2>/dev/null

restart: down up

logs:
	docker logs -f $(PROXY_NAME)

reload:
	docker exec $(PROXY_NAME) /opt/proxy/reload.sh

shell:
	docker exec -it --user 0 $(PROXY_NAME) bash

workspace:
	docker run --rm -it --network=container:$(PROXY_NAME) \
		$(WORKSPACE_IMAGE) bash

rebuild: down build up

test:
	docker run --rm \
		-v $(CURDIR)/proxy:/opt/proxy \
		-v $(CURDIR)/tests:/opt/tests \
		-w /opt \
		--entrypoint python \
		$(PROXY_IMAGE) \
		-m pytest -v tests/

add-secret:
	@[ -n "$(NAME)" ] || { echo 'usage: NAME=X make add-secret  (value on stdin)'; exit 1; }
	@[ -f .run/auth.token ] || { echo "$(PROXY_NAME): .run/auth.token missing; is the proxy up?"; exit 1; }
	@TOKEN=$$(cat .run/auth.token); \
	NAME="$(NAME)" python3 -c 'import json,os,sys; print(json.dumps({"name": os.environ["NAME"], "value": sys.stdin.read()}))' \
		| curl -sS --fail --show-error \
			-H "Authorization: Bearer $$TOKEN" \
			-H "Content-Type: application/json" \
			--data-binary @- \
			http://127.0.0.1:39997/admin/secrets \
		&& echo

set-config:
	@./bin/credproxy push-config
