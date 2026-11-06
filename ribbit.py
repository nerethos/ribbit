#!/usr/bin/env python3
"""
ribbit.py - terminal hub for several Claude / API / Foundry / OpenAI profiles.

Every profile is launched as its own process with its own environment. Subscription
profiles use Claude Code's normal login in an isolated CLAUDE_CONFIG_DIR, so you sign in
once per account and nothing here reads, copies or swaps tokens. No calls to Anthropic
are made by this tool.

  ribbit.py                 full-screen TUI (launch, usage bars, add/edit/delete)
  ribbit.py run <name> [-- extra args]   launch a profile without the TUI
  ribbit.py list            print profiles and last known usage
  ribbit.py hook            statusLine command; saves rate_limits per account

API keys are never stored. A key field holds a reference that is resolved at launch:
  env:NAME     read from the environment of the shell that started the hub
  cmd:<cmd>    run a command and use its stdout (pass, secret-tool, az, op read ...)
  file:<path>  read a file (keep it chmod 600)
  prompt       ask on the terminal at launch

Stdlib only, Python 3.8+. Windows needs: pip install windows-curses
"""
import getpass
import json
import locale
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
from datetime import datetime
from pathlib import Path

try:
    import curses
except ImportError:  # Windows without windows-curses
    curses = None

STALE_AFTER = 600
ACTIVE_WITHIN = 120
BAR_WIDTH = 30
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,30}$")
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SECRET_NAME_RE = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD)$", re.I)
KEYLIKE_RE = re.compile(r"(sk-[A-Za-z0-9_-]{8,}|[A-Za-z0-9_-]{40,})")
LABELS = {"five_hour": "5h", "seven_day": "7d"}

TYPES = {
    "subscription": "Claude subscription (own login, isolated config dir)",
    "anthropic": "Anthropic API (Console key) in Claude Code",
    "foundry": "Azure AI Foundry in Claude Code",
    "openai": "OpenAI key for another CLI tool (e.g. codex, aider)",
}
TAGS = {"subscription": "sub", "anthropic": "api", "foundry": "foundry", "openai": "openai"}

# Removed from the child's environment so a stray global value cannot override a profile.
SCRUB = [
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL",
    "CLAUDE_CODE_USE_FOUNDRY", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
    "ANTHROPIC_FOUNDRY_RESOURCE", "ANTHROPIC_FOUNDRY_BASE_URL",
    "ANTHROPIC_FOUNDRY_API_KEY", "ANTHROPIC_FOUNDRY_AUTH_TOKEN",
    "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL", "OPENAI_API_KEY", "OPENAI_BASE_URL",
]


class ProfileError(Exception):
    pass


# ---------------------------------------------------------------- paths / config

def _base(kind):
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    if kind == "config":
        return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))


def config_path():
    return _base("config") / "ribbit" / "profiles.json"


def state_dir():
    return _base("state") / "ribbit" / "usage"


def safe_name(name):
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name)


def expand(path):
    return os.path.abspath(os.path.expanduser(os.path.expandvars(path)))


def default_profiles():
    return [
        {"name": "personal", "label": "Personal", "type": "subscription",
         "config_dir": "~/.claude-personal"},
        {"name": "work", "label": "Work", "type": "subscription",
         "config_dir": "~/.claude-work"},
    ]


def valid_profile(p):
    """Minimal shape check so the rest of the program can index name/type safely."""
    return (isinstance(p, dict) and isinstance(p.get("name"), str) and NAME_RE.match(p["name"])
            and p.get("type") in TYPES)


def load_profiles():
    p = config_path()
    if not p.exists():
        return default_profiles(), ""
    try:
        data = json.loads(p.read_text())
        profs = data.get("profiles", [])
        if not isinstance(profs, list):
            raise ValueError("profiles is not a list")
        good, seen = [], set()
        for item in profs:
            if valid_profile(item) and item["name"] not in seen:
                seen.add(item["name"])
                good.append(item)
        skipped = len(profs) - len(good)
        if not skipped:
            return good, ""
        bak = p.with_suffix(".json.bak")
        kept = ""
        try:
            if not bak.exists():
                shutil.copy(p, bak)
            kept = "; original kept at %s" % bak
        except Exception:
            pass
        return good, "skipped %d invalid or duplicate profile(s) in profiles.json%s" % (skipped, kept)
    except Exception as e:
        bak = p.with_suffix(".json.broken")
        try:
            shutil.copy(p, bak)
        except Exception:
            pass
        return default_profiles(), "could not parse profiles.json (%s); copy kept at %s" % (e, bak)


