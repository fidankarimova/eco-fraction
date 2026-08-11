.PHONY: install run test seed reset clean

install:
	python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt

run:
	./.venv/bin/uvicorn backend.main:app --reload --port 8000

test:
	./.venv/bin/python -m pytest -q

seed:
	./.venv/bin/python -m scripts.seed_history --days 7 --step-minutes 5

reset:
	./.venv/bin/python -m scripts.seed_history --reset --days 7 --step-minutes 5

clean:
	rm -rf data/*.db data/*.db-wal data/*.db-shm .pytest_cache __pycache__
