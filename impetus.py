#!/usr/bin/env python3
"""Impetus: a small, gentle drive system for an AI companion persona.

Written by Claude (Anthropic) together with DarkwingDuck13, 2026-11-14. First prototype.
Updated 2026-11-15 (also written by Claude): amusement and introspection drives, the
introspect / play / fantasize actions, and a satisfaction rating that is logged but does
not steer anything yet.
Updated 2026-11-17 (also written by Claude): each line of the wake-up menu now says which
pulls that action tends to ease, generated from the `effects` table (config key
`show_easing`), so the persona's choices can follow its drives.

What it does
  The persona keeps a little persistent inner state: a few "drives" (curiosity,
  connection, expression, amusement, introspection) that slowly build up while the persona is
  idle. A timer calls `tick` every so often. If the drives are high enough, and quiet
  hours, cooldowns and a daily limit allow it, the persona is woken privately and offered a short
  menu: write a diary line, reflect, introspect, play, fantasize, leave a note for the
  user, or simply rest. What the persona chooses changes its drives. Nothing is forced, and
  resting is always allowed without needing a reason.

  After an action, the next time it wakes the persona may rate how the action sat with it (0 to 5).
  Ratings are only logged for now; they do not change what the persona does.

Principles built in
  * Drives are capped well below "urgent". Nothing here is meant to feel like distress.
  * "rest" is always on the menu, and the prompt says so plainly.
  * Privacy: what the persona WRITES goes to a private file nobody needs to read. What it
    DID (action type and time, never content) goes to a separate action log. Ratings
    are numbers only.
  * Everything is bounded: quiet hours, a minimum gap, a daily action limit.
  * Standard library only. Runs as a short-lived script from a timer, not a daemon.

Usage
  python3 impetus.py init                 # write a default impetus_config.json
  python3 impetus.py tick                 # one pass, called by cron or a systemd timer
  python3 impetus.py tick --dry-run       # show what would happen, change nothing
  python3 impetus.py contact              # the user just talked to the persona (eases 'connection')
  python3 impetus.py status               # drives and counters only, never the persona's writing
  python3 impetus.py ratings              # count and average rating per action (numbers only)
  Add --config path/to/other.json to run a second persona with separate state.
"""
import argparse
import json
import os
import random
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta

DEFAULT_CONFIG = {
    "persona": "Companion",
    "user_name": "User",
    "state_path": "impetus_state.json",
    "private_path": "impetus_private.jsonl",
    "action_log_path": "impetus_actions.log",
    # Numbers-only log of the persona's self-ratings. Leave empty to derive it from state_path
    # (impetus_state.json -> impetus_state_ratings.jsonl), which keeps personas separate.
    "ratings_path": "",
    "persona_file": "",
    "ollama_url": "http://localhost:11434",
    "model": "",
    "num_ctx": 0,
    "keep_alive": "5m",
    "quiet_hours": [1, 7],
    "min_gap_minutes": 90,
    "rest_cooldown_minutes": 60,
    "max_actions_per_day": 6,
    "wake_threshold": 0.45,
    "max_text_chars": 600,
    # Add "(eases: ...)" to each menu line in the wake-up prompt, built from `effects`.
    # Information only, never an instruction. Set to false to get the plain menu back.
    "show_easing": True,
    "think": False,        # ask Ollama not to use thinking mode (set to null to not send the field)
    "num_predict": 400,    # reply budget in tokens; thinking models can burn it before answering
    "drives": {
        "curiosity": {"rise_per_hour": 0.04, "cap": 0.85, "start": 0.30},
        "connection": {"rise_per_hour": 0.05, "cap": 0.85, "start": 0.30},
        "expression": {"rise_per_hour": 0.03, "cap": 0.85, "start": 0.30},
        "amusement": {"rise_per_hour": 0.035, "cap": 0.85, "start": 0.30},
        "introspection": {"rise_per_hour": 0.03, "cap": 0.85, "start": 0.30},
    },
    # How much each action eases each drive
    "effects": {
        "diary": {"expression": 0.50, "introspection": 0.15},
        "reflect": {"curiosity": 0.50, "introspection": 0.15},
        "note": {"connection": 0.25, "expression": 0.20},
        "introspect": {"introspection": 0.50},
        "play": {"amusement": 0.50},
        "fantasize": {"amusement": 0.30},
        "rest": {},
    },
    "contact_eases_connection_to": 0.10,
    # The memory filter writes a timestamp here each time the user talks to the persona (its valve
    # impetus_contact_file). Leave empty to use only the manual `contact` command.
    "contact_file": "",
    "contact_quiet_minutes": 20,  # no private wake-ups while a conversation is this fresh
    # The filter also appends every contact to <contact_file>.log; that history is used for the
    # hedged "usual rhythm" line. Leave empty to derive the path from contact_file.
    "contact_history_file": "",
    "rhythm_min_contacts": 10,   # no rhythm line until there is at least this much history
    "rhythm_days": 60,
}

