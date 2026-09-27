#!/usr/bin/env python3
"""program-poller.py — always-on watcher for NEW / UPDATED bug-bounty programs on YesWeHack,
which pings Telegram or Discord on any change. Built 2026-09-27.

Designed to run on a free always-on host (GitHub Actions cron every ~15 min) OR anywhere:
it is stateless except for a small JSON state file it reads/writes (state/programs.json), so
on GitHub Actions the workflow commits the updated state back to the repo after each run.

Config via ENV (set as GitHub Actions secrets, or a local .env):
  YWH_EMAIL, YWH_PASSWORD            — to log in and read /programs (JWT is short-lived; we re-login each run)
  DISCORD_WEBHOOK                    — if set, notify via Discord webhook (simplest: just a URL)
  TELEGRAM_TOKEN, TELEGRAM_CHAT_ID   — if set, notify via Telegram bot instead/also
  STATE_FILE                         — default state/programs.json
  RECENCY_FILTER_DAYS                — optional; only alert on programs updated within N days (default: alert on any new/changed)

What counts as a change: a program id we've never seen (NEW), or a program whose last_update_at
moved (UPDATED) — that's the operator's recency signal (a fresh update often means old bugs were
just fixed and live surface is worth a look).
"""
import json, os, sys, time, urllib.request, urllib.error

YWH_API = "https://api.yeswehack.com"
STATE_FILE = os.environ.get("STATE_FILE", "state/programs.json")


def _post(url, data, headers=None):
    req = urllib.request.Request(url, data=json.dumps(data).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def _get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def ywh_programs():
    email, pw = os.environ.get("YWH_EMAIL"), os.environ.get("YWH_PASSWORD")
    if not (email and pw):
        print("poller: YWH_EMAIL/YWH_PASSWORD unset — skipping YWH", file=sys.stderr); return []
    tok = _post(f"{YWH_API}/login", {"email": email, "password": pw})
    token = tok.get("token") or tok.get("access_token")
    if not token:
        print("poller: YWH login returned no token (2FA? check creds)", file=sys.stderr); return []
    auth = {"Authorization": f"Bearer {token}"}
    out, page = [], 1
    while True:
        d = _get(f"{YWH_API}/programs?page={page}", headers=auth)
        items = d.get("items", [])
        out += items
        if page >= (d.get("pagination", {}).get("nb_pages", 1)): break
        page += 1
    return [{"platform": "YWH", "slug": p.get("slug"), "title": p.get("title"),
             "updated": p.get("last_update_at"), "bounty": p.get("bounty"),
             "url": f"https://yeswehack.com/programs/{p.get('slug')}"} for p in out]


def load_state():
    try:
        return json.load(open(STATE_FILE, encoding="utf-8"))
    except Exception:
        return {}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
    json.dump(state, open(STATE_FILE, "w", encoding="utf-8"), indent=2)


def notify(lines):
    msg = "🆕 Bug-bounty program changes:\n" + "\n".join(lines)
    sent = False
    dw = os.environ.get("DISCORD_WEBHOOK")
    if dw:
        try:
            _post(dw, {"content": msg[:1900]}); sent = True; print("notified Discord")
        except Exception as e:
            print(f"discord notify failed: {e}", file=sys.stderr)
    tt, tc = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if tt and tc:
        try:
            _post(f"https://api.telegram.org/bot{tt}/sendMessage", {"chat_id": tc, "text": msg[:4000]})
            sent = True; print("notified Telegram")
        except Exception as e:
            print(f"telegram notify failed: {e}", file=sys.stderr)
    if not sent:
        print("poller: no notify channel configured (set DISCORD_WEBHOOK or TELEGRAM_TOKEN+CHAT_ID)", file=sys.stderr)
        print(msg)


def main():
    progs = ywh_programs()
    if not progs:
        print("poller: no programs fetched (creds/network?) — not overwriting state", file=sys.stderr); return 1
    state = load_state()
    changes = []
    for p in progs:
        key = f"{p['platform']}:{p['slug']}"
        prev = state.get(key)
        if prev is None:
            changes.append(f"[NEW] {p['platform']} · {p['title']} — {p['url']}")
        elif prev.get("updated") != p.get("updated"):
            changes.append(f"[UPDATED] {p['platform']} · {p['title']} — {p['url']}")
        state[key] = {"updated": p.get("updated"), "title": p.get("title")}
    save_state(state)
    if changes:
        print(f"{len(changes)} change(s)")
        notify(changes)
    else:
        print("no changes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
