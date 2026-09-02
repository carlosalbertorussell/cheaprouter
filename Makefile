.PHONY: install dev test run run-stdio compile clean

install:
	pip install -r requirements.txt

dev:
	pip install -e ".[dev]"

test:
	python -m pytest -v

compile:
	python -m py_compile server.py providers.py pricing.py router.py client.py history.py
	@echo "All modules compile cleanly"

run:
	python server.py

run-stdio:
	python server.py --stdio

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
	rm -f /tmp/routing_history.jsonl