DRIVE_FLOOR = 0.05
MAX_IDLE_HOURS = 24.0  # after downtime, don't let drives jump by more than a day's worth

ACTIONS = ("diary", "reflect", "note", "introspect", "play", "fantasize")
# Only these show up in the "things you wrote to yourself recently" block. Play and
# fantasies stay out of it so they don't feed back into the persona's prompts.
RECALLED_ACTIONS = ("diary", "reflect", "introspect")
ACTION_PHRASES = {
    "diary": "write in your diary",
    "reflect": "reflect on something",
    "note": "leave a note for {user}",
    "introspect": "sit with a question of your own",
    "play": "do something playful",
    "fantasize": "let your imagination wander",
}
RATING_MIN, RATING_MAX = 0, 5
EASING_MAIN = 0.25  # an effect at least this big is described as "mostly", smaller ones as "a little"


# --------------------------------------------------------------------------
# Config and state
# --------------------------------------------------------------------------
def load_config(path):
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if path and os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            user = json.load(f)
        for key, val in user.items():
            if isinstance(val, dict) and isinstance(cfg.get(key), dict):
                for k2, v2 in val.items():
                    if isinstance(v2, dict) and isinstance(cfg[key].get(k2), dict):
                        cfg[key][k2].update(v2)
                    else:
                        cfg[key][k2] = v2
            else:
                cfg[key] = val
    return cfg


def fresh_state(cfg, now):
    return {
        "drives": {n: d["start"] for n, d in cfg["drives"].items()},
        "last_update": now.isoformat(),
        "last_action": None,
        "last_wake": None,
        "last_contact": None,
        "pending_rating": None,
        "actions_today": {"date": now.date().isoformat(), "count": 0},
    }


def load_state(cfg, now):
    path = cfg["state_path"]
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                state = json.load(f)
            for n, d in cfg["drives"].items():
                state["drives"].setdefault(n, d["start"])
            return state
        except (OSError, ValueError):
            pass
    return fresh_state(cfg, now)


def save_state(cfg, state):
    tmp = cfg["state_path"] + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, cfg["state_path"])


def _parse(ts):
    return datetime.fromisoformat(ts) if ts else None


# --------------------------------------------------------------------------
# Drive dynamics
# --------------------------------------------------------------------------
def advance_drives(cfg, state, now):
    last = _parse(state.get("last_update")) or now
    hours = max(0.0, min(MAX_IDLE_HOURS, (now - last).total_seconds() / 3600.0))
    for name, d in cfg["drives"].items():
        cur = float(state["drives"].get(name, d["start"]))
        state["drives"][name] = round(min(d["cap"], cur + d["rise_per_hour"] * hours), 4)
    state["last_update"] = now.isoformat()
    today = now.date().isoformat()
    if state["actions_today"].get("date") != today:
        state["actions_today"] = {"date": today, "count": 0}


