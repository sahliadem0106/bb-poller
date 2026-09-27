#!/usr/bin/env python3
"""program-poller.py — always-on watcher for NEW / UPDATED YesWeHack bug-bounty programs;
pings Discord (and/or Telegram) whenever the public program directory changes. Rebuilt 2026-09-27.

Auth-free by design: it reads the PUBLIC endpoint https://api.yeswehack.com/programs, which returns
every public program (slug, title, last_update_at, bounty range) with NO login. That sidesteps the
earlier failure entirely — YWH's /login endpoint 401s GitHub's CI IPs, but the public listing does
not, so this runs reliably from any host.

State: a small JSON file (state/programs.json) mapping "<slug>" -> {updated, title}. On GitHub
Actions the workflow commits the updated state back after each run so the next run can diff.

Config via ENV (GitHub Actions secrets):
  DISCORD_WEBHOOK                    — if set, notify via Discord webhook (just the URL)
  TELEGRAM_TOKEN, TELEGRAM_CHAT_ID   — if set, notify via Telegram bot instead/also
  STATE_FILE                         — default state/programs.json
No YWH credentials are needed or used.
"""
import json, os, sys, time, urllib.request

PROGRAMS_URL = "https://api.yeswehack.com/programs?page={page}"
STATE_FILE = os.environ.get("STATE_FILE", "state/programs.json")
UA = "Mozilla/5.0 (X11; Linux x86_64) program-poller/2.0"


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def _post(url, payload):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"User-Agent": UA, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def ywh_programs():
    """Fetch every public YWH program from the auth-free directory (paginated)."""
    out, page = [], 1
    try:
        while True:
            d = _get(PROGRAMS_URL.format(page=page))
            out += d.get("items", [])
            if page >= d.get("pagination", {}).get("nb_pages", 1):
                break
            page += 1
    except Exception as e:
        print(f"poller: YWH fetch failed ({e}) — skipping this cycle", file=sys.stderr)
        return []
    progs = []
    for p in out:
        lo, hi = p.get("bounty_reward_min"), p.get("bounty_reward_max")
        bounty = f"${lo}-${hi}" if (lo or hi) else ("VDP" if p.get("vdp") else "")
        progs.append({"slug": p.get("slug"), "title": p.get("title"),
                      "updated": p.get("last_update_at"), "bounty": bounty,
                      "url": f"https://yeswehack.com/programs/{p.get('slug')}"})
    return progs


def load_state():
    try:
        return json.load(open(STATE_FILE, encoding="utf-8"))
    except Exception:
        return {}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
    json.dump(state, open(STATE_FILE, "w", encoding="utf-8"), indent=2)


def _chunk(lines, limit):
    """Group lines into messages each <= limit chars, splitting on line boundaries so no
    change is ever dropped or cut in half (fixes the old truncate-to-one-message behaviour)."""
    chunks, cur = [], ""
    for ln in lines:
        piece = (cur + "\n" + ln) if cur else ln
        if len(piece) > limit:
            if cur:
                chunks.append(cur); cur = ln
            else:  # a single line longer than the limit — hard-split it
                while len(ln) > limit:
                    chunks.append(ln[:limit]); ln = ln[limit:]
                cur = ln
        else:
            cur = piece
    if cur:
        chunks.append(cur)
    return chunks


def notify(lines):
    header = "YesWeHack program changes:"
    sent = False
    dw = os.environ.get("DISCORD_WEBHOOK")
    if dw:
        try:
            chunks = _chunk([header] + lines, 1900)
            for ch in chunks:
                _post(dw, {"content": ch, "flags": 4})  # flags 4 = SUPPRESS_EMBEDS: no link previews
                time.sleep(0.6)                          # stay under Discord webhook rate limit
            sent = True; print(f"notified Discord ({len(chunks)} msg)")
        except Exception as e:
            print(f"discord notify failed: {e}", file=sys.stderr)
    tt, tc = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if tt and tc:
        try:
            for ch in _chunk([header] + lines, 3900):
                _post(f"https://api.telegram.org/bot{tt}/sendMessage",
                      {"chat_id": tc, "text": ch, "disable_web_page_preview": True})
                time.sleep(0.4)
            sent = True; print("notified Telegram")
        except Exception as e:
            print(f"telegram notify failed: {e}", file=sys.stderr)
    if not sent:
        print("poller: NO notify channel configured (set DISCORD_WEBHOOK or TELEGRAM_TOKEN+CHAT_ID)", file=sys.stderr)
        for ch in _chunk([header] + lines, 1900):
            print(ch)


def main():
    progs = ywh_programs()
    if not progs:
        print("poller: nothing fetched this cycle; state untouched, will retry next tick")
        return 0
    state = load_state()
    changes = []
    for p in progs:
        key = p["slug"]
        if not key:
            continue
        prev = state.get(key)
        tag = f" [{p['bounty']}]" if p.get("bounty") else ""
        if prev is None:
            changes.append(f"[NEW] {p['title']}{tag} — {p['url']}")
        elif prev.get("updated") != p.get("updated"):
            changes.append(f"[UPDATED] {p['title']}{tag} — {p['url']}")
        state[key] = {"updated": p.get("updated"), "title": p.get("title")}
    save_state(state)
    if changes:
        print(f"{len(changes)} change(s)")
        notify(changes)
    else:
        print(f"no changes ({len(progs)} programs tracked)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
