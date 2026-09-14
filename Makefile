.PHONY: contracts contracts-check hooks

CONTRACTS_REPO ?= https://github.com/GuilhermeFortuna/q_contracts.git

contracts:
	contracts_tmp="$$(mktemp -d)"; \
	trap 'rm -rf "$$contracts_tmp"' EXIT; \
	git clone --quiet "$(CONTRACTS_REPO)" "$$contracts_tmp/q_contracts"; \
	git -C "$$contracts_tmp/q_contracts" checkout --quiet "$$(cat CONTRACTS_REV)"; \
	rm -rf contracts; \
	mkdir -p contracts; \
	cp -R "$$contracts_tmp/q_contracts/generated/python/q_contracts/." contracts/
	mkdir -p contracts/schema/api/arrow; \
	cp "$$contracts_tmp/q_contracts/schema/api/arrow/"*.schema.json contracts/schema/api/arrow/

contracts-check:
	contracts_tmp="$$(mktemp -d)"; \
	trap 'rm -rf "$$contracts_tmp"' EXIT; \
	git clone --quiet "$(CONTRACTS_REPO)" "$$contracts_tmp/q_contracts"; \
	git -C "$$contracts_tmp/q_contracts" checkout --quiet "$$(cat CONTRACTS_REV)"; \
	generated_tmp="$$contracts_tmp/generated"; \
	if python3 -c 'import yaml' 2>/dev/null; then \
		python3 "$$contracts_tmp/q_contracts/tools/generate.py" --language python --out "$$generated_tmp"; \
	else \
		uv run --project "$$contracts_tmp/q_contracts" python "$$contracts_tmp/q_contracts/tools/generate.py" \
			--language python --out "$$generated_tmp"; \
	fi; \
	mkdir -p "$$generated_tmp/python/q_contracts/schema/api/arrow"; \
	cp "$$contracts_tmp/q_contracts/schema/api/arrow/"*.schema.json "$$generated_tmp/python/q_contracts/schema/api/arrow/"; \
	diff -ru --exclude='__pycache__' --exclude='*.pyc' --exclude='schema' contracts "$$generated_tmp/python/q_contracts"; \
	python3 -c 'import json, pathlib, sys; left=pathlib.Path("contracts/schema/api/arrow"); right=pathlib.Path("'"$$generated_tmp"'/python/q_contracts/schema/api/arrow"); sys.exit(any(json.loads(p.read_text()) != json.loads((right / p.name).read_text()) for p in left.glob("*.schema.json")))'

hooks:
	git config core.hooksPath .githooks
	@echo "Git hooks enabled from .githooks"