def save_profiles(profiles):
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".profiles.", suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump({"profiles": profiles}, f, indent=2)
    if os.name != "nt":
        os.chmod(tmp, 0o600)
    os.replace(tmp, p)


# ---------------------------------------------------------------- secret references

def check_ref(ref, required):
    """Return an error string, or None if ref is acceptable."""
    ref = ref.strip()
    if not ref:
        return "a key reference is required" if required else None
    if ref == "prompt":
        return None
    for prefix in ("env:", "cmd:", "file:"):
        if ref.startswith(prefix):
            rest = ref[len(prefix):].strip()
            if not rest:
                return "nothing after " + prefix
            if prefix == "env:" and not ENV_NAME_RE.match(rest):
                return "env: takes a variable NAME (e.g. env:ANTHROPIC_API_KEY), not the key itself"
            return None
    return ("not stored: use env:NAME, cmd:<command>, file:<path> or prompt "
            "(raw keys are never written to the config)")


def resolve_secret(ref, who):
    ref = (ref or "").strip()
    if not ref:
        return None
    if ref == "prompt":
        return getpass.getpass("  key for %s (input hidden): " % who).strip() or None
    kind, _, rest = ref.partition(":")
    rest = rest.strip()
    if kind == "env":
        val = os.environ.get(rest)
        if not val:
            raise ProfileError("environment variable %s is not set in the shell that started the hub" % rest)
        return val
    if kind == "file":
        try:
            return Path(expand(rest)).read_text().strip()
        except OSError as e:
            raise ProfileError("cannot read key file: %s" % e)
    if kind == "cmd":
        try:
            argv = shlex.split(rest, posix=(os.name != "nt"))
            r = subprocess.run(argv, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired, ValueError) as e:
            raise ProfileError("key command failed: %s" % e)
        if r.returncode != 0:
            raise ProfileError("key command exited %d: %s" % (r.returncode, r.stderr.strip()[:120]))
        out = r.stdout.strip()
        if not out:
            raise ProfileError("key command printed nothing")
        return out
    raise ProfileError("unknown key reference: " + ref)


def key_status(ref):
    ref = (ref or "").strip()
    if not ref:
        return "none", "dim"
    if ref == "prompt":
        return "asked at launch", "dim"
    kind, _, rest = ref.partition(":")
    rest = rest.strip()
    if kind == "env":
        ok = bool(os.environ.get(rest))
        return ref + ("  set" if ok else "  NOT SET in this shell"), ("green" if ok else "red")
    if kind == "file":
        ok = Path(expand(rest)).is_file()
        return ref + ("  found" if ok else "  MISSING"), ("green" if ok else "red")
    return ref + "  (runs at launch)", "dim"


def parse_extra_env(text):
    out = {}
    for tok in shlex.split(text, posix=True):
        if "=" not in tok:
            raise ValueError("expected KEY=VALUE, got " + tok)
        k, v = tok.split("=", 1)
        if not ENV_NAME_RE.match(k):
            raise ValueError("bad variable name " + k)
        if SECRET_NAME_RE.search(k) or KEYLIKE_RE.search(v):
            raise ValueError("%s looks like a secret; use a key reference field instead" % k)
        out[k] = v
    return out


# ---------------------------------------------------------------- launching

def build_env(p, secret):
    env = {k: v for k, v in os.environ.items() if k not in SCRUB}
    t = p["type"]
    if t != "openai":
        env["CLAUDE_CONFIG_DIR"] = expand(p.get("config_dir") or "~/.claude-" + p["name"])
        env["CLAUDE_ACCOUNT"] = p["name"]
    if t == "anthropic":
        env["ANTHROPIC_API_KEY"] = secret
        if p.get("base_url"):
            env["ANTHROPIC_BASE_URL"] = p["base_url"]
        if p.get("model"):
            env["ANTHROPIC_MODEL"] = p["model"]
    elif t == "foundry":
        env["CLAUDE_CODE_USE_FOUNDRY"] = "1"
        if p.get("base_url"):
            env["ANTHROPIC_FOUNDRY_BASE_URL"] = p["base_url"]
        elif p.get("resource"):
            env["ANTHROPIC_FOUNDRY_RESOURCE"] = p["resource"]
        if secret:
            env["ANTHROPIC_FOUNDRY_API_KEY"] = secret
        models = p.get("models") or {}
        for k, var in (("opus", "ANTHROPIC_DEFAULT_OPUS_MODEL"),
                       ("sonnet", "ANTHROPIC_DEFAULT_SONNET_MODEL"),
                       ("haiku", "ANTHROPIC_DEFAULT_HAIKU_MODEL")):
            if models.get(k):
                env[var] = models[k]
    elif t == "openai":
        env["OPENAI_API_KEY"] = secret
        if p.get("base_url"):
            env["OPENAI_BASE_URL"] = p["base_url"]
    env.update(p.get("extra_env") or {})
    return env


