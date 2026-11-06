import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "ribbit.py")
root = tempfile.mkdtemp(prefix="ribbit-robust-")
os.makedirs(root + "/home")
os.makedirs(root + "/bin")
os.environ["HOME"] = root + "/home"
os.environ["XDG_CONFIG_HOME"] = root + "/cfg"
os.environ["XDG_STATE_HOME"] = root + "/state"
sys.path.insert(0, REPO)
import ribbit as h

fails = 0


def check(name, cond, extra=""):
    global fails
    if not cond:
        fails += 1
    print(("PASS " if cond else "FAIL ") + name + ((" :: " + str(extra)) if (extra and not cond) else ""))


def raises(fn, exc):
    try:
        fn()
    except exc as e:
        return str(e) or "raised"
    except Exception as e:  # wrong type
        return None
    return None


cfg = h.config_path()
cfg.parent.mkdir(parents=True, exist_ok=True)

# ---- #2: OSError from launch() becomes ProfileError
blocker = os.path.join(root, "afile")
open(blocker, "w").write("x")
os.makedirs(root + "/bin", exist_ok=True)
fake = root + "/bin/claude"
open(fake, "w").write("#!/bin/sh\nexit 0\n")
os.chmod(fake, 0o755)
os.environ["PATH"] = root + "/bin" + os.pathsep + os.environ["PATH"]

msg = raises(lambda: h.launch({"name": "t", "type": "subscription",
                               "config_dir": blocker + "/sub"}), h.ProfileError)
check("launch: unusable config dir -> ProfileError", msg is not None, msg)

bad = root + "/bin/badtool"
open(bad, "w").write("#!/nonexistent/interpreter\n")
os.chmod(bad, 0o755)
os.environ["RIBBIT_T_KEY"] = "k" * 12
msg = raises(lambda: h.launch({"name": "o", "type": "openai", "command": "badtool", "key": "env:RIBBIT_T_KEY"}),
             h.ProfileError)
check("launch: unexecutable command -> ProfileError", msg is not None, msg)

# CLI exit code and message (no traceback)
cfg.write_text(json.dumps({"profiles": [
    {"name": "t", "type": "subscription", "config_dir": blocker + "/sub"}]}))
r = subprocess.run([sys.executable, SCRIPT, "run", "t"], capture_output=True, text=True, env=os.environ)
check("cli run: exit 1 on OSError", r.returncode == 1, (r.returncode, r.stderr[-300:]))
check("cli run: no traceback", "Traceback" not in r.stderr, r.stderr[-300:])
check("cli run: readable message", r.stderr.startswith("x "), r.stderr[:200])

# ---- #3: malformed profiles
good = {"name": "good", "label": "G", "type": "subscription", "config_dir": "~/.claude-good"}
mixed = {"profiles": [
    good,
    {"name": "notype"},
    "just a string",
    {"name": "BAD NAME", "type": "subscription"},
    {"name": "weird", "type": "carrier-pigeon"},
    {"name": 5, "type": "anthropic"},
    dict(good),                     # duplicate of "good"
    {"name": "second", "type": "openai", "command": "codex", "key": "env:OPENAI_API_KEY"},
]}
cfg.write_text(json.dumps(mixed))
bak = cfg.with_suffix(".json.bak")
if bak.exists():
    bak.unlink()
profs, note = h.load_profiles()
check("load: keeps only valid entries", [p["name"] for p in profs] == ["good", "second"], profs)
check("load: reports skipped count", "skipped 6" in note, note)
check("load: backup written", bak.exists())
check("load: backup is the original", json.loads(bak.read_text()) == mixed)

# an existing backup is not overwritten by a later load
bak.write_text("keep me")
h.load_profiles()
check("load: existing backup not overwritten", bak.read_text() == "keep me")

# a clean file gives no note and no backup
bak.unlink()
cfg.write_text(json.dumps({"profiles": [good]}))
profs, note = h.load_profiles()
check("load: clean file has no note", note == "", note)
check("load: clean file creates no backup", not bak.exists())

# TUI screen builds from the cleaned list (used to raise KeyError: 'type')
cfg.write_text(json.dumps(mixed))
profs, note = h.load_profiles()
try:
    h.build_screen(profs, 0, time.time())
    ok = True
except Exception as e:
    ok = False
check("build_screen works on loaded profiles", ok)

# CLI list works on the same file
bak.unlink()
r = subprocess.run([sys.executable, SCRIPT, "list"], capture_output=True, text=True, env=os.environ)
check("cli list: exit 0 on a mixed file", r.returncode == 0, r.stderr[-300:])
check("cli list: lists valid names", "good" in r.stdout and "second" in r.stdout, r.stdout)
check("cli list: mentions skipped on stderr", "skipped" in r.stderr, r.stderr)
check("cli list: no traceback", "Traceback" not in r.stderr, r.stderr[-300:])

# cli run on a skipped profile says there is no such profile
r = subprocess.run([sys.executable, SCRIPT, "run", "notype"], capture_output=True, text=True, env=os.environ)
check("cli run: skipped profile not runnable", r.returncode == 2 and "no profile named" in r.stderr, r.stderr)

# top-level shapes that are not a dict
for raw in ("[]", '"x"', "null"):
    cfg.write_text(raw)
    profs, note = h.load_profiles()
    check("load: top-level %s falls back to defaults" % raw, len(profs) == 2 and note != "", note)

# ---- #6: install_hook with odd settings.json shapes
prof = {"name": "hk", "type": "subscription", "config_dir": root + "/cd"}
os.makedirs(root + "/cd", exist_ok=True)
sj = h.settings_path(prof)
for raw, expect in (("[]", "not a JSON object"), ('"text"', "not a JSON object"), ("123", "not a JSON object"),
                    ("null", "not a JSON object")):
    sj.write_text(raw)
    try:
        m = h.install_hook(prof)
        crashed = False
    except Exception as e:
        m, crashed = repr(e), True
    check("install_hook %s: no crash" % raw, not crashed, m)
    check("install_hook %s: message" % raw, expect in m, m)
    check("install_hook %s: file untouched" % raw, sj.read_text() == raw)

for sl in ('"echo hi"', "[1, 2]", "5", '{"type": "command"}'):
    sj.write_text('{"statusLine": %s}' % sl)
    before = sj.read_text()
    try:
        m = h.install_hook(prof)
        crashed = False
    except Exception as e:
        m, crashed = repr(e), True
    check("install_hook statusLine=%s: no crash" % sl, not crashed, m)
    check("install_hook statusLine=%s: refuses" % sl, "already has a statusLine" in m, m)
    check("install_hook statusLine=%s: file untouched" % sl, sj.read_text() == before)

# the normal path still works
sj.unlink()
check("install_hook: fresh install", "installed" in h.install_hook(prof))
check("install_hook: idempotent", "already installed" in h.install_hook(prof))

shutil.rmtree(root, ignore_errors=True)
print("\nFAILED: %d" % fails)
sys.exit(1 if fails else 0)
