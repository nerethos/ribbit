import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
import ribbit as h

fails = 0


def check(name, cond, extra=""):
    global fails
    if not cond:
        fails += 1
    print(("PASS " if cond else "FAIL ") + name + ((" :: " + str(extra)) if (extra and not cond) else ""))


# ---- looks_secret: flags pasted keys, leaves ordinary text alone
for text in ("sk-ant-api03-abcdef1234567890",
             "echo sk-ant-api03-abcdef1234567890",
             "--api-key=sk-proj-abcdefghijklmnop",
             "Zm9vYmFyYmF6cXV4MTIzNDU2Nzg5MGFiY2RlZmdoaWo"):
    check("looks_secret flags: " + text[:34], h.looks_secret(text))

for text in ("pass show anthropic/api",
             "op read op://vault/item/credential",
             "secret-tool lookup service anthropic",
             "gh auth token",
             "az keyvault secret show --vault-name myvault "
             "--name very-long-secret-name-for-the-anthropic-api-key-prod --query value -o tsv",
             "--model claude-sonnet-5-5 --no-auto-commits",
             ""):
    check("looks_secret allows: " + (text[:34] or "(empty)"), not h.looks_secret(text))

check("url_has_userinfo", h.url_has_userinfo("https://user:pass@host.example/v1"))
check("url_has_userinfo (user only)", h.url_has_userinfo("https://token@host.example"))
check("url without userinfo", not h.url_has_userinfo("https://host.example/path?x=a@b"))

# ---- check_ref
msg = h.check_ref("cmd:echo sk-ant-api03-abcdef1234567890", True) or ""
check("check_ref rejects a key inside cmd:", "looks like a key" in msg, msg)
check("check_ref still accepts cmd:pass", h.check_ref("cmd:pass show anthropic/api", True) is None)
check("check_ref still accepts cmd:op", h.check_ref("cmd:op read op://vault/item/credential", True) is None)
check("check_ref accepts a long secret *name* in cmd:",
      h.check_ref("cmd:az keyvault secret show --vault-name v --name "
                  "very-long-secret-name-for-the-anthropic-api-key-prod --query value -o tsv", True) is None)


# ---- form validators
def field(ptype, key):
    return next(f for f in h.fields_for(ptype, set(), False) if f["key"] == key)


base = field("openai", "base_url")["validate"]
check("base_url rejects user:pass@", base("https://user:pass@host.example/v1", {}) is not None)
check("base_url rejects key in query", base("https://api.example.com/v1?api_key=sk-abcdefghijkl", {}) is not None)
check("base_url accepts a normal URL", base("https://my-resource.services.ai.azure.com/anthropic", {}) is None)
check("base_url accepts blank", base("", {}) is None)

fbase = field("foundry", "base_url")["validate"]
check("foundry base_url rejects userinfo", fbase("https://u:p@x.example/anthropic", {"resource": ""}) is not None)
check("foundry base_url still needs resource or url", fbase("", {"resource": ""}) is not None)
check("foundry base_url ok with resource set", fbase("", {"resource": "res1"}) is None)

abase = field("anthropic", "base_url")["validate"]
check("anthropic base_url rejects userinfo", abase("https://u:p@gw.example", {}) is not None)

args = field("openai", "args")["validate"]
check("args reject a key", args("--api-key sk-ant-api03-abcdefghij1234", {}) is not None)
check("args accept normal flags", args("--model gpt-5 --no-auto-commits", {}) is None)
check("args accept blank", args("", {}) is None)

cmd = field("openai", "command")["validate"]
check("command requires a value", cmd("", {}) == "required")
check("command accepts codex", cmd("codex", {}) is None)
check("command accepts aider --yes", cmd("aider --yes", {}) is None)
check("command rejects a key", cmd("my-tool sk-ant-api03-abcdefghij1234", {}) is not None)

# ---- review follow-ups: URL fragments and percent-encoding
B64 = "Zm9vYmFyYmF6cXV4MTIzNDU2Nzg5MGFiY2RlZmdoaWo"
check("base_url rejects a key followed by a #fragment",
      base("https://api.example.com/v1?api_key=sk-abcdefghijkl#section", {}) is not None)
check("base_url rejects a long token followed by a #fragment",
      base("https://api.example.com/v1?token=" + B64 + "#section", {}) is not None)
check("base_url rejects a token with %3D padding",
      base("https://api.example.com/v1?token=" + B64 + "%3D", {}) is not None)
check("base_url rejects a token with lowercase %3d padding",
      base("https://api.example.com/v1?token=" + B64 + "%3d", {}) is not None)
check("looks_secret sees through percent-encoding", h.looks_secret("x%3Dsk-ant-api03-abcdefghij1234"))
check("base_url still allows ordinary percent-encoding and fragments",
      base("https://example.com/a%20b/path?name=hello%20world#top", {}) is None)
check("base_url still allows an Azure endpoint with a path",
      base("https://my-resource.services.ai.azure.com/anthropic/v1/messages?api-version=2024-10-21", {}) is None)

print("\nFAILED: %d" % fails)
sys.exit(1 if fails else 0)
