.PHONY: test test-integration demo bench

# Unit tests: tool service, workflow and API in the Temporal test environment (time skipping).
test:
	python3 -m pytest -q -m 'not integration'

# Needs the Temporal dev server and real worker processes (kill -9, zombie, chaos). About 3 minutes.
test-integration:
	python3 -m pytest -q -m integration

# One session run to the end on a worker process. Needs the Temporal dev server.
demo:
	python3 -m scripts.demo

# Throughput and worker-death-to-resume measurements, printed as markdown. Takes several minutes.
bench:
	python3 -m scripts.bench
