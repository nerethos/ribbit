import contextlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "ribbit.py")
SP = tempfile.mkdtemp(prefix="ribbit-hook-")
root = SP + "/t"
os.makedirs(root + "/home")
os.makedirs(root + "/bin")
os.environ["HOME"] = root + "/home"
os.environ["XDG_CONFIG_HOME"] = root + "/cfg"
os.environ["XDG_STATE_HOME"] = root + "/state"
sys.path.insert(0, REPO)
import ribbit as h

POSIX = os.name != "nt"
fails = 0


def check(name, cond, extra=""):
    global fails
    if not cond:
        fails += 1
    print(("PASS " if cond else "FAIL ") + name + ((" :: " + str(extra)[:300]) if (extra and not cond) else ""))


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


PAYLOAD = json.dumps({"rate_limits": {"five_hour": {"used_percentage": 42}}})


def run_hook(state_home, extra_env=None):
    env = dict(os.environ, XDG_STATE_HOME=state_home, CLAUDE_ACCOUNT="z")
    env.pop("RIBBIT_DEBUG", None)
    env.update(extra_env or {})
    return subprocess.run([sys.executable, SCRIPT, "hook"], input=PAYLOAD, capture_output=True, text=True, env=env)


# ---------------------------------------------------------------- #7 hook failures are visible
st_ok = root + "/state_ok"
r = run_hook(st_ok)
check("hook ok: exit 0", r.returncode == 0, r.stderr)
check("hook ok: normal status line", r.stdout.startswith("z |") and "42%" in r.stdout, r.stdout)
check("hook ok: no failure note", "usage not saved" not in r.stdout, r.stdout)
check("hook ok: snapshot written", os.path.exists(st_ok + "/ribbit/usage/z.json"))

# the usage dir cannot be created because something else sits at that path
st_bad = root + "/state_bad"
os.makedirs(st_bad + "/ribbit")
open(st_bad + "/ribbit/usage", "w").write("in the way")
r = run_hook(st_bad)
check("hook failure: still exit 0", r.returncode == 0, (r.returncode, r.stderr))
check("hook failure: status line still printed", r.stdout.startswith("z"), r.stdout)
check("hook failure: note names the error type", "ribbit: usage not saved (" in r.stdout, r.stdout)
check("hook failure: no traceback", "Traceback" not in r.stderr, r.stderr)
logp = st_bad + "/ribbit/hook-errors.log"
check("hook failure: error appended to the log", os.path.exists(logp) and "usage" not in "" and
      len(open(logp).read().strip().splitlines()) == 1, os.path.exists(logp))
if POSIX:
    check("hook failure: log is private", mode(logp) == 0o600, oct(mode(logp)))

# nowhere writable at all: still never fails
r = run_hook("/proc/nope" if os.path.isdir("/proc") else st_bad + "/ribbit/usage/x")
check("hook, unwritable state home: exit 0", r.returncode == 0, (r.returncode, r.stderr))
check("hook, unwritable state home: note shown", "usage not saved" in r.stdout, r.stdout)

# the log is capped
lp = root + "/capstate/ribbit/hook-errors.log"
os.environ["XDG_STATE_HOME"] = root + "/capstate"
for i in range(400):
    h.log_hook_error(RuntimeError("x" * 200 + str(i)))
size = os.path.getsize(lp)
check("log is capped", size <= h.HOOK_LOG_KEEP + 400, size)
check("log keeps the newest entries", open(lp).read().rstrip().endswith("399"))
check("log starts on a whole line", open(lp).read().splitlines()[0].count("RuntimeError") == 1)
os.environ["XDG_STATE_HOME"] = root + "/state"

