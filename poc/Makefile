# One command to run the demo, one to run the tests.
# Host needs: Docker (Desktop on a Mac), Python 3.10+, and `pip install pytest`.
# Optional: BUILD_CA_BUNDLE=/path/ca.pem if your network intercepts TLS during `docker build`.
.PHONY: image demo test bench down

image:
	image/build.sh

demo: image
	python3 demo.py

test: image
	python3 -m pytest

bench: image
	python3 bench/coldstart.py

down:
	python3 stack.py down
