PYTHON ?= python3
CONFIG ?= configs/app.example.yaml

.PHONY: test validate-registry mock-run api

test:
	$(PYTHON) -m unittest discover -s tests -v

validate-registry:
	$(PYTHON) -m open_sprite_pipeline.cli validate-registry --config $(CONFIG)

mock-run:
	$(PYTHON) -m open_sprite_pipeline.cli run \
		--config $(CONFIG) \
		--image tests/data/mock_input.png \
		--prompt "toy robot." \
		--mode hero \
		--mock

api:
	$(PYTHON) -m uvicorn open_sprite_pipeline.api:app --reload
