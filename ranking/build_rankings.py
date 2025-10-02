#!/usr/bin/env python3
"""Generate a GitHub Pages-ready ranking summary for Astrid Wanja Brune Olsen.

This script signs into the public ITF player profile using Playwright, collects
current singles and doubles rankings, year-end ranking history, and yearly
win/loss records (where available). It renders a lightweight static HTML page
under ``docs/rankings/index.html`` and stores the raw data alongside it as
``docs/rankings/data.json``.

The Playwright browser binaries (Chromium) must be installed before running the
script:

    python -m playwright install chromium

"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any, Dict, List, Mapping, MutableMapping, Optional

from playwright.async_api import BrowserContext, Page, Playwright, async_playwright
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

PLAYER_ID = "800375000"
PLAYER_NAME = "Astrid Wanja Brune Olsen"
CIRCUIT_CODE = "WT"
PROFILE_URL_TEMPLATE = (
    "https://www.itftennis.com/en/players/astrid-wanja-brune-olsen/"
    f"{PLAYER_ID}/nor/wt/{{segment}}/overview/"
)
MATCH_TYPES: Mapping[str, Mapping[str, str]] = {
    "S": {"label": "Singles", "segment": "s"},
    "D": {"label": "Doubles", "segment": "d"},
}
WAIT_AFTER_LOAD_SECONDS = 12
REQUEST_PAUSE_SECONDS = 0.75
OUTPUT_DIR = Path(__file__).resolve().parent.parent / "docs" / "rankings"
OUTPUT_HTML = OUTPUT_DIR / "index.html"
OUTPUT_JSON = OUTPUT_DIR / "data.json"
OUTPUT_WTA_HTML = OUTPUT_DIR / "wta.html"


class FetchError(RuntimeError):
    """Raised when an ITF API endpoint returns an unexpected payload."""


async def fetch_json(page: Page, url: str) -> Any:
    """Fetch ``url`` via the page context and parse JSON, raising if blocked."""

    payload = await page.evaluate(
        """
        async (targetUrl) => {
            const response = await fetch(targetUrl, { credentials: 'include' });
            const text = await response.text();
            return { status: response.status, text };
        }
        """,
        url,
    )
    status = payload.get("status")
    text = payload.get("text", "")
    if status != 200:
        raise FetchError(f"{url} returned HTTP {status}")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:  # pragma: no cover - defensive guard
        excerpt = text[:200].replace("\n", " ")
        raise FetchError(f"Non-JSON response from {url}: {excerpt}") from exc


@dataclass
class WinLossSummary:
    year: int
    wins: int
    losses: int
    win_rate: Optional[int]
    surfaces: List[str]


@dataclass
class MatchTypeData:
    label: str
    overview: Mapping[str, Any]
    year_end_rankings: Mapping[str, Any]
    win_loss_summaries: List[WinLossSummary]
    win_loss_errors: List[str]

    @property
    def current_rankings(self) -> List[Mapping[str, Any]]:
        return list(self.overview.get("rankings", []))

    @property
    def career_highs(self) -> List[Mapping[str, Any]]:
        return list(self.overview.get("careerHighRankings", []))


async def collect_match_type_data(
    context: BrowserContext, match_type: str, *, label: str, segment: str
) -> MatchTypeData:
    page = await context.new_page()
    url = PROFILE_URL_TEMPLATE.format(segment=segment)
    try:
        await page.goto(url, wait_until="load", timeout=120_000)
    except PlaywrightTimeoutError as exc:
        await page.close()
        raise RuntimeError(f"Timed out loading {label.lower()} profile page") from exc

    try:
        await page.wait_for_load_state("networkidle", timeout=60_000)
    except PlaywrightTimeoutError:
        # The ITF site keeps some analytics requests open; fall back to a fixed delay.
        pass

    await page.wait_for_timeout(WAIT_AFTER_LOAD_SECONDS * 1_000)

    overview_url = (
        "https://www.itftennis.com/tennis/api/PlayerApi/GetPlayerOverview"
        f"?circuitCode={CIRCUIT_CODE}&matchTypeCode={match_type}&playerId={PLAYER_ID}"
    )
    year_end_url = (
        "https://www.itftennis.com/tennis/api/PlayerRankApi/GetYearEndRankings"
        f"?circuitCode={CIRCUIT_CODE}&matchTypeCode={match_type}&playerId={PLAYER_ID}"
    )

    overview = await fetch_json(page, overview_url)
    await asyncio.sleep(REQUEST_PAUSE_SECONDS)
    year_end = await fetch_json(page, year_end_url)

    years = sorted({int(y) for y in overview.get("years", [])}, reverse=True)
    win_loss_summaries: List[WinLossSummary] = []
    win_loss_errors: List[str] = []

    for year in years:
        win_loss_url = (
            "https://www.itftennis.com/tennis/api/PlayerApi/GetPlayerWinLoss"
            f"?circuitCode={CIRCUIT_CODE}&matchTypeCode={match_type}"
            f"&playerId={PLAYER_ID}&year={year}"
        )
        try:
            data = await fetch_json(page, win_loss_url)
        except FetchError as exc:
            win_loss_errors.append(f"{year}: {exc}")
        else:
            overall = data.get("overall") or {}
            surfaces = []
            for surface in data.get("surfaces", []):
                name = surface.get("name") or "Surface"
                wins = surface.get("wins", 0)
                losses = surface.get("losses", 0)
                rate = surface.get("winRate")
                surface_text = f"{name}: {wins}-{losses}"
                if rate is not None:
                    surface_text += f" ({rate}%)"
                surfaces.append(surface_text)
            win_loss_summaries.append(
                WinLossSummary(
                    year=year,
                    wins=overall.get("wins", 0),
                    losses=overall.get("losses", 0),
                    win_rate=overall.get("winRate"),
                    surfaces=surfaces,
                )
            )
        await asyncio.sleep(REQUEST_PAUSE_SECONDS)

    await page.close()

    win_loss_summaries.sort(key=lambda item: item.year, reverse=True)
    return MatchTypeData(
        label=label,
        overview=overview,
        year_end_rankings=year_end,
        win_loss_summaries=win_loss_summaries,
        win_loss_errors=win_loss_errors,
    )


def render_year_end_table(year_end: Mapping[str, Any]) -> str:
    columns: List[str] = list(year_end.get("columnNames", []))
    rows: List[Mapping[str, Any]] = list(year_end.get("yearRankings", []))
    if not columns or not rows:
        return "<p>No year-end ranking data available.</p>"

    header_cells = "".join(f"<th>{escape(col)}</th>" for col in columns)
    body_rows: List[str] = []
    for row in sorted(rows, key=lambda item: item.get("year", 0), reverse=True):
        year = row.get("year", "—")
        rankings = row.get("rankings", [])
        cells = [f"<td>{escape(str(year))}</td>"]
        for value in rankings:
            cell_value = "—" if value in (None, "") else str(value)
            cells.append(f"<td>{escape(cell_value)}</td>")
        body_rows.append(f"<tr>{''.join(cells)}</tr>")

    return (
        "<table class=\"data-table\">"
        f"<thead><tr>{header_cells}</tr></thead>"
        f"<tbody>{''.join(body_rows)}</tbody></table>"
    )


def render_win_loss_table(summaries: List[WinLossSummary]) -> str:
    if not summaries:
        return "<p>No win-loss records were returned for this discipline.</p>"

    rows: List[str] = []
    for summary in summaries:
        win_rate = f"{summary.win_rate}%" if summary.win_rate is not None else "—"
        surfaces = ", ".join(summary.surfaces) if summary.surfaces else "—"
        rows.append(
            "<tr>"
            f"<td>{summary.year}</td>"
            f"<td>{summary.wins}</td>"
            f"<td>{summary.losses}</td>"
            f"<td>{win_rate}</td>"
            f"<td>{escape(surfaces)}</td>"
            "</tr>"
        )

    header = (
        "<thead><tr><th>Year</th><th>Wins</th><th>Losses</th>"
        "<th>Win rate</th><th>Surface breakdown</th></tr></thead>"
    )
    return f"<table class=\"data-table\">{header}<tbody>{''.join(rows)}</tbody></table>"


def render_current_section(items: List[Mapping[str, Any]]) -> str:
    if not items:
        return "<p>No ranking information available.</p>"
    list_items = []
    for entry in items:
        name = escape(str(entry.get("name", "")))
        rank = entry.get("rank")
        date = escape(str(entry.get("date", "")))
        rank_display = "—" if rank in (None, "") else str(rank)
        list_items.append(f"<li><strong>{name}</strong>: {rank_display} <span class=\"date\">({date})</span></li>")
    return f"<ul class=\"rank-list\">{''.join(list_items)}</ul>"


def render_page(match_data: Mapping[str, MatchTypeData]) -> str:
    generated = datetime.now(timezone.utc).strftime("%d %B %Y %H:%M UTC")
    sections: List[str] = []

    for match_type in ("S", "D"):
        data = match_data.get(match_type)
        if not data:
            continue
        section_parts = [f"<section><h2>{escape(data.label)}</h2>"]
        section_parts.append("<h3>Current rankings</h3>")
        section_parts.append(render_current_section(data.current_rankings))
        section_parts.append("<h3>Career-best rankings</h3>")
        section_parts.append(render_current_section(data.career_highs))
        section_parts.append("<h3>Year-end rankings</h3>")
        section_parts.append(render_year_end_table(data.year_end_rankings))
        section_parts.append("<h3>Yearly win-loss record</h3>")
        section_parts.append(render_win_loss_table(data.win_loss_summaries))
        if data.win_loss_errors:
            errors = "".join(f"<li>{escape(err)}</li>" for err in data.win_loss_errors)
            section_parts.append(
                "<details><summary>Win-loss fetch notes</summary>"
                f"<ul>{errors}</ul></details>"
            )
        section_parts.append("</section>")
        sections.append("".join(section_parts))

    sections_html = "".join(sections)
    return f"""<!DOCTYPE html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>{escape(PLAYER_NAME)} — ITF ranking snapshot</title>
  <style>
    :root {{
      color-scheme: light dark;
      --max-width: 960px;
      --bg: #f9fafb;
      --fg: #111827;
      --accent: #2563eb;
      font-family: "Inter", "Segoe UI", system-ui, sans-serif;
    }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--fg);
    }}
    main {{
      margin: 0 auto;
      padding: 2.5rem 1.25rem 3rem;
      max-width: var(--max-width);
      line-height: 1.6;
    }}
    h1 {{
      margin-top: 0;
      font-size: clamp(2rem, 3vw, 2.75rem);
    }}
    h2 {{
      border-bottom: 2px solid rgba(37, 99, 235, 0.25);
      padding-bottom: 0.35rem;
      margin-top: 2.5rem;
    }}
    h3 {{
      margin-top: 1.75rem;
      font-size: 1.15rem;
    }}
    .data-table {{
      width: 100%;
      border-collapse: collapse;
      margin-top: 0.75rem;
      font-size: 0.95rem;
      background: white;
      border: 1px solid rgba(15, 23, 42, 0.12);
      border-radius: 8px;
      overflow: hidden;
    }}
    .data-table thead {{
      background: rgba(37, 99, 235, 0.08);
    }}
    .data-table th,
    .data-table td {{
      padding: 0.55rem 0.75rem;
      text-align: left;
      border-bottom: 1px solid rgba(15, 23, 42, 0.08);
    }}
    .data-table tbody tr:last-child td {{
      border-bottom: none;
    }}
    .rank-list {{
      list-style: none;
      padding-left: 0;
      display: grid;
      gap: 0.35rem;
    }}
    .rank-list li {{
      background: white;
      border-radius: 8px;
      padding: 0.65rem 0.75rem;
      border: 1px solid rgba(15, 23, 42, 0.1);
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      flex-wrap: wrap;
      gap: 0.35rem;
    }}
    .rank-list .date {{
      color: rgba(15, 23, 42, 0.6);
      font-size: 0.85rem;
    }}
    details {{
      margin-top: 1rem;
      background: rgba(37, 99, 235, 0.08);
      padding: 0.75rem 1rem;
      border-radius: 8px;
    }}
    footer {{
      margin-top: 3rem;
      font-size: 0.9rem;
      color: rgba(15, 23, 42, 0.6);
    }}
    @media (prefers-color-scheme: dark) {{
      :root {{
        --bg: #0b1120;
        --fg: #e2e8f0;
      }}
      .rank-list li,
      .data-table,
      details {{
        background: rgba(15, 23, 42, 0.35);
        border-color: rgba(148, 163, 184, 0.25);
      }}
      .rank-list .date,
      .data-table thead,
      footer {{
        color: rgba(148, 163, 184, 0.8);
      }}
    }}
  </style>