def _split(s):
    return shlex.split(s or "", posix=(os.name != "nt"))


def launch(p, extra=()):
    t = p["type"]
    cmd = p.get("command") or ("claude" if t != "openai" else "")
    if not cmd:
        raise ProfileError("no command set for this profile (edit it and set one)")
    argv = _split(cmd) + _split(p.get("args")) + list(extra)
    exe = shutil.which(argv[0])
    if not exe:
        raise ProfileError("'%s' not found on PATH" % argv[0])
    if t in ("anthropic", "openai") and not (p.get("key") or "").strip():
        raise ProfileError("this profile has no key reference")
    secret = resolve_secret(p.get("key"), p["name"])
    if t != "openai":
        d = expand(p.get("config_dir") or "~/.claude-" + p["name"])
        try:
            os.makedirs(d, mode=0o700, exist_ok=True)
        except OSError as e:
            raise ProfileError("cannot use config dir %s: %s" % (d, e.strerror or e))
    env = build_env(p, secret)
    # A no-op handler (not SIG_IGN, which children would inherit) so Ctrl-C reaches the
    # child tool without Python killing it.
    old = signal.signal(signal.SIGINT, lambda *a: None)
    try:
        return subprocess.call([exe] + argv[1:], env=env)
    except OSError as e:
        raise ProfileError("could not start '%s': %s" % (argv[0], e.strerror or e))
    finally:
        signal.signal(signal.SIGINT, old)


# ---------------------------------------------------------------- statusline hook / usage state

def label(key):
    if key in LABELS:
        return LABELS[key]
    k = key[len("seven_day_"):] if key.startswith("seven_day_") else key
    return k.replace("_", " ").title()


def parse_reset(w):
    for k in ("resets_at", "reset_at", "reset_time", "resets"):
        v = w.get(k)
        if v is None:
            continue
        if isinstance(v, (int, float)):
            return v / 1000.0 if v > 1e11 else float(v)
        if isinstance(v, str):
            try:
                return float(v)
            except ValueError:
                pass
            try:
                return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
            except ValueError:
                pass
    return None


def fmt_dur(s):
    s = int(max(s, 0))
    d, r = divmod(s, 86400)
    h, r = divmod(r, 3600)
    m = r // 60
    if d:
        return "%dd %dh" % (d, h)
    if h:
        return "%dh %02dm" % (h, m)
    return "%dm" % m


def cmd_hook():
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        data = {}
    acct = safe_name(os.environ.get("CLAUDE_ACCOUNT") or "default")
    rl = data.get("rate_limits") if isinstance(data, dict) else None
    try:
        d = state_dir()
        d.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(d), prefix="." + acct + ".", suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump({"account": acct, "updated": time.time(), "rate_limits": rl}, f)
        os.replace(tmp, d / (acct + ".json"))
        if os.environ.get("RIBBIT_DEBUG"):
            (d / (acct + ".raw.txt")).write_text(raw)
    except Exception:
        pass  # never break the statusline
    parts = [acct]
    if isinstance(rl, dict):
        for key, w in rl.items():
            if isinstance(w, dict) and isinstance(w.get("used_percentage"), (int, float)):
                parts.append("%s %d%%" % (label(key), int(w["used_percentage"])))
    else:
        parts.append("no subscription limits")
    print(" | ".join(parts))
    return 0


def read_state(name):
    try:
        return json.loads((state_dir() / (safe_name(name) + ".json")).read_text())
    except Exception:
        return None


def hook_command():
    script = os.path.abspath(__file__)
    py = sys.executable or "python3"
    if os.name == "nt":
        return subprocess.list2cmdline([py, script, "hook"])
    return " ".join(shlex.quote(x) for x in (py, script, "hook"))


def settings_path(p):
    return Path(expand(p.get("config_dir") or "~/.claude-" + p["name"])) / "settings.json"


def hook_installed(p):
    try:
        cur = json.loads(settings_path(p).read_text()).get("statusLine") or {}
        return os.path.basename(__file__) in str(cur.get("command", ""))
    except Exception:
        return False


