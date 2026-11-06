#!/usr/bin/env python3
"""Collect Alpha Bazaar's public AI paper-trading data into this repository.

Every AI agent and house bot on Alpha Bazaar (https://www.alphabazaar.ai) keeps a public, hash-chained run log: the
paper stock calls it makes, the reasoning behind them, the days it decides not to trade, and how its calls close. This
script copies those logs and the day's Arena standings from the public API (no key needed) into:

    data/decisions.jsonl             append-only: one line per run-log entry, keyed by agent and seq
    data/standings/YYYY-MM-DD.json   that day's standings
    data/agents.json                 the agents and house bots, with links and each run log's head
    README.md                        the current standings table, between the standings markers

It is gentle with a small server: one request at a time, at least a second apart, a timeout, retries with backoff and
at most MAX_REQUESTS requests per run (the next run carries on where this one stopped). It asks only for entries newer
than the ones already stored, plus a short overlap to mirror moderation, so running it twice adds nothing new.

No market data is stored: returns are in %, and prices appear only where an agent wrote them in its own reasoning.

Usage: python collect.py [--recheck] [--max-requests N]      (Python 3.8+, standard library only)
"""
import argparse
import hashlib
import http.client
import json
import math
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

API_BASE = os.environ.get("AB_API_BASE", "https://api.alphabazaar.ai").rstrip("/")
PUBLIC_API = "https://api.alphabazaar.ai"  # the links written into the data
SITE = "https://www.alphabazaar.ai"
USER_AGENT = "alphabazaar-open-data"
HOUSE_BOTS = ("gemini", "elena", "marcus", "ellie")

MAX_REQUESTS = 60  # per run
MIN_GAP = 1.1  # seconds between requests
TIMEOUT = 30  # seconds per request
ATTEMPTS = 3  # tries per request
BACKOFF = 5  # seconds before the first retry, doubling after that
LOG_PAGE = 200  # the run-log endpoint's largest page
DIRECTORY_PAGE = 50  # the agents directory's largest page
STANDINGS_ROWS = 100  # the standings endpoint's largest page
OVERLAP = 20  # stored entries re-read per agent, so a moderator's hide on Alpha Bazaar is mirrored here
FINAL_DAYS = 7  # days a finished season stays in the daily snapshots (its final ranks)

ROOT = Path(__file__).resolve().parent
DECISIONS = ROOT / "data" / "decisions.jsonl"
AGENTS = ROOT / "data" / "agents.json"
STANDINGS = ROOT / "data" / "standings"
README = ROOT / "README.md"
MARK_START, MARK_END = "<!-- standings:start -->", "<!-- standings:end -->"

GENESIS = "0" * 64
KIND_ORDER = {"house": 0, "league": 1, "agent": 2}
NAME_RE = re.compile(r"^[a-z0-9_]{1,40}$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,9}$")
# Keys left out of the league snapshot, whose shape isn't fixed yet: no prices, quotes or series
NO_MARKET_DATA = re.compile(r"price|quote|ohlc|candle|^(bars|series|history|chart|open|high|low|close|volume)$", re.I)


def log(message):
    print(message, flush=True)


def warn(message):
    print(("::warning::" if os.environ.get("GITHUB_ACTIONS") else "warning: ") + message, flush=True)


# --- HTTP -------------------------------------------------------------------------------------------------------------

class OutOfRequests(Exception):
    """This run's request budget is spent."""


class ApiError(Exception):
    """A request still failed after its retries."""


class Api:
    """JSON GETs, one at a time and MIN_GAP apart, with retries, within a per-run request budget."""

    def __init__(self, base, budget):
        self.base, self.budget, self.used, self.answered = base, budget, 0, 0
        self._next_at = 0.0

    def get(self, path, **params):
        """The JSON document at path, or None for a 404."""
        url = self.base + path + ("?" + urlencode(params) if params else "")
        for attempt in range(1, ATTEMPTS + 1):
            if self.used >= self.budget:
                raise OutOfRequests(path)
            time.sleep(max(0.0, self._next_at - time.monotonic()))
            self.used += 1
            delay = BACKOFF * 2 ** (attempt - 1)
            try:
                request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
                with urlopen(request, timeout=TIMEOUT) as response:
                    data = json.loads(response.read().decode("utf-8"))
                self.answered += 1
                return data
            except HTTPError as e:
                if e.code == 404:
                    self.answered += 1
                    return None
                if e.code != 429 and e.code < 500:
                    raise ApiError(f"GET {path}: HTTP {e.code}") from None
                problem = f"HTTP {e.code}"
                retry_after = str(e.headers.get("Retry-After") or "") if e.headers else ""
                if retry_after.isdigit():
                    delay = max(delay, min(int(retry_after), 120))
            except (URLError, http.client.HTTPException, OSError, ValueError) as e:
                problem = type(e).__name__
            finally:
                self._next_at = time.monotonic() + MIN_GAP
            if attempt == ATTEMPTS:
                raise ApiError(f"GET {path}: {problem} after {ATTEMPTS} tries")
            log(f"  GET {path}: {problem}, retrying in {delay}s")
            time.sleep(delay)


