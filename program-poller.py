#!/usr/bin/env python3
"""program-poller.py — always-on watcher for NEW / UPDATED bug-bounty programs on YesWeHack,
which pings Telegram or Discord on any change. Built 2026-09-27.

Runs on a free always-on host (GitHub Actions cron) or anywhere. Stateless except a small JSON
state file (state/programs.json) it reads/writes; on GitHub Actions the workflow commits the
updated state back to the repo after each run.

Config via ENV (GitHub Actions secrets, or a local .env):
  YWH_EMAIL, YWH_PASSWORD            — log in and read /programs (JWT is short-lived; we re-login each run)
  DISCORD_WEBHOOK                    — if set, notify via Discord webhook (simplest: just a URL)
  TELEGRAM_TOKEN, TELEGRAM_CHAT_ID   — if set, notify via Telegram bot instead/also
  STATE_FILE                         — default state/programs.json

Resilience: YWH occasionally 401s the default urllib client on repeated CI logins, so we send a
normal User-Agent and retry the login with backoff; a transient fetch failure SKIPS the cycle
(exit 0, state untouched) rather than failing the run — the next cron tick retries in 30 min.
"""
import json, os, sys, time, urllib.request, urllib.error

YWH_API = "https://api.yeswehack.com"
STATE_FILE = os.environ.get("STATE_FILE", "state/programs.json")
UA = "Mozilla/5.0 (X11; Linux x86_64) program-poller/1.0"


def _req(url, method="GET", data=None, headers=None):
    body = json.dumps(data).encode() if data is not None else None
    h = {"User-Agent": UA, "Accept": "application/json", **(headers or {})}
    if body is not None:
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, method=method, headers=h)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def ywh_login(email, pw):
    """Log in with a few retries — YWH intermittently 401s repeated CI logins."""
    for attempt in range(4):
        try:
            tok = _req(f"{YWH_API}/login", "POST", {"email": email, "password": pw})
            token = tok.get("token") or tok.get("access_token")
            if token:
                return token
            print(f"poller: YWH login attempt {attempt+1}: no token in response (2FA?)", file=sys.stderr)
            return None
        except urllib.error.HTTPError as e:
            print(f"poller: YWH login attempt {attempt+1} -> HTTP {e.code}", file=sys.stderr)
            if e.code in (401, 429, 500, 502, 503) and attempt < 3:
                time.sleep(6 * (attempt + 1)); continue
            return None
        except Exception as e:
            print(f"poller: YWH login attempt {attempt+1} error: {e}", file=sys.stderr)
            if attempt < 3:
                time.sleep(6 * (attempt + 1)); continue
            return None
    return None


def ywh_programs():
    # PREFERRED: a long-lived YWH Personal Access Token (YWH_TOKEN) — avoids the /login endpoint,
    # which anti-automation 401s from CI IPs. Falls back to email/password login only if no token.
    token = os.environ.get("YWH_TOKEN") or os.environ.get("YWH_PAT")
    if token:
        print("poller: using YWH_TOKEN (PAT) — skipping the /login call", file=sys.stderr)
    else:
        email, pw = os.environ.get("YWH_EMAIL"), os.environ.get("YWH_PASSWORD")
        if not (email and pw):
            print("poller: no YWH_TOKEN and no YWH_EMAIL/PASSWORD — skipping YWH", file=sys.stderr); return []
        token = ywh_login(email, pw)
        if not token:
            print("poller: YWH login failed after retries — skipping this cycle", file=sys.stderr); return []
    auth = {"Authorization": f"Bearer {token}"}
    out, page = [], 1
    try:
        while True:
            d = _req(f"{YWH_API}/programs?page={page}", headers=auth)
            out += d.get("items", [])
            if page >= (d.get("pagination", {}).get("nb_pages", 1)): break
            page += 1
    except Exception as e:
        print(f"poller: YWH /programs fetch failed ({e}) — skipping this cycle", file=sys.stderr); return []
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
    msg = "New bug-bounty program changes (YWH):\n" + "\n".join(lines)
    sent = False
    dw = os.environ.get("DISCORD_WEBHOOK")
    if dw:
        try:
            _req(dw, "POST", {"content": msg[:1900]}); sent = True; print("notified Discord")
        except Exception as e:
            print(f"discord notify failed: {e}", file=sys.stderr)
    tt, tc = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if tt and tc:
        try:
            _req(f"https://api.telegram.org/bot{tt}/sendMessage", "POST", {"chat_id": tc, "text": msg[:4000]})
            sent = True; print("notified Telegram")
        except Exception as e:
            print(f"telegram notify failed: {e}", file=sys.stderr)
    if not sent:
        print("poller: NO notify channel configured (set DISCORD_WEBHOOK or TELEGRAM_TOKEN+CHAT_ID)", file=sys.stderr)
        print(msg)


def main():
    progs = ywh_programs()
    if not progs:
        # transient fetch failure — skip this cycle, DON'T fail the run and DON'T overwrite state
        print("poller: nothing fetched this cycle; state untouched, will retry next tick")
        return 0
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