def install_hook(p):
    sp = settings_path(p)
    data = {}
    if sp.exists():
        try:
            data = json.loads(sp.read_text())
        except Exception:
            return "settings.json is not valid JSON, left untouched"
        if not isinstance(data, dict):
            return "settings.json is not a JSON object, left untouched"
    want = {"type": "command", "command": hook_command()}
    cur = data.get("statusLine")
    if cur == want:
        return "usage hook already installed"
    if cur:
        shown = cur.get("command", "?") if isinstance(cur, dict) else str(cur)
        return "settings.json already has a statusLine (%s); not overwriting" % str(shown)[:60]
    sp.parent.mkdir(parents=True, exist_ok=True)
    if sp.exists():
        bak = sp.with_name("settings.json.hub-backup")
        if not bak.exists():
            shutil.copy(sp, bak)
    data["statusLine"] = want
    fd, tmp = tempfile.mkstemp(dir=str(sp.parent), prefix=".settings.", suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, sp)
    return "usage hook installed in " + str(sp)


# ---------------------------------------------------------------- screen model

def pct_style(pct):
    return "red" if pct >= 85 else "yellow" if pct >= 60 else "green"


def profile_lines(p, i, sel, now):
    """Lines for one profile. Each line is a list of (text, style)."""
    t = p["type"]
    mark = "▸" if i == sel else " "
    state = read_state(p["name"]) if t != "openai" else None
    active = stale = False
    age = 0
    if state:
        age = now - state.get("updated", 0)
        active = age < ACTIVE_WITHIN
        stale = age > STALE_AFTER
    head = [(" %s " % mark, "orange" if i == sel else "dim"),
            ("%d " % (i + 1), "bold"),
            (p["name"], "bold"),
            ("  [%s]" % (p.get("label") or p["name"]), "dim"),
            ("  " + TAGS.get(t, t), "cyan")]
    if active:
        head.append(("   ● active", "orange"))
    elif stale and t == "subscription":
        head.append(("   as of %s ago" % fmt_dur(age), "dim"))
    lines = [head]
    pad = "      "

    if t == "subscription":
        shown = False
        rl = (state or {}).get("rate_limits")
        if isinstance(rl, dict):
            for key, w in rl.items():
                if not isinstance(w, dict) or not isinstance(w.get("used_percentage"), (int, float)):
                    continue
                shown = True
                pct = float(w["used_percentage"])
                reset = parse_reset(w)
                expired = reset is not None and reset < now
                if expired:
                    pct, note = 0.0, "window reset"
                elif reset is not None:
                    note = "resets " + fmt_dur(reset - now)
                else:
                    note = ""
                col = "dim" if (stale and not expired) else pct_style(pct)
                n = int(round(BAR_WIDTH * max(0.0, min(100.0, pct)) / 100.0))
                lines.append([(pad + "%-7s" % label(key), "dim"), ("━" * n, col),
                              ("─" * (BAR_WIDTH - n), "dim"),
                              ("  %3d%%" % int(pct), col), ("  " + note, "dim")])
        if not shown:
            if state:
                msg = "no subscription limits reported in the last session"
            elif hook_installed(p):
                msg = "no usage data yet (appears after the first message in a session)"
            else:
                msg = "no usage data yet. Press i to install the usage hook for this profile"
            lines.append([(pad + msg, "dim")])
    elif t == "anthropic":
        txt, sty = key_status(p.get("key"))
        lines.append([(pad + "key ", "dim"), (txt, sty)])
        extra = []
        if p.get("base_url"):
            extra.append("base " + p["base_url"])
        if p.get("model"):
            extra.append("model " + p["model"])
        if extra:
            lines.append([(pad + "  ".join(extra), "dim")])
    elif t == "foundry":
        target = p.get("base_url") or p.get("resource") or "?"
        if p.get("key"):
            txt, sty = key_status(p["key"])
        else:
            txt, sty = "Entra ID (az login / default credential chain)", "dim"
        lines.append([(pad + "resource " + target + "   key ", "dim"), (txt, sty)])
        models = p.get("models") or {}
        if models:
            lines.append([(pad + "models " + "  ".join("%s=%s" % kv for kv in models.items()), "dim")])
        else:
            lines.append([(pad + "no model deployments pinned (docs say to pin them)", "yellow")])
    elif t == "openai":
        txt, sty = key_status(p.get("key"))
        lines.append([(pad + "cmd %s   key " % (p.get("command") or "(not set)"), "dim"), (txt, sty)])
    lines.append([("", "normal")])
    return lines


