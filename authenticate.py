"""Sign in to Goodreads on the Pi's touchscreen, then apply anything queued.

Run this when the MCP says the session has expired:

    docker compose exec goodreads-mcp python authenticate.py

A real Chromium window opens on the touchscreen, over whatever the kiosk is
showing. Sign in, clear any captcha or two-factor prompt, and the window
closes on its own once it sees you're through. The kiosk is still running
underneath -- nothing needs restarting.

This writes to the SAME profile directory the headless MCP reads, which is
the whole point: cookies earned here are what the server uses afterwards.
That also means it needs the same Chromium build, which is why this runs
inside the container rather than against the host's browser -- Chrome
upgrades a profile forward and won't open one written by a newer version.
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import goodreads_mcp_server as g  # noqa: E402

# How long to leave the window open waiting for a human. Generous, because
# a captcha plus a two-factor code from a phone is not a 60-second job.
WAIT_SECONDS = int(os.environ.get("GOODREADS_AUTH_TIMEOUT", "600"))
POLL_SECONDS = 3


def main() -> int:
    if not os.environ.get("WAYLAND_DISPLAY") and not os.environ.get("DISPLAY"):
        print(
            "No display available. This has to run where the touchscreen is:\n"
            "  WAYLAND_DISPLAY and XDG_RUNTIME_DIR must be set, and the\n"
            "  Wayland socket mounted into the container.",
            file=sys.stderr,
        )
        return 2

    pending = g._queue_read()
    print(f"Opening Goodreads on the touchscreen. {len(pending)} action(s) queued.")
    print("Sign in, clear anything it asks for, and this closes by itself.\n")

    with g._browser(headed=True) as driver:
        g._go(driver, f"{g.BASE}/user/sign_in")

        # Watch the cookie jar rather than the page. Page state only tells
        # you about the page: during a "Continue with Amazon" handoff the
        # browser isn't on goodreads.com at all, so there's no header to read
        # and every guess from markup is wrong. Cookies are visible across
        # domains via CDP, so the detour simply doesn't matter.
        baseline = g._goodreads_cookie_names(driver)
        print(f"  watching {len(baseline)} signed-out cookies for a change...")

        deadline = time.time() + WAIT_SECONDS
        signed_in = False
        announced_offsite = False
        while time.time() < deadline:
            try:
                fresh = g._goodreads_cookie_names(driver) - baseline
                if not g._on_goodreads(driver):
                    # Mid federated login. Say so once, decide nothing, and
                    # above all don't navigate -- that would abandon the flow.
                    if not announced_offsite:
                        print("  federated login in progress, waiting...", flush=True)
                        announced_offsite = True
                elif fresh and "/user/sign_in" not in driver.current_url:
                    # A new cookie is only a hint. Prove it by loading a page
                    # that lives behind auth and seeing whether it bounces.
                    print(f"  new cookie(s): {', '.join(sorted(fresh))} -- verifying", flush=True)
                    g._go(driver, f"{g.BASE}{g.MY_BOOKS_PATH}")
                    if "/user/sign_in" not in driver.current_url:
                        signed_in = True
                        break
                    baseline |= fresh  # not it; stop re-checking this one
            except Exception:  # noqa: BLE001 - the window may be mid-navigation
                pass
            remaining = int(deadline - time.time())
            if remaining % 30 == 0:
                print(f"  waiting... {remaining}s left", flush=True)
            time.sleep(POLL_SECONDS)

        if not signed_in:
            print("\nTimed out without a signed-in session. Nothing was applied.")
            return 1

        print("\nSigned in.")
        if not pending:
            print("Nothing was queued, so there's nothing to apply.")
            return 0

        print(f"Applying {len(pending)} queued action(s)...")
        applied, failed = g._flush_queue(driver)
        for line in applied:
            print(f"  applied  {line}")
        for line in failed:
            print(f"  FAILED   {line}")
        if not applied and not failed:
            # Everything bounced back to the queue, which means the session
            # still isn't good enough to act with. Saying nothing here read
            # as success and hid a real failure.
            still = len(g._queue_read())
            print(
                f"  NOTHING APPLIED -- all {still} action(s) are still queued.\n"
                "  The sign-in looked complete but Goodreads still treats this\n"
                "  browser as signed out. Try signing in again."
            )
            return 1
        if failed:
            print(
                "\nFailures are dropped rather than retried -- a book Goodreads "
                "can't find won't be found on a retry either."
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