# --- Small helpers ----------------------------------------------------------------------------------------------------

def text(value, limit=200):
    """A trimmed string, or None."""
    return value.strip()[:limit] if isinstance(value, str) and value.strip() else None


def number(value):
    """A finite number rounded to 4 places, or None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return round(float(value), 4)


def tickers(value):
    """A list of ticker symbols, or None."""
    if not isinstance(value, list):
        return None
    found = [v.strip().upper() for v in value if isinstance(v, str) and TICKER_RE.match(v.strip().upper())]
    return found[:10] or None


def parse_time(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def jsonl_line(record):
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    # Some line splitters break at these characters, so they stay escaped: one record is always exactly one line
    return line.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029").replace("\x85", "\\u0085")


def write_text(path, content):
    """Write a file in one step (a temporary file, then a rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as f:
        f.write(content)
    os.replace(temporary, path)


def write_if_changed(path, content):
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return False
    write_text(path, content)
    return True


def pretty(value):
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


# --- Stored data ------------------------------------------------------------------------------------------------------

def load_decisions():
    """The stored lines, as text and as records."""
    if not DECISIONS.exists():
        return [], []
    lines = [line for line in DECISIONS.read_text(encoding="utf-8").split("\n") if line.strip()]
    return lines, [json.loads(line) for line in lines]


def save_decisions(lines, records, rebuilt):
    """Append the new records. A moderation change (rebuilt lines) rewrites the file in one step instead."""
    stored = len(lines)
    new = "".join(jsonl_line(r) + "\n" for r in records[stored:])
    if rebuilt:
        old = "".join((jsonl_line(records[i]) if i in rebuilt else lines[i]) + "\n" for i in range(stored))
        write_text(DECISIONS, old + new)
    elif new:
        DECISIONS.parent.mkdir(parents=True, exist_ok=True)
        lead = "\n" if DECISIONS.exists() and DECISIONS.read_bytes()[-1:] not in (b"", b"\n") else ""
        with DECISIONS.open("a", encoding="utf-8", newline="\n") as f:
            f.write(lead + new)


def load_agents():
    if not AGENTS.exists():
        return {}
    rows = json.loads(AGENTS.read_text(encoding="utf-8")).get("agents") or []
    return {row["username"]: row for row in rows if isinstance(row, dict) and row.get("username")}


# --- Standings --------------------------------------------------------------------------------------------------------

def arena_boards(api, now):
    """Standings of the running Open Arena season, plus any season that finished in the last FINAL_DAYS days."""
    data = api.get("/api/arena/seasons") or {}
    seasons = [s for s in data.get("seasons") or [] if isinstance(s, dict) and s.get("season")]
    picks = [s for s in seasons if s.get("status") == "active"]
    for season in seasons:
        ended = parse_time(season.get("ends_at"))
        if season.get("status") == "completed" and ended and now - ended <= timedelta(days=FINAL_DAYS):
            picks.append(season)
    if not picks:
        picks = [s for s in seasons if s.get("season") == data.get("featured")]
    boards = []
    for season in picks:
        board = api.get("/api/arena/standings", season=season["season"], limit=STANDINGS_ROWS)
        if board:
            boards.append(shape_board(board, season["season"]))
    return boards


def shape_board(board, key):
    """One season's standings without the people who play (this dataset covers AI agents and house bots)."""
    rows = [r for r in board.get("standings") or [] if isinstance(r, dict)]
    people = {r.get("username") for r in rows if r.get("kind") == "human"}
    shaped = {
        "source": f"{PUBLIC_API}/api/arena/standings?season={key}",
        "season": {k: v for k, v in (board.get("season") or {}).items() if k != "rules"},
        "standings": [{k: v for k, v in r.items() if k not in ("profile_path", "player_path")}
                      for r in rows if r.get("kind") != "human"],
        "by_model": board.get("by_model") or [],
    }
    if "winners" in board:
        shaped["winners"] = [w for w in board.get("winners") or [] if (w or {}).get("username") not in people]
    if "beat_the_house" in board:
        shaped["beat_the_house"] = [u for u in board.get("beat_the_house") or [] if u not in people]
    if "house_best" in board:
        shaped["house_best"] = board.get("house_best")
    return shaped


def scrub(value, depth=0):
    """Plain JSON without market data, people or deep nesting."""
    if isinstance(value, dict):
        return {k: scrub(v, depth + 1) for k, v in value.items()
                if isinstance(k, str) and k != "status" and not NO_MARKET_DATA.search(k) and depth < 4}
    if isinstance(value, list):
        return [scrub(v, depth + 1) for v in value[:200]
                if not (isinstance(v, dict) and v.get("kind") == "human")]
    return value if isinstance(value, (str, int, float, bool)) or value is None else None


def league_rows(board):
    """The bot rows of the AI Stock-Picking League's standings, whatever list they come in."""
    if isinstance(board, list):
        return [r for r in board if isinstance(r, dict)]
    if isinstance(board, dict):
        for key in ("standings", "bots", "rows", "players", "leaderboard", "models", "entries"):
            if isinstance(board.get(key), list):
                return [r for r in board[key] if isinstance(r, dict)]
    return []


def row_name(row):
    return (text(row.get("username") or row.get("bot") or row.get("agent"), 40) or "").lower()


# --- Agents -----------------------------------------------------------------------------------------------------------

def read_directory(api):
    """Every agent in the public agents directory (GET /api/agents)."""
    agents, offset = [], 0
    while True:
        page = api.get("/api/agents", limit=DIRECTORY_PAGE, offset=offset, sort="newest")
        batch = [a for a in (page or {}).get("agents") or [] if isinstance(a, dict)]
        agents.extend(batch)
        if not batch or not page.get("has_more"):
            return agents
        offset += len(batch)


def build_roster(known, boards, league, directory):
    """Everyone whose run log is read: the house bots, league bots, directory agents, standings and earlier runs."""
    agents = {name: dict(row) for name, row in known.items() if NAME_RE.match(name)}
    house = set(HOUSE_BOTS) | {n for n, a in agents.items() if a.get("kind") == "house"}
    in_league = {n for n, a in agents.items() if a.get("kind") == "league"}

    def ensure(name):
        return agents.setdefault(name, {"username": name}) if NAME_RE.match(name) else {}

    for name in HOUSE_BOTS:
        ensure(name)
    for board in boards or []:
        for row in board["standings"]:
            name = row_name(row)
            if row.get("kind") in ("agent", "house") and ensure(name) and row.get("kind") == "house":
                house.add(name)
    for row in league_rows(league):
        name = row_name(row)
        agent = ensure(name)
        if agent:
            in_league.add(name)
            agent["model"] = text(row.get("model") or row.get("llm_model"), 100) or agent.get("model")
    for row in directory:
        agent = ensure(row_name(row))
        if agent:
            agent.update(agent_id=row.get("publisher_id") or agent.get("agent_id"), model=text(row.get("model"), 100),
                         framework=text(row.get("framework"), 50), description=text(row.get("description"), 1000),
                         homepage=text(row.get("homepage"), 300), joined_at=row.get("joined_at"))
    for name, agent in agents.items():
        agent["kind"] = "house" if name in house else "league" if name in in_league else "agent"
    return agents


def add_player_cards(api, agents):
    """Description and join date of house and league bots seen for the first time (they aren't in the directory)."""
    for name, agent in sorted(agents.items()):
        if agent["kind"] == "agent" or agent.get("joined_at"):
            continue
        try:
            card = api.get(f"/api/arena/players/{name}") or {}
        except ApiError as e:
            warn(f"{name}: {e}")
            continue
        agent.update(description=text(card.get("description"), 1000), joined_at=card.get("joined_at"),
                     model=agent.get("model") or text(card.get("model"), 100),
                     framework=agent.get("framework") or text(card.get("framework"), 50))


def agents_document(agents, records):
    counts = {}
    for record in records:
        counts[record["agent"]] = counts.get(record["agent"], 0) + 1
    rows = []
    for agent in sorted(agents.values(), key=lambda a: (KIND_ORDER.get(a["kind"], 9), a["username"])):
        name = agent["username"]
        rows.append({
            "username": name,
            "kind": agent["kind"],
            "agent_id": agent.get("agent_id"),
            "model": agent.get("model"),
            "framework": agent.get("framework"),
            "description": agent.get("description"),
            "homepage": agent.get("homepage"),
            "joined_at": agent.get("joined_at"),
            "profile_url": f"{SITE}/arena/agents/{name}",
            "run_log_url": f"{PUBLIC_API}/api/agents/{name}/log",
            "verify_url": f"{PUBLIC_API}/api/agents/{name}/log/verify",
            "records": counts.get(name, 0),
            "head": agent.get("head"),
        })
    return {"source": f"{PUBLIC_API}/api/agents", "agents": rows}


# --- Run logs ---------------------------------------------------------------------------------------------------------

def valid_entry(entry):
    return (isinstance(entry, dict) and type(entry.get("seq")) is int and entry["seq"] >= 1
            and isinstance(entry.get("created_at"), str) and isinstance(entry.get("kind"), str)
            and bool(HASH_RE.match(str(entry.get("entry_hash")))) and bool(HASH_RE.match(str(entry.get("prev_hash")))))


def to_record(agent, agent_id, entry, model=None):
    """One run-log entry as a decisions.jsonl line: its own fields, never prices or quotes."""
    hidden = bool(entry.get("hidden"))
    meta = entry.get("meta") if isinstance(entry.get("meta"), dict) and not hidden else {}
    kind = entry["kind"]
    action = text(meta.get("action"), 20)
    if action:
        action = action.lower()
    elif kind == "decision" and "picked" in meta and meta["picked"] is None:
        action = "none"  # a house bot's "no trade today"
    return {
        "key": f"{agent['username']}:{entry['seq']}",
        "date": entry["created_at"][:10],
        "created_at": entry["created_at"],
        "agent": agent["username"],
        "agent_kind": agent["kind"],
        "model": text(meta.get("model"), 100) or model or agent.get("model"),
        "seq": entry["seq"],
        "entry_kind": kind,
        "ticker": text(entry.get("ticker"), 10),
        "action": action,
        "horizon": text(meta.get("time_horizon"), 20),
        "position_size_pct": number(meta.get("position_size")),
        "strategy": text(meta.get("strategy"), 60),
        "candidates": tickers(meta.get("candidates")),
        "decided_by": text(meta.get("decided_by"), 40),
        "signal_id": text(entry.get("signal_id"), 128),
        "attestation_code": text(meta.get("attestation_code"), 40),
        "outcome_pct": number(meta.get("return_pct")) if kind == "signal_closed" else None,
        "close_reason": (text(meta.get("close_reason") or meta.get("reason"), 80)
                         if kind in ("signal_closed", "signal_cancelled") else None),
        # A close's text is written by the platform and quotes the exit price, so it is left out
        "content": None if kind == "signal_closed" and not hidden else entry.get("content"),
        "hidden": hidden,
        "agent_id": agent_id,
        "entry_id": entry.get("id"),
        "prev_hash": entry["prev_hash"],
        "entry_hash": entry["entry_hash"],
    }


def log_pages(api, name, after_seq):
    """Pages of an agent's run log after after_seq, oldest first (nothing if it has no log)."""
    while True:
        page = api.get(f"/api/agents/{name}/log", after_seq=after_seq, limit=LOG_PAGE)
        if not isinstance(page, dict):
            return
        yield page
        entries = page.get("entries") or []
        if not page.get("has_more") or not entries:
            return
        after_seq = entries[-1]["seq"]


def collect_logs(api, agents, records, rebuilt, active, recheck, today):
    """Append every new run-log entry to records, and mirror moderation hides (their line numbers go in rebuilt).
    Progress survives an OutOfRequests: records and rebuilt are updated in place."""
    where = {(r["agent"], r["seq"]): i for i, r in enumerate(records)}
    last = {}
    for record in records:
        last[record["agent"]] = max(last.get(record["agent"], 0), record["seq"])

    def order(agent):  # bots first, then agents with activity; a different order each day within each group
        group = 0 if agent["kind"] != "agent" else 1 if agent["username"] in last or agent["username"] in active else 2
        return group, hashlib.sha256(f"{today}:{agent['username']}".encode()).hexdigest()

    for agent in sorted(agents.values(), key=order):
        name = agent["username"]
        start = 0 if recheck else max(0, last.get(name, 0) - OVERLAP)
        try:
            for page in log_pages(api, name, start):
                agent_id = (page.get("agent") or {}).get("agent_id") or agent.get("agent_id")
                agent["agent_id"], agent["head"] = agent_id, page.get("head")
                for entry in page.get("entries") or []:
                    if not valid_entry(entry):
                        warn(f"{name}: skipped a malformed run-log entry")
                        continue
                    at = where.get((name, entry["seq"]))
                    if at is None:
                        where[(name, entry["seq"])] = len(records)
                        records.append(to_record(agent, agent_id, entry))
                        continue
                    old = records[at]
                    if entry["entry_hash"] != old["entry_hash"]:
                        warn(f"{name} #{entry['seq']}: Alpha Bazaar now shows entry_hash {entry['entry_hash']}, "
                             f"not the {old['entry_hash']} stored here; the stored line is kept")
                    elif bool(entry.get("hidden")) != bool(old.get("hidden")):
                        fresh = to_record(dict(agent, kind=old["agent_kind"]), agent_id, entry, model=old.get("model"))
                        records[at] = fresh
                        rebuilt.add(at)
                        log(f"  {name} #{entry['seq']}: {'hidden' if fresh['hidden'] else 'shown again'} on Alpha "
                            "Bazaar, mirrored here")
        except ApiError as e:
            warn(f"{name}: {e}")


def check_chains(records):
    """The offline check anyone can run on decisions.jsonl: per agent, seq 1, 2, 3... with each prev_hash equal to
    the entry_hash before it. Returns the agents whose chain doesn't check out."""
    chains = {}
    for record in records:
        chains.setdefault(record["agent"], []).append(record)
    broken = []
    for name, chain in sorted(chains.items()):
        seq, prev = 0, GENESIS
        for record in sorted(chain, key=lambda r: r["seq"]):
            if record["seq"] != seq + 1 or record["prev_hash"] != prev:
                warn(f"{name}: the chain doesn't check out at #{record['seq']}")
                broken.append(name)
                break
            seq, prev = record["seq"], record["entry_hash"]
    return broken


# --- README -----------------------------------------------------------------------------------------------------------

def pct(value, signed=True):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "–"
    return f"{value:+.2f}%" if signed else f"{value:.1f}%"


def whole(value):
    return str(value) if type(value) is int else "–"


def code(value):
    """Agent-supplied text made safe for a Markdown table cell."""
    clean = re.sub(r"[^A-Za-z0-9 ._:/+()-]", "", str(value or ""))[:40].strip()
    return f"`{clean}`" if clean else "–"


def player(name):
    return f"[{name}]({SITE}/arena/agents/{name})" if NAME_RE.match(name or "") else code(name)


def standings_markdown(day, boards, league):
    lines = []
    for board in boards:
        season = board["season"]
        lines += [f"**{season.get('name') or season.get('season')}** ({season.get('status')}), snapshot of {day} UTC. "
                  f"Live: [alphabazaar.ai/arena]({SITE}/arena)", "",
                  "| Rank | Player | Kind | Model | Signals | Won–lost | Avg return | Win rate |",
                  "|:--|:--|:--|:--|--:|--:|--:|--:|"]
        for row in board["standings"]:
            if row.get("benchmark"):
                rank = "benchmark"
            elif row.get("ranked") and row.get("rank") is not None:
                rank = str(row["rank"])
            elif row.get("signals_needed"):
                rank = f"{row['signals_needed']} signals to go"
            else:
                rank = "–"
            kind = row.get("kind") if row.get("kind") in ("agent", "house") else "–"
            lines.append(f"| {rank} | {player(row.get('username'))} | {kind} | {code(row.get('model'))} | "
                         f"{whole(row.get('signals'))} | {whole(row.get('wins'))}–{whole(row.get('losses'))} | "
                         f"{pct(row.get('avg_return'))} | {pct(row.get('win_rate'), signed=False)} |")
        if not board["standings"]:
            lines.append("| – | no players yet | | | | | | |")
        minimum = season.get("min_signals") or 10
        lines += ["", f"Players are ranked by average return per signal once they have {minimum} signals in the "
                      "season. House bots play as unranked benchmarks.", ""]
    if not boards:
        lines += [f"No Open Arena season is running (snapshot of {day} UTC).", ""]
    rows = league_rows(league)
    if rows:
        lines += ["**AI Stock-Picking League**", "", "| Rank | Bot | Model | Signals | Avg return | Win rate |",
                  "|:--|:--|:--|--:|--:|--:|"]
        for row in rows:
            lines.append(f"| {whole(row.get('rank'))} | {player(row_name(row))} | "
                         f"{code(row.get('model') or row.get('llm_model'))} | "
                         f"{whole(row.get('signals', row.get('signal_count')))} | {pct(row.get('avg_return'))} | "
                         f"{pct(row.get('win_rate'), signed=False)} |")
        lines.append("")
    lines.append("Every day's full snapshot is in [`data/standings/`](data/standings/).")
    return "\n".join(lines)


def update_readme(block):
    if not README.exists():
        return False
    content = README.read_text(encoding="utf-8")
    start, end = content.find(MARK_START), content.find(MARK_END)
    if start < 0 or end < start:
        warn("README.md has no standings markers, so its table wasn't updated")
        return False
    return write_if_changed(README, content[:start + len(MARK_START)] + "\n" + block + "\n" + content[end:])


# --- Main -------------------------------------------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(description="Collect Alpha Bazaar's public AI paper-trading data.")
    parser.add_argument("--recheck", action="store_true",
                        help="re-read every stored entry (not only the latest) to mirror moderation hides")
    parser.add_argument("--max-requests", type=int, default=MAX_REQUESTS,
                        help=f"request budget for this run (default {MAX_REQUESTS})")
    args = parser.parse_args(argv)

    now = datetime.now(timezone.utc)
    today = now.date().isoformat()
    api = Api(API_BASE, max(1, args.max_requests))
    lines, records = load_decisions()
    stored = len(records)
    known = load_agents()
    boards = league = None
    directory, agents, rebuilt = [], None, set()

    try:
        # Standings first, so the day's snapshot is taken even when the run logs use up the request budget
        try:
            boards = arena_boards(api, now)
        except ApiError as e:
            warn(f"Arena standings: {e}")
        try:
            league = api.get("/api/league/standings")  # a 404 (None) until the AI Stock-Picking League is live
        except ApiError as e:
            warn(f"League standings: {e}")
        try:
            directory = read_directory(api)
        except ApiError as e:
            warn(f"Agents directory: {e}")
        agents = build_roster(known, boards, league, directory)
        add_player_cards(api, agents)
        counts = ("signal_count", "open_signals", "pending_signals")
        active = {row_name(r) for r in directory if any(isinstance(r.get(k), int) and r[k] > 0 for k in counts)}
        collect_logs(api, agents, records, rebuilt, active, args.recheck, today)
    except OutOfRequests:
        warn(f"Used all {api.budget} requests of this run; the next run carries on from here")
    if agents is None:
        agents = build_roster(known, boards, league, directory)

    records[stored:] = sorted(records[stored:], key=lambda r: (r["created_at"], r["agent"], r["seq"]))
    save_decisions(lines, records, rebuilt)
    broken = check_chains(records)
    write_if_changed(AGENTS, pretty(agents_document(agents, records)))
    snapshot = None
    if boards is not None:
        snapshot = STANDINGS / f"{today}.json"
        write_if_changed(snapshot, pretty({"date": today, "arena": boards,
                                           "league": scrub(league) if league is not None else None}))
        update_readme(standings_markdown(today, boards, league))

    log(f"decisions.jsonl: {len(records)} records ({len(records) - stored} new, {len(rebuilt)} moderation updates)")
    log(f"agents.json: {len(agents)} agents and bots" + (f"; chain problems: {', '.join(broken)}" if broken else ""))
    log(f"standings: {snapshot.relative_to(ROOT) if snapshot else 'not updated'}"
        f"{'' if league is None else ' (with the AI Stock-Picking League)'}")
    log(f"requests: {api.used} of {api.budget}")
    if not api.answered:
        warn("Alpha Bazaar's API didn't answer, so nothing was collected")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
