# ribbit

A terminal hub for hopping between your Claude Code logins, API keys and Azure
AI Foundry / OpenAI connections, with live usage bars for each account.

One file, Python standard library only.

> Unofficial. Not affiliated with or endorsed by Anthropic. "Claude" is a
> trademark of Anthropic.

```
 ribbit   your own logins, one isolated config dir each

   1 personal  [Personal]  sub   ● active
      5h     ━━━━━━━━━━━━━━━━━━━━━━━───────   76%  resets 2h 46m
      Fable  ━━━━━━━━━━━━━━━━━━────────────   59%  resets 5d 3h

 ▸ 2 work  [Work]  sub
      5h     ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━─   96%  resets 2h 36m
      7d     ━━━━━━━━━━━━──────────────────   40%  resets 4d 22h

   3 api  [Console API]  api
      key env:ANTHROPIC_API_KEY  NOT SET in this shell

   4 foundry  [Azure Foundry]  foundry
      resource my-resource   key Entra ID (az login / default credential chain)
      models sonnet=claude-sonnet-5  opus=claude-opus-4-8
```

Keys: `↑↓` select, `enter` launch, `a` add, `e` edit, `d` delete,
`i` install the usage hook, `q` quit.

## How it works

Every profile is launched as its own `claude` process with its own environment.

- **Subscription profiles** are normal Claude Code logins, each in an isolated
  `CLAUDE_CONFIG_DIR`. You sign in once per account inside that profile.
  Switching means quitting one session and launching the other.
- ribbit never reads, copies or swaps tokens, and it makes no calls to
  Anthropic. Usage bars come from the `rate_limits` data Claude Code passes to
  a statusline command. ribbit installs a small hook (`ribbit.py hook`) for that.
- Before each launch it strips stray provider variables from the child's
  environment (`ANTHROPIC_API_KEY`, `CLAUDE_CODE_USE_FOUNDRY` and friends).
  This matters because a global `ANTHROPIC_API_KEY` overrides your
  subscription login in Claude Code.

If one of the accounts is a work account, your employer's policy applies to it
as usual.

## Install

```bash
mkdir -p ~/.local/bin
cp ribbit.py ~/.local/bin/ && chmod +x ~/.local/bin/ribbit.py
alias ribbit='python3 ~/.local/bin/ribbit.py'
ribbit
```

The first run seeds two subscription profiles, `personal` (`~/.claude-personal`)
and `work` (`~/.claude-work`). Launch each one and log in. Press `i` on a
subscription profile to install the usage hook into its `settings.json`. An
existing `statusLine` setting is never overwritten.

Other commands:

```
ribbit run <name> [-- extra args]   launch a profile without the UI
ribbit list                         print profiles and last known usage
ribbit hook                         statusLine command (used by the hook)
```

## Profile types

| Type | Runs | Environment set |
|------|------|-----------------|
| subscription | `claude` | `CLAUDE_CONFIG_DIR` |
| anthropic | `claude` | `ANTHROPIC_API_KEY`, optional `ANTHROPIC_BASE_URL`, `ANTHROPIC_MODEL` |
| foundry | `claude` | `CLAUDE_CODE_USE_FOUNDRY=1`, `ANTHROPIC_FOUNDRY_RESOURCE` or `ANTHROPIC_FOUNDRY_BASE_URL`, optional `ANTHROPIC_FOUNDRY_API_KEY`, `ANTHROPIC_DEFAULT_{OPUS,SONNET,HAIKU}_MODEL` |
| openai | a command you choose (e.g. `codex`, `aider`) | `OPENAI_API_KEY`, optional `OPENAI_BASE_URL` |

Notes:

- Claude Code cannot use OpenAI models. An openai profile launches a different
  tool with your OpenAI key in its environment.
- Foundry with no key uses the Azure default credential chain (`az login`).
- Pin your Foundry deployment names. Unpinned model aliases can resolve to a
  model your account does not have.

## Keys are never stored

A key field holds a reference that is resolved at launch and handed only to
that child process:

| Reference | Meaning |
|-----------|---------|
| `env:NAME` | read from the environment of the shell that started ribbit |
| `cmd:<command>` | run a command and use its output (`pass show x`, `op read ...`, `az ...`) |
| `file:<path>` | read a file (keep it `chmod 600`) |
| `prompt` | ask on the terminal at launch |

Pasting a raw key into the form is rejected, masked on screen and not echoed
back. `cmd:` references run whatever is in your own `profiles.json`, so keep
that file private (ribbit writes it `0600`).

## Where things live

| What | Linux / macOS | Windows |
|------|---------------|---------|
| profiles | `~/.config/ribbit/profiles.json` | `%LOCALAPPDATA%\ribbit\profiles.json` |
| usage snapshots | `~/.local/state/ribbit/usage/` | `%LOCALAPPDATA%\ribbit\usage\` |
| per-profile Claude config | `~/.claude-<name>` by default | same |

Set `RIBBIT_DEBUG=1` before launching a profile to also save the raw statusline
payload next to its usage snapshot. Useful if a bar or reset time looks wrong.

## Limitations

- Usage only updates while that account has a running session, because Claude
  Code only emits `rate_limits` then. An idle account shows its last reading
  with an "as of ..." tag.
- Only `used_percentage` is a confirmed field name. Reset times and per-model
  bars are read from any matching keys in `rate_limits`.
- Separate config dirs mean separate settings, `CLAUDE.md` and MCP servers.
  Symlink the files you want shared.
- Windows needs `pip install windows-curses` and has not been tested.
- Not tested against a real Foundry resource.

## Tests

```bash
for t in tests/test_*.py; do python3 "$t" || break; done
```

`tests/test_tui.py` drives the curses UI through a pseudo-terminal, so it needs Unix.
GitHub Actions runs the same loop on Python 3.8, 3.10, 3.12 and 3.13.

## Licence

MIT. See [LICENSE](LICENSE).