def build_screen(profiles, sel, now):
    lines, starts = [], []
    for i, p in enumerate(profiles):
        starts.append(len(lines))
        lines.extend(profile_lines(p, i, sel, now))
    starts.append(len(lines))
    return lines, starts


# ---------------------------------------------------------------- curses UI

ATTRS = {}


def init_attrs():
    curses.start_color()
    try:
        curses.use_default_colors()
        bg = -1
    except curses.error:
        bg = curses.COLOR_BLACK
    wide = curses.COLORS >= 256

    def pair(n, c256, c8):
        curses.init_pair(n, c256 if wide else c8, bg)
        return curses.color_pair(n)

    ATTRS.update({
        "normal": 0,
        "bold": curses.A_BOLD,
        "green": pair(1, 108, curses.COLOR_GREEN),
        "yellow": pair(2, 179, curses.COLOR_YELLOW),
        "red": pair(3, 167, curses.COLOR_RED),
        "orange": pair(4, 209, curses.COLOR_YELLOW) | curses.A_BOLD,
        "cyan": pair(5, 109, curses.COLOR_CYAN),
        "dim": pair(6, 245, curses.COLOR_WHITE) if wide else curses.A_DIM,
    })


def put(win, y, segs, w):
    x = 0
    for text, style in segs:
        if x >= w - 1:
            break
        t = text[: w - 1 - x]
        try:
            win.addstr(y, x, t, ATTRS.get(style, 0))
        except curses.error:
            pass
        x += len(t)


def draw(stdscr, st):
    now = time.time()
    h, w = stdscr.getmaxyx()
    stdscr.erase()
    clock = time.strftime("%H:%M")
    put(stdscr, 0, [(" ribbit", "bold"), ("   your own logins, one isolated config dir each", "dim")], w)
    try:
        stdscr.addstr(0, max(0, w - len(clock) - 2), clock, ATTRS["dim"])
    except curses.error:
        pass
    profiles = st["profiles"]
    if not profiles:
        put(stdscr, 3, [("  no profiles. Press a to add one.", "dim")], w)
    lines, starts = build_screen(profiles, st["sel"], now)
    body = max(3, h - 5)
    if profiles:
        s = starts[st["sel"]]
        e = starts[st["sel"] + 1]
        if s < st["scroll"]:
            st["scroll"] = s
        if e - st["scroll"] > body:
            st["scroll"] = min(s, e - body)
        st["scroll"] = max(0, st["scroll"])
    for row, segs in enumerate(lines[st["scroll"]: st["scroll"] + body]):
        put(stdscr, 2 + row, segs, w)
    if st["msg"] and now - st["msg_at"] < 8:
        put(stdscr, h - 3, [(" " + st["msg"], st["msg_style"])], w)
    put(stdscr, h - 2, [(" ↑↓ select   enter launch   a add   e edit   d delete   "
                         "i usage hook   q quit", "dim")], w)
    stdscr.refresh()


def say(st, msg, style="normal"):
    st["msg"], st["msg_style"], st["msg_at"] = msg, style, time.time()


def read_line(stdscr, prompt, default="", mask=False):
    h, w = stdscr.getmaxyx()
    y = h - 2
    buf = list(default)
    curses.curs_set(1)
    stdscr.timeout(-1)
    try:
        while True:
            stdscr.move(y, 0)
            stdscr.clrtoeol()
            shown = "".join(buf)
            # A pasted raw key must not be displayed: hide anything long that is not a reference.
            if mask and len(shown) >= 8 and not shown.startswith(("env:", "cmd:", "file:", "prompt")):
                shown = "•" * len(shown)
            text = (prompt + shown)[-(w - 2):]
            try:
                stdscr.addstr(y, 1, text, ATTRS["bold"])
            except curses.error:
                pass
            stdscr.refresh()
            try:
                ch = stdscr.get_wch()
            except curses.error:
                continue
            if ch in ("\n", "\r") or ch == curses.KEY_ENTER:
                return "".join(buf)
            if ch == "\x1b":
                return None
            if ch in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                if buf:
                    buf.pop()
            elif ch == "\x15":
                buf.clear()
            elif isinstance(ch, str) and ch.isprintable():
                buf.append(ch)
    finally:
        curses.curs_set(0)
        stdscr.timeout(2000)


