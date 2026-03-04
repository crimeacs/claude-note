API_URL  := http://localhost:8000
OBSIDIAN := http://localhost:8080

# ── Docker ──────────────────────────────────────────────

.PHONY: up down rebuild logs logs-api logs-obsidian

up:                          ## Start all containers
	docker compose up -d

down:                        ## Stop all containers
	docker compose down

rebuild:                     ## Rebuild and restart
	docker compose up --build -d

logs:                        ## Follow all container logs
	docker compose logs -f

logs-api:                    ## Follow claude-note API logs
	docker compose logs -f claude-note

logs-obsidian:               ## Follow Obsidian container logs
	docker compose logs -f obsidian

# ── API smoke tests ─────────────────────────────────────

.PHONY: test-health test-event test-status test-drain test-all

test-health:                 ## GET /health
	@curl -sf $(API_URL)/health | python3 -m json.tool

test-event:                  ## POST a fake hook event to /events
	@curl -sf -X POST $(API_URL)/events \
	  -H 'Content-Type: application/json' \
	  -d '{"session_id":"test-$(shell date +%s)","hook_event_name":"UserPromptSubmit","cwd":"/tmp","transcript_path":""}' \
	  | python3 -m json.tool

test-status:                 ## GET /status
	@curl -sf $(API_URL)/status | python3 -m json.tool

test-drain:                  ## POST /drain — process all pending sessions
	@curl -sf -X POST $(API_URL)/drain | python3 -m json.tool

test-all: test-health test-event test-status test-drain  ## Run all smoke tests
	@echo "\nAll tests passed."

# ── Simulate a full session ─────────────────────────────

SESSION_ID := sim-$(shell date +%s)

.PHONY: sim-session

sim-session:                 ## Simulate a full Claude Code session (prompt → tool → stop)
	@echo "==> UserPromptSubmit"
	@curl -sf -X POST $(API_URL)/events \
	  -H 'Content-Type: application/json' \
	  -d '{"session_id":"$(SESSION_ID)","hook_event_name":"UserPromptSubmit","cwd":"/tmp","transcript_path":""}' \
	  | python3 -m json.tool
	@sleep 1
	@echo "==> PostToolUse (Read)"
	@curl -sf -X POST $(API_URL)/events \
	  -H 'Content-Type: application/json' \
	  -d '{"session_id":"$(SESSION_ID)","hook_event_name":"PostToolUse","cwd":"/tmp","transcript_path":"","tool_name":"Read"}' \
	  | python3 -m json.tool
	@sleep 1
	@echo "==> Stop"
	@curl -sf -X POST $(API_URL)/events \
	  -H 'Content-Type: application/json' \
	  -d '{"session_id":"$(SESSION_ID)","hook_event_name":"Stop","cwd":"/tmp","transcript_path":""}' \
	  | python3 -m json.tool
	@echo "\nSession $(SESSION_ID) submitted. Check status:"
	@echo "  make test-status"

# ── CLIProxyAPI ─────────────────────────────────────────

.PHONY: cliproxy-login cliproxy-logs

cliproxy-login:              ## OAuth login to Claude via CLIProxyAPI (prints URL)
	docker compose stop cliproxy
	docker compose run --rm cliproxy ./CLIProxyAPI --claude-login --no-browser
	docker compose up -d cliproxy

cliproxy-logs:               ## Follow CLIProxyAPI logs
	docker compose logs -f cliproxy

# ── Hooks ───────────────────────────────────────────────

HOOKS_SCRIPT := hooks-installation/manage-hooks.py

.PHONY: install-hooks remove-hooks

install-hooks:               ## Install API hooks into ~/.claude/settings.json (backs up first)
	@python3 $(HOOKS_SCRIPT) install

remove-hooks:                ## Remove API hooks from ~/.claude/settings.json (backs up first)
	@python3 $(HOOKS_SCRIPT) remove

# ── QMD Semantic Search ────────────────────────────────

QMD_URL := http://localhost:8686
Q ?= test query

.PHONY: qmd-logs qmd-status qmd-reindex qmd-search qmd-vsearch qmd-query qmd-compare

qmd-logs:                    ## Follow QMD logs
	docker compose logs -f qmd

qmd-status:                  ## Show QMD index status and health
	@curl -sf $(QMD_URL)/status | python3 -m json.tool

qmd-reindex:                 ## Trigger manual re-index of vault
	@curl -sf -X POST $(QMD_URL)/reindex | python3 -m json.tool

qmd-search:                  ## Test BM25 search (make qmd-search Q="query")
	@curl -sf -X POST $(QMD_URL)/search \
	  -H 'Content-Type: application/json' \
	  -d '{"query":"$(Q)","limit":5}' \
	  | python3 -m json.tool

qmd-vsearch:                 ## Test vector search (make qmd-vsearch Q="query")
	@curl -sf -X POST $(QMD_URL)/vsearch \
	  -H 'Content-Type: application/json' \
	  -d '{"query":"$(Q)","limit":5,"min_score":0.3}' \
	  | python3 -m json.tool

qmd-query:                   ## Test hybrid search (make qmd-query Q="query")
	@curl -sf -X POST $(QMD_URL)/query \
	  -H 'Content-Type: application/json' \
	  -d '{"query":"$(Q)","limit":5}' \
	  | python3 -m json.tool

qmd-compare:                 ## Compare all search modes (make qmd-compare Q="query")
	@curl -sf "$(QMD_URL)/compare?q=$(Q)&limit=5" | python3 -m json.tool

# ── Help ────────────────────────────────────────────────

.PHONY: help
help:                        ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*##' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

.DEFAULT_GOAL := help
