# goodreads-mcp

Voice control for a Goodreads shelf from a [Pebble Index](https://www.pebble.computer/) ring.

Say *"put Project Hail Mary on want to read"* or *"what am I reading"* and it happens.

```
Pebble ring  ──MCP/HTTPS──▶  this server  ──Chromium──▶  goodreads.com
 (voice)                      (Pi, :8005)    (headless)     (signed in as you)
```

Runs on the same Raspberry Pi as the rest of my home services — see
[pi-home-services](https://github.com/SarjuThakkar/pi-home-services) for the
Docker Compose orchestration and Cloudflare Tunnel setup.

## Why a browser and not an API

Because there is no API. **Goodreads retired its public API in December 2020**
and has not issued keys since. There is no sanctioned programmatic way to
shelve a book, so the only route left is to be a signed-in browser.

That's a real trade-off and worth stating plainly:

- **It breaks when Goodreads changes their markup.** The selectors in
  `_shelve()` are the fragile part; everything else is stable.
- **It's slower than an API** — several seconds per action, since a real page
  has to load.
- **It needs a human occasionally**, for a captcha or a two-factor prompt.

Alternatives with real APIs exist ([Hardcover](https://docs.hardcover.app/api/getting-started/)
has a proper GraphQL one). This is Goodreads on purpose — that's where the
books already are.

## How the session works

Two entry points, one Chromium profile:

| | Mode | Where |
|---|---|---|
| Normal operation | `--headless=new` | invisible, never touches the screen |
| Authentication | headed | the Pi's touchscreen, on request |

They share **one profile directory**, so cookies earned by the headed login
are exactly what the headless runs use afterwards. `--headless=new` is not
optional: the legacy headless mode doesn't share a profile with a headed run
reliably, which would mean re-authenticating constantly.

The profile lives in a volume. Losing it means signing in again — nothing
worse.

### When the session dies

Actions are **queued, not failed**. Say "add X to want to read" while signed
out and it's recorded; the next authentication applies it.

The tools say **"queued"**, never "added". A queued book isn't shelved yet,
and reporting otherwise would mean the ring tells you something is done when
it isn't.

```
add_to_shelf   → "Goodreads is signed out, so I've queued 'Piranesi' for
                  want to read. It'll go up next time you authenticate."
pending_books  → what's waiting
sync_books     → apply the queue (also runs automatically after signing in)
```

## Tools

| Tool | What it does |
|---|---|
| `goodreads_status` | Signed in? Anything queued? |
| `search_books(query)` | Find a book, to confirm which one is meant |
| `add_to_shelf(book, shelf)` | Shelve it — want to read / currently reading / read |
| `list_shelf(shelf)` | Read a shelf back |
| `pending_books` | What's queued while signed out |
| `sync_books` | Apply the queue |

Only the three built-in shelves are supported. Spoken variants map onto them —
"finished" and "done" both mean `read`, "wishlist" means `to-read`.

## Signing in

```bash
docker compose exec goodreads-mcp python authenticate.py
```

A Chromium window opens on the touchscreen over the kiosk. Sign in, clear any
captcha or two-factor prompt, and it closes once it sees a valid session, then
applies anything queued. The kiosk keeps running underneath.

This runs **inside the container**, not against the host's Chromium, because
Chrome upgrades a profile forward and won't open one written by a newer build.
One Chromium, one profile, no version skew.

## Being a good citizen

This drives a real site as a real signed-in user, and it's deliberately
unhurried — a `POLITE_DELAY` between navigations, no parallelism, one browser
at a time. It touches your own shelves and the search page, nothing else. It
is not a crawler and shouldn't be turned into one.

## Setup

```bash
cp .env.example .env      # fill in MCP_BEARER_TOKEN
docker build -t goodreads-mcp .
docker run -p 8005:8000 --env-file .env -v goodreads-profile:/profile goodreads-mcp
```

Point the Pebble app at `https://<your-host>/mcp` with the bearer token, then
sign in with `authenticate.py`.

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `MCP_BEARER_TOKEN` | — | Static token Pebble sends as `Authorization: Bearer <token>` |
| `GOODREADS_PROFILE` | `/profile` | Persistent Chromium profile — the signed-in session |
| `GOODREADS_QUEUE` | `/profile/queue.json` | Queued actions |
| `GOODREADS_AUTH_TIMEOUT` | `600` | Seconds the sign-in window waits for you |
| `PORT` | `8000` | Port inside the container |