def run_form(stdscr, title, fields, values):
    i = 0
    while i < len(fields):
        f = fields[i]
        err = ""
        while True:
            h, w = stdscr.getmaxyx()
            stdscr.erase()
            put(stdscr, 0, [(" " + title, "bold"), ("   esc cancels", "dim")], w)
            row = 2
            for g in fields[:i]:
                put(stdscr, row, [("   %s: " % g["prompt"], "dim"), (str(values.get(g["key"], "")), "normal")], w)
                row += 1
            row += 1
            for ln in textwrap.wrap(f.get("help", ""), max(20, w - 6)):
                put(stdscr, row, [("   " + ln, "cyan")], w)
                row += 1
            if err:
                put(stdscr, row + 1, [("   " + err, "red")], w)
            dflt = values.get(f["key"])
            if dflt is None:
                d = f.get("default", "")
                dflt = d(values) if callable(d) else d
            val = read_line(stdscr, f["prompt"] + ": ", dflt, mask=f.get("secret", False))
            if val is None:
                return False
            val = val.strip()
            msg = f["validate"](val, values) if f.get("validate") else None
            if msg:
                # never keep a rejected key-field value, so it is not shown or prefilled again
                values[f["key"]] = "" if f.get("secret") else val
                err = msg
                continue
            values[f["key"]] = val
            break
        i += 1
    return True


def fields_for(ptype, taken, editing):
    def v_name(v, vals):
        if not NAME_RE.match(v):
            return "lowercase letters, digits, - and _ only (max 31)"
        if v in taken:
            return "a profile with that name exists"

    def v_ref(required):
        return lambda v, vals: check_ref(v, required)

    def v_url(v, vals):
        if v and not re.match(r"^https?://\S+$", v):
            return "must start with http:// or https://"

    def v_resource(v, vals):
        if v and not re.match(r"^[A-Za-z0-9-]+$", v):
            return "resource name only (no URL, no dots); use the base URL field for a URL"

    def v_foundry_target(v, vals):
        e = v_url(v, vals)
        if e:
            return e
        if not v and not vals.get("resource"):
            return "need a resource name or a base URL"

    def v_model(v, vals):
        if v and not re.match(r"^[A-Za-z0-9._:/-]+$", v):
            return "letters, digits and . _ : / - only"

    def v_cmd(v, vals):
        if not v:
            return "required"

    def v_env(v, vals):
        try:
            parse_extra_env(v)
        except ValueError as e:
            return str(e)

    cfg = {"key": "config_dir", "prompt": "config dir",
           "default": lambda vals: "~/.claude-" + vals.get("name", "x"),
           "help": "Isolated Claude Code config dir. First launch asks you to log in; "
                   "that login stays in this directory."}
    refhelp = ("A reference, not the key: env:NAME, cmd:<command>, file:<path> or prompt. "
               "Examples: env:ANTHROPIC_API_KEY   cmd:pass show anthropic/api")
    F = []
    if not editing:
        F.append({"key": "name", "prompt": "name", "validate": v_name,
                  "help": "Short id used in the list, usage files and `run <name>`. Cannot be changed later."})
    F.append({"key": "label", "prompt": "label", "default": lambda vals: vals.get("name", "").title(),
              "help": "Display label, e.g. Personal, Work."})
    if ptype == "subscription":
        F.append(cfg)
    elif ptype == "anthropic":
        F.append({"key": "key", "secret": True, "prompt": "key ref", "validate": v_ref(True),
                  "default": "env:ANTHROPIC_API_KEY",
                  "help": refhelp})
        F.append(cfg)
        F.append({"key": "base_url", "prompt": "base url (optional)", "validate": v_url,
                  "help": "Leave blank for api.anthropic.com. Set it to use a gateway."})
        F.append({"key": "model", "prompt": "model (optional)", "validate": v_model,
                  "help": "Sets ANTHROPIC_MODEL. Blank uses Claude Code's default."})
    elif ptype == "foundry":
        F.append({"key": "resource", "prompt": "resource name", "validate": v_resource,
                  "help": "Your Foundry resource name, e.g. my-resource. Leave blank if you will give a base URL."})
        F.append({"key": "base_url", "prompt": "base url (optional)", "validate": v_foundry_target,
                  "help": "Alternative to the resource name, e.g. https://my-resource.services.ai.azure.com/anthropic"})
        F.append({"key": "key", "secret": True, "prompt": "key ref (blank = Entra ID)", "validate": v_ref(False),
                  "help": "Blank uses your az login / Azure default credential chain. Otherwise: " + refhelp})
        F.append(cfg)
        for k in ("opus", "sonnet", "haiku"):
            F.append({"key": "m_" + k, "prompt": k + " deployment (optional)", "validate": v_model,
                      "help": "Deployment name you created in Foundry for this model. Claude Code's docs say to "
                              "pin these; unpinned aliases can resolve to a model your account lacks."})
    elif ptype == "openai":
        F.append({"key": "key", "secret": True, "prompt": "key ref", "validate": v_ref(True),
                  "default": "env:OPENAI_API_KEY",
                  "help": refhelp + "   Claude Code cannot use OpenAI models; this launches another tool."})
        F.append({"key": "base_url", "prompt": "base url (optional)", "validate": v_url,
                  "help": "Sets OPENAI_BASE_URL, e.g. for Azure OpenAI or a gateway."})
        F.append({"key": "command", "prompt": "command", "validate": v_cmd,
                  "help": "The tool to run with OPENAI_API_KEY set, e.g. codex or aider."})
        F.append({"key": "args", "prompt": "args (optional)", "help": "Arguments always passed to the command."})
    F.append({"key": "extra_env", "prompt": "extra env (optional)", "validate": v_env,
              "help": "Non-secret KEY=VALUE pairs, space separated."})
    return F


