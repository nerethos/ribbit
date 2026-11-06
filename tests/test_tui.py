import fcntl, json, os, pty, re, select, shutil, struct, sys, tempfile, termios, time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "ribbit.py")
SP = tempfile.mkdtemp(prefix="ribbit-test-")
root = SP + "/tuitest"
shutil.rmtree(root, ignore_errors=True)
os.makedirs(root + "/home"); os.makedirs(root + "/bin")
fake = root + "/bin/claude"
open(fake, "w").write('#!/bin/sh\nenv | grep -E "^(CLAUDE_|ANTHROPIC_)" | sort > "$HUB_OUT"\nexit 0\n')
os.chmod(fake, 0o755)

env = dict(os.environ, HOME=root + "/home", XDG_CONFIG_HOME=root + "/cfg", XDG_STATE_HOME=root + "/state",
           TERM="xterm-256color", LANG="C.UTF-8", LC_ALL="C.UTF-8", HUB_OUT=root + "/out.txt",
           PATH=root + "/bin:" + os.environ["PATH"], ANTHROPIC_API_KEY="GLOBAL-SHOULD-NOT-LEAK")

pid, fd = pty.fork()
if pid == 0:
    fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 32, 110, 0, 0))
    os.execvpe(sys.executable, [sys.executable, SCRIPT], env)

log = b""


def _read(wait):
    """Read whatever arrives within `wait` seconds; b'' if nothing."""
    global log
    r, _, _ = select.select([fd], [], [], wait)
    if not r:
        return b""
    try:
        chunk = os.read(fd, 65536)
    except OSError:
        return b""
    log += chunk
    return chunk


def drain(quiet=0.4, maxwait=8.0):
    """Read until the terminal has been silent for `quiet` seconds (or maxwait passes)."""
    data = b""
    start = last = time.time()
    while time.time() - start < maxwait:
        chunk = _read(0.05)
        if chunk:
            data += chunk
            last = time.time()
        elif time.time() - last >= quiet:
            break
    return data


def text(b):
    return re.sub(r"\x1b(\[[0-9;?]*[A-Za-z]|\([A-Z0-9]|[=>])", "", b.decode("utf-8", "replace"))


def send(s, quiet=0.4):
    """Send keys, then wait for the screen to settle."""
    os.write(fd, s.encode() if isinstance(s, str) else s)
    return drain(quiet)


def send_expect(s, needle, timeout=8.0):
    """Send keys and read until `needle` appears in the output (or timeout). Returns the text read."""
    os.write(fd, s.encode())
    data = b""
    end = time.time() + timeout
    while time.time() < end and needle not in text(data):
        data += _read(0.05)
    data += drain(0.2, 2.0)      # pick up the rest of the repaint
    return text(data)


def wait_until(pred, timeout=8.0):
    """Poll a condition (usually a file check) instead of sleeping and hoping."""
    end = time.time() + timeout
    while time.time() < end:
        try:
            if pred():
                return True
        except Exception:
            pass
        _read(0.05)              # keep the terminal drained meanwhile
    return False


def read_json(path):
    try:
        return json.load(open(path))
    except Exception:
        return {"profiles": []}


fails = 0
def check(name, cond, extra=""):
    global fails
    if not cond:
        fails += 1
    print(("PASS " if cond else "FAIL ") + name + ((" :: " + str(extra)[:300]) if (extra and not cond) else ""))


# the first paint can take a moment on a slow machine: wait for it rather than guessing
first = ""
data = b""
end = time.time() + 15
while time.time() < end and not ("personal" in first and "work" in first and "enter launch" in first):
    data += _read(0.1)
    first = text(data)
first += text(drain(0.3, 2.0))
check("first screen has title and both default profiles", all(s in first for s in ("ribbit", "personal", "work")), first[:400])
check("first screen has usage hint + key help", "usage hook" in first and "enter launch" in first)

