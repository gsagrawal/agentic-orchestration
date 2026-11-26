.PHONY: test demo bench test-runtime test-integration demo-runtime bench-runtime
test:
	python3 -m pytest -q

demo:
	python3 -m scripts.demo

bench:
	python3 -m scripts.bench

# Temporal-based runtime (agent_runtime/); the older orchestrator/ tests run under `make test`.
test-runtime:  # unit tests: tool service + workflow in the Temporal test environment (time skipping)
	python3 -m pytest -q -m 'not integration' tests/test_agent_runtime_*.py

# Needs the Temporal dev server and real worker processes.
test-integration:
	python3 -m pytest -q -m integration tests/test_agent_runtime_*.py

# Needs the Temporal dev server, which the temporalio SDK downloads on first use.
demo-runtime:
	python3 -m scripts.runtime_demo

# Throughput and recovery measurements (writes markdown to stdout; takes several minutes).
bench-runtime:
	python3 -m scripts.runtime_bench
