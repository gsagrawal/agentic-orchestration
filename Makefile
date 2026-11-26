.PHONY: test demo bench test-runtime test-integration demo-runtime
test:
	python3 -m pytest -q

demo:
	python3 -m scripts.demo

bench:
	python3 -m scripts.bench

# Temporal-based runtime (agent_runtime/); the older orchestrator/ tests run under `make test`.
test-runtime:  # unit tests: tool service + workflow in the Temporal test environment (time skipping)
	python3 -m pytest -q tests/test_agent_runtime_tool_service.py tests/test_agent_runtime_workflow.py

# Needs the Temporal dev server and real worker processes.
test-integration:
	python3 -m pytest -q -m integration

# Needs the Temporal dev server, which the temporalio SDK downloads on first use.
demo-runtime:
	python3 -m scripts.runtime_demo
