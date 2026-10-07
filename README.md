# DSS-Guard

**Commit only the tool effects an MCP declaration permits.**

AI agent apps let tools describe themselves, and then trust the description.
VS Code, Codex CLI, ChatGPT, Claude.ai, Gemini CLI and Goose skip or relax the
"Allow this tool?" prompt for MCP tools annotated `readOnlyHint: true`, and
treat `destructiveHint: false` as "nothing will be lost". Nothing checks either
claim.

In an audit of 753 MCP server repositories we confirmed **80 tools whose
declarations are wrong**, 50 of them consequential: tools labelled safe that
create cloud identities, overwrite user files, or delete data. A pre-registered
random sample puts consequential mislabels at about one server in a hundred:
rare, severe, and invisible to the validation these systems publish.

DSS-Guard makes the declaration something that is checked instead of trusted.

## How it works

DSS-Guard is a drop-in proxy between any MCP client and any MCP server. For
every call to a tool whose declaration promises something, it:

1. turns the declaration into a contract: `readOnlyHint: true` means *no
   change*; `destructiveHint: false` means *nothing that existed is lost*;
2. checkpoints the server's state (copy-on-write; a single `clonefile` on APFS);
3. forwards the call unchanged;
4. reads the **decision-sufficient state**: file contents, git refs, objects and
   config, database rows, knowledge-graph records. It does not read raw bytes, so
   caches and timestamps never count as effects;
5. **commits** if the change is within the contract, or **restores the
   checkpoint exactly** and tells the agent what the tool tried to do.

Tools that make no promise pass straight through, adding about 1 ms.

## Results

From the evaluation in the accompanying paper (*Declarations Are Not Enough:
Measuring and Enforcing Effect Declarations for Agent Tools*):

- **Every observable violating call blocked and exactly restored, with no false
  blocks**, across 177 cases on the official MCP reference servers, a widely
  used vendor server and two third-party servers.
- **Calls it lets through return exactly what they would without it.**
- It **found violations that a manual code review had missed**.
- The design choices matter. A byte-level file monitor *misses* a git branch
  reset that makes commits unreachable, because the loss is semantic, and it
  falsely blocks git's read-only tools. Reading the MCP specification literally
  ("additive updates only") blocks honest commits.
- Cost per guarded call: about 1 ms for tools without a promise, 20–26 ms for git
  reads, and 11–39 ms for file reads on 100–1,000-file workspaces.

Vendor findings were reported to their maintainers. Details of issues that are
not yet fixed are withheld until they are.

## Install

```sh
git clone https://github.com/ranausmanai/dssguard
pip install ./dssguard        # Python >= 3.10; depends only on the official MCP SDK
```

## Use

Wrap any stdio MCP server. Tell DSS-Guard which directories the server can
change and how to read each one:

```sh
# a filesystem server
dssguard --root work=/path/to/project:tree -- npx @modelcontextprotocol/server-filesystem /path/to/project

# the git server
dssguard --root repo=/path/to/repo:git -- uvx mcp-server-git --repository /path/to/repo

# the memory server (reads the JSONL file as records)
dssguard --root mem=/path/to/dir:memory:memory.jsonl -- npx @modelcontextprotocol/server-memory
```

In your MCP client's configuration, replace the server command with the
`dssguard ... -- <command>` line.

| Option | Meaning |
|---|---|
| `--root NAME=PATH:VIEW[:FILE]` | A guarded root. VIEW is `tree`, `git`, `memory` (needs FILE), `sqlite` (needs FILE) or `raw`. Repeatable. |
| `--scratch REL` | A path inside each root that the server owns (for example its output directory). Excluded from the view and logged. |
| `--nondestructive lossless\|additive` | How to read `destructiveHint: false`. `lossless` (default) allows a branch to fast-forward but not a record to vanish; `additive` is the specification's literal wording. |
| `--observe` | Log decisions but never roll back. Use it as a conformance test in CI or in a registry. |
| `--log FILE` | One JSON line per call: contract, verdict, facts added and removed, what was lost, timings. |
| `--store DIR` | Checkpoint directory. It must be on the same volume as the roots for cheap clones. |

## Guarantee and scope

If every effect of a call lands inside the guarded roots, a change that violates
the declaration never persists. Either the contract held, or the roots are
byte-identical to their state before the call. This holds whether the
declaration was wrong by accident, came from a framework default, or injected
content steered the model to an overreaching argument.

DSS-Guard does **not** see:

- writes outside the guarded roots (pair it with OS write confinement such as
  Landlock or `sandbox-exec` to close this);
- remote effects (nothing local can undo an HTTP `DELETE`, so keep confirmation
  for tools that declare `openWorldHint`);
- a server's in-process memory.

It defends against wrong declarations. It does not defend against a malicious
server that tries to escape observation.

## Tests

```sh
PYTHONPATH=. python tests/test_core.py
```

The tests cover byte-exact in-place rollback, open file handles seeing restored
content, the git view ignoring index stat-cache refreshes while catching a lost
branch tip, config and hook changes, a same-size write that preserves its
modification time, and both contracts.

## Author

Rana Muhammad Usman, independent researcher (usmanashrafrana@gmail.com).

## License

MIT