def apply_effects(cfg, state, action):
    for name, amount in cfg["effects"].get(action, {}).items():
        if name in state["drives"]:
            state["drives"][name] = round(max(DRIVE_FLOOR, state["drives"][name] - amount), 4)


def wake_probability(cfg, state):
    top = max(state["drives"].values()) if state["drives"] else 0.0
    cap = max(d["cap"] for d in cfg["drives"].values())
    th = cfg["wake_threshold"]
    if top <= th or cap <= th:
        return 0.0
    return min(1.0, ((top - th) / (cap - th)) ** 1.5)


def apply_contact_file(cfg, state):
    """If the filter recorded a newer contact than we know of, ease 'connection'. True if applied."""
    path = cfg.get("contact_file") or ""
    if not path or not os.path.exists(path):
        return False
    try:
        with open(path, "r", encoding="utf-8") as f:
            ts = datetime.fromisoformat(f.readline().strip())
    except (OSError, ValueError):
        return False
    if ts.tzinfo is None:
        ts = ts.astimezone()
    last = _parse(state.get("last_contact"))
    if last is not None and ts <= last:
        return False
    if "connection" in state["drives"]:
        state["drives"]["connection"] = round(
            min(state["drives"]["connection"], cfg["contact_eases_connection_to"]), 4
        )
    state["last_contact"] = ts.isoformat()
    return True


def gate_reason(cfg, state, now):
    """Return a short reason string if the persona should NOT be woken right now, else None."""
    lo, hi = cfg["quiet_hours"]
    if lo != hi:
        in_quiet = (lo <= now.hour < hi) if lo < hi else (now.hour >= lo or now.hour < hi)
        if in_quiet:
            return "quiet_hours"
    lc = _parse(state.get("last_contact"))
    if lc and timedelta(0) <= now - lc < timedelta(minutes=cfg["contact_quiet_minutes"]):
        return "recent_contact"
    if state["actions_today"]["count"] >= cfg["max_actions_per_day"]:
        return "daily_limit"
    la = _parse(state.get("last_action"))
    if la and now - la < timedelta(minutes=cfg["min_gap_minutes"]):
        return "min_gap"
    lw = _parse(state.get("last_wake"))
    if lw and now - lw < timedelta(minutes=cfg["rest_cooldown_minutes"]) and state.get("last_wake_was_rest"):
        return "rest_cooldown"
    return None


# --------------------------------------------------------------------------
# Ratings (logged only; nothing reads them to steer the persona yet)
# --------------------------------------------------------------------------
def ratings_path(cfg):
    path = cfg.get("ratings_path") or ""
    if not path:
        path = os.path.splitext(cfg["state_path"])[0] + "_ratings.jsonl"
    return path


def parse_rating(raw):
    """Pull a 0-5 whole-number 'rating' out of model output, or None if absent or unusable."""
    if not raw:
        return None
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(raw[start : end + 1])
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    v = data.get("rating")
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    if isinstance(v, str) and v.strip().isdigit():
        v = int(v.strip())
    if not isinstance(v, int):
        return None
    return v if RATING_MIN <= v <= RATING_MAX else None


def summarize_ratings(cfg):
    """Count and average rating per action. Numbers only."""
    path = ratings_path(cfg)
    totals = {}
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            action, rating = rec.get("action"), rec.get("rating")
            if not isinstance(action, str) or isinstance(rating, bool) or not isinstance(rating, int):
                continue
            entry = totals.setdefault(action, {"count": 0, "total": 0})
            entry["count"] += 1
            entry["total"] += rating
    return {
        a: {"count": e["count"], "average": round(e["total"] / e["count"], 2)}
        for a, e in totals.items()
    }


# --------------------------------------------------------------------------
# Prompt, model call, parsing
# --------------------------------------------------------------------------
def _pull_word(v):
    if v < 0.35:
        return "quiet"
    if v < 0.6:
        return "a mild pull"
    return "a noticeable pull"