def values_from_profile(p):
    v = {k: p.get(k, "") for k in ("name", "label", "config_dir", "key", "base_url", "model",
                                    "resource", "command", "args")}
    for k, val in (p.get("models") or {}).items():
        v["m_" + k] = val
    v["extra_env"] = " ".join("%s=%s" % kv for kv in (p.get("extra_env") or {}).items())
    return v


def profile_from_values(ptype, v):
    p = {"name": v["name"], "label": v.get("label") or v["name"], "type": ptype}
    for k in ("config_dir", "key", "base_url", "model", "resource", "command", "args"):
        if v.get(k):
            p[k] = v[k]
    models = {k: v["m_" + k] for k in ("opus", "sonnet", "haiku") if v.get("m_" + k)}
    if models:
        p["models"] = models
    if v.get("extra_env"):
        p["extra_env"] = parse_extra_env(v["extra_env"])
    return p


def pick_type(stdscr):
    h, w = stdscr.getmaxyx()
    stdscr.erase()
    put(stdscr, 0, [(" add profile", "bold"), ("   esc cancels", "dim")], w)
    order = list(TYPES)
    for n, t in enumerate(order, 1):
        put(stdscr, 1 + n, [("   %d  " % n, "orange"), (TYPES[t], "normal")], w)
    stdscr.refresh()
    stdscr.timeout(-1)
    try:
        while True:
            try:
                ch = stdscr.get_wch()
            except curses.error:
                continue
            if ch == "\x1b":
                return None
            if isinstance(ch, str) and ch in "1234"[: len(order)] and ch:
                return order[int(ch) - 1]
    finally:
        stdscr.timeout(2000)


def confirm(stdscr, text):
    h, w = stdscr.getmaxyx()
    stdscr.move(h - 2, 0)
    stdscr.clrtoeol()
    try:
        stdscr.addstr(h - 2, 1, text + " (y/N) ", ATTRS["bold"])
    except curses.error:
        pass
    stdscr.refresh()
    stdscr.timeout(-1)
    try:
        while True:
            try:
                ch = stdscr.get_wch()
            except curses.error:
                continue
            return isinstance(ch, str) and ch.lower() == "y"
    finally:
        stdscr.timeout(2000)


def do_launch(stdscr, st):
    p = st["profiles"][st["sel"]]
    curses.def_prog_mode()
    curses.endwin()
    err = None
    try:
        print("\n  -> %s  (%s)\n" % (p["name"], TAGS.get(p["type"], p["type"])))
        code = launch(p)
        if code:
            say(st, "%s exited with status %s" % (p["name"], code), "yellow")
    except (ProfileError, OSError) as e:
        err = str(e)
    except KeyboardInterrupt:
        pass
    if err:
        print("\n  x %s" % err)
        try:
            input("  press Enter to return ")
        except (EOFError, KeyboardInterrupt):
            pass
    curses.reset_prog_mode()
    stdscr.clear()
    stdscr.refresh()


