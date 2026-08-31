"""Voice control for a Goodreads shelf from the Pebble Index ring.

Pebble's cloud agent talks MCP to this server; this server drives a real
Chromium against goodreads.com, signed in as you.

    export MCP_BEARER_TOKEN=$(openssl rand -hex 32)
    export GOODREADS_PROFILE=/profile      # persistent Chromium profile
    export GOODREADS_QUEUE=/profile/queue.json

Why a browser and not an API: Goodreads retired its public API in December
2020 and does not issue keys any more. There is no sanctioned programmatic
write path, so the only way to shelve a book is to be a signed-in browser.

How the session works, and why it's split in two:

    normal operation   headless, invisible, never touches the screen
    authentication     headed, on the Pi's touchscreen, when you say so

Both share ONE Chromium profile directory, so cookies earned by the headed
login are what the headless runs use afterwards. `--headless=new` is not
optional here -- the legacy headless mode does not share a profile with a
headed run reliably, which would mean re-authenticating constantly.

When the session dies, actions are queued rather than failed, and applied
next time you authenticate. The tools say "queued", never "added" -- a
queued action has not happened yet, and reporting otherwise would mean the
ring tells you a book is shelved when it isn't.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastmcp import FastMCP
from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException, TimeoutException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("goodreads_mcp")

BEARER = os.environ.get("MCP_BEARER_TOKEN", "")
PROFILE = os.environ.get("GOODREADS_PROFILE", "/profile")
QUEUE_PATH = Path(os.environ.get("GOODREADS_QUEUE", "/profile/queue.json"))
CHROMIUM = os.environ.get("CHROMIUM_BINARY", "/usr/bin/chromium")
CHROMEDRIVER = os.environ.get("CHROMEDRIVER_BINARY", "/usr/bin/chromedriver")

BASE = "https://www.goodreads.com"

# Goodreads' three built-in shelves. Everything a person says maps onto one
# of these; custom shelves are deliberately out of scope.
SHELVES = {
    "want to read": "to-read",
    "to read": "to-read",
    "toread": "to-read",
    "want": "to-read",
    "wishlist": "to-read",
    "reading": "currently-reading",
    "currently reading": "currently-reading",
    "started": "currently-reading",
    "read": "read",
    "finished": "read",
    "done": "read",
    "completed": "read",
}
SHELF_LABELS = {
    "to-read": "want to read",
    "currently-reading": "currently reading",
    "read": "read",
}

# Goodreads renders signed-out and signed-in headers differently. These are
# the cheapest reliable discriminators, checked against the real page.
SIGNED_OUT_MARKERS = ("sign in", "sign up")
SIGNED_IN_MARKER = "my books"

PAGE_TIMEOUT = 30
# Deliberately unhurried. This drives a real site as a real user; there is no
# voice command that needs to fire faster than this, and hammering Goodreads
# is both rude and the fastest route to being blocked.
POLITE_DELAY = 1.5


class BearerAuth(BaseHTTPMiddleware):
    """Static bearer check. Pebble sends whatever header you configure."""

    async def dispatch(self, request: Request, call_next):
        if request.url.path == "/healthz":
            return await call_next(request)
        if request.headers.get("authorization", "") != f"Bearer {BEARER}":
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


mcp = FastMCP("goodreads")

# One browser at a time. Chromium refuses to open a profile that another
# process already holds, so concurrent tool calls would fail on the lock
# rather than queue politely.
_lock = asyncio.Lock()


class GoodreadsError(RuntimeError):
    """Something went wrong driving the site."""


class SignedOut(GoodreadsError):
    """The saved session is gone. Actions get queued rather than failed."""


# --- the browser -------------------------------------------------------

def _options(headed: bool = False) -> Options:
    o = Options()
    o.binary_location = CHROMIUM
    if not headed:
        o.add_argument("--headless=new")
    o.add_argument(f"--user-data-dir={PROFILE}")
    o.add_argument("--no-sandbox")
    o.add_argument("--disable-dev-shm-usage")
    o.add_argument("--window-size=1280,900")
    # Chromium advertises itself as automated by default, which some sites
    # treat as a bot signal. This is about not being gratuitously flagged
    # while doing something the account owner asked for, not about evading
    # anything -- the session is a real signed-in user throughout.
    o.add_argument("--disable-blink-features=AutomationControlled")
    o.add_experimental_option("excludeSwitches", ["enable-automation"])
    return o


@contextmanager
def _browser(headed: bool = False):
    Path(PROFILE).mkdir(parents=True, exist_ok=True)
    driver = webdriver.Chrome(service=Service(CHROMEDRIVER), options=_options(headed))
    driver.set_page_load_timeout(PAGE_TIMEOUT)
    try:
        yield driver
    finally:
        try:
            driver.quit()
        except Exception:  # noqa: BLE001 - teardown must never mask a real error
            logger.warning("browser did not shut down cleanly", exc_info=True)


def _go(driver, url: str) -> None:
    driver.get(url)
    time.sleep(POLITE_DELAY)


def _signed_in(driver) -> bool:
    """Is this session still authenticated?

    Reads the page text rather than poking at cookies: a cookie can be
    present and expired, but the rendered header is the truth.
    """
    try:
        body = driver.find_element(By.TAG_NAME, "body").text.lower()
    except NoSuchElementException:
        return False
    if SIGNED_IN_MARKER in body:
        return True
    return not any(m in body for m in SIGNED_OUT_MARKERS)


def _require_session(driver) -> None:
    _go(driver, BASE)
    if not _signed_in(driver):
        raise SignedOut(
            "the saved Goodreads session has expired -- run the authenticate "
            "command on the Pi to sign in again"
        )


# --- the queue ---------------------------------------------------------

def _queue_read() -> list[dict]:
    try:
        return json.loads(QUEUE_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _queue_write(items: list[dict]) -> None:
    QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Write-then-rename so a crash mid-write can't leave a truncated queue.
    tmp = QUEUE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, indent=2))
    tmp.replace(QUEUE_PATH)


def _queue_push(action: str, **fields) -> int:
    items = _queue_read()
    items.append({
        "action": action,
        "queued_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **fields,
    })
    _queue_write(items)
    return len(items)


def _flush_queue(driver) -> tuple[list[str], list[str]]:
    """Apply everything queued. Returns (applied, failed) descriptions."""
    items = _queue_read()
    if not items:
        return [], []
    applied, failed, remaining = [], [], []
    for item in items:
        try:
            if item.get("action") == "shelve":
                title = _shelve(driver, item["book"], item["shelf"])
                applied.append(f"{title} -> {SHELF_LABELS.get(item['shelf'], item['shelf'])}")
            else:
                logger.warning("unknown queued action %r, dropping", item.get("action"))
        except SignedOut:
            # Still signed out: keep this and everything after it untouched.
            remaining.append(item)
        except GoodreadsError as err:
            # A real failure (book not found, say) will never succeed on a
            # retry, so drop it rather than retrying forever -- but say so.
            failed.append(f"{item.get('book')}: {err}")
    _queue_write(remaining)
    return applied, failed


# --- driving the site --------------------------------------------------

def _search(driver, query: str) -> list[dict]:
    """Search Goodreads and return the top results."""
    _go(driver, f"{BASE}/search?q={query.replace(' ', '+')}")
    results = []
    for row in driver.find_elements(By.CSS_SELECTOR, "tr[itemtype='http://schema.org/Book']")[:5]:
        try:
            link = row.find_element(By.CSS_SELECTOR, "a.bookTitle")
            author = row.find_element(By.CSS_SELECTOR, "a.authorName").text.strip()
            results.append({
                "title": link.text.strip(),
                "author": author,
                "url": link.get_attribute("href"),
            })
        except NoSuchElementException:
            continue
    return results


def _shelve(driver, book: str, shelf: str) -> str:
    """Put a book on a shelf. Returns the title actually shelved."""
    results = _search(driver, book)
    if not results:
        raise GoodreadsError(f"I couldn't find a book called '{book}' on Goodreads")
    top = results[0]
    _go(driver, top["url"])

    if not _signed_in(driver):
        raise SignedOut("session expired mid-action")

    # The shelf control is a button plus a dropdown. Clicking the main button
    # shelves as want-to-read; anything else needs the dropdown opened first.
    try:
        if shelf == "to-read":
            button = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable(
                    (By.CSS_SELECTOR, "button.WantToReadButton, .wtrToRead button")
                )
            )
            button.click()
        else:
            WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable(
                    (By.CSS_SELECTOR, "button.WantToReadDropdown, .wtrShelfButton")
                )
            ).click()
            time.sleep(POLITE_DELAY)
            label = SHELF_LABELS.get(shelf, shelf)
            option = driver.find_element(
                By.XPATH,
                f"//div[@role='menu']//*[contains(translate(text(),"
                f"'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),"
                f"'{label}')]",
            )
            option.click()
        time.sleep(POLITE_DELAY)
    except (TimeoutException, NoSuchElementException) as err:
        raise GoodreadsError(
            f"couldn't find the shelf control on the page for '{top['title']}' "
            "-- Goodreads may have changed its markup"
        ) from err
    return top["title"]


def _shelf_contents(driver, shelf: str, limit: int = 15) -> list[str]:
    _go(driver, f"{BASE}/review/list?shelf={shelf}&per_page=20")
    if not _signed_in(driver):
        raise SignedOut("session expired")
    titles = []
    for el in driver.find_elements(By.CSS_SELECTOR, "td.field.title a")[:limit]:
        text = re.sub(r"\s+", " ", el.text).strip()
        if text:
            titles.append(text)
    return titles


def _resolve_shelf(spoken: str) -> str:
    key = re.sub(r"[^a-z ]", "", spoken.lower()).strip()
    if key in SHELVES:
        return SHELVES[key]
    for phrase, slug in SHELVES.items():
        if phrase in key:
            return slug
    raise GoodreadsError(
        f"I don't know a shelf called '{spoken.strip()}'. "
        "There's want to read, currently reading, and read."
    )


async def _run(fn, *args):
    """Selenium is blocking; keep it off the event loop."""
    return await asyncio.to_thread(fn, *args)


# --- tools -------------------------------------------------------------

@mcp.tool
async def goodreads_status() -> str:
    """Check whether Goodreads is signed in, and what's waiting to be applied.

    Use this when the user asks if books are working, or why something
    hasn't shown up.
    """
    logger.info("goodreads_status: called")
    pending = _queue_read()
    async with _lock:
        try:
            def check():
                with _browser() as d:
                    _go(d, BASE)
                    return _signed_in(d)
            ok = await _run(check)
        except Exception as err:  # noqa: BLE001 - report, never crash the tool
            logger.exception("status check failed")
            return f"Couldn't check Goodreads: {err}"

    if ok and not pending:
        return "Goodreads is signed in and everything's up to date."
    if ok and pending:
        return (
            f"Goodreads is signed in, and {len(pending)} action(s) are still "
            "queued -- say 'sync my books' to apply them."
        )
    queued = f" {len(pending)} action(s) are waiting." if pending else ""
    return (
        "Goodreads is signed out. Run the authenticate command on the Pi's "
        f"touchscreen to sign in again.{queued}"
    )


@mcp.tool
async def search_books(query: str) -> str:
    """Search Goodreads for a book.

    Use this when the user isn't sure of an exact title, or to confirm which
    edition they mean before shelving it.

    Args:
        query: Title, author, or both.
    """
    logger.info("search_books: query=%r", query)
    if not query.strip():
        return "What should I search for?"
    async with _lock:
        try:
            def search():
                with _browser() as d:
                    _require_session(d)
                    return _search(d, query)
            results = await _run(search)
        except SignedOut as err:
            return f"Couldn't search: {err}."
        except Exception as err:  # noqa: BLE001
            logger.exception("search failed")
            return f"Couldn't search Goodreads: {err}"
    if not results:
        return f"Nothing on Goodreads matches '{query.strip()}'."
    return "Found: " + "; ".join(f"{r['title']} by {r['author']}" for r in results) + "."


@mcp.tool
async def add_to_shelf(book: str, shelf: str = "want to read") -> str:
    """Put a book on one of your Goodreads shelves.

    Args:
        book: Title, as the user said it. An author helps for common titles.
        shelf: "want to read", "currently reading", or "read". Defaults to
            want to read.

    IMPORTANT: if the result says the action was queued, tell the user it
    will be applied next time they authenticate on the Pi. Do not describe a
    queued book as already shelved.
    """
    logger.info("add_to_shelf: book=%r shelf=%r", book, shelf)
    if not book.strip():
        return "Which book?"
    try:
        slug = _resolve_shelf(shelf)
    except GoodreadsError as err:
        return str(err)

    async with _lock:
        try:
            def shelve():
                with _browser() as d:
                    _require_session(d)
                    return _shelve(d, book, slug)
            title = await _run(shelve)
        except SignedOut:
            n = _queue_push("shelve", book=book.strip(), shelf=slug)
            return (
                f"Goodreads is signed out, so I've queued '{book.strip()}' for "
                f"{SHELF_LABELS[slug]}. It'll go up next time you authenticate "
                f"on the Pi. {n} action(s) waiting."
            )
        except GoodreadsError as err:
            return str(err)
        except Exception as err:  # noqa: BLE001
            logger.exception("shelving failed")
            return f"Couldn't shelve that: {err}"
    return f"Put {title} on your {SHELF_LABELS[slug]} shelf."


@mcp.tool
async def list_shelf(shelf: str = "currently reading") -> str:
    """Read back what's on one of your Goodreads shelves.

    Args:
        shelf: "want to read", "currently reading", or "read". Defaults to
            currently reading.
    """
    logger.info("list_shelf: shelf=%r", shelf)
    try:
        slug = _resolve_shelf(shelf)
    except GoodreadsError as err:
        return str(err)
    async with _lock:
        try:
            def read():
                with _browser() as d:
                    _require_session(d)
                    return _shelf_contents(d, slug)
            titles = await _run(read)
        except SignedOut as err:
            return f"Couldn't read that shelf: {err}."
        except Exception as err:  # noqa: BLE001
            logger.exception("shelf read failed")
            return f"Couldn't read that shelf: {err}"
    if not titles:
        return f"Nothing on your {SHELF_LABELS[slug]} shelf."
    return f"On {SHELF_LABELS[slug]}: " + ", ".join(titles) + "."


@mcp.tool
async def pending_books() -> str:
    """List book actions queued while Goodreads was signed out.

    Use this when the user asks what's waiting, or whether a book went
    through.
    """
    logger.info("pending_books: called")
    items = _queue_read()
    if not items:
        return "Nothing's waiting -- everything's been applied."
    parts = [
        f"{i.get('book')} to {SHELF_LABELS.get(i.get('shelf'), i.get('shelf'))}"
        for i in items
    ]
    return f"{len(items)} waiting: " + ", ".join(parts) + "."


@mcp.tool
async def sync_books() -> str:
    """Apply any book actions that were queued while Goodreads was signed out.

    Use this after the user has authenticated on the Pi, or when they ask to
    sync their books.
    """
    logger.info("sync_books: called")
    if not _queue_read():
        return "Nothing's waiting -- everything's been applied."
    async with _lock:
        try:
            def flush():
                with _browser() as d:
                    _require_session(d)
                    return _flush_queue(d)
            applied, failed = await _run(flush)
        except SignedOut as err:
            return f"Still signed out, so nothing was applied -- {err}."
        except Exception as err:  # noqa: BLE001
            logger.exception("sync failed")
            return f"Couldn't sync: {err}"
    bits = []
    if applied:
        bits.append(f"Applied {len(applied)}: " + ", ".join(applied) + ".")
    if failed:
        bits.append(f"Couldn't apply {len(failed)}: " + "; ".join(failed) + ".")
    return " ".join(bits) if bits else "Nothing to apply."


async def healthz(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


app = mcp.http_app(path="/mcp")
app.add_middleware(BearerAuth)
app.router.routes.insert(0, Route("/healthz", healthz))

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
