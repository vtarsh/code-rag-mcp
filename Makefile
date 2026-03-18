# code-rag-mcp — Makefile
# Usage: make <target> [PROFILE=my-org] [MODEL=coderank|minilm]

PROFILE  ?= $(shell cat .active_profile 2>/dev/null || echo "example")
MODEL    ?= coderank
SCRIPTS   = scripts
PYTHON    = python3
export CODE_RAG_HOME ?= $(shell pwd)

.PHONY: init build update test health clean register switch-model help profile

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

init: ## Run setup wizard for a new profile
	$(PYTHON) setup_wizard.py

build: ## Full pipeline: clone → extract → index → vectors → graph (~30-60 min)
	@echo "Building with profile: $(PROFILE)"
	@echo "This may take 30-60 minutes depending on org size..."
	ACTIVE_PROFILE=$(PROFILE) $(SCRIPTS)/full_update.sh --full

update: ## Incremental update (changed repos only)
	ACTIVE_PROFILE=$(PROFILE) $(SCRIPTS)/full_update.sh

test: ## Run tests
	$(PYTHON) -m pytest tests/ -q

health: ## Run health check and diagnostics
	ACTIVE_PROFILE=$(PROFILE) $(PYTHON) -c "from src.tools.service import health_check_tool; print(health_check_tool())"

clean: ## Remove generated data (db/, raw/, extracted/) — keeps profiles
	rm -rf db/ raw/ extracted/ logs/ *.pid
	@echo "Cleaned. Run 'make build' to rebuild."

register: ## Register MCP server in Claude Code settings
	@$(PYTHON) -c "\
import json, os; \
p = os.path.expanduser('~/.claude/settings.json'); \
s = json.load(open(p)) if os.path.exists(p) else {}; \
s.setdefault('mcpServers', {})['code-rag'] = { \
    'command': 'python3', \
    'args': ['$$(pwd)/mcp_server.py'], \
    'env': {'ACTIVE_PROFILE': '$(PROFILE)', 'CODE_RAG_HOME': '$$(pwd)'} \
}; \
json.dump(s, open(p, 'w'), indent=2); \
print('Registered code-rag MCP server for profile: $(PROFILE)')"

switch-model: ## Rebuild vectors with a different model: make switch-model MODEL=minilm
	@echo "Switching to model: $(MODEL) for profile: $(PROFILE)"
	ACTIVE_PROFILE=$(PROFILE) $(PYTHON) $(SCRIPTS)/build_vectors.py --model=$(MODEL)
	@echo "Done. Restart daemon to use new vectors."

profile: ## Set active profile: make profile PROFILE=my-org
	@echo "$(PROFILE)" > .active_profile
	@echo "Active profile set to: $(PROFILE)"
