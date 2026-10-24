#!/usr/bin/env python3
"""
Bid News Agent
==============
A scheduled Python agent that:
  1. Fetches a configurable news/tender listing page.
  2. Extracts candidate bid items (title + URL).
  3. Asks GLM (via `z-ai` CLI) to judge relevance, categorize,
     and produce a 2-line summary.
  4. Sends rich Telegram notifications (with inline "View original"
     button) for each newly-discovered relevant bid.
  5. Persists every sent item in SQLite to prevent duplicate alerts.

Usage:
    python bid_agent.py            # Run one cycle, then exit.
    python bid_agent.py --daemon   # Run forever on a fixed interval.
    python bid_agent.py --dry-run  # Fetch + filter, but do NOT send
                                    # Telegram messages. Good for testing.
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable
from urllib.parse import urljoin, urlparse

import requests
import yaml
from bs4 import BeautifulSoup
from dotenv import load_dotenv


# ---------------------------------------------------------------------------
# Constants & paths
# ---------------------------------------------------------------------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.yaml")
ENV_PATH = os.path.join(SCRIPT_DIR, ".env")


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------
@dataclass
class BidItem:
    """A single candidate item pulled from the listing page."""
    site_name: str
    source_url: str          # Absolute URL of the bid detail page
    title: str               # Best-effort title text
    raw_html: str = ""       # Optional HTML snippet for richer LLM context
    # Optional structured metadata (filled when the source exposes JSON)
    item_id: str = ""        # Site-native tender ID (used as dedupe key alongside URL)
    company_name: str = ""   # Issuing organization
    deadline: str = ""       # Bid closing date (text or ISO)
    deadline_iso: str = ""   # Raw ISO datetime if available
    opening_date: str = ""   # Bid opening date (text or ISO)
    published_at: str = ""   # Publish date if known
    region: str = ""         # Region if known
    source_name: str = ""     # Publication source (e.g. "Addis Zemen")
    bid_doc_price: str = ""  # Cost to obtain tender documents
    bid_bond: str = ""       # Required bid bond / security
    category_hint: str = ""  # Source-side category hint (LLM may override)
    found_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )


@dataclass
class LLMJudgment:
    """Structured response returned by the GLM filter."""
    is_relevant: bool
    relevance_score: int
    category: str
    deadline: str
    budget: str
    summary: str
    raw: str = ""


class RateLimitError(Exception):
    """Raised when the z-ai API returns HTTP 429."""


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def setup_logging(log_path: str, level: str = "INFO") -> logging.Logger:
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    logger = logging.getLogger("bid_agent")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    # Prevent duplicate handlers when reloaded
    if logger.handlers:
        return logger
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------
def load_config() -> dict[str, Any]:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Login authenticator
# ---------------------------------------------------------------------------
class LoginAuthenticator:
    """Performs a Laravel/Inertia-style form login and returns an
    authenticated `requests.Session` that can be reused for listing
    fetches. Reads credentials from environment variables.
    """

    def __init__(
        self,
        login_cfg: dict[str, Any],
        user_agent: str,
        logger: logging.Logger,
        email_env_var: str = "SITE_LOGIN_EMAIL",
        password_env_var: str = "SITE_LOGIN_PASSWORD",
    ) -> None:
        self.cfg = login_cfg
        self.user_agent = user_agent
        self.logger = logger
        self.email_env_var = email_env_var
        self.password_env_var = password_env_var

    def login(self) -> requests.Session | None:
        email = os.environ.get(self.email_env_var, "").strip()
        password = os.environ.get(self.password_env_var, "").strip()
        if not email or not password:
            self.logger.warning(
                "Login skipped — %s / %s not set in environment.",
                self.email_env_var, self.password_env_var,
            )
            return None

        url = self.cfg.get("url", "")
        action_url = self.cfg.get("action_url", url)
        email_field = self.cfg.get("email_field", "email")
        password_field = self.cfg.get("password_field", "password")
        csrf_field = self.cfg.get("csrf_field", "_token")

        session = requests.Session()
        session.headers.update({
            "User-Agent": self.user_agent,
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })

        # Step 1: GET the login page to obtain cookies + CSRF token
        try:
            self.logger.info("Login step 1: GET %s", url)
            resp = session.get(url, timeout=30)
            resp.raise_for_status()
        except Exception as exc:
            self.logger.error("Login GET failed: %s", exc)
            return None

        csrf_token = self._extract_csrf(resp.text)
        if not csrf_token:
            self.logger.error(
                "Could not extract CSRF token from login page. Aborting login."
            )
            return None
        self.logger.info("Login step 1 OK — CSRF token: %s…", csrf_token[:12])

        # Step 2: POST credentials
        body = {
            email_field: email,
            password_field: password,
            csrf_field: csrf_token,
        }
        # Detect any extra static fields the site may require
        for k, v in (self.cfg.get("extra_fields") or {}).items():
            body[k] = v

        try:
            self.logger.info("Login step 2: POST %s", action_url)
            post_resp = session.post(
                action_url,
                data=body,
                timeout=30,
                allow_redirects=True,
                headers={"Referer": url},
            )
        except Exception as exc:
            self.logger.error("Login POST failed: %s", exc)
            return None

        # Laravel returns 302 redirect on success; 422 on validation error;
        # 200 with the login form re-rendered (with `errors` populated) on
        # failed credentials.
        if post_resp.status_code in (200, 302) and "/login" not in post_resp.url:
            self.logger.info(
                "Login OK (final URL: %s, status=%d)",
                post_resp.url, post_resp.status_code,
            )
            return session
        # Fallback heuristic: did the response set an `auth.user` cookie?
        # Inspect the resulting page props for an authenticated user.
        if self._looks_authenticated(post_resp.text):
            self.logger.info("Login OK (auth.user present in subsequent page)")
            return session
        self.logger.error(
            "Login likely FAILED — final URL: %s, status=%d. "
            "Check credentials.",
            post_resp.url, post_resp.status_code,
        )
        return None

    def _extract_csrf(self, html_src: str) -> str | None:
        """Pull the CSRF token out of a Laravel/Inertia page."""
        # Inertia.js: token is in the `data-page` JSON on <div id="app">
        m = re.search(
            r'<div id="app" data-page="([^"]+)"',
            html_src, re.DOTALL,
        )
        if m:
            try:
                page_data = json.loads(html.unescape(m.group(1)))
                token = (page_data.get("props") or {}).get("csrf_token")
                if token:
                    return token
            except json.JSONDecodeError:
                pass
        # Standard Laravel: <meta name="csrf-token" content="...">
        m = re.search(
            r'<meta[^>]+name=["\']csrf-token["\'][^>]+content=["\']([^"\']+)',
            html_src,
        )
        if m:
            return m.group(1)
        # Fallback: hidden form input
        m = re.search(
            r'<input[^>]+name=["\']_token["\'][^>]+value=["\']([^"\']+)',
            html_src,
        )
        if m:
            return m.group(1)
        return None

    @staticmethod
    def _looks_authenticated(html_src: str) -> bool:
        """Heuristic: an Inertia.js page that reports `auth.user` populated."""
        m = re.search(
            r'<div id="app" data-page="([^"]+)"',
            html_src, re.DOTALL,
        )
        if not m:
            return False
        try:
            page_data = json.loads(html.unescape(m.group(1)))
        except json.JSONDecodeError:
            return False
        auth = (page_data.get("props") or {}).get("auth") or {}
        user = auth.get("user")
        return bool(user)


# ---------------------------------------------------------------------------
# SQLite dedupe store
# ---------------------------------------------------------------------------
class SentStore:
    """SQLite-backed store of item URLs already sent to Telegram."""

    def __init__(self, db_path: str, logger: logging.Logger) -> None:
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sent_items (
                url         TEXT PRIMARY KEY,
                title       TEXT NOT NULL,
                category    TEXT,
                sent_at     TEXT NOT NULL,
                source_site TEXT
            )
            """
        )
        self.conn.commit()
        self.logger = logger

    def already_sent(self, url: str) -> bool:
        cur = self.conn.execute(
            "SELECT 1 FROM sent_items WHERE url = ?", (url,)
        )
        return cur.fetchone() is not None

    def mark_sent(
        self,
        url: str,
        title: str,
        category: str,
        source_site: str,
    ) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO sent_items (url, title, category, sent_at, source_site)
            VALUES (?, ?, ?, ?, ?)
            """,
            (url, title, category, datetime.now(timezone.utc).isoformat(), source_site),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()


# ---------------------------------------------------------------------------
# Scraper
# ---------------------------------------------------------------------------
class SiteScraper:
    """Fetches a listing page and extracts candidate bid items.

    Supports two parsing modes (selectable via site config):
      - `parser: html`        (default) — CSS selectors over rendered HTML.
      - `parser: inertia_json`           — extract JSON from
        `<div id="app" data-page="...">` (Laravel/Inertia.js SPAs).
    """

    def __init__(
        self,
        site_cfg: dict[str, Any],
        user_agent: str,
        logger: logging.Logger,
        session: requests.Session | None = None,
    ) -> None:
        self.cfg = site_cfg
        self.user_agent = user_agent
        self.logger = logger
        self.session = session  # If provided, use for cookie persistence
        self.name = site_cfg.get("name", site_cfg.get("url", "site"))
        self.url = site_cfg["url"]
        self.use_browser = bool(site_cfg.get("use_browser", False))
        self.parser_mode = (site_cfg.get("parser") or "html").lower()

    # ---- Public entry ----------------------------------------------------
    def fetch_items(self) -> list[BidItem]:
        """Return the list of candidate items on the listing page."""
        html = self._fetch_html()
        if not html:
            return []
        if self.parser_mode == "inertia_json":
            return self._parse_inertia_json(html)
        return self._parse_items(html)

    # ---- HTML acquisition -----------------------------------------------
    def _fetch_html(self) -> str:
        if self.use_browser:
            return self._fetch_via_browser()
        return self._fetch_via_requests()

    def _fetch_via_requests(self) -> str:
        try:
            headers = {
                "User-Agent": self.user_agent,
                "Accept-Language": "en-US,en;q=0.9",
            }
            if self.session:
                resp = self.session.get(self.url, headers=headers, timeout=30)
            else:
                resp = requests.get(self.url, headers=headers, timeout=30)
            resp.raise_for_status()
            return resp.text
        except Exception as exc:
            self.logger.error(
                "HTTP fetch failed for %s: %s", self.url, exc
            )
            return ""

    def _fetch_via_browser(self) -> str:
        """Use `agent-browser` CLI to render JS-heavy pages."""
        try:
            # open
            subprocess.run(
                ["agent-browser", "open", self.url],
                check=True, capture_output=True, timeout=60,
            )
            time.sleep(2)  # allow JS to populate
            # dump HTML via eval
            proc = subprocess.run(
                ["agent-browser", "eval", "document.documentElement.outerHTML"],
                capture_output=True, text=True, timeout=30,
            )
            if proc.returncode != 0:
                self.logger.error(
                    "agent-browser eval failed: %s", proc.stderr[:500]
                )
                return ""
            # The CLI prints JSON-ish output; strip wrapper if present.
            out = proc.stdout.strip()
            try:
                parsed = json.loads(out)
                if isinstance(parsed, dict) and "result" in parsed:
                    out = parsed["result"]
            except json.JSONDecodeError:
                pass
            return out
        except Exception as exc:
            self.logger.error("agent-browser fetch failed for %s: %s",
                              self.url, exc)
            return ""

    # ---- Inertia JSON parsing -------------------------------------------
    def _parse_inertia_json(self, html_src: str) -> list[BidItem]:
        """Parse a Laravel/Inertia SPA page that embeds tender data
        as JSON inside `<div id="app" data-page="...">`."""
        m = re.search(
            r'<div id="app" data-page="([^"]+)"',
            html_src, re.DOTALL,
        )
        if not m:
            self.logger.error(
                "[%s] inertia_json parser: no <div id='app' data-page='...'> "
                "found in HTML. Aborting.", self.name,
            )
            return []
        try:
            page_data = json.loads(html.unescape(m.group(1)))
        except json.JSONDecodeError as exc:
            self.logger.error(
                "[%s] inertia_json parser: failed to decode data-page JSON: %s",
                self.name, exc,
            )
            return []

        # Walk the dotted path (default: props.tenders.data)
        path = self.cfg.get("json_path", "props.tenders.data")
        node: Any = page_data
        for seg in path.split("."):
            if isinstance(node, dict) and seg in node:
                node = node[seg]
            elif isinstance(node, list) and seg.isdigit():
                node = node[int(seg)]
            else:
                self.logger.error(
                    "[%s] inertia_json parser: path '%s' not found in data-page "
                    "(stopped at segment '%s').", self.name, path, seg,
                )
                return []
        if not isinstance(node, list):
            self.logger.error(
                "[%s] inertia_json parser: path '%s' did not resolve to a list.",
                self.name, path,
            )
            return []

        id_field = self.cfg.get("id_field", "id")
        title_field = self.cfg.get("title_field", "title")
        detail_template = self.cfg.get(
            "detail_url_template", self.url.rstrip("/") + "/{id}"
        )
        company_field = self.cfg.get("company_field", "company")
        deadline_field = self.cfg.get("deadline_field", "bid_closing_date_text")
        deadline_iso_field = self.cfg.get("deadline_iso_field", "bid_closing_date")
        opening_field = self.cfg.get("opening_date_field", "bid_opening_date_text")
        opening_iso_field = self.cfg.get("opening_date_iso_field", "bid_opening_date")
        published_field = self.cfg.get("published_at_field", "published_at")
        region_field = self.cfg.get("region_field", "region")
        sources_field = self.cfg.get("sources_field", "sources")
        bid_doc_price_field = self.cfg.get("bid_document_price_field", "bid_document_price")
        bid_bond_field = self.cfg.get("bid_bond_field", "bid_bond")

        def _pick_text(raw_item: dict, text_field: str, iso_field: str = "") -> str:
            """Prefer the human-readable text field; fall back to the ISO
            datetime field if the text is empty/null/None."""
            val = raw_item.get(text_field)
            if val and isinstance(val, str) and val.strip():
                return val.strip()
            if iso_field:
                iso = raw_item.get(iso_field)
                if iso and isinstance(iso, str) and iso.strip():
                    return iso.strip()
            return ""

        items: list[BidItem] = []
        for raw_item in node:
            if not isinstance(raw_item, dict):
                continue
            item_id = str(raw_item.get(id_field, "")).strip()
            title = str(raw_item.get(title_field, "")).strip()
            if not item_id or not title:
                continue
            detail_url = detail_template.format(id=item_id)

            # Company may be a nested dict with `name_en` / `name_am`
            company = raw_item.get(company_field)
            company_name = ""
            if isinstance(company, dict):
                company_name = (
                    company.get("name_en")
                    or company.get("name_am")
                    or company.get("name")
                    or ""
                )
            elif isinstance(company, str):
                company_name = company

            # Region may also be a nested dict
            region = raw_item.get(region_field)
            region_name = ""
            if isinstance(region, dict):
                region_name = (
                    region.get("name_en") or region.get("name") or ""
                )
            elif isinstance(region, str):
                region_name = region

            # Sources is a list of {name_en, name_am, publication_date}
            # Pick the first source's name_en as the publication source.
            source_name = ""
            sources_list = raw_item.get(sources_field) or []
            if isinstance(sources_list, list) and sources_list:
                first = sources_list[0]
                if isinstance(first, dict):
                    source_name = (
                        first.get("name_en")
                        or first.get("name_am")
                        or first.get("name")
                        or ""
                    )

            bid_doc_price = str(raw_item.get(bid_doc_price_field) or "").strip()
            bid_bond = str(raw_item.get(bid_bond_field) or "").strip()
            deadline_text = _pick_text(raw_item, deadline_field, deadline_iso_field)
            opening_text = _pick_text(raw_item, opening_field, opening_iso_field)

            items.append(
                BidItem(
                    site_name=self.name,
                    source_url=detail_url,
                    title=title[:500],
                    raw_html=json.dumps(raw_item, ensure_ascii=False)[:2000],
                    item_id=item_id,
                    company_name=company_name,
                    deadline=deadline_text or "Not specified",
                    deadline_iso=str(raw_item.get(deadline_iso_field) or ""),
                    opening_date=opening_text or "Not specified",
                    published_at=str(raw_item.get(published_field) or ""),
                    region=region_name or "Not specified",
                    source_name=source_name or "Not specified",
                    bid_doc_price=bid_doc_price or "Not specified",
                    bid_bond=bid_bond or "Not specified",
                )
            )
        self.logger.info(
            "[%s] inertia_json parser: extracted %d items via path '%s'",
            self.name, len(items), path,
        )
        return items

    # ---- HTML parsing ---------------------------------------------------
    def _parse_items(self, html: str) -> list[BidItem]:
        soup = BeautifulSoup(html, "html.parser")
        base = self.url

        # Strategy A: RSS feed provided
        if self.cfg.get("rss"):
            return self._parse_rss_via_request(self.cfg["rss"], base)

        container_sel = self.cfg.get("container")
        item_sel = self.cfg.get("item_selector", "article, .bid-item, .tender, li")
        link_sel = self.cfg.get("link_selector", "a")
        title_sel = self.cfg.get("title_selector", "h2, h3, .title, a")

        # Scope to container if provided
        if container_sel:
            root = soup.select_one(container_sel) or soup.body or soup
        else:
            root = soup.body or soup

        nodes = root.select(item_sel) if root else []
        self.logger.info(
            "[%s] Found %d candidate nodes via selector '%s'",
            self.name, len(nodes), item_sel,
        )

        items: list[BidItem] = []
        seen_urls: set[str] = set()
        for node in nodes:
            link = node.select_one(link_sel)
            if not link or not link.get("href"):
                continue
            href = link.get("href").strip()
            abs_url = urljoin(base, href)
            # Ignore in-page anchors and the listing page itself
            if abs_url in seen_urls or abs_url.rstrip("/") == self.url.rstrip("/"):
                continue
            if not self._looks_like_detail_page(abs_url, node):
                continue
            seen_urls.add(abs_url)

            title_node = node.select_one(title_sel) or link
            title = self._clean_text(title_node.get_text(" "))
            if not title:
                continue

            items.append(
                BidItem(
                    site_name=self.name,
                    source_url=abs_url,
                    title=title,
                    raw_html=str(node)[:2000],
                )
            )
        return items

    def _parse_rss_via_request(self, rss_url: str, base: str) -> list[BidItem]:
        try:
            if self.session:
                resp = self.session.get(
                    rss_url,
                    headers={"User-Agent": self.user_agent},
                    timeout=30,
                )
            else:
                resp = requests.get(
                    rss_url,
                    headers={"User-Agent": self.user_agent},
                    timeout=30,
                )
            resp.raise_for_status()
        except Exception as exc:
            self.logger.error("RSS fetch failed for %s: %s", rss_url, exc)
            return []
        soup = BeautifulSoup(resp.content, "xml")
        items: list[BidItem] = []
        for entry in soup.find_all(["item", "entry"]):
            title_t = entry.find("title")
            link_t = entry.find("link")
            url = (link_t.text or "").strip() if link_t else ""
            if not url:
                continue
            url = urljoin(base, url)
            title = self._clean_text(title_t.text if title_t else "")
            items.append(
                BidItem(
                    site_name=self.name,
                    source_url=url,
                    title=title or "(no title)",
                )
            )
        self.logger.info("[%s] RSS yielded %d items", self.name, len(items))
        return items

    def _looks_like_detail_page(self, url: str, node: Any) -> bool:
        """Quick heuristic to drop nav/footer links."""
        path = urlparse(url).path.lower()
        # Heuristic: detail URLs tend to have a slug or numeric id
        if not path or path in ("/", "/home", "/news", "/bids"):
            return False
        if any(seg in path for seg in ("login", "signup", "register", "contact")):
            return False
        return True

    @staticmethod
    def _clean_text(s: str) -> str:
        s = re.sub(r"\s+", " ", s).strip()
        return s[:300]   # Cap length


# ---------------------------------------------------------------------------
# LLM filter
# ---------------------------------------------------------------------------
class LLMFilter:
    """Calls an LLM to judge each candidate bid item.

    Supports two backends (auto-selected by environment variable):
      - `GEMINI_API_KEY` set → use Google Gemini (free tier: 1,500
        requests/day on gemini-2.5-flash). Recommended for free
        24/7 hosting.
      - Otherwise → fall back to `z-ai chat` CLI (GLM-4).
    """

    # Override via env var GEMINI_MODEL if a different model name is
    # needed. As of late 2025, Google has retired gemini-2.0-flash
    # and gemini-2.5-flash in favor of gemini-3.8-flash.
    DEFAULT_GEMINI_MODEL = "gemini-3.8-flash"

    def __init__(self, brief: str, min_score: int, logger: logging.Logger) -> None:
        self.brief = brief.strip()
        self.min_score = min_score
        self.logger = logger
        self.gemini_api_key = os.environ.get("GEMINI_API_KEY", "").strip()
        self.gemini_model = os.environ.get("GEMINI_MODEL", self.DEFAULT_GEMINI_MODEL)
        if self.gemini_api_key:
            self.logger.info(
                "LLM backend: Gemini (model=%s) — free tier",
                self.gemini_model,
            )
        else:
            self.logger.info(
                "LLM backend: z-ai CLI (GLM-4) — no GEMINI_API_KEY in env"
            )

    SYSTEM_PROMPT_TEMPLATE = """\