</head>
<body>
  <main>
    <h1>{escape(PLAYER_NAME)} — ITF ranking snapshot</h1>
    <p class=\"intro\">Snapshot of publicly available ITF ranking and performance data. Created on {escape(generated)}.</p>
    {sections_html}
    <footer>
      Data collected from the <a href=\"https://www.itftennis.com/\">International Tennis Federation</a> player profile. Last updated {escape(generated)}.
    </footer>
  </main>
</body>
</html>"""


def render_wta_page(match_data: Mapping[str, MatchTypeData]) -> str:
    """Render a simplified WTA rankings page matching astridwanja.com style."""
    generated = datetime.now(timezone.utc).strftime("%d %B %Y")
    
    # Extract WTA rankings
    singles_wta_rank = None
    singles_wta_date = None
    doubles_wta_rank = None
    doubles_wta_date = None
    
    singles_data = match_data.get("S")
    if singles_data:
        for ranking in singles_data.current_rankings:
            if "WTA" in ranking.get("name", ""):
                singles_wta_rank = ranking.get("rank")
                singles_wta_date = ranking.get("date")
                break
    
    doubles_data = match_data.get("D")
    if doubles_data:
        for ranking in doubles_data.current_rankings:
            if "WTA" in ranking.get("name", ""):
                doubles_wta_rank = ranking.get("rank")
                doubles_wta_date = ranking.get("date")
                break
    
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{escape(PLAYER_NAME)} — WTA Rankings</title>
  <style>
    * {{
      margin: 0;
      padding: 0;
      box-sizing: border-box;
    }}
    
    body {{
      font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif;
      background-color: #ffffff;
      color: #1a1a1a;
      line-height: 1.6;
      -webkit-font-smoothing: antialiased;
      padding: 40px 20px;
    }}
    
    .rankings-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
      gap: 30px;
      max-width: 800px;
      margin: 0 auto;
    }}
    
    .ranking-card {{
      background: #fafafa;
      border-radius: 8px;
      padding: 40px 30px;
      text-align: center;
      transition: transform 0.2s ease, box-shadow 0.2s ease;
      border: 1px solid #e5e5e5;
    }}
    
    .ranking-card:hover {{
      transform: translateY(-5px);
      box-shadow: 0 10px 30px rgba(0,0,0,0.08);
    }}
    
    .ranking-label {{
      font-size: 1.1rem;
      color: #666;
      margin-bottom: 15px;
      font-weight: 400;
      text-transform: uppercase;
      letter-spacing: 1px;
    }}
    
    .ranking-number {{
      font-size: 4rem;
      font-weight: 300;
      color: #1a1a1a;
      margin: 10px 0;
      line-height: 1;
    }}
    
    .ranking-date {{
      font-size: 0.9rem;
      color: #999;
      margin-top: 15px;
    }}
    
    @media (max-width: 768px) {{
      .ranking-number {{
        font-size: 3rem;
      }}
      
      .rankings-grid {{
        grid-template-columns: 1fr;
      }}
    }}
  </style>
</head>
<body>
  <div class="rankings-grid">
    <div class="ranking-card">
      <div class="ranking-label">WTA Singles Ranking</div>
      <div class="ranking-number">{singles_wta_rank if singles_wta_rank else "—"}</div>
      <div class="ranking-date">Updated: {escape(singles_wta_date) if singles_wta_date else "N/A"}</div>
    </div>
    
    <div class="ranking-card">
      <div class="ranking-label">WTA Doubles Ranking</div>
      <div class="ranking-number">{doubles_wta_rank if doubles_wta_rank else "—"}</div>
      <div class="ranking-date">Updated: {escape(doubles_wta_date) if doubles_wta_date else "N/A"}</div>
    </div>
  </div>
</body>
</html>"""