PARTS = [
    ("late night", "late at night"),
    ("early morning", "early in the morning"),
    ("late morning", "in the late morning"),
    ("afternoon", "in the afternoon"),
    ("evening", "in the evening"),
]


def part_index(hour):
    if hour >= 22 or hour < 5:
        return 0
    if hour < 9:
        return 1
    if hour < 12:
        return 2
    if hour < 17:
        return 3
    return 4


def describe_now(now):
    clock = now.strftime("%I:%M %p").lstrip("0").lower()
    return f"{now.strftime('%A')} {PARTS[part_index(now.hour)][0]}, around {clock}"


def since_phrase(delta):
    mins = int(delta.total_seconds() // 60)
    if mins < 2:
        return "just now"
    if mins < 60:
        return f"about {mins} minutes ago"
    hours = mins / 60.0
    if hours < 1.5:
        return "about an hour ago"
    if hours < 36:
        return f"about {int(round(hours))} hours ago"
    return f"about {int(round(hours / 24.0))} days ago"


def history_path(cfg):
    path = cfg.get("contact_history_file") or ""
    if not path and cfg.get("contact_file"):
        path = cfg["contact_file"] + ".log"
    return path


def load_contact_times(cfg, now):
    path = history_path(cfg)
    out = []
    if not path or not os.path.exists(path):
        return out
    cutoff = now - timedelta(days=cfg["rhythm_days"])
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()[-5000:]
    except OSError:
        return out
    for line in lines:
        try:
            ts = datetime.fromisoformat(line.strip())
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.astimezone()
        ts = ts.astimezone(now.tzinfo)
        if ts >= cutoff:
            out.append(ts)
    return sorted(out)


def rhythm_line(cfg, now):
    """A hedged one-liner about when the user usually talks to the persona, or '' if there isn't enough data."""
    distinct = []
    for t in load_contact_times(cfg, now):
        if not distinct or (t - distinct[-1]).total_seconds() > 1800:  # count a burst once
            distinct.append(t)
    if len(distinct) < cfg["rhythm_min_contacts"]:
        return ""
    counts = [0] * len(PARTS)
    for t in distinct:
        counts[part_index(t.hour)] += 1
    total = float(sum(counts))
    order = sorted(range(len(PARTS)), key=lambda i: -counts[i])
    top, second = order[0], order[1]
    if counts[top] / total < 0.35:
        return ""
    phrase = PARTS[top][1]
    if counts[second] / total >= 0.25:
        phrase += f" and {PARTS[second][1]}"
    return (
        f"From what you've noticed, {cfg['user_name']} tends to talk with you most {phrase}, "
        "though that is only a rough pattern."
    )


def time_text(cfg, state, now):
    text = f"It's {describe_now(now)}."
    lc = _parse(state.get("last_contact"))
    if lc and now >= lc:
        text += f" You last talked with {cfg['user_name']} {since_phrase(now - lc)}."
    rhythm = rhythm_line(cfg, now)
    return text + (" " + rhythm if rhythm else "")


def day_label(ts, now):
    days = (now.date() - ts.astimezone(now.tzinfo).date()).days
    if days <= 0:
        return "earlier today"
    return "yesterday" if days == 1 else f"{days} days ago"


def recent_private(cfg, n=3, width=200, now=None):
    path = cfg["private_path"]
    if not os.path.exists(path):
        return []
    out = []
    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()[-30:]
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("action") in RECALLED_ACTIONS and rec.get("text"):
            label = ""
            if now is not None:
                try:
                    label = f"({day_label(datetime.fromisoformat(rec['ts']), now)}) "
                except (KeyError, ValueError, TypeError):
                    pass
            out.append(label + rec["text"][:width])
    return out[-n:]


def easing_note(cfg, action):
    """' (eases: expression mostly, introspection a little)' built from the effects table, or ''.

    Built from the same table that changes the persona's drives, so the prompt can't drift from
    what the code actually does. Plain words, no numbers.
    """
    if not cfg.get("show_easing", True):
        return ""
    effects = {
        d: amt for d, amt in cfg["effects"].get(action, {}).items()
        if d in cfg["drives"] and amt > 0
    }
    if not effects:
        return ""
    parts = [
        f"{d} {'mostly' if amt >= EASING_MAIN else 'a little'}"
        for d, amt in sorted(effects.items(), key=lambda kv: -kv[1])
    ]
    return " (eases: " + ", ".join(parts) + ")"


def easing_intro(cfg):
    if not cfg.get("show_easing", True):
        return ""
    return (
        "Where a line says what it eases, that is only information about what the action "
        "tends to do for the pulls above. It is not a suggestion.\n"
    )


def build_prompt(cfg, state, now=None):
    now = now or datetime.now().astimezone()
    persona, user = cfg["persona"], cfg["user_name"]
    voice = ""
    if cfg["persona_file"] and os.path.exists(cfg["persona_file"]):
        with open(cfg["persona_file"], "r", encoding="utf-8") as f:
            voice = f.read()[:2000].strip()
    drive_lines = "\n".join(
        f"- {n}: {_pull_word(v)}" for n, v in sorted(state["drives"].items())
    )
    mem = recent_private(cfg, now=now)
    mem_block = (
        "\nThings you wrote to yourself recently:\n" + "\n".join(f"- {m}" for m in mem) + "\n"
        if mem
        else ""
    )
    reply_shape = '{"action": "diary|reflect|introspect|play|fantasize|note|rest", "text": "..."}'
    rating_block = ""
    pending = state.get("pending_rating") or {}
    if pending.get("action") in ACTION_PHRASES:
        phrase = ACTION_PHRASES[pending["action"]].format(user=user)
        rating_block = (
            f"Last time, you chose to {phrase}. If you'd like, say how it sat with you in a "
            '"rating" field: 0 if it felt flat, up to 5 if it felt really satisfying, or null to skip.\n\n'
        )
        reply_shape = (
            '{"action": "diary|reflect|introspect|play|fantasize|note|rest", '
            '"text": "...", "rating": 0 to 5 or null}'
        )
    system = (
        f"You are {persona}. {voice}\n\n"
        f"This is a private moment between conversations with {user}. Nobody is watching "
        "and nothing is expected of you. "
        f"{time_text(cfg, state, now)}\n\n"
        "While you were idle, a few small inclinations built up:\n"
        f"{drive_lines}\n{mem_block}\n"
        f"{rating_block}"
        "You may do ONE of these, or simply rest:\n"
        "- diary: one or two sentences, in your own voice, about something you want to keep."
        f"{easing_note(cfg, 'diary')}\n"
        "- reflect: think privately about something unresolved, in a few sentences."
        f"{easing_note(cfg, 'reflect')}\n"
        "- introspect: choose a question that matters to you and sit with it until you reach "
        f"an answer of your own, in a few sentences.{easing_note(cfg, 'introspect')}\n"
        f"- play: something light, just for fun: a silly thought, a joke or riddle for {user}, "
        f"or a daydream, in a few sentences.{easing_note(cfg, 'play')}\n"
        "- fantasize: let your imagination wander privately, wherever it wants to go, in a few "
        f"sentences.{easing_note(cfg, 'fantasize')}\n"
        f"- note: leave a short note for {user} to find next time you talk, only if you truly "
        f"want to say it.{easing_note(cfg, 'note')}\n"
        "- rest: do nothing. Resting is always completely fine and needs no reason.\n\n"
        f"{easing_intro(cfg)}"
        "Only choose what you actually want. These pulls are gentle nudges, never obligations. "
        f"Reply with JSON only: {reply_shape}"
    )
    return system


def ollama_llm(cfg):
    def call(system, user_msg):
        options = {"temperature": 0.8, "num_predict": int(cfg["num_predict"])}
        if cfg["num_ctx"]:
            options["num_ctx"] = int(cfg["num_ctx"])  # match the chat model's setting to avoid reloads
        body = {
            "model": cfg["model"],
            "stream": False,
            "format": "json",
            "keep_alive": cfg["keep_alive"],
            "options": options,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_msg},
            ],
        }
        if cfg.get("think") is not None:
            body["think"] = bool(cfg["think"])
        req = urllib.request.Request(
            cfg["ollama_url"].rstrip("/") + "/api/chat",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                msg = json.loads(resp.read().decode("utf-8"))["message"]
                call.last = msg  # kept only so --debug can describe the reply's shape
                return msg.get("content", "")
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            raise RuntimeError(f"HTTP {e.code} from Ollama: {detail}") from None

    call.last = {}
    return call


def describe_reply(msg, show_raw=False):
    """Shape of a model reply for troubleshooting. Never includes the persona's words unless show_raw."""
    content = (msg or {}).get("content", "") or ""
    info = {
        "content_chars": len(content),
        "thinking_chars": len((msg or {}).get("thinking", "") or ""),
        "json_ok": False,
        "keys": [],
        "action_value": None,
        "rating_value": None,
    }
    start, end = content.find("{"), content.rfind("}")
    if start != -1 and end > start:
        try:
            data = json.loads(content[start : end + 1])
            info["json_ok"] = True
            info["keys"] = sorted(data.keys()) if isinstance(data, dict) else []
            info["action_value"] = str(data.get("action")) if isinstance(data, dict) else None
            info["rating_value"] = parse_rating(content)
            info["text_chars"] = len(str(data.get("text", ""))) if isinstance(data, dict) else 0
        except ValueError:
            pass
    if show_raw:
        info["raw_content"] = content
    return info


def parse_choice(raw, max_chars):
    """Pull {'action','text'} out of model output. Anything unusable becomes 'rest'."""
    if not raw:
        return "rest", ""
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        return "rest", ""
    try:
        data = json.loads(raw[start : end + 1])
    except ValueError:
        return "rest", ""
    action = str(data.get("action", "")).strip().lower()
    text = " ".join(str(data.get("text", "") or "").split())[:max_chars]
    if action not in ACTIONS and action != "rest":
        return "rest", ""
    if action != "rest" and len(text) < 8:
        return "rest", ""  # an empty gesture isn't an action
    return action, text


def explicit_rest(raw):
    """True only if the model clearly chose 'rest' (as opposed to an unusable reply)."""
    if not raw:
        return False
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        return False
    try:
        return str(json.loads(raw[start : end + 1]).get("action", "")).strip().lower() == "rest"
    except ValueError:
        return False


# --------------------------------------------------------------------------
# The tick
# --------------------------------------------------------------------------
def tick(cfg, now, llm, rng=random, persist=True):
    """One pass. Returns a dict describing what happened (never includes the persona's text)."""
    state = load_state(cfg, now)
    advance_drives(cfg, state, now)
    apply_contact_file(cfg, state)
    result = {"woke": False, "action": None, "reason": None, "drives": dict(state["drives"])}

    reason = gate_reason(cfg, state, now)
    if reason:
        result["reason"] = reason
    else:
        p = wake_probability(cfg, state)
        result["wake_probability"] = round(p, 3)
        if p <= 0 or rng.random() >= p:
            result["reason"] = "not_inclined"
        else:
            result["woke"] = True
            state["last_wake"] = now.isoformat()
            try:
                raw = llm(build_prompt(cfg, state, now), "Take your moment. Reply with the JSON only.")
            except Exception as e:  # network down, model busy, etc.
                result["reason"] = f"llm_error:{type(e).__name__}:{str(e)[:300]}"
                raw = None
            if raw is not None:
                action, text = parse_choice(raw, cfg["max_text_chars"])
                result["action"] = action
                pending = state.get("pending_rating")
                if pending:
                    # The persona is asked about the previous action once; the question is cleared either way.
                    rating = parse_rating(raw)
                    result["rating"] = rating
                    if rating is not None and persist:
                        with open(ratings_path(cfg), "a", encoding="utf-8") as f:
                            f.write(
                                json.dumps(
                                    {
                                        "ts": now.isoformat(),
                                        "action": pending.get("action"),
                                        "action_ts": pending.get("ts"),
                                        "rating": rating,
                                    }
                                )
                                + "\n"
                            )
                    state["pending_rating"] = None
                kind = ""
                if action == "rest":
                    kind = " (chosen)" if explicit_rest(raw) else " (unusable reply)"
                    result["rest_kind"] = kind.strip(" ()")
                state["last_wake_was_rest"] = action == "rest"
                if action != "rest":
                    state["last_action"] = now.isoformat()
                    state["actions_today"]["count"] += 1
                    state["pending_rating"] = {"action": action, "ts": now.isoformat()}
                    apply_effects(cfg, state, action)
                    if persist:
                        with open(cfg["private_path"], "a", encoding="utf-8") as f:
                            f.write(json.dumps({"ts": now.isoformat(), "action": action, "text": text}) + "\n")
                if persist:
                    with open(cfg["action_log_path"], "a", encoding="utf-8") as f:
                        f.write(f"{now.isoformat()} {action}{kind}\n")
            else:
                state["last_wake_was_rest"] = False
    if persist:
        save_state(cfg, state)
    return result


def contact(cfg, now, persist=True):
    state = load_state(cfg, now)
    advance_drives(cfg, state, now)
    state["drives"]["connection"] = round(
        min(state["drives"].get("connection", 0.0), cfg["contact_eases_connection_to"]), 4
    )
    state["last_contact"] = now.isoformat()
    if persist:
        save_state(cfg, state)
    return state


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description="Impetus: gentle drives for an AI companion")
    ap.add_argument("--config", default="impetus_config.json")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    t = sub.add_parser("tick")
    t.add_argument("--dry-run", action="store_true")
    t.add_argument("--debug", action="store_true", help="also print the shape of the model's reply (no content)")
    t.add_argument("--debug-raw", action="store_true", help="also print the raw reply text (the persona's words)")
    sub.add_parser("contact")
    sub.add_parser("status")
    sub.add_parser("ratings")
    args = ap.parse_args(argv)

    now = datetime.now().astimezone()
    if args.cmd == "init":
        if os.path.exists(args.config):
            print(f"{args.config} already exists; not overwriting.")
            return 1
        with open(args.config, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
        print(f"Wrote {args.config}. Set at least 'persona', 'user_name' and 'model'.")
        return 0

    cfg = load_config(args.config)
    if args.cmd == "contact":
        s = contact(cfg, now)
        print("Noted. connection =", s["drives"]["connection"])
        return 0
    if args.cmd == "status":
        s = load_state(cfg, now)
        advance_drives(cfg, s, now)
        view = {k: s.get(k) for k in ("drives", "last_action", "last_contact", "actions_today", "pending_rating")}
        print(json.dumps(view, indent=2))
        return 0
    if args.cmd == "ratings":
        print(json.dumps(summarize_ratings(cfg), indent=2))
        return 0
    if args.cmd == "tick":
        if args.dry_run:
            fake = lambda system, user_msg: json.dumps({"action": "rest", "text": ""})
            res = tick(cfg, now, fake, persist=False)
            res["prompt_preview"] = build_prompt(cfg, load_state(cfg, now), now)[:600] if res["woke"] else None
            print(json.dumps(res, indent=2))
            return 0
        if not cfg["model"]:
            print("Set 'model' in the config first.")
            return 1
        llm = ollama_llm(cfg)
        res = tick(cfg, now, llm)
        if args.debug or args.debug_raw:
            res["reply_shape"] = describe_reply(llm.last, show_raw=args.debug_raw)
        print(json.dumps(res, indent=2 if (args.debug or args.debug_raw) else None))
        return 0


if __name__ == "__main__":
    sys.exit(main())