You are an expert procurement analyst. You will receive a single
candidate item that has been pulled from a TENDER LISTING PAGE.
This means that, by default, the item is ALREADY a bid / tender /
procurement notice. Your job is NOT to second-guess whether it is
a bid — assume it is. Your job is to:
  1. Confirm the item is a real procurement opportunity matching
     the user-supplied relevance criteria (vs. e.g. a press
     release, blog post, or news article that accidentally slipped
     onto the listing page, OR a tender in a non-matching category
     that was mis-tagged).
  2. Categorize it.
  3. Extract deadline, budget, and a DETAILED summary.
  4. Translate any non-English (e.g. Amharic) content into English
     for the summary, but keep the original-language title verbatim.

If the item is clearly a tender / RFP / RFQ / EOI / call for
proposals / procurement notice / prequalification notice that
MATCHES the user-supplied relevance criteria — even if its title
is in a non-English language like Amharic — set is_relevant = true
and relevance_score >= 80.

If the item does NOT match the user-supplied relevance criteria
(e.g. the tender is about office supplies but the user only wants
vehicle tenders), set is_relevant = false and relevance_score = 0.

Additional user-supplied relevance criteria:
{brief}

Respond with STRICT JSON ONLY (no markdown fences, no commentary)
matching this exact schema:

{{
  "is_relevant": <bool>,
  "relevance_score": <int 0-100>,
  "category": "<one of: Construction | IT & Software | Supplies & Equipment | Consultancy | Medical | Transportation | Vehicle Purchase | Vehicle Maintenance | Other>",
  "deadline": "<ISO date YYYY-MM-DD if mentioned, else null>",
  "budget": "<raw text snippet if mentioned, else null>",
  "summary": "<4-6 sentences in English, up to 500 chars. Include: what is being procured, who is issuing it, key deadlines, and any notable terms (bid bond, document fee, etc.). Translate Amharic to English. Be specific and actionable.>"
}}
"""

    def judge(self, item: BidItem) -> LLMJudgment | None:
        # Merge system instructions into the user prompt (works for
        # both Gemini and z-ai CLI backends).
        system_prompt = self.SYSTEM_PROMPT_TEMPLATE.format(brief=self.brief)
        user_prompt = self._build_user_prompt(item)
        combined_prompt = f"{system_prompt}\n\n--- BEGIN ITEM ---\n{user_prompt}"

        # Try up to 3 times with backoff. 429 errors get a longer wait.
        last_err = None
        raw: str = ""
        for attempt in range(3):
            try:
                if self.gemini_api_key:
                    raw = self._call_gemini(combined_prompt)
                else:
                    raw = self._call_zai_cli(combined_prompt)
                if raw:
                    break
            except RateLimitError as exc:
                last_err = exc
                wait = 30 * (attempt + 1)   # 30s, 60s, 90s
                self.logger.warning(
                    "LLM rate-limited for %s (attempt %d, waiting %ds)",
                    item.source_url, attempt + 1, wait,
                )
                time.sleep(wait)
            except Exception as exc:
                last_err = exc
                wait = 2 ** attempt   # 1s, 2s, 4s
                self.logger.warning(
                    "LLM call attempt %d failed for %s (retry in %ds): %s",
                    attempt + 1, item.source_url, wait, str(exc)[:160],
                )
                time.sleep(wait)
        else:
            self.logger.error(
                "LLM call gave up after retries for %s: %s",
                item.source_url, last_err,
            )
            return None

        parsed = self._parse_json(raw)
        if not parsed:
            self.logger.warning(
                "Could not parse LLM JSON for %s. Raw: %s",
                item.source_url, (raw or "")[:200],
            )
            return None

        # Small courtesy throttle to avoid hammering the API
        time.sleep(1.2)

        return LLMJudgment(
            is_relevant=bool(parsed.get("is_relevant", False)),
            relevance_score=int(parsed.get("relevance_score", 0) or 0),
            category=str(parsed.get("category", "Other")),
            deadline=str(parsed.get("deadline") or "N/A"),
            budget=str(parsed.get("budget") or "N/A"),
            summary=str(parsed.get("summary", "")).strip(),
            raw=raw,
        )

    def _build_user_prompt(self, item: BidItem) -> str:
        snippet = item.raw_html[:1500] if item.raw_html else "(no snippet)"
        # Include ALL structured metadata so the LLM has full context.
        meta_lines = []
        if item.company_name:
            meta_lines.append(f"Issuing organization: {item.company_name}")
        if item.deadline:
            meta_lines.append(f"Bid closing date: {item.deadline}")
        if item.opening_date:
            meta_lines.append(f"Bid opening date: {item.opening_date}")
        if item.published_at:
            meta_lines.append(f"Published at: {item.published_at}")
        if item.region and item.region != "Not specified":
            meta_lines.append(f"Region: {item.region}")
        if item.source_name and item.source_name != "Not specified":
            meta_lines.append(f"Publication source: {item.source_name}")
        if item.bid_doc_price and item.bid_doc_price != "Not specified":
            meta_lines.append(f"Bid document price: {item.bid_doc_price}")
        if item.bid_bond and item.bid_bond != "Not specified":
            meta_lines.append(f"Bid bond: {item.bid_bond}")
        meta_block = "\n".join(meta_lines) if meta_lines else "(no metadata)"
        return (
            f"Title: {item.title}\n"
            f"URL: {item.source_url}\n"
            f"Metadata:\n{meta_block}\n"
            f"Raw item JSON:\n{snippet}\n"
        )

    # ---- Gemini backend -------------------------------------------------
    def _call_gemini(self, prompt: str) -> str:
        """Call Google Gemini via REST API. Returns the model's text content."""
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.gemini_model}:generateContent?key={self.gemini_api_key}"
        )
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            # Slightly lower temperature = more deterministic JSON output
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": 1024},
        }
        try:
            resp = requests.post(url, json=payload, timeout=60)
        except Exception as exc:
            raise RuntimeError(f"Gemini HTTP error: {exc}")

        if resp.status_code == 429:
            raise RateLimitError("Gemini API returned 429 (rate-limited)")
        if resp.status_code != 200:
            # Surface a useful chunk of the error body for debugging
            body = resp.text[:300]
            raise RuntimeError(
                f"Gemini HTTP {resp.status_code}: {body}"
            )
        try:
            data = resp.json()
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Gemini response not JSON: {exc}")

        # Standard Gemini response shape:
        # {"candidates":[{"content":{"parts":[{"text":"..."}]}}]}
        candidates = data.get("candidates") or []
        if not candidates:
            # Maybe a prompt feedback block (e.g. safety filter blocked)
            feedback = data.get("promptFeedback") or {}
            block_reason = feedback.get("blockReason")
            raise RuntimeError(
                f"Gemini returned no candidates"
                + (f" (blockReason={block_reason})" if block_reason else "")
            )
        parts = candidates[0].get("content", {}).get("parts", [])
        if not parts:
            raise RuntimeError("Gemini candidate has no parts")
        return parts[0].get("text", "")

    # ---- z-ai CLI backend (fallback) ------------------------------------
    def _call_zai_cli(self, prompt: str) -> str:
        """Invoke `z-ai chat` via subprocess and return the assistant text."""
        cmd = [
            "z-ai", "chat",
            "--prompt", prompt,
            "-o", "/dev/stdout",
        ]
        env = os.environ.copy()
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=90, env=env,
        )
        if proc.returncode != 0:
            # Detect HTTP 429 rate-limiting for smarter backoff
            err_text = (proc.stderr or "") + (proc.stdout or "")
            if "429" in err_text or "Too many requests" in err_text:
                raise RateLimitError("z-ai API returned 429 (rate-limited)")
            raise RuntimeError(
                f"z-ai CLI exited {proc.returncode}: {proc.stderr[:300]}"
            )
        out = proc.stdout
        # The CLI prepends 🚀 status lines and appends a "✅ File saved
        # to: /dev/stdout" line — but the latter may be glued to the
        # JSON's closing `}` on the same line (e.g. `}✅ File saved...`).
        # Strategy: strip whole status lines, then truncate anything
        # from the first ✅ or 🚀 emoji encountered mid-line.
        cleaned_lines: list[str] = []
        for line in out.splitlines():
            if line.startswith("🚀") or line.startswith("❌"):
                continue
            if line.startswith("✅"):
                continue
            for marker in ("✅", "❌"):
                idx = line.find(marker)
                if idx >= 0:
                    line = line[:idx]
            cleaned_lines.append(line)
        out = "\n".join(cleaned_lines).strip()
        # Unwrap the JSON envelope: {"choices":[{"message":{"content":"..."}}]}
        try:
            data = json.loads(out)
            if isinstance(data, dict) and "choices" in data:
                return data["choices"][0]["message"]["content"]
            if isinstance(data, dict):
                return (
                    data.get("content")
                    or data.get("response")
                    or data.get("text")
                    or out
                )
        except json.JSONDecodeError:
            pass
        return out

    @staticmethod
    def _parse_json(raw: str) -> dict | None:
        if not raw:
            return None
        # Strip markdown code fences if present
        raw2 = raw.strip()
        if raw2.startswith("```"):
            raw2 = re.sub(r"^```(?:json)?\s*", "", raw2)
            raw2 = re.sub(r"\s*```$", "", raw2)
        try:
            return json.loads(raw2)
        except json.JSONDecodeError:
            # Last-ditch: try to extract first {...} block
            match = re.search(r"\{[\s\S]*\}", raw2)
            if match:
                try:
                    return json.loads(match.group(0))
                except json.JSONDecodeError:
                    return None
            return None


