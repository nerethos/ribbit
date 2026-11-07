import contextlib, io, json, os, shutil, signal, stat, subprocess, sys, tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "ribbit.py")
SP = tempfile.mkdtemp(prefix="ribbit-test-")
root = os.path.join(SP, "hubtest")
shutil.rmtree(root, ignore_errors=True)
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

# ---- check_ref
check("ref empty required", h.check_ref("", True) is not None)
check("ref empty optional", h.check_ref("", False) is None)
check("ref env ok", h.check_ref("env:ANTHROPIC_API_KEY", True) is None)
check("ref env bad name", h.check_ref("env:bad name", True) is not None)
check("ref raw anthropic key rejected", "not stored" in (h.check_ref("sk-ant-api03-abcdef1234567890", True) or ""))
check("ref raw long token rejected", h.check_ref("A" * 50, True) is not None)
check("ref cmd ok", h.check_ref("cmd:pass show anthropic/api", True) is None)
check("ref prompt ok", h.check_ref("prompt", True) is None)
check("ref file empty", h.check_ref("file:", True) is not None)

# ---- extra env
check("extra env ok", h.parse_extra_env("FOO=bar BAZ=qux") == {"FOO": "bar", "BAZ": "qux"})
for bad in ("ANTHROPIC_API_KEY=x", "X=sk-ant-abcdefghijkl", "noequals", "1BAD=x"):
    try:
        h.parse_extra_env(bad); ok = False
    except ValueError:
        ok = True
    check("extra env rejects " + bad, ok)

# ---- secrets
os.environ["MY_KEY"] = "from-env"
check("resolve env", h.resolve_secret("env:MY_KEY", "t") == "from-env")
try:
    h.resolve_secret("env:NOPE_NOT_SET", "t"); ok = False
except h.ProfileError:
    ok = True
check("resolve env missing raises", ok)
kf = root + "/key.txt"
open(kf, "w").write("filekey\n")
check("resolve file stripped", h.resolve_secret("file:" + kf, "t") == "filekey")
check("resolve cmd", h.resolve_secret("cmd:echo topsecret", "t") == "topsecret")
for bad in ("cmd:false", "cmd:/no/such/binary", "file:/no/such/file"):
    try:
        h.resolve_secret(bad, "t"); ok = False
    except h.ProfileError:
        ok = True
    check("resolve fails cleanly " + bad, ok)

# ---- build_env scrubbing and per-type env
os.environ.update({"ANTHROPIC_API_KEY": "GLOBAL", "CLAUDE_CODE_USE_FOUNDRY": "1",
                   "OPENAI_API_KEY": "GLOBAL", "ANTHROPIC_MODEL": "GLOBAL"})
