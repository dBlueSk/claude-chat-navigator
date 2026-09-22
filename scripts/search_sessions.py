#!/usr/bin/env python3
"""Search past Claude Code sessions for Chat Navigator.

Claude Code saves each session as a .jsonl file under ~/.claude/projects/.
This script reads those files and finds messages that match what the user
remembers. It only reads files; it never changes them. Standard library only.

Commands
  search  QUERY [QUERY ...]   find matching messages (several phrasings help)
  search  --stdin             same, but read one phrasing per line from stdin
                              (safest way to pass text that contains quotes or $)
  show    REF                 show the turns around a hit (REF comes from search)
  recent                      list recent sessions, newest first

Common options
  --project TEXT   only sessions whose project folder contains TEXT
  --since DATE     only sessions on or after DATE (YYYY-MM-DD)
  --until DATE     only sessions on or before DATE (YYYY-MM-DD)
  --include-current  also search the newest session (skipped by default,
                     because while Claude Code is running it is this one)

Safety
  Read-only: never writes, deletes, runs programs or uses the network.
  Only reads session files inside ~/.claude/projects/.
  Masks things that look like secrets (API keys, tokens, passwords) in output.
"""

import argparse
import glob
import json
import os
import re
import sys
from datetime import datetime

ROOT = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
PROJECTS = os.path.join(ROOT, "projects")

STOPWORDS = set("""a an and are as at be but by can did do does for from had has have how i if in
into is it its me my of on or our so that the their them then there these this to was we were what
when where which who why will with you your about just like where's we've i'm it's that's""".split())

TAG_BLOCK = re.compile(r"<(system-reminder|local-command-stdout|local-command-stderr|command-message|user-prompt-submit-hook)>.*?</\1>", re.S)
ANY_TAG = re.compile(r"</?[a-zA-Z][\w-]*(\s[^>]*)?>")
SELF_MARKERS = ("chat-navigator", "search_sessions.py")
SESSION_ID = re.compile(r"^[A-Za-z0-9_-]+$")

# Things that look like secrets are masked before anything is printed,
# so an old session's pasted key never re-enters a conversation.
SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    re.compile(r"\b(sk|pk|rk)-[A-Za-z0-9_-]{16,}"),            # OpenAI/Anthropic/Stripe-style keys
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),              # GitHub tokens
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),            # Slack tokens
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                      # AWS access key id
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"),                  # Google API key
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),  # JWT
    re.compile(r"(?i)\b(password|passwd|secret|api[_-]?key|token)(\s*[:=]\s*)(['\"]?)[^\s'\"]{6,}\3"),
]


def redact(text):
    for pat in SECRET_PATTERNS:
        if pat.groups >= 2:
            text = pat.sub(lambda m: m.group(1) + m.group(2) + "[hidden]", text)
        else:
            text = pat.sub("[hidden]", text)
    return text


# ---------- reading session files ----------

def session_files(include_current):
    files = glob.glob(os.path.join(PROJECTS, "*", "*.jsonl"))
    files.sort(key=os.path.getmtime, reverse=True)
    if files and not include_current:
        files = files[1:]
    return files


def clean(text):
    text = TAG_BLOCK.sub(" ", text)
    text = ANY_TAG.sub(" ", text)
    return re.sub(r"[ \t]+", " ", text).strip()


def message_text(entry):
    """Plain text of a user/assistant entry, or '' if it has none worth searching."""
    if entry.get("type") not in ("user", "assistant"):
        return ""
    if entry.get("isMeta") or entry.get("isSidechain"):
        return ""
    msg = entry.get("message")
    if not isinstance(msg, dict):
        return ""
    content = msg.get("content")
    if isinstance(content, str):
        parts = [content]
    elif isinstance(content, list):
        # Only the words people actually read: skip tool calls, tool output, hidden thinking.
        parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
    else:
        return ""
    text = redact(clean("\n".join(parts)))
    if any(m in text for m in SELF_MARKERS):
        return ""  # don't find our own searches
    return text


def load_session(path):
    """Return (info, messages). messages = list of dicts with role, text, time."""
    messages, title, cwd, first_time = [], None, None, None
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if entry.get("type") == "summary" and entry.get("summary") and not title:
                title = redact(str(entry["summary"]))
            cwd = cwd or entry.get("cwd")
            text = message_text(entry)
            if not text:
                continue
            ts = entry.get("timestamp", "")
            first_time = first_time or ts
            messages.append({"role": entry["type"], "text": text, "time": ts})
    if not title:
        first_user = next((m["text"] for m in messages if m["role"] == "user"), "")
        title = shorten(first_user, 70) or "(untitled session)"
    info = {
        "id": os.path.splitext(os.path.basename(path))[0],
        "path": path,
        "project": cwd or os.path.basename(os.path.dirname(path)),
        "date": (first_time or "")[:10] or datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%d"),
        "title": title,
    }
    return info, messages


def in_filters(info, args):
    if args.project and args.project.lower() not in info["project"].lower():
        return False
    if args.since and info["date"] and info["date"] < args.since:
        return False
    if args.until and info["date"] and info["date"] > args.until:
        return False
    return True


# ---------- helpers ----------

