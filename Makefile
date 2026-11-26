.PHONY: test demo bench test-runtime
test:
	python3 -m pytest -q

demo:
	python3 -m scripts.demo

bench:
	python3 -m scripts.bench

# Temporal-based runtime (agent_runtime/); the older orchestrator/ tests run under `make test`.
test-runtime:
	python3 -m pytest -q tests/test_agent_runtime_tool_service.py
