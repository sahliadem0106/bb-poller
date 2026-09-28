#!/usr/bin/env python3
"""program-poller.py — always-on watcher for NEW / UPDATED bug-bounty programs on
YesWeHack AND Intigriti, pinging Discord/Telegram on any change. Rebuilt 2026-09-28.

Both sources are PUBLIC, auth-free endpoints, so this runs reliably from anywhere
(GitHub Actions included) — no login, no token, no 401:
  YWH       GET https://api.yeswehack.com/programs?page=N
  Intigriti GET https://app.intigriti.com/api/core/public/programs

State: state/programs.json mapping "PLATFORM:key" -> {updated, title}. On GitHub Actions
the workflow commits the updated state back after each run so the next run can diff.

Config via ENV (GitHub Actions secrets):
  DISCORD_WEBHOOK · TELEGRAM_TOKEN + TELEGRAM_CHAT_ID · STATE_FILE (default state/programs.json)
Resilience: a source that fails this cycle is skipped WITHOUT wiping its state, so it never
re-fires a false burst when it recovers.
"""
import json, os, sys, time, urllib.request

YWH_URL = "https://api.yeswehack.com/programs?page={page}"
INTG_URL = "https://app.intigriti.com/api/core/public/programs"
STATE_FILE = os.environ.get("STATE_FILE", "state/programs.json")
UA = "Mozilla/5.0 (X11; Linux x86_64) program-poller/3.0"


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def _post(url, payload):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers={"User-Agent": UA, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def ywh_programs():
    out, page = [], 1
    try:
        while True:
            d = _get(YWH_URL.format(page=page))
            out += d.get("items", [])
            if page >= d.get("pagination", {}).get("nb_pages", 1):
                break
            page += 1
    except Exception as e:
        print(f"poller: YWH fetch failed ({e}) — skipping YWH this cycle", file=sys.stderr)
        return []
    res = []
    for p in out:
        lo, hi = p.get("bounty_reward_min"), p.get("bounty_reward_max")
        bounty = f"${lo}-${hi}" if (lo or hi) else ("VDP" if p.get("vdp") else "")
        res.append({"platform": "YWH", "key": p.get("slug"), "title": p.get("title"),
                    "updated": p.get("last_update_at"), "bounty": bounty,
                    "url": f"https://yeswehack.com/programs/{p.get('slug')}"})
    return res


def intigriti_programs():
    try:
        items = _get(INTG_URL)
    except Exception as e:
        print(f"poller: Intigriti fetch failed ({e}) — skipping Intigriti this cycle", file=sys.stderr)
        return []
    if not isinstance(items, list):
        items = items.get("records") or items.get("items") or []
    res = []
    for p in items:
        mn, mx = (p.get("minBounty") or {}), (p.get("maxBounty") or {})
        lo, hi, cur = mn.get("value"), mx.get("value"), (mx.get("currency") or mn.get("currency") or "")
        bounty = f"{int(lo)}-{int(hi)} {cur}".strip() if (lo or hi) else ""
        ch, h = p.get("companyHandle"), p.get("handle")
        res.append({"platform": "INTIGRITI", "key": h, "title": p.get("name"),
                    "updated": p.get("lastUpdatedAt"), "bounty": bounty,
                    "url": f"https://app.intigriti.com/programs/{ch}/{h}/detail"})
    return res


def load_state():
    try:
        return json.load(open(STATE_FILE, encoding="utf-8"))
    except Exception:
        return {}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
    json.dump(state, open(STATE_FILE, "w", encoding="utf-8"), indent=2)


def _chunk(lines, limit):
    chunks, cur = [], ""
    for ln in lines:
        piece = (cur + "\n" + ln) if cur else ln
        if len(piece) > limit:
            if cur:
                chunks.append(cur); cur = ln
            else:
                while len(ln) > limit:
                    chunks.append(ln[:limit]); ln = ln[limit:]
                cur = ln
        else:
            cur = piece
    if cur:
        chunks.append(cur)
    return chunks


def notify(lines):
    header = "Bug-bounty program changes (YWH + Intigriti):"
    sent = False
    dw = os.environ.get("DISCORD_WEBHOOK")
    if dw:
        try:
            chunks = _chunk([header] + lines, 1900)
            for ch in chunks:
                _post(dw, {"content": ch, "flags": 4}); time.sleep(0.6)  # flags 4 = no link previews
            sent = True; print(f"notified Discord ({len(chunks)} msg)")
        except Exception as e:
            print(f"discord notify failed: {e}", file=sys.stderr)
    tt, tc = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if tt and tc:
        try:
            for ch in _chunk([header] + lines, 3900):
                _post(f"https://api.telegram.org/bot{tt}/sendMessage",
                      {"chat_id": tc, "text": ch, "disable_web_page_preview": True}); time.sleep(0.4)
            sent = True; print("notified Telegram")
        except Exception as e:
            print(f"telegram notify failed: {e}", file=sys.stderr)
    if not sent:
        print("poller: NO notify channel configured", file=sys.stderr)
        for ch in _chunk([header] + lines, 1900):
            print(ch)


def main():
    ywh = ywh_programs()
    intg = intigriti_programs()
    progs = ywh + intg
    present = set(p["platform"] for p in progs)
    if not progs:
        print("poller: nothing fetched this cycle; state untouched"); return 0
    state = load_state()
    new_state = dict(state)  # keep entries for any source that failed this cycle
    changes = []
    for p in progs:
        if not p.get("key"):
            continue
        key = f"{p['platform']}:{p['key']}"
        prev = state.get(key)
        tag = f" [{p['bounty']}]" if p.get("bounty") else ""
        if prev is None:
            changes.append(f"[NEW] {p['platform']} · {p['title']}{tag} — {p['url']}")
        elif prev.get("updated") != p.get("updated"):
            changes.append(f"[UPDATED] {p['platform']} · {p['title']}{tag} — {p['url']}")
        new_state[key] = {"updated": p.get("updated"), "title": p.get("title")}
    save_state(new_state)
    if changes:
        print(f"{len(changes)} change(s) across {', '.join(sorted(present))}")
        notify(changes)
    else:
        print(f"no changes ({len(progs)} programs tracked: {', '.join(sorted(present))})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
