.PHONY: test demo bench
test:
	python3 -m pytest -q

demo:
	python3 scripts/demo.py

bench:
	python3 scripts/bench.py
