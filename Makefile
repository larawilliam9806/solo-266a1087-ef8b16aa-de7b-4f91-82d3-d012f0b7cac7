.PHONY: install run test docker docker-down clean

PYTHON ?= python3

install:
	$(PYTHON) -m pip install -r requirements.txt
	$(PYTHON) -m pip install pytest

run:
	DATABASE_PATH=$${DATABASE_PATH:-instance/tide.db} $(PYTHON) -m app

test:
	$(PYTHON) -m pytest -q

docker:
	docker compose up --build

docker-down:
	docker compose down

clean:
	rm -rf .pytest_cache
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