# ---------------------------------------------------------------- #8 permissions
if POSIX:
    old = os.umask(0o022)
    try:
        st_perm = root + "/state_perm"
        # an old, loose debug file from an earlier run
        os.makedirs(st_perm + "/ribbit/usage")
        raw = st_perm + "/ribbit/usage/z.raw.txt"
        open(raw, "w").write("old")
        os.chmod(raw, 0o644)
        r = run_hook(st_perm, {"RIBBIT_DEBUG": "1"})
        check("debug: hook exit 0", r.returncode == 0, r.stderr)
        check("debug: payload written", json.loads(open(raw).read())["rate_limits"]["five_hour"]["used_percentage"] == 42)
        check("debug: existing loose file tightened to 0600", mode(raw) == 0o600, oct(mode(raw)))

        st_new = root + "/state_new"
        r = run_hook(st_new, {"RIBBIT_DEBUG": "1"})
        check("debug: new file is 0600", mode(st_new + "/ribbit/usage/z.raw.txt") == 0o600)
        check("state dir is created 0700", mode(st_new + "/ribbit/usage") == 0o700, oct(mode(st_new + "/ribbit/usage")))
        check("snapshot file is 0600", mode(st_new + "/ribbit/usage/z.json") == 0o600)
    finally:
        os.umask(old)

# ---------------------------------------------------------------- #9 key file permissions
keyf = root + "/key.txt"
open(keyf, "w").write("sk-test-value-for-permission-checks-123456\n")
if POSIX:
    os.chmod(keyf, 0o644)
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        val = h.resolve_secret("file:" + keyf, "x")
    check("loose key file: value still returned", val.startswith("sk-test"), val)
    check("loose key file: warns on stderr", "accessible by other users" in err.getvalue() and "chmod 600" in err.getvalue(), err.getvalue())
    check("loose key file: warning does not contain the key", "sk-test" not in err.getvalue())
    txt, sty = h.key_status("file:" + keyf)
    check("loose key file: flagged yellow in the list", sty == "yellow" and "chmod 600" in txt, (txt, sty))

    os.chmod(keyf, 0o640)
    check("group-readable key file is also flagged", h.key_status("file:" + keyf)[1] == "yellow")

    os.chmod(keyf, 0o600)
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        val = h.resolve_secret("file:" + keyf, "x")
    check("private key file: no warning", err.getvalue() == "", err.getvalue())
    txt, sty = h.key_status("file:" + keyf)
    check("private key file: green and found", sty == "green" and "found" in txt, (txt, sty))
check("missing key file is still red", h.key_status("file:" + root + "/nope")[1] == "red")

# ---------------------------------------------------------------- #9 key command stderr
tool = root + "/bin/failing"
open(tool, "w").write('#!/bin/sh\necho "denied for token sk-ant-api03-abcdefghijklmnopqrstuvwx1234 today" >&2\n'
                      'echo "second line AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA" >&2\nexit 3\n')
os.chmod(tool, 0o755)
try:
    h.resolve_secret("cmd:" + tool, "x")
    msg = ""
except h.ProfileError as e:
    msg = str(e)
check("failing cmd: reports the exit status", "exited 3" in msg, msg)
check("failing cmd: key-like token redacted", "sk-ant" not in msg and "<redacted>" in msg, msg)
check("failing cmd: only the first line is shown", "second line" not in msg and "AAAA" not in msg, msg)
check("failing cmd: ordinary words are kept", "denied for token" in msg, msg)

quiet = root + "/bin/quiet"
open(quiet, "w").write("#!/bin/sh\nexit 4\n")
os.chmod(quiet, 0o755)
try:
    h.resolve_secret("cmd:" + quiet, "x")
    msg = ""
except h.ProfileError as e:
    msg = str(e)
check("failing cmd with no stderr: clean message", msg == "key command exited 4", msg)

good = root + "/bin/good"
open(good, "w").write("#!/bin/sh\necho the-secret\n")
os.chmod(good, 0o755)
check("working cmd unaffected", h.resolve_secret("cmd:" + good, "x") == "the-secret")

shutil.rmtree(SP, ignore_errors=True)
print("\nFAILED: %d" % fails)
sys.exit(1 if fails else 0)
