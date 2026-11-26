.PHONY: test demo bench
test:
	python3 -m pytest -q

demo:
	python3 -m scripts.demo

bench:
	python3 -m scripts.bench