# launch 'work' (select with j, Enter) -> fake claude runs and exits, TUI returns
send("j"); send("\r")
out_path = root + "/out.txt"
wait_until(lambda: os.path.exists(out_path) and "CLAUDE_ACCOUNT=work" in open(out_path).read())
out = open(out_path).read() if os.path.exists(out_path) else ""
check("enter launches selected profile in its own env", "CLAUDE_ACCOUNT=work" in out and "CLAUDE_CONFIG_DIR=" in out, out)
check("global ANTHROPIC_API_KEY not leaked to subscription profile", "ANTHROPIC_API_KEY" not in out, out)
drain()

# add subscription profile t1
send("a"); send("1"); send("t1\r"); send("\r"); send("\r"); send("\r")
cfgp = root + "/cfg/ribbit/profiles.json"
wait_until(lambda: any(p["name"] == "t1" for p in read_json(cfgp)["profiles"]))
t1 = next((p for p in read_json(cfgp)["profiles"] if p["name"] == "t1"), None)
check("add subscription profile saved with defaults", t1 and t1["type"] == "subscription" and t1["config_dir"] == "~/.claude-t1" and t1["label"] == "T1", t1)

# add anthropic profile: raw key must be rejected, then a reference accepted
send("a"); send("2"); send("api1\r"); send("\r")                     # name, label
send("\x15")                                                          # clear the prefilled default, then paste a raw key
pasted = send_expect("sk-ant-api03-abcdefghij1234567", "•")
check("pasted raw key is masked while typing", "sk-ant-api03" not in pasted and "•" in pasted, pasted[-200:])
bad = send_expect("\r", "not stored")
check("raw key rejected on screen", "not stored" in bad, bad[-300:])
check("rejected key not echoed back", "sk-ant-api03" not in bad, bad[-300:])
send("env:ANTHROPIC_API_KEY\r")                                       # field was cleared; type a reference
send("\r"); send("\r"); send("\r"); send("\r")                        # config dir, base url, model, extra env
wait_until(lambda: any(p["name"] == "api1" for p in read_json(cfgp)["profiles"]))
raw = open(cfgp).read()
data = json.loads(raw)
api1 = next((p for p in data["profiles"] if p["name"] == "api1"), None)
check("anthropic profile saved with key reference only", api1 and api1["key"] == "env:ANTHROPIC_API_KEY" and "sk-ant" not in raw, raw[-400:])

# edit api1: change label
send("e"); send("\x15"); send("My API\r")
for _ in range(5):
    send("\r", 0.25)
wait_until(lambda: next((p for p in read_json(cfgp)["profiles"] if p["name"] == "api1"), {}).get("label") == "My API")
data = read_json(cfgp)
check("edit changes label", next(p for p in data["profiles"] if p["name"] == "api1")["label"] == "My API")

# launch the anthropic profile: resolves env:ANTHROPIC_API_KEY from hub's environment
send("\r")
wait_until(lambda: "CLAUDE_ACCOUNT=api1" in (open(out_path).read() if os.path.exists(out_path) else ""))
out = open(out_path).read()
check("launch api profile passes referenced key", "ANTHROPIC_API_KEY=GLOBAL-SHOULD-NOT-LEAK" in out and "CLAUDE_ACCOUNT=api1" in out, out)
drain()

# delete api1
send("d"); send("y")
wait_until(lambda: all(p["name"] != "api1" for p in read_json(cfgp)["profiles"]))
data = read_json(cfgp)
check("delete removes profile", all(p["name"] != "api1" for p in data["profiles"]))

# install hook on profile 1 (personal)
send("1"); send("i")
sp = root + "/home/.claude-personal/settings.json"
wait_until(lambda: os.path.exists(sp) and "statusLine" in json.load(open(sp)))
check("i installs hook into selected profile's settings.json",
      os.path.exists(sp) and "ribbit.py" in json.load(open(sp))["statusLine"]["command"])

# quit
os.write(fd, b"q")
code = None
end = time.time() + 10
while time.time() < end:
    _read(0.05)
    done, status = os.waitpid(pid, os.WNOHANG)
    if done:
        code = os.WEXITSTATUS(status) if os.WIFEXITED(status) else -1
        break
check("q exits cleanly with status 0", code == 0, code)
check("no traceback in terminal output", b"Traceback" not in log, text(log)[-600:])
shutil.rmtree(SP, ignore_errors=True)
print("\nFAILED: %d" % fails)
sys.exit(1 if fails else 0)