def shorten(text, n):
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def terms(query):
    words = re.findall(r"[\w'.-]+", query.lower())
    return [w for w in words if w not in STOPWORDS and len(w) > 1]


def score(text_lower, queries):
    best, hit_word = 0.0, None
    for q in queries:
        ts = terms(q)
        if not ts:
            continue
        found = [t for t in ts if t in text_lower]
        s = len(found) / len(ts)
        if len(ts) > 1 and q.lower().strip() in text_lower:
            s += 1.0  # the whole phrase appears
        if s > best:
            best, hit_word = s, (found[0] if found else None)
    return best, hit_word


def snippet(text, word, width=420):
    if not word:
        return shorten(text, width)
    i = text.lower().find(word)
    start = max(0, i - width // 3)
    piece = text[start : start + width]
    return ("…" if start > 0 else "") + " ".join(piece.split()) + ("…" if start + width < len(text) else "")


# ---------- commands ----------

def cmd_search(args):
    queries = list(args.queries or [])
    if args.stdin:
        queries += [line.strip() for line in sys.stdin if line.strip()]
    if not queries:
        sys.exit("Give at least one search phrase (as arguments, or one per line with --stdin).")
    hits = []
    for path in session_files(args.include_current):
        info, msgs = load_session(path)
        if not in_filters(info, args):
            continue
        per_session = []
        for i, m in enumerate(msgs):
            s, word = score(m["text"].lower(), queries)
            if s >= args.min_score:
                per_session.append((s, i, word))
        per_session.sort(key=lambda x: -x[0])
        for s, i, word in per_session[:2]:  # at most 2 hits from one session
            hits.append((s, info, i, msgs[i], word))
    hits.sort(key=lambda h: h[1]["date"], reverse=True)  # newer first on ties
    hits.sort(key=lambda h: -h[0])                          # best match first
    hits = hits[: args.limit]
    if not hits:
        print("NO MATCHES. Try other words, a broader idea, or fewer filters.")
        return
    for s, info, i, m, word in hits:
        strength = "strong" if s >= 1.0 else "partial"
        print("=" * 60)
        print(f"TITLE:   {info['title']}")
        print(f"DATE:    {info['date']}    PROJECT: {info['project']}")
        print(f"MATCH:   {strength} ({s:.2f})    SPEAKER: {m['role']}")
        print(f"REF:     {info['id']}:{i}")
        print(f"TEXT:    {snippet(m['text'], word)}")
    print("=" * 60)
    print(f"{len(hits)} result(s). Use: show REF   to read the turns around one.")


def find_session(sid):
    # Only plain session ids, and only files that really live under ~/.claude/projects/.
    if not SESSION_ID.match(sid):
        return None
    root = os.path.realpath(PROJECTS) + os.sep
    for path in glob.glob(os.path.join(PROJECTS, "*", sid + "*.jsonl")):
        if os.path.realpath(path).startswith(root):
            return path
    return None


def cmd_show(args):
    sid, _, idx = args.ref.partition(":")
    path = find_session(sid)
    if not path:
        sys.exit(f"Session {sid} not found.")
    info, msgs = load_session(path)
    try:
        idx = int(idx or 0)
    except ValueError:
        sys.exit("REF should look like SESSION:NUMBER, as printed by search.")
    lo, hi = max(0, idx - args.before), min(len(msgs), idx + args.after + 1)
    print(f"TITLE: {info['title']}\nDATE:  {info['date']}    PROJECT: {info['project']}\nFILE:  {info['path']}\n")
    for j in range(lo, hi):
        mark = ">>" if j == idx else "  "
        body = msgs[j]["text"] if args.full else shorten(msgs[j]["text"], args.max_chars)
        print(f"{mark} [{j}] {msgs[j]['role'].upper()}: {body}\n")


def cmd_recent(args):
    shown = 0
    for path in session_files(args.include_current):
        info, msgs = load_session(path)
        if not msgs or not in_filters(info, args):
            continue
        print(f"{info['date']}  {info['id'][:8]}  {shorten(info['title'], 60):<60}  {info['project']}")
        shown += 1
        if shown >= args.limit:
            break
    if not shown:
        print("No sessions found.")


def main():
    if not os.path.isdir(PROJECTS):
        sys.exit(f"No Claude Code sessions folder found at {PROJECTS}.")
    p = argparse.ArgumentParser(description="Search past Claude Code sessions.")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--project")
        sp.add_argument("--since")
        sp.add_argument("--until")
        sp.add_argument("--include-current", action="store_true")

    s = sub.add_parser("search"); s.add_argument("queries", nargs="*"); common(s)
    s.add_argument("--stdin", action="store_true", help="read phrasings from stdin, one per line")
    s.add_argument("--limit", type=int, default=6)
    s.add_argument("--min-score", type=float, default=0.5)
    s.set_defaults(func=cmd_search)

    sh = sub.add_parser("show"); sh.add_argument("ref")
    sh.add_argument("--before", type=int, default=2); sh.add_argument("--after", type=int, default=2)
    sh.add_argument("--max-chars", type=int, default=1500); sh.add_argument("--full", action="store_true")
    sh.set_defaults(func=cmd_show)

    r = sub.add_parser("recent"); common(r); r.add_argument("--limit", type=int, default=15)
    r.set_defaults(func=cmd_recent)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
