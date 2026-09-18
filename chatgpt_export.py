#!/usr/bin/env python3
"""Download your ChatGPT conversation history as JSON.

Uses the same private backend API as the chatgpt.com web app, authenticated
with your browser session cookie (see README.md / config.example.json).

Usage:
    python chatgpt_export.py check            # verify authentication
    python chatgpt_export.py list             # list conversations (id, date, title)
    python chatgpt_export.py export           # download everything (incremental)
    python chatgpt_export.py export -n 10     # download the 10 most recent conversations
    python chatgpt_export.py export -t rabbit # download conversations whose title contains "rabbit"
    python chatgpt_export.py export -s "electric cords"  # conversations containing a string
    python chatgpt_export.py export --full    # re-download even unchanged conversations
    python chatgpt_export.py get <id>         # print one conversation
"""
import argparse
import itertools
import json
import re
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

from curl_cffi import requests

BASE = "https://chatgpt.com"
HERE = Path(__file__).resolve().parent


def load_config(path: Path) -> dict:
    if not path.exists():
        sys.exit(f"Config file not found: {path}\nCopy config.example.json to config.json and fill it in.")
    cfg = json.loads(path.read_text())
    if not (cfg.get("cookie") or cfg.get("session_token") or cfg.get("access_token")):
        sys.exit("Config needs one of: 'cookie', 'session_token' or 'access_token'.")
    return cfg