def tui(stdscr):
    curses.curs_set(0)
    init_attrs()
    stdscr.timeout(2000)
    profiles, note = load_profiles()
    st = {"profiles": profiles, "sel": 0, "scroll": 0, "msg": "", "msg_style": "normal", "msg_at": 0}
    if note:
        say(st, note, "yellow")
    while True:
        draw(stdscr, st)
        try:
            ch = stdscr.get_wch()
        except curses.error:
            continue
        n = len(st["profiles"])
        if ch in ("q", "Q", "\x1b"):
            break
        elif ch in (curses.KEY_UP, "k") and n:
            st["sel"] = (st["sel"] - 1) % n
        elif ch in (curses.KEY_DOWN, "j") and n:
            st["sel"] = (st["sel"] + 1) % n
        elif isinstance(ch, str) and ch.isdigit() and ch != "0" and int(ch) <= n:
            st["sel"] = int(ch) - 1
        elif (ch in ("\n", "\r") or ch == curses.KEY_ENTER) and n:
            do_launch(stdscr, st)
        elif ch == "a":
            t = pick_type(stdscr)
            if t:
                vals = {}
                names = {p["name"] for p in st["profiles"]}
                if run_form(stdscr, "add %s profile" % TAGS[t], fields_for(t, names, False), vals):
                    try:
                        st["profiles"].append(profile_from_values(t, vals))
                        save_profiles(st["profiles"])
                        st["sel"] = len(st["profiles"]) - 1
                        say(st, "saved " + vals["name"], "green")
                    except (ValueError, OSError) as e:
                        say(st, "not saved: %s" % e, "red")
        elif ch == "e" and n:
            p = st["profiles"][st["sel"]]
            vals = values_from_profile(p)
            if run_form(stdscr, "edit " + p["name"], fields_for(p["type"], set(), True), vals):
                try:
                    st["profiles"][st["sel"]] = profile_from_values(p["type"], vals)
                    save_profiles(st["profiles"])
                    say(st, "saved " + p["name"], "green")
                except (ValueError, OSError) as e:
                    say(st, "not saved: %s" % e, "red")
        elif ch == "d" and n:
            p = st["profiles"][st["sel"]]
            if confirm(stdscr, "Delete profile '%s'? (its config dir is left alone)" % p["name"]):
                st["profiles"].pop(st["sel"])
                st["sel"] = max(0, min(st["sel"], len(st["profiles"]) - 1))
                try:
                    save_profiles(st["profiles"])
                    say(st, "deleted " + p["name"], "green")
                except OSError as e:
                    say(st, "not saved: %s" % e, "red")
        elif ch == "i" and n:
            p = st["profiles"][st["sel"]]
            if p["type"] != "subscription":
                say(st, "only subscription profiles report usage limits", "yellow")
            else:
                try:
                    say(st, install_hook(p), "green")
                except OSError as e:
                    say(st, "hook not installed: %s" % e, "red")


# ---------------------------------------------------------------- CLI entry points

def cli_run(argv):
    if not argv:
        print("usage: ribbit.py run <profile> [-- extra args]", file=sys.stderr)
        return 2
    name, rest = argv[0], argv[1:]
    if rest[:1] == ["--"]:
        rest = rest[1:]
    profiles, _ = load_profiles()
    prof = next((p for p in profiles if p["name"] == name), None)
    if not prof:
        print("no profile named %s" % name, file=sys.stderr)
        return 2
    try:
        return launch(prof, rest)
    except (ProfileError, OSError) as e:
        print("x %s" % e, file=sys.stderr)
        return 1


def cli_list():
    profiles, note = load_profiles()
    if note:
        print(note, file=sys.stderr)
    now = time.time()
    for i, p in enumerate(profiles, 1):
        bits = []
        st = read_state(p["name"]) if p["type"] != "openai" else None
        rl = (st or {}).get("rate_limits")
        if isinstance(rl, dict):
            for key, w in rl.items():
                if isinstance(w, dict) and isinstance(w.get("used_percentage"), (int, float)):
                    reset = parse_reset(w)
                    tail = ""
                    if reset is not None:
                        tail = " (resets %s)" % fmt_dur(reset - now) if reset > now else " (reset)"
                    bits.append("%s %d%%%s" % (label(key), int(w["used_percentage"]), tail))
        print("%d  %-14s %-8s %s" % (i, p["name"], TAGS.get(p["type"], p["type"]), "  ".join(bits)))
    return 0


def main():
    a = sys.argv[1:]
    if a[:1] == ["hook"]:
        return cmd_hook()
    if a[:1] == ["run"]:
        return cli_run(a[1:])
    if a[:1] == ["list"]:
        return cli_list()
    if a[:1] in (["-h"], ["--help"]):
        print(__doc__)
        return 0
    if curses is None:
        print("curses is not available. On Windows: pip install windows-curses", file=sys.stderr)
        return 1
    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")
    curses.wrapper(tui)
    return 0


if __name__ == "__main__":
    sys.exit(main())
