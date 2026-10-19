PY := .venv/bin/python
UV := $(HOME)/.local/bin/uv

.PHONY: setup index chat test api widget widget-dev check-token eval-rag eval

setup:            ## create .venv on Python 3.12 and install pinned deps
	$(UV) venv --python 3.12 .venv
	$(UV) pip install --python $(PY) -r requirements.txt

index:            ## rebuild the menu/FAQ search index from MongoDB
	$(PY) -m app.ingestion.sync

chat:             ## terminal chat
	$(PY) chat.py --branch gulberg --debug

test:
	$(PY) -m pytest -q tests

api:              ## website API (+ built widget at /widget/) on http://localhost:8000
	.venv/bin/uvicorn app.api:app --reload --port 8000

widget:           ## build the embeddable widget into widget/dist
	cd widget && npm ci && npm run build

widget-dev:       ## widget dev server on http://localhost:5173 (run `make api` too)
	cd widget && npm run dev

check-token:      ## check a real app login token: make check-token TOKEN=eyJ...
	$(PY) scripts/check_token.py "$(TOKEN)"

eval-rag:         ## RAG + tool evaluation, no LLM (seconds-minutes): run on every change
	$(PY) -m eval.run_eval rag

eval:             ## end-to-end golden set through the agent; resumable, paced to Groq limits
	$(PY) -m eval.run_eval e2e --resume $(ARGS)
