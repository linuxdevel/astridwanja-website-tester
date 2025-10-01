PYTHON ?= python3
VENV ?= .venv
VENV_BIN := $(VENV)/bin
VENV_PYTHON := $(VENV_BIN)/python
BASE_URL ?= https://astridwanja.com
INTERNAL_DOMAINS ?=
JSON_REPORT ?= website-check-report.json
MARKDOWN_REPORT ?= website-check-report.md
SUMMARY_FILE ?= website-check-summary.txt

.PHONY: install check clean rankings install-browsers

$(VENV_PYTHON):
	@if ! $(PYTHON) -m venv $(VENV); then \
		echo "Default venv creation failed, attempting to bootstrap via virtualenv"; \
		rm -rf $(VENV); \
		$(PYTHON) -m pip install --user --break-system-packages --upgrade virtualenv; \
		$(PYTHON) -m virtualenv $(VENV); \
	fi
	$(VENV_PYTHON) -m pip install --upgrade pip

install: $(VENV_PYTHON)
	$(VENV_PYTHON) -m pip install -r requirements.txt

install-browsers: install
	$(VENV_PYTHON) -m playwright install chromium

check: install
	BASE_URL=$(BASE_URL) INTERNAL_DOMAINS=$(INTERNAL_DOMAINS) $(VENV_PYTHON) scripts/website_checker.py --json-output $(JSON_REPORT) --markdown-output $(MARKDOWN_REPORT)

clean:
	rm -f $(JSON_REPORT) $(MARKDOWN_REPORT) $(SUMMARY_FILE)
	find . -type d -name "__pycache__" -prune -exec rm -rf {} +
	rm -rf $(VENV)

rankings: install
	$(VENV_PYTHON) ranking/build_rankings.py
