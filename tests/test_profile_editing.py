import fcntl
import json
import os
import pty
import re
import select
import shutil
import struct
import sys
import tempfile
import termios
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "ribbit.py")
SP = tempfile.mkdtemp(prefix="ribbit-edit-")
root = SP + "/t"
os.makedirs(root + "/home")
os.makedirs(root + "/bin")
open(root + "/bin/claude", "w").write("#!/bin/sh\nexit 0\n")
os.chmod(root + "/bin/claude", 0o755)

os.environ["HOME"] = root + "/home"
os.environ["XDG_CONFIG_HOME"] = root + "/cfg"
os.environ["XDG_STATE_HOME"] = root + "/state"
os.environ["PATH"] = root + "/bin" + os.pathsep + os.environ["PATH"]
sys.path.insert(0, REPO)
import ribbit as h

fails = 0


def check(name, cond, extra=""):
    global fails
    if not cond:
        fails += 1
    print(("PASS " if cond else "FAIL ") + name + ((" :: " + str(extra)[:300]) if (extra and not cond) else ""))


def names():
    return [p["name"] for p in json.load(open(h.config_path()))["profiles"]]


def sub(name, label=None):
    return {"name": name, "label": label or name, "type": "subscription", "config_dir": "~/.claude-" + name}


# ---------------------------------------------------------------- #4 extra_env round trip
for env in ({"A": "b c"}, {"A": "1", "B": "2"}, {"MSG": "it's fine"}, {"P": 'say "hi" there'},
            {"E": ""}, {"PATHS": "/a b/c:/d e/f"}):
    p = {"name": "x", "type": "anthropic", "key": "env:ANTHROPIC_API_KEY", "extra_env": env}
    try:
        back = h.profile_from_values("anthropic", h.values_from_profile(p))["extra_env"]
    except Exception as e:
        back = repr(e)
    check("extra_env round trip %r" % (env,), back == env, back)

p = {"name": "x", "type": "anthropic", "key": "env:ANTHROPIC_API_KEY", "extra_env": {"A": "1", "B": "2"}}
check("simple extra_env text is unchanged", h.values_from_profile(p)["extra_env"] == "A=1 B=2",
      h.values_from_profile(p)["extra_env"])

# ---------------------------------------------------------------- #5 change helpers
ps = [sub("a"), sub("b")]
h.add_profile(ps, sub("c"))
check("add_profile appends", [q["name"] for q in ps] == ["a", "b", "c"])
try:
    h.add_profile(ps, sub("a"))
    ok = False
except ValueError as e:
    ok = "already exists" in str(e)
check("add_profile rejects a duplicate name", ok)

h.replace_profile(ps, sub("b", "Bee"))
check("replace_profile swaps by name", [q["label"] for q in ps] == ["a", "Bee", "c"])
try:
    h.replace_profile(ps, sub("zzz"))
    ok = False
except ValueError as e:
    ok = "no longer exists" in str(e)
check("replace_profile errors on a missing name", ok)

h.remove_profile(ps, "a")
check("remove_profile removes by name", [q["name"] for q in ps] == ["b", "c"])
h.remove_profile(ps, "nope")
check("remove_profile is a no-op for a missing name", [q["name"] for q in ps] == ["b", "c"])

# ---------------------------------------------------------------- #5 two instances (logic level)
cfg = h.config_path()
cfg.parent.mkdir(parents=True, exist_ok=True)
h.save_profiles([sub("base")])
inst1, _ = h.load_profiles()            # both "instances" start from the same file
inst2, _ = h.load_profiles()
h.update_profiles(lambda ps: h.add_profile(ps, sub("from2")))     # instance 2 saves first
fresh = h.update_profiles(lambda ps: h.add_profile(ps, sub("from1")))   # instance 1 saves second
check("both instances' additions survive", names() == ["base", "from2", "from1"], names())
check("update_profiles returns the merged list", [q["name"] for q in fresh] == ["base", "from2", "from1"])

try:
    h.update_profiles(lambda ps: h.add_profile(ps, sub("from2")))
    ok = False
except ValueError:
    ok = True
check("conflicting add errors", ok)
check("a failed change writes nothing", names() == ["base", "from2", "from1"], names())

h.update_profiles(lambda ps: h.remove_profile(ps, "from2"))
check("remove only touches its own profile", names() == ["base", "from1"], names())

# ---------------------------------------------------------------- #5 two instances (real TUI)
env = dict(os.environ, TERM="xterm-256color", LANG="C.UTF-8", LC_ALL="C.UTF-8")
cfg.unlink()
pid, fd = pty.fork()
if pid == 0:
    fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 32, 110, 0, 0))
    os.execvpe(sys.executable, [sys.executable, SCRIPT], env)

log = b""


def drain(quiet=0.3, maxwait=5.0):
    """Read until the terminal has been silent for `quiet` seconds."""
    global log
    data = b""
    start = last = time.time()
    while time.time() - start < maxwait:
        r, _, _ = select.select([fd], [], [], 0.05)
        if r:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                break
            data += chunk
            last = time.time()
        elif time.time() - last >= quiet:
            break
    log += data
    return data


def send(s):
    os.write(fd, s.encode())
    return drain()


def text(b):
    return re.sub(r"\x1b(\[[0-9;?]*[A-Za-z]|\([A-Z0-9]|[=>])", "", b.decode("utf-8", "replace"))


def wait_for(pred, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if pred():
                return True
        except Exception:
            pass
        time.sleep(0.05)
    return False


first = text(drain(0.5, 8.0))
check("TUI started on the default profiles", "personal" in first and "work" in first, first[:200])

# another instance adds a profile while this one is open
h.save_profiles([sub("personal"), sub("work"), sub("ext", "External")])

# add t1 from the TUI
send("a")
send("1")
send("t1\r")
send("\r")
send("\r")
send("\r")
wait_for(lambda: "t1" in names())
check("TUI add keeps a profile added by another instance",
      names() == ["personal", "work", "ext", "t1"], names())

# adding a name that appeared elsewhere meanwhile is refused, not overwritten
h.save_profiles([sub("personal"), sub("work"), sub("ext", "External"), sub("t1"), sub("dup", "Original")])
send("a")
send("1")
send("dup\r")
send("\r")
send("\r")
screen = text(send("\r"))
check("duplicate from another instance is refused on screen", "already exists" in screen, screen[-300:])
data = json.load(open(h.config_path()))["profiles"]
check("the original duplicate is kept untouched",
      [q["label"] for q in data if q["name"] == "dup"] == ["Original"], data)

# delete the selected profile; a profile added elsewhere in the meantime survives
h.save_profiles([sub("personal"), sub("work"), sub("ext", "External"), sub("t1"), sub("dup", "Original"),
                 sub("late")])
send("1")
send("d")
send("y")
wait_for(lambda: "personal" not in names())
check("TUI delete keeps a profile added by another instance",
      "late" in names() and "personal" not in names(), names())

send("q")
code = None
for _ in range(40):
    done, status = os.waitpid(pid, os.WNOHANG)
    if done:
        code = os.WEXITSTATUS(status) if os.WIFEXITED(status) else -1
        break
    time.sleep(0.1)
check("TUI exits cleanly", code == 0, code)
check("no traceback in terminal output", b"Traceback" not in log, text(log)[-500:])

shutil.rmtree(SP, ignore_errors=True)
print("\nFAILED: %d" % fails)
sys.exit(1 if fails else 0)