sub = {"name": "work", "label": "Work", "type": "subscription", "config_dir": "~/.claude-work"}
e = h.build_env(sub, None)
check("sub: config dir expanded", e["CLAUDE_CONFIG_DIR"] == root + "/home/.claude-work", e.get("CLAUDE_CONFIG_DIR"))
check("sub: account set", e["CLAUDE_ACCOUNT"] == "work")
check("sub: globals scrubbed", not any(k in e for k in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_USE_FOUNDRY", "OPENAI_API_KEY", "ANTHROPIC_MODEL")))
api = {"name": "api", "type": "anthropic", "base_url": "https://gw.example", "model": "m1"}
e = h.build_env(api, "S1")
check("anthropic env", e["ANTHROPIC_API_KEY"] == "S1" and e["ANTHROPIC_BASE_URL"] == "https://gw.example" and e["ANTHROPIC_MODEL"] == "m1")
fy = {"name": "fy", "type": "foundry", "resource": "res1", "models": {"sonnet": "son-dep", "opus": "op-dep"}}
e = h.build_env(fy, "FK")
check("foundry env", e["CLAUDE_CODE_USE_FOUNDRY"] == "1" and e["ANTHROPIC_FOUNDRY_RESOURCE"] == "res1"
      and e["ANTHROPIC_FOUNDRY_API_KEY"] == "FK" and e["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "son-dep"
      and "ANTHROPIC_DEFAULT_HAIKU_MODEL" not in e and "ANTHROPIC_API_KEY" not in e)
e = h.build_env(dict(fy, base_url="https://r.services.ai.azure.com/anthropic"), None)
check("foundry base_url wins, Entra (no key)", e["ANTHROPIC_FOUNDRY_BASE_URL"].startswith("https://")
      and "ANTHROPIC_FOUNDRY_RESOURCE" not in e and "ANTHROPIC_FOUNDRY_API_KEY" not in e)
oa = {"name": "oa", "type": "openai", "command": "codex", "base_url": "https://oa.example"}
e = h.build_env(oa, "OK")
check("openai env", e["OPENAI_API_KEY"] == "OK" and e["OPENAI_BASE_URL"] == "https://oa.example"
      and "CLAUDE_CONFIG_DIR" not in e and "ANTHROPIC_API_KEY" not in e)

# ---- save / load
prof = [sub, api, fy, oa]
h.save_profiles(prof)
cp = h.config_path()
check("config mode 600", stat.S_IMODE(os.stat(cp).st_mode) == 0o600, oct(os.stat(cp).st_mode))
loaded, note = h.load_profiles()
check("config roundtrip", loaded == prof and not note)
cp.write_text("{ broken")
loaded, note = h.load_profiles()
check("broken config -> defaults + note + backup", len(loaded) == 2 and "could not parse" in note and cp.with_suffix(".json.broken").exists())
h.save_profiles(prof)

# ---- form value roundtrip
v = h.values_from_profile(fy)
check("form roundtrip foundry", h.profile_from_values("foundry", v) == dict(fy, label="fy"), h.profile_from_values("foundry", v))

# ---- hook installer
cfgdir = root + "/cfg-a"
p = {"name": "a", "type": "subscription", "config_dir": cfgdir}
os.makedirs(cfgdir)
json.dump({"theme": "dark"}, open(cfgdir + "/settings.json", "w"))
m = h.install_hook(p)
s = json.load(open(cfgdir + "/settings.json"))
check("hook installed, other keys kept", "installed" in m and s["theme"] == "dark" and s["statusLine"]["command"].endswith("ribbit.py hook"), m)
check("backup written", os.path.exists(cfgdir + "/settings.json.hub-backup"))
check("hook idempotent", "already installed" in h.install_hook(p))
check("hook_installed()", h.hook_installed(p))
p2 = {"name": "b", "type": "subscription", "config_dir": root + "/cfg-b"}
os.makedirs(p2["config_dir"])
json.dump({"statusLine": {"type": "command", "command": "my-own-status"}}, open(p2["config_dir"] + "/settings.json", "w"))
m = h.install_hook(p2)
check("existing statusLine not overwritten", "not overwriting" in m and json.load(open(p2["config_dir"] + "/settings.json"))["statusLine"]["command"] == "my-own-status", m)
p3 = {"name": "c", "type": "subscription", "config_dir": root + "/cfg-c"}
os.makedirs(p3["config_dir"])
open(p3["config_dir"] + "/settings.json", "w").write("{not json")
check("invalid settings left untouched", "not valid JSON" in h.install_hook(p3) and open(p3["config_dir"] + "/settings.json").read() == "{not json")

# ---- hook subcommand + list + screen model
import time
now = int(time.time())
payload = json.dumps({"rate_limits": {"five_hour": {"used_percentage": 76.4, "resets_at": now + 10020},
                                        "seven_day_fable": {"used_percentage": 59, "resets_at": now + 444000}}})
r = subprocess.run([sys.executable, SCRIPT, "hook"], input=payload, text=True,
                   capture_output=True, env=dict(os.environ, CLAUDE_ACCOUNT="work"))
check("hook output", r.stdout.strip() == "work | 5h 76% | Fable 59%", r.stdout + r.stderr)
r = subprocess.run([sys.executable, SCRIPT, "hook"], input="garbage", text=True,
                   capture_output=True, env=dict(os.environ, CLAUDE_ACCOUNT="fy"))
check("hook survives garbage", r.returncode == 0)
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    h.cli_list()
out = buf.getvalue()
check("list shows usage", "5h 76%" in out and "Fable 59%" in out, out)
lines, starts = h.build_screen(prof, 0, time.time())
flat = "\n".join("".join(t for t, _ in ln) for ln in lines)
check("screen renders all profiles", all(n in flat for n in ("work", "api", "fy", "oa")))
check("screen shows bars + active", "━" in flat and "active" in flat and "resets 2h" in flat, flat)
check("screen flags unpinned/pinned models", "son-dep" in flat)
check("screen never contains secrets", "GLOBAL" not in flat and "FK" not in flat.replace("Foundry", ""))

# ---- real launch through a fake claude
fake = root + "/bin/claude"
open(fake, "w").write('#!/bin/sh\nenv | grep -E "^(CLAUDE_|ANTHROPIC_|OPENAI_)" | sort > "$HUB_OUT"\necho "args: $@" >> "$HUB_OUT"\nexit 3\n')
os.chmod(fake, 0o755)
os.environ["PATH"] = root + "/bin:" + os.environ["PATH"]
os.environ["HUB_OUT"] = root + "/out.txt"
code = h.launch(sub, ["--resume"])
outtxt = open(root + "/out.txt").read()
check("launch returns child exit code", code == 3, code)
check("launch sub env", "CLAUDE_ACCOUNT=work" in outtxt and "ANTHROPIC_API_KEY" not in outtxt and "args: --resume" in outtxt, outtxt)
check("config dir created 0700", stat.S_IMODE(os.stat(root + "/home/.claude-work").st_mode) == 0o700)
check("SIGINT handler restored", signal.getsignal(signal.SIGINT) is signal.default_int_handler)
os.environ["MY_ANTH"] = "real-anthropic-key"
h.launch(dict(api, key="env:MY_ANTH"))
outtxt = open(root + "/out.txt").read()
check("launch anthropic passes resolved key", "ANTHROPIC_API_KEY=real-anthropic-key" in outtxt, outtxt)
h.launch(dict(fy, key="", base_url=""))
outtxt = open(root + "/out.txt").read()
check("launch foundry no key => Entra", "CLAUDE_CODE_USE_FOUNDRY=1" in outtxt and "FOUNDRY_API_KEY" not in outtxt, outtxt)
unusable_file = root + "/a_file"
open(unusable_file, "w").write("not a dir")
for bad, label in ((dict(api, key=""), "anthropic without key"), (dict(oa, command=""), "openai without command"),
                   (dict(sub, command="no-such-tool-xyz"), "missing binary"),
                   (dict(sub, config_dir=unusable_file + "/sub"), "unusable config dir")):
    try:
        h.launch(bad); ok = False
    except h.ProfileError:
        ok = True
    check("launch error: " + label, ok)
r = subprocess.run([sys.executable, SCRIPT, "run", "work", "--", "--model", "x"],
                   capture_output=True, text=True, env=os.environ)
check("cli run + exit code", r.returncode == 3 and "args: --model x" in open(root + "/out.txt").read(), r.stderr)

shutil.rmtree(SP, ignore_errors=True)
print("\nFAILED: %d" % fails)
sys.exit(1 if fails else 0)