# ---------------------------------------------------------------------------
# Telegram notifier
# ---------------------------------------------------------------------------
class TelegramNotifier:
    SEND_URL = "https://api.telegram.org/bot{token}/sendMessage"
    ANSWER_URL = "https://api.telegram.org/bot{token}/answerCallbackQuery"

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        message_template: str,
        logger: logging.Logger,
    ) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.template = message_template
        self.logger = logger

    def send_bid(self, item: BidItem, judgment: LLMJudgment) -> bool:
        # Note: the template references {source_url} as a raw URL inside a
        # Markdown link, so we deliberately do NOT escape that one.
        text = self.template.format(
            title=self._md_escape(item.title),
            category=self._md_escape(judgment.category),
            company_name=self._md_escape(item.company_name or "Not specified"),
            deadline=self._md_escape(item.deadline or judgment.deadline or "Not specified"),
            opening_date=self._md_escape(item.opening_date or "Not specified"),
            published_at=self._md_escape(item.published_at or "Not specified"),
            region=self._md_escape(item.region or "Not specified"),
            source_name=self._md_escape(item.source_name or "Not specified"),
            bid_doc_price=self._md_escape(item.bid_doc_price or "Not specified"),
            bid_bond=self._md_escape(item.bid_bond or "Not specified"),
            budget=self._md_escape(judgment.budget or "Not specified"),
            summary=self._md_escape(judgment.summary or "(no summary)"),
            source_url=item.source_url,   # raw URL — must NOT be escaped
            site_name=self._md_escape(item.site_name),
            found_at=item.found_at,
        )
        keyboard = {
            "inline_keyboard": [[
                {"text": "🔗 View original tender", "url": item.source_url},
                {"text": "✅ Mark as read", "callback_data": f"read:{item.source_url[:48]}"},
            ]]
        }
        return self._send(text, keyboard)

    def _send(self, text: str, keyboard: dict | None = None) -> bool:
        payload: dict[str, Any] = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }
        if keyboard:
            payload["reply_markup"] = json.dumps(keyboard)
        try:
            resp = requests.post(
                self.SEND_URL.format(token=self.bot_token),
                json=payload, timeout=20,
            )
            data = resp.json()
            if not data.get("ok"):
                self.logger.error(
                    "Telegram API error: %s",
                    json.dumps(data, ensure_ascii=False)[:300],
                )
                # If Markdown parse failed, retry as plain text
                if "can't parse entities" in str(data.get("description", "")).lower():
                    self.logger.info("Retrying as plain text…")
                    payload.pop("parse_mode", None)
                    resp = requests.post(
                        self.SEND_URL.format(token=self.bot_token),
                        json=payload, timeout=20,
                    )
                    data = resp.json()
                    if not data.get("ok"):
                        self.logger.error("Retry failed: %s", data)
                        return False
                else:
                    return False
            return True
        except Exception as exc:
            self.logger.error("Telegram send exception: %s", exc)
            return False

    @staticmethod
    def _md_escape(s: str) -> str:
        """Escape MarkdownV1 special characters (preserving URL position)."""
        if s is None:
            return ""
        # MarkdownV1 special chars: _ * [ ]
        return str(s).replace("_", "\\_").replace("*", "\\*").replace("[", "\\[").replace("]", "\\]")


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------
class BidAgent:
    def __init__(self, cfg: dict[str, Any], logger: logging.Logger) -> None:
        self.cfg = cfg
        self.logger = logger
        self.user_agent = os.environ.get(
            "USER_AGENT", "Mozilla/5.0 (compatible; BidNewsAgent/1.0)"
        )
        self.store = SentStore(cfg["db_path"], logger)
        self.filter = LLMFilter(
            brief=cfg["filter_brief"],
            min_score=cfg.get("min_relevance_score", 60),
            logger=logger,
        )
        self.telegram = self._maybe_init_telegram()

    def _maybe_init_telegram(self) -> TelegramNotifier | None:
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        if not token or not chat_id or token.startswith("your_"):
            self.logger.warning(
                "Telegram credentials not configured — notifications disabled."
            )
            return None
        return TelegramNotifier(
            bot_token=token,
            chat_id=chat_id,
            message_template=self.cfg["message_template"],
            logger=self.logger,
        )

    # ---- Single run cycle ------------------------------------------------
    def run_once(self, dry_run: bool = False) -> dict[str, int]:
        stats = {"scraped": 0, "new": 0, "sent": 0, "skipped": 0, "errors": 0}
        for site_cfg in self.cfg.get("sites", []):
            try:
                # Authenticate if the site declares a `login` block
                session = self._maybe_login(site_cfg)

                scraper = SiteScraper(
                    site_cfg, self.user_agent, self.logger, session=session,
                )
                items = scraper.fetch_items()
                stats["scraped"] += len(items)
                self.logger.info(
                    "Scraped %d items from %s", len(items), scraper.name
                )

                max_items = self.cfg.get("max_items_per_run", 20)
                if len(items) > max_items:
                    self.logger.warning(
                        "Site returned %d items (cap=%d) — processing first %d only.",
                        len(items), max_items, max_items,
                    )
                    items = items[:max_items]

                for item in items:
                    if self.store.already_sent(item.source_url):
                        stats["skipped"] += 1
                        continue
                    stats["new"] += 1

                    judgment = self.filter.judge(item)
                    if judgment is None:
                        stats["errors"] += 1
                        continue
                    if not judgment.is_relevant or judgment.relevance_score < self.filter.min_score:
                        self.logger.info(
                            "Skip (not relevant, score=%d): %s",
                            judgment.relevance_score, item.title[:80],
                        )
                        # Still mark as seen so we don't re-judge every cycle
                        self.store.mark_sent(
                            item.source_url, item.title, "irrelevant", item.site_name
                        )
                        stats["skipped"] += 1
                        continue

                    if dry_run:
                        self.logger.info(
                            "[DRY] Would send: %s (%s, score=%d)",
                            item.title[:80], judgment.category, judgment.relevance_score
                        )
                        self.store.mark_sent(
                            item.source_url, item.title, judgment.category, item.site_name
                        )
                        stats["sent"] += 1
                        continue

                    if not self.telegram:
                        self.logger.error(
                            "Cannot send — Telegram not configured. Item: %s",
                            item.source_url,
                        )
                        stats["errors"] += 1
                        continue

                    ok = self.telegram.send_bid(item, judgment)
                    if ok:
                        self.store.mark_sent(
                            item.source_url, item.title, judgment.category, item.site_name
                        )
                        stats["sent"] += 1
                        self.logger.info(
                            "Sent: %s (%s, score=%d)",
                            item.title[:80], judgment.category, judgment.relevance_score
                        )
                    else:
                        stats["errors"] += 1
                    # Light throttle to be polite to Telegram
                    time.sleep(1)
            except Exception:
                self.logger.error("Site run failed:\n%s", traceback.format_exc())
                stats["errors"] += 1

        self.logger.info("Cycle complete: %s", stats)
        return stats

    def _maybe_login(self, site_cfg: dict[str, Any]) -> requests.Session | None:
        """If the site declares a `login` block, authenticate and return
        an authenticated session. Otherwise return None (no auth)."""
        login_cfg = site_cfg.get("login")
        if not login_cfg:
            return None
        authenticator = LoginAuthenticator(
            login_cfg=login_cfg,
            user_agent=self.user_agent,
            logger=self.logger,
        )
        return authenticator.login()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="Bid News Agent")
    parser.add_argument("--daemon", action="store_true",
                        help="Run forever on the configured interval.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch and judge, but do not send Telegram messages.")
    args = parser.parse_args()

    load_dotenv(ENV_PATH)
    cfg = load_config()
    logger = setup_logging(cfg["log_file"], cfg.get("log_level", "INFO"))
    logger.info("===== Bid News Agent starting =====")
    logger.info("Mode: %s", "daemon" if args.daemon else "single")
    logger.info("Dry-run: %s", args.dry_run)

    agent = BidAgent(cfg, logger)

    if args.daemon:
        interval = int(cfg.get("run_interval_minutes", 30)) * 60
        logger.info("Daemon interval: %d seconds", interval)
        try:
            while True:
                agent.run_once(dry_run=args.dry_run)
                logger.info("Sleeping %d seconds…", interval)
                time.sleep(interval)
        except KeyboardInterrupt:
            logger.info("Received Ctrl-C, exiting cleanly.")
        finally:
            agent.store.close()
    else:
        agent.run_once(dry_run=args.dry_run)
        agent.store.close()

    logger.info("===== Bid News Agent finished =====")
    return 0


if __name__ == "__main__":
    sys.exit(main())