def serialize_for_json(match_data: Mapping[str, MatchTypeData]) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "player": {
            "id": PLAYER_ID,
            "name": PLAYER_NAME,
        },
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "match_types": {},
    }
    for code, data in match_data.items():
        payload["match_types"][code] = {
            "label": data.label,
            "overview": data.overview,
            "year_end_rankings": data.year_end_rankings,
            "win_loss": [
                {
                    "year": summary.year,
                    "wins": summary.wins,
                    "losses": summary.losses,
                    "win_rate": summary.win_rate,
                    "surfaces": summary.surfaces,
                }
                for summary in data.win_loss_summaries
            ],
            "win_loss_errors": data.win_loss_errors,
        }
    return payload


async def build_rankings() -> Mapping[str, MatchTypeData]:
    if not OUTPUT_DIR.exists():
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            locale="en-US",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 720},
        )
        try:
            results: Dict[str, MatchTypeData] = {}
            for code, config in MATCH_TYPES.items():
                data = await collect_match_type_data(
                    context,
                    code,
                    label=config["label"],
                    segment=config["segment"],
                )
                results[code] = data
            return results
        finally:
            await context.close()
            await browser.close()


def save_outputs(match_data: Mapping[str, MatchTypeData]) -> None:
    html = render_page(match_data)
    OUTPUT_HTML.write_text(html, encoding="utf-8")
    
    wta_html = render_wta_page(match_data)
    OUTPUT_WTA_HTML.write_text(wta_html, encoding="utf-8")
    
    json_payload = serialize_for_json(match_data)
    OUTPUT_JSON.write_text(json.dumps(json_payload, indent=2), encoding="utf-8")


def main() -> None:
    match_data = asyncio.run(build_rankings())
    save_outputs(match_data)
    print(f"Wrote {OUTPUT_HTML.relative_to(Path.cwd())}")
    print(f"Wrote {OUTPUT_WTA_HTML.relative_to(Path.cwd())}")
    print(f"Wrote {OUTPUT_JSON.relative_to(Path.cwd())}")


if __name__ == "__main__":
    main()