class ChatGPT:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.delay = float(cfg.get("delay", 1.0))
        self.s = requests.Session(impersonate=cfg.get("impersonate", "chrome"))
        self.s.headers.update({
            "Accept": "application/json",
            "Referer": BASE + "/",
            "Origin": BASE,
            "oai-device-id": cfg.get("device_id") or str(uuid.uuid4()),
            "oai-language": "en-US",
        })
        if cfg.get("user_agent"):
            self.s.headers["User-Agent"] = cfg["user_agent"]
        if cfg.get("cookie"):
            # Full "Cookie:" header copied from the browser devtools.
            self.s.headers["Cookie"] = cfg["cookie"]
        elif cfg.get("session_token"):
            self.s.cookies.set("__Secure-next-auth.session-token", cfg["session_token"], domain="chatgpt.com")
        self.access_token = cfg.get("access_token")
        self.account_id = cfg.get("account_id")

    def authenticate(self) -> dict:
        """Exchange the session cookie for a short-lived bearer access token."""
        session = {}
        if self.cfg.get("cookie") or self.cfg.get("session_token"):
            r = self.s.get(BASE + "/api/auth/session")
            if r.status_code != 200:
                raise RuntimeError(f"/api/auth/session -> HTTP {r.status_code}: {r.text[:300]}")
            session = r.json()
            if not session.get("accessToken"):
                raise RuntimeError("No accessToken in /api/auth/session response: session cookie expired or invalid.")
            self.access_token = session["accessToken"]
        self.s.headers["Authorization"] = f"Bearer {self.access_token}"
        if self.account_id:
            self.s.headers["chatgpt-account-id"] = self.account_id
        return session

    def get(self, path: str, params: dict | None = None, retries: int = 5):
        for attempt in range(retries):
            r = self.s.get(BASE + path, params=params)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 401 and attempt == 0 and (self.cfg.get("cookie") or self.cfg.get("session_token")):
                self.authenticate()  # token expired: refresh once
                continue
            if r.status_code in (429, 500, 502, 503, 504):
                wait = int(r.headers.get("retry-after") or 0) or min(60, 5 * 2 ** attempt)
                print(f"  HTTP {r.status_code} on {path}, retrying in {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            raise RuntimeError(f"GET {path} -> HTTP {r.status_code}: {r.text[:300]}")
        raise RuntimeError(f"GET {path}: too many retries")

    def iter_conversations(self, archived: bool = False, page_size: int = 100):
        offset = 0
        while True:
            params = {"offset": offset, "limit": page_size, "order": "updated"}
            if archived:
                params["is_archived"] = "true"
            data = self.get("/backend-api/conversations", params)
            items = data.get("items") or []
            yield from items
            offset += len(items)
            # "total" is unreliable (it can be far below the real count), so page until exhausted.
            if len(items) < page_size:
                break
            time.sleep(self.delay)

    def search(self, query: str):
        """Full-text search, as in the web app's sidebar search."""
        cursor = None
        while True:
            params = {"query": query}
            if cursor:
                params["cursor"] = cursor
            data = self.get("/backend-api/conversations/search", params)
            yield from data.get("items") or []
            cursor = data.get("cursor")
            if not cursor:
                break
            time.sleep(self.delay)

    def conversation(self, conv_id: str) -> dict:
        return self.get(f"/backend-api/conversation/{conv_id}")


def cmd_check(api: ChatGPT, args):
    session = api.authenticate()
    user = session.get("user", {})
    print(f"Authenticated as: {user.get('name')} <{user.get('email')}>" if user else "Authenticated (access token).")
    print(f"Session expires: {session.get('expires')}")
    data = api.get("/backend-api/conversations", {"offset": 0, "limit": 1, "order": "updated"})
    print(f"API OK, latest conversation: {(data.get('items') or [{}])[0].get('title')}")


def cmd_list(api: ChatGPT, args):
    api.authenticate()
    for c in api.iter_conversations(archived=args.archived):
        print(f"{c['id']}  {str(c.get('update_time'))[:19]}  {c.get('title')}")


def cmd_get(api: ChatGPT, args):
    api.authenticate()
    print(json.dumps(api.conversation(args.id), ensure_ascii=False, indent=2))


def to_epoch(t) -> float | None:
    """Listing gives ISO strings, search gives epoch floats: normalize to epoch."""
    if t is None or isinstance(t, (int, float)):
        return t
    return datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp()


def conversation_text(conv: dict) -> str:
    """All text of a conversation (title + every message, all branches)."""
    texts = [conv.get("title") or ""]
    for node in conv.get("mapping", {}).values():
        msg = node.get("message") or {}
        for part in (msg.get("content") or {}).get("parts") or []:
            if isinstance(part, str):
                texts.append(part)
    return "\n".join(texts)


def select_candidates(api: ChatGPT, args) -> list[dict]:
    """Return [{id, title, update_time}] of conversations to download."""
    if args.contains:
        cands = [{"id": c["conversation_id"], "title": c.get("title"), "update_time": to_epoch(c.get("update_time"))}
                 for c in api.search(args.contains)]
        print(f"{len(cands)} conversations returned by search for {args.contains!r}")
        return cands
    if args.title:
        if re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", args.title):
            return [{"id": args.title, "title": args.title, "update_time": None}]
        # Paging the whole listing is slow (~7s per 100): use search, then filter on titles.
        needle = args.title.casefold()
        cands = [{"id": c["conversation_id"], "title": c.get("title"), "update_time": c.get("update_time")}
                 for c in api.search(args.title) if needle in (c.get("title") or "").casefold()]
        cands = list({c["id"]: c for c in cands}.values())
    else:
        n = args.last or None  # 0 = everything
        listing = api.iter_conversations(page_size=min(100, n or 100))
        cands = list(itertools.islice(listing, n))
        if args.archived and not n:
            seen = {c["id"] for c in cands}
            cands += [c for c in api.iter_conversations(archived=True) if c["id"] not in seen]
        cands = [{"id": c["id"], "title": c.get("title"), "update_time": to_epoch(c.get("update_time"))}
                 for c in cands]
    print(f"{len(cands)} conversations selected")
    return cands


def cmd_export(api: ChatGPT, args):
    api.authenticate()
    out = Path(args.output)
    conv_dir = out / "conversations"
    conv_dir.mkdir(parents=True, exist_ok=True)
    index_path = out / "index.json"
    index = {}
    if index_path.exists():
        index = {c["id"]: c for c in json.loads(index_path.read_text()) if isinstance(c.get("update_time"), (int, float))}

    cands = select_candidates(api, args)
    done = skipped = failed = nomatch = 0
    for i, c in enumerate(cands, 1):
        cid = c["id"]
        path = conv_dir / f"{cid}.json"
        prev = index.get(cid)
        if (not args.full and prev and path.exists() and c["update_time"] is not None
                and abs(prev["update_time"] - c["update_time"]) < 1):
            conv = None
        else:
            try:
                conv = api.conversation(cid)
            except RuntimeError as e:
                print(f"[{i}/{len(cands)}] FAILED {cid}: {e}", file=sys.stderr)
                failed += 1
                continue
            time.sleep(api.delay)
        if args.contains:
            # Server search is fuzzy: keep only conversations really containing the string.
            text = conversation_text(conv or json.loads(path.read_text()))
            if args.contains.casefold() not in text.casefold():
                print(f"[{i}/{len(cands)}] no exact match, ignored: {c['title']}")
                nomatch += 1
                continue
        if conv is None:
            skipped += 1
        else:
            path.write_text(json.dumps(conv, ensure_ascii=False, indent=2))
            if c["update_time"] is None:  # selected by id: take metadata from the conversation itself
                c.update(title=conv.get("title"), update_time=conv.get("update_time"))
            done += 1
            print(f"[{i}/{len(cands)}] {c['title'] or cid}")
        index[cid] = c

    ordered = sorted(index.values(), key=lambda c: c["update_time"] or 0, reverse=True)
    index_path.write_text(json.dumps(ordered, ensure_ascii=False, indent=2))
    if args.single_file:
        all_convs = [json.loads((conv_dir / f"{c['id']}.json").read_text())
                     for c in ordered if (conv_dir / f"{c['id']}.json").exists()]
        (out / "conversations.json").write_text(json.dumps(all_convs, ensure_ascii=False, indent=2))
    extra = f", {nomatch} without exact match" if args.contains else ""
    print(f"Done: {done} downloaded, {skipped} unchanged, {failed} failed{extra} -> {out}")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-c", "--config", default=str(HERE / "config.json"))
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check").set_defaults(func=cmd_check)
    sp = sub.add_parser("list")
    sp.add_argument("--archived", action="store_true", help="list archived conversations instead")
    sp.set_defaults(func=cmd_list)
    sp = sub.add_parser("get")
    sp.add_argument("id")
    sp.set_defaults(func=cmd_get)
    sp = sub.add_parser("export")
    sp.add_argument("-o", "--output", default=str(HERE / "export"))
    sp.add_argument("--full", action="store_true", help="re-download even unchanged conversations")
    sp.add_argument("--archived", action="store_true", help="also include archived conversations")
    sel = sp.add_mutually_exclusive_group()
    sel.add_argument("-n", "--last", type=int, default=0, metavar="N",
                     help="the N most recently updated conversations (default 0 = all)")
    sel.add_argument("-t", "--title", metavar="STR",
                     help="conversations whose title contains STR (case-insensitive), or with this exact id")
    sel.add_argument("-s", "--contains", metavar="STR",
                     help="conversations whose title or messages contain STR (case-insensitive)")
    sp.add_argument("--single-file", action="store_true", help="also write all conversations to conversations.json")
    sp.set_defaults(func=cmd_export)
    args = p.parse_args()

    api = ChatGPT(load_config(Path(args.config)))
    try:
        args.func(api, args)
    except RuntimeError as e:
        sys.exit(f"Error: {e}")


if __name__ == "__main__":
    main()
