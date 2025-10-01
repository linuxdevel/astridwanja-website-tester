# Astrid Wanja Website Tester

Automated crawler that verifies that https://astridwanja.com loads successfully, every page and link resolves, and referenced images are valid. A GitHub Actions workflow runs the checker on a schedule and sends an email alert if any issues are detected.

## Project layout

- `scripts/website_checker.py` – crawler script that performs all checks and writes JSON/Markdown reports. Internal pages are crawled recursively, while every HTTP(S) link—internal or external—is requested to ensure it succeeds; LinkedIn responses with HTTP 999 are treated as informational warnings (only surfaced when other link errors exist).
- `requirements.txt` – Python dependencies for the crawler.
- `.github/workflows/website-check.yml` – scheduled GitHub Actions workflow that runs the crawler and delivers notifications.

## Makefile shortcuts

```bash
make install            # Install Python dependencies using PYTHON=<path> (defaults to python3)
make check              # Run the website checker (override BASE_URL/INTERNAL_DOMAINS/JSON_REPORT/MARKDOWN_REPORT as needed)
make clean              # Remove generated reports and __pycache__ folders
```

All targets are phony, so you can safely rerun them. Example with a custom Python interpreter:

```bash
make install PYTHON=python3.12
```

## Running the checker locally

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
BASE_URL=https://astridwanja.com python3 scripts/website_checker.py
```

Reports (`website-check-report.json` and `website-check-report.md`) will be written to the repository root.

If you prefer not to manage a virtual environment manually, the `Makefile` offers shortcuts:

```bash
make install
make check
```

The `install` target automatically creates (or recreates) `.venv/` if it is missing—falling back to `virtualenv` if the built-in `venv` module is unavailable—while `make clean` removes the reports, cached bytecode, and the virtual environment so the repository stays tidy.

## GitHub Actions workflow

The workflow runs automatically once every 24 hours (03:00 UTC) and can also be triggered manually from the **Actions** tab via the `Run workflow` button.

### Required repository secrets

Set these secrets in the repository settings to enable email notifications:

- `SMTP_SERVER_ADDRESS` – SMTP server hostname
- `SMTP_SERVER_PORT` – SMTP server port (as a number, e.g. `587`)
- `SMTP_USERNAME` – SMTP account username
- `SMTP_PASSWORD` – SMTP account password or app-specific password
- `NOTIFY_EMAIL_FROM` – email address that appears in the FROM field
- `NOTIFY_EMAIL_TO` – comma-separated list of recipient email addresses

Without the secrets the workflow still runs, but the notification step will be skipped automatically by GitHub Actions.

### Optional configuration

- Set `INTERNAL_DOMAINS` in the workflow or via environment variables if additional domains should be considered part of the crawl (e.g., language-specific subdomains). These values only affect which pages are queued for crawling; all links are still requested and only failing ones are reported as errors. When LinkedIn responds with HTTP 999 (its bot protection), the checker records a warning advising manual verification instead of failing the run.
- Adjust the cron expression in `.github/workflows/website-check.yml` to change the schedule.

### Artifacts and reporting

Every run uploads the JSON and Markdown reports as workflow artifacts and adds a short summary to the run log. When issues are detected, the Markdown report is attached to the notification email for quick review.
