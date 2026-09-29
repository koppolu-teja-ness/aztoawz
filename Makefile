.PHONY: install lint format typecheck test validate-cfn validate-kb scan run-graph run-api run-ui

install:
	python -m pip install --upgrade pip
	python -m pip install -e .[dev]

lint:
	ruff check .
	black --check .

format:
	black .
	ruff check . --fix

typecheck:
	mypy src tests

test:
	pytest -q

validate-cfn:
	cfn-lint infra/cfn/*.yaml infra/cfn/*.yml infra/cfn/*.cfn.yaml

validate-kb:
	python scripts/validate_kb.py

scan:
	checkov -d infra/cfn
	$(MAKE) validate-kb

run-graph:
	@echo "Graph runner scaffold only. Implement orchestration entrypoint under src/graph/."

run-api:
	@echo "API scaffold only. Implement FastAPI app under src/api/."

run-ui:
	@echo "UI scaffold only. Implement UI app under src/ui/."
