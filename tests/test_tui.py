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
def drain(t=0.4):
    global log
    end = time.time() + t
    data = b""
    while time.time() < end:
        r, _, _ = select.select([fd], [], [], 0.05)
        if r:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                break
            data += chunk
    log += data
    return data

def send(s, t=0.35):
    os.write(fd, s.encode() if isinstance(s, str) else s)
    return drain(t)

def text(b):
    return re.sub(r"\x1b(\[[0-9;?]*[A-Za-z]|\([A-Z0-9]|[=>])", "", b.decode("utf-8", "replace"))

fails = 0
def check(name, cond, extra=""):
    global fails
    if not cond:
        fails += 1
    print(("PASS " if cond else "FAIL ") + name + ((" :: " + str(extra)[:300]) if (extra and not cond) else ""))

first = text(drain(1.2))
check("first screen has title and both default profiles", all(s in first for s in ("ribbit", "personal", "work")), first[:400])
check("first screen has usage hint + key help", "usage hook" in first and "enter launch" in first)

# launch 'work' (select with j, Enter) -> fake claude runs and exits, TUI returns
send("j"); send("\r", 1.0)
out = open(root + "/out.txt").read() if os.path.exists(root + "/out.txt") else ""
check("enter launches selected profile in its own env", "CLAUDE_ACCOUNT=work" in out and "CLAUDE_CONFIG_DIR=" in out, out)
check("global ANTHROPIC_API_KEY not leaked to subscription profile", "ANTHROPIC_API_KEY" not in out, out)

# add subscription profile t1
send("a"); send("1"); send("t1\r"); send("\r"); send("\r"); send("\r", 0.6)
cfgp = root + "/cfg/ribbit/profiles.json"
data = json.load(open(cfgp)) if os.path.exists(cfgp) else {"profiles": []}
t1 = next((p for p in data["profiles"] if p["name"] == "t1"), None)
check("add subscription profile saved with defaults", t1 and t1["type"] == "subscription" and t1["config_dir"] == "~/.claude-t1" and t1["label"] == "T1", t1)

# add anthropic profile: raw key must be rejected, then a reference accepted
send("a"); send("2"); send("api1\r"); send("\r")                     # name, label
send("\x15")                                                          # clear the prefilled default, then paste a raw key
pasted = text(send("sk-ant-api03-abcdefghij1234567", 0.5))
check("pasted raw key is masked while typing", "sk-ant-api03" not in pasted and "•" in pasted, pasted[-200:])
bad = text(send("\r", 0.5))
check("raw key rejected on screen", "not stored" in bad, bad[-300:])
check("rejected key not echoed back", "sk-ant-api03" not in bad, bad[-300:])
send("env:ANTHROPIC_API_KEY\r")                                       # field was cleared; type a reference
send("\r"); send("\r"); send("\r"); send("\r", 0.6)                   # config dir, base url, model, extra env
raw = open(cfgp).read()
data = json.loads(raw)
api1 = next((p for p in data["profiles"] if p["name"] == "api1"), None)
check("anthropic profile saved with key reference only", api1 and api1["key"] == "env:ANTHROPIC_API_KEY" and "sk-ant" not in raw, raw[-400:])

# edit api1: change label
send("e"); send("\x15"); send("My API\r");
for _ in range(5):
    send("\r", 0.25)
drain(0.5)
data = json.load(open(cfgp))
check("edit changes label", next(p for p in data["profiles"] if p["name"] == "api1")["label"] == "My API")

# launch the anthropic profile: resolves env:ANTHROPIC_API_KEY from hub's environment
send("\r", 1.0)
out = open(root + "/out.txt").read()
check("launch api profile passes referenced key", "ANTHROPIC_API_KEY=GLOBAL-SHOULD-NOT-LEAK" in out and "CLAUDE_ACCOUNT=api1" in out, out)

# delete api1
send("d"); send("y", 0.6)
data = json.load(open(cfgp))
check("delete removes profile", all(p["name"] != "api1" for p in data["profiles"]))

# install hook on profile 1 (personal)
send("1"); send("i", 0.6)
sp = root + "/home/.claude-personal/settings.json"
check("i installs hook into selected profile's settings.json",
      os.path.exists(sp) and "ribbit.py" in json.load(open(sp))["statusLine"]["command"])

# quit
send("q", 0.8)
code = None
for _ in range(30):
    done, status = os.waitpid(pid, os.WNOHANG)
    if done:
        code = os.WEXITSTATUS(status) if os.WIFEXITED(status) else -1
        break
    time.sleep(0.1)
check("q exits cleanly with status 0", code == 0, code)
check("no traceback in terminal output", b"Traceback" not in log, text(log)[-600:])
shutil.rmtree(SP, ignore_errors=True)
print("\nFAILED: %d" % fails)
sys.exit(1 if fails else 0)
