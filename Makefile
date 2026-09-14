.PHONY: contracts contracts-check hooks

CONTRACTS_REPO ?= https://github.com/GuilhermeFortuna/q_contracts.git

contracts:
	contracts_tmp="$$(mktemp -d)"; \
	trap 'rm -rf "$$contracts_tmp"' EXIT; \
	git clone --quiet "$(CONTRACTS_REPO)" "$$contracts_tmp/q_contracts"; \
	git -C "$$contracts_tmp/q_contracts" checkout --quiet "$$(cat CONTRACTS_REV)"; \
	rm -rf contracts; \
	mkdir -p contracts; \
	cp -R "$$contracts_tmp/q_contracts/generated/python/q_contracts/." contracts/; \
	mkdir -p contracts/schema/api/arrow; \
	cp "$$contracts_tmp/q_contracts/schema/api/arrow/"*.schema.json contracts/schema/api/arrow/; \
	mkdir -p contracts/schema/catalog; \
	cp "$$contracts_tmp/q_contracts/schema/catalog/dataset-manifest.schema.json" contracts/schema/catalog/

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
	mkdir -p "$$generated_tmp/python/q_contracts/schema/catalog"; \
	cp "$$contracts_tmp/q_contracts/schema/catalog/dataset-manifest.schema.json" "$$generated_tmp/python/q_contracts/schema/catalog/"; \
	diff -ru --exclude='__pycache__' --exclude='*.pyc' --exclude='schema' contracts "$$generated_tmp/python/q_contracts"; \
	python3 -c 'import json, pathlib, sys; left=pathlib.Path("contracts/schema/api/arrow"); right=pathlib.Path("'"$$generated_tmp"'/python/q_contracts/schema/api/arrow"); names=sorted(p.name for p in left.glob("*.schema.json")); sys.exit(names != sorted(p.name for p in right.glob("*.schema.json")) or any(json.loads((left / n).read_text()) != json.loads((right / n).read_text()) for n in names))'; \
	python3 -c 'import json, pathlib, sys; left=pathlib.Path("contracts/schema/catalog/dataset-manifest.schema.json"); right=pathlib.Path("'"$$generated_tmp"'/python/q_contracts/schema/catalog/dataset-manifest.schema.json"); sys.exit(not left.exists() or not right.exists() or json.loads(left.read_text()) != json.loads(right.read_text()))'

hooks:
	git config core.hooksPath .githooks
	@echo "Git hooks enabled from .githooks"
