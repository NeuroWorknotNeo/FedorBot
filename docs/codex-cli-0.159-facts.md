> Снимок исследования Codex CLI 0.159.0 от 29.09.2026, сделанный при переносе бота (по исходникам тега `rust-v0.159.0` и живым запускам против локального фейкового сервера OpenAI). Пути `/tmp/...` относятся к машине, где шло исследование. Краткая выжимка того, что важно для бота, — в [AGENTS.md](../AGENTS.md).

# OpenAI Codex CLI: source-verified facts for porting a Claude Code Telegram bot

- **Verified against:** `openai/codex` tag **`rust-v0.159.0`** (release "0.159.0", published 2026-09-29T08:05:42Z; marked *Latest*; newer tags are `0.160.0-alpha.*` pre-releases).
- **Binary tested:** `codex-cli 0.159.0` (`/tmp/codex_bin/codex`, Linux x86_64 musl, byte-identical to `bin/codex` inside `codex-package-x86_64-unknown-linux-musl.tar.gz`).
- **Source tree used:** `/tmp/codex_tar/codex-rust-v0.159.0` (codeload tarball of the tag). All file paths below are relative to the repo root unless noted; line numbers are for this tag.
- **Method:** source reading + running the real binary against a local fake Responses API (`/tmp/fake_openai/server.py`, python `http.server` on 127.0.0.1 streaming SSE). Everything marked **LIVE** was observed from the binary; **SOURCE** = read from code; **UNVERIFIED** = could not be confirmed.
- Date of research: 2026-09-29.

Reusable artifacts left on this machine:

| Path | What |
|---|---|
| `/tmp/codex_bin/codex` | codex-cli 0.159.0 (standalone binary) |
| `/tmp/codex_bin/codex-code-mode-host` | required sibling helper (see gotcha #1) |
| `/tmp/codex_bin/codex-resources/bwrap` | bundled bubblewrap (see gotcha #2) |
| `/tmp/codex_pkg/` | full official package layout (`bin/codex`, `bin/codex-code-mode-host`, `codex-path/rg`, `codex-resources/bwrap`, `codex-package.json`) |
| `/tmp/fake_openai/server.py` | fake Responses API server (scenarios chosen by `SCENARIO_*` marker in the prompt) |
| `/tmp/fake_openai/as_client.py` | minimal `codex app-server` stdio JSON-RPC client (resume + compact) |
| `/tmp/fake_openai/out_*.jsonl`, `/tmp/fake_openai/requests_18080/*.json` | captured JSONL and captured HTTP requests |
| `/tmp/codex_home_test/sessions/...` | real rollout files produced by the runs |

---

## 0. TL;DR: the things that will bite the bot

1. **Install the full package, not the bare binary.** Every current model preset (`gpt-6-*`, `gpt-5.6-*`) has `"tool_mode": "code_mode_only"` (models-manager/models.json). Its only execution tool is a JavaScript `exec` tool hosted by a separate binary, `codex-code-mode-host`, which must sit next to `codex`.
   - The bare `codex-x86_64-unknown-linux-musl.tar.gz` does **not** contain it. **LIVE**, every run then starts with this JSONL item:
     `{"type":"item.completed","item":{"id":"item_0","type":"error","message":"Code Mode is unavailable because failed to spawn code-mode host /tmp/codex_bin/codex-code-mode-host: host executable was not found. Code mode will fail closed; enable `features.code_mode_host` and install `codex-code-mode-host`."}}`
   - With it missing, the model cannot run shell commands or edit files.
   - Fixes:
     - use `install.sh`, npm, or `codex-package-x86_64-unknown-linux-musl.tar.gz`, which ship `bin/codex-code-mode-host`, `codex-resources/bwrap` and `codex-path/rg`;
     - or place `codex-code-mode-host` (release asset `codex-code-mode-host-x86_64-unknown-linux-musl.tar.gz`) next to `codex`.
2. **The Linux sandbox is bubblewrap and it needs unprivileged user namespaces.**
   - Without a bwrap binary (system `bwrap` on PATH, or `codex-resources/bwrap` next to the exe) every sandboxed command dies with (**LIVE**, as the tool output): `bubblewrap is unavailable: no system bwrap was found on PATH and no bundled codex-resources/bwrap binary was found next to the Codex executable`.
   - With bwrap present but userns blocked (Ubuntu ≥ 24.04 `kernel.apparmor_restrict_unprivileged_userns=1`, many VPS/containers; this very VM), commands fail with **LIVE**: `bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted`.
   - Such sandbox-denied commands produce **no `command_execution` item at all** in `--json` output. On such hosts, run inside your own container/VM with `--dangerously-bypass-approvals-and-sandbox` (or `-s danger-full-access`).
3. **`-c` flags must all be on the same side of `resume`.**
   - `-c` is a clap *global* arg. If any `-c` appears after `resume`, every `-c` given before `resume` is **discarded**.
   - **LIVE:** `codex exec -c openai_base_url=A resume -c openai_base_url=B …` sent traffic only to B. In my first attempt `-c developer_instructions=…` after `resume` silently dropped `-c openai_base_url=…` before it.
   - Safest pattern: `codex exec [non-global flags like -s/-C/--add-dir/-p/--color] resume [all -c, -m, --json, -o, --skip-git-repo-check, …] <ID> -- "<prompt>"`.
4. **Resume failure modes are asymmetric.**
   - An unknown UUID fails: exit 1, stderr `Error: thread/resume: thread/resume failed: no rollout found for thread id <id> (code -32600)`, and **nothing** on stdout.
   - `resume --last` with no match, or `resume <name>` with an unknown name, **silently starts a NEW thread**. Always compare the `thread.started.thread_id` with the id you asked for.
5. **stdin.** For a *new* session with a positional prompt, `codex exec` **reads stdin to EOF when stdin is not a TTY** and appends it as a `<stdin>` block.
   - A bot that spawns with an open pipe will hang. Spawn with `stdin=subprocess.DEVNULL`, or pass the prompt via stdin with `-`.
   - `resume` with an explicit prompt does not read stdin.
6. **Prompts starting with `-` must follow `--`.** **LIVE:** `codex exec "-hello"` printed the help text and exited 0; `codex exec -- "-hello"` works.
7. **`turn.completed.usage` is thread-cumulative, not per turn.** It is `ThreadTokenUsage.total`, restored across resumes. **LIVE:** 1200 → 2400 → 3600 input tokens over three resumed runs.
   - `--json` never prints model, context window or rate limits. Get them from the rollout file (`token_count` events) or from app-server.
8. **Terminal events.**
   - A successful turn ends with `turn.completed`.
   - A failed turn ends with `turn.failed`, exit code 1.
   - **Top-level `{"type":"error"}` events are not necessarily fatal.** Retries emit `error` events like `Reconnecting... 2/5 (…)`.
   - **SIGINT (exit 1) and SIGTERM (exit 143) emit no terminal event at all.** The session stays resumable.
9. **Changing `developer_instructions` on resume does nothing until a compaction** (**LIVE**). After a compaction (including the automatic one on a model switch) the *current* process's value is re-injected. Pass the same value on every invocation.
10. **Switching `-m` on resume** emits a warning item (`This session was recorded with model …`) and triggers a remote compaction request before the turn (**LIVE**).

---

## 1. Latest version and installation (Linux server)

**Version (LIVE, `gh release list -R openai/codex`):**
- `0.159.0  Latest  rust-v0.159.0  2026-09-29T08:05:42Z`
- Previous stables: `0.158.0` (2026-09-28) and `0.157.1` (2026-09-26).
- Tags are `rust-vX.Y.Z`. Pre-releases are `X.Y.Z-alpha.N`.

### Install methods
1. **Official script (recommended by the README):**
   ```sh
   curl -fsSL https://chatgpt.com/codex/install.sh | sh
   # force GitHub Releases instead of releases.openai.com:
   curl -fsSL https://chatgpt.com/codex/install.sh | CODEX_INSTALLER_USE_RELEASES_OPENAI_COM=false sh
   ```
   `install.sh` is also a release asset.
   - **Options:** `--release VERSION`, env `CODEX_RELEASE`, `CODEX_NON_INTERACTIVE=1`, `CODEX_INSTALL_DIR` (default `$HOME/.local/bin`), `CODEX_HOME` (default `$HOME/.codex`), `CODEX_INSTALLER_USE_RELEASES_OPENAI_COM`.
   - **What it downloads:** `codex-package-<target>.tar.gz`, verified against `codex-package_SHA256SUMS`. Sources are `https://releases.openai.com/codex`, falling back to GitHub. If no package asset exists it uses the legacy platform npm tarball.
   - **Resulting layout (SOURCE, install.sh `install_package_release` / `update_visible_command`):**
     ```
     ~/.local/bin/codex  ->  $CODEX_HOME/packages/standalone/current/bin/codex
     $CODEX_HOME/packages/standalone/current -> releases/0.159.0-x86_64-unknown-linux-musl
     $CODEX_HOME/packages/standalone/releases/0.159.0-x86_64-unknown-linux-musl/
         bin/codex  bin/codex-code-mode-host  codex -> bin/codex
         codex-path/rg  codex-resources/bwrap  codex-resources/voice/...  codex-package.json
     ```
   - It adds `export PATH="$HOME/.local/bin:$PATH"` to your shell profile if needed. On Linux, validation requires `codex-resources/bwrap` to be present.
2. **npm:** `npm install -g @openai/codex`.
   - Package `@openai/codex@0.159.0`, `"engines": {"node": ">=16"}`, bin `codex` → `bin/codex.js`.
   - Platform binaries come in via optionalDependencies such as `"@openai/codex-linux-x64": "npm:@openai/codex@0.159.0-linux-x64"`.
   - The JS shim runs `vendor/<target-triple>/…/codex` and sets `CODEX_MANAGED_BY_NPM=1`.
3. **Homebrew:** `brew install --cask codex`.
4. **Standalone release assets (Linux):**
   - `codex-x86_64-unknown-linux-musl.tar.gz` (also `.zst`, `.sigstore`) contains only the file `codex-x86_64-unknown-linux-musl`, which you rename to `codex`. **It lacks code-mode host, bwrap and rg** (see §0.1–0.2).
   - `codex-package-x86_64-unknown-linux-musl.tar.gz` (also `.tar.zst`, `.sigstore`) is the full layout above. **Use this.**
   - The helpers are also separate assets: `codex-code-mode-host-x86_64-unknown-linux-musl.tar.gz` and `bwrap-x86_64-unknown-linux-musl.tar.gz`.
   - The same set exists for `aarch64-unknown-linux-musl`.
   - Other assets: `codex-app-server-*` (standalone app-server), `codex-responses-api-proxy-*`, `config-schema.json` (JSON schema of config.toml), `install.sh` / `install.ps1`.
5. **Python:** wheels `openai_codex_cli_bin-0.159.0-py3-none-manylinux_2_17_x86_64.whl` etc.
   - Project `openai-codex-cli-bin`, "Pinned Codex CLI runtime for the Python SDK" (sdk/python-runtime/pyproject.toml).
   - The SDK itself is `openai-codex` (sdk/python/pyproject.toml).
6. `codex update` subcommand: "Update Codex to the latest version".

**Where helpers are looked up (SOURCE):**
- Code-mode host: the error message path `/tmp/codex_bin/codex-code-mode-host` shows it is expected next to the executable; install-context/src/lib.rs names it `codex-code-mode-host`.
- bwrap (linux-sandbox/src/bundled_bwrap.rs `find_legacy_for_exe`) candidates, in order:
  1. `<exe_dir>/codex-resources/bwrap`
  2. `<package_target_dir>/codex-resources/bwrap`
  3. `<exe_dir>/bwrap`
  4. the package-layout `codex-resources/bwrap` via InstallContext
- A system `bwrap` on PATH is preferred if it supports `--perms` (linux-sandbox/src/launcher.rs `preferred_bwrap_launcher`).
- The bundled bwrap is digest-verified (`verify_digest(... expected_sha256() ...)`), so it must be the exact release asset.

---

## 2. `codex exec` CLI

### 2.1 `codex exec --help` (verbatim, LIVE)
```
Run Codex non-interactively

Usage: codex exec [OPTIONS] [PROMPT]
       codex exec [OPTIONS] <COMMAND> [ARGS]

Commands:
  resume  Resume a previous session by id or pick the most recent with --last
  fork    Fork a previous session by id into a new session
  review  Run a code review against the current repository
  help    Print this message or the help of the given subcommand(s)

Arguments:
  [PROMPT]
          Initial instructions for the agent. If not provided as an argument (or if `-` is used),
          instructions are read from stdin. If stdin is piped and a prompt is also provided, stdin
          is appended as a `<stdin>` block

Options:
  -c, --config <key=value>
          Override a configuration value that would otherwise be loaded from `~/.codex/config.toml`.
          Use a dotted path (`foo.bar.baz`) to override nested values. The `value` portion is parsed
          as TOML. If it fails to parse as TOML, the raw string is used as a literal.
          
          Examples: - `-c model="o3"` - `-c 'sandbox_permissions=["disk-full-read-access"]'` - `-c
          shell_environment_policy.inherit=all`

      --enable <FEATURE>
          Enable a feature (repeatable). Equivalent to `-c features.<name>=true`

      --disable <FEATURE>
          Disable a feature (repeatable). Equivalent to `-c features.<name>=false`

      --strict-config
          Error out when config.toml contains fields that are not recognized by this version of
          Codex

  -i, --image <FILE>...
          Optional image(s) to attach to the initial prompt

  -m, --model <MODEL>
          Model the agent should use

      --oss
          Use open-source provider

      --local-provider <OSS_PROVIDER>
          Specify which local provider to use (lmstudio or ollama). If not specified with --oss,
          will use config default or show selection

  -p, --profile <CONFIG_PROFILE_V2>
          Layer $CODEX_HOME/<name>.config.toml on top of the base user config

  -s, --sandbox <SANDBOX_MODE>
          Select the sandbox policy to use when executing model-generated shell commands
          
          [possible values: read-only, workspace-write, danger-full-access]

      --approve-for-me
          Route approval requests through automatic review using the workspace-write sandbox

      --dangerously-bypass-approvals-and-sandbox
          Skip all confirmation prompts and execute commands without sandboxing. EXTREMELY
          DANGEROUS. Intended solely for running in environments that are externally sandboxed

      --dangerously-bypass-hook-trust
          Run enabled hooks without requiring persisted hook trust for this invocation. DANGEROUS.
          Intended only for automation that already vets hook sources

  -C, --cd <DIR>
          Tell the agent to use the specified directory as its working root

      --worktree
          Run the session in a new managed Git worktree

      --add-dir <DIR>
          Additional directories that should be writable alongside the primary workspace

      --thread-source <SOURCE>
          Source classification for newly created or forked threads

      --skip-git-repo-check
          Allow running Codex outside a Git repository

      --ephemeral
          Run without persisting session files to disk

      --ignore-user-config
          Do not load `$CODEX_HOME/config.toml`; auth still uses `CODEX_HOME`

      --ignore-rules
          Do not load user or project execpolicy `.rules` files

      --output-schema <FILE>
          Path to a JSON Schema file describing the model's final response shape

      --color <COLOR>
          Specifies color settings for use in the output
          
          [default: auto]
          [possible values: always, never, auto]

      --json
          Print events to stdout as JSONL

  -o, --output-last-message <FILE>
          Specifies file where the last message from the agent should be written

  -h, --help
          Print help (see a summary with '-h')

  -V, --version
          Print version
```
Notes:
- `--json` has the hidden alias `--experimental-json` (exec/src/cli.rs).
- `--yolo` is a hidden alias of `--dangerously-bypass-approvals-and-sandbox` (codex-rs/utils/cli/src/shared_options.rs `alias = "yolo"`). **LIVE:** `codex exec --yolo --json …` ran fine, and also skipped the git check.
- `--full-auto` does not exist at this tag: `error: unexpected argument '--full-auto' found`, exit 2 (see §10).
- `codex exec` is aliased as `codex e`.

### 2.2 `codex exec resume --help` (verbatim, LIVE)
```
Resume a previous session by id or pick the most recent with --last

Usage: codex exec resume [OPTIONS] [SESSION_ID] [PROMPT]

Arguments:
  [SESSION_ID]
          Conversation/session id (UUID) or thread name. UUIDs take precedence if it parses. If
          omitted, use --last to pick the most recent recorded session

  [PROMPT]
          Prompt to send after resuming the session. If `-` is used, read from stdin

Options:
  -c, --config <key=value>
          Override a configuration value that would otherwise be loaded from `~/.codex/config.toml`.
          Use a dotted path (`foo.bar.baz`) to override nested values. The `value` portion is parsed
          as TOML. If it fails to parse as TOML, the raw string is used as a literal.
          
          Examples: - `-c model="o3"` - `-c 'sandbox_permissions=["disk-full-read-access"]'` - `-c
          shell_environment_policy.inherit=all`

      --last
          Resume the most recent recorded session (newest) without specifying an id

      --all
          Show all sessions (disables cwd filtering)

      --enable <FEATURE>
          Enable a feature (repeatable). Equivalent to `-c features.<name>=true`

      --disable <FEATURE>
          Disable a feature (repeatable). Equivalent to `-c features.<name>=false`

  -i, --image <FILE>
          Optional image(s) to attach to the prompt sent after resuming

      --strict-config
          Error out when config.toml contains fields that are not recognized by this version of
          Codex

  -m, --model <MODEL>
          Model the agent should use

      --dangerously-bypass-approvals-and-sandbox
          Skip all confirmation prompts and execute commands without sandboxing. EXTREMELY
          DANGEROUS. Intended solely for running in environments that are externally sandboxed

      --dangerously-bypass-hook-trust
          Run enabled hooks without requiring persisted hook trust for this invocation. DANGEROUS.
          Intended only for automation that already vets hook sources

      --worktree
          Run the session in a new managed Git worktree

      --thread-source <SOURCE>
          Source classification for newly created or forked threads

      --skip-git-repo-check
          Allow running Codex outside a Git repository

      --ephemeral
          Run without persisting session files to disk

      --ignore-user-config
          Do not load `$CODEX_HOME/config.toml`; auth still uses `CODEX_HOME`

      --ignore-rules
          Do not load user or project execpolicy `.rules` files

      --output-schema <FILE>
          Path to a JSON Schema file describing the model's final response shape

      --json
          Print events to stdout as JSONL

  -o, --output-last-message <FILE>
          Specifies file where the last message from the agent should be written

  -h, --help
          Print help (see a summary with '-h')
```
(`--worktree` is listed but rejected at runtime: `--worktree is not supported with codex exec resume`, exec/src/lib.rs.)

### 2.3 Resuming non-interactively with a new prompt

The syntax is `codex exec resume <SESSION_ID_or_NAME> [PROMPT]`, or `codex exec resume --last [PROMPT]`.
- With `--last`, a lone positional argument is treated as the prompt (exec/src/cli.rs `From<ResumeArgsRaw> for ResumeArgs`).
- `--last` only considers sessions whose latest cwd matches the current cwd (add `--all` to disable that) and whose `model_provider` matches the current provider (exec/src/lib.rs `resolve_resume_thread_id`, `resume_lookup_model_providers`).
- `resume <UUID>`: the UUID is used directly, with no cwd filter. Resolving a name (non-UUID) is cwd-filtered unless `--all`.
- The emitted `thread.started` carries the **same** thread_id on resume (**LIVE**).

Which flags work after `resume` (exec/src/cli.rs: `global = true` args, plus `mark_exec_global_args`):

| Flag | Accepted after `resume`? | Notes |
|---|---|---|
| `--json` (alias `--experimental-json`) | yes (global) | |
| `-o/--output-last-message` | yes (global) | **LIVE**: file contains the last `agent_message` text |
| `-c/--config`, `--enable`, `--disable` | yes (global) | **Values after `resume` REPLACE all values before it** (clap global semantics; LIVE-verified with two ports) |
| `-m/--model` | yes (global via `mark_exec_global_args`) | triggers the model-switch warning and compaction (§0.10) |
| `--dangerously-bypass-approvals-and-sandbox`, `--dangerously-bypass-hook-trust`, `--worktree` | yes (global) | `--worktree` then errors for resume |
| `--skip-git-repo-check`, `--ephemeral`, `--ignore-user-config`, `--ignore-rules`, `--output-schema`, `--strict-config`, `--thread-source` | yes (global) | |
| `-i/--image` | yes (resume's own `-i`, comma-delimited, one value per flag) | parent `-i` images are also attached (`imgs.chain(args.images)`) |
| `-s/--sandbox`, `-C/--cd`, `--add-dir`, `-p/--profile`, `--oss`, `--local-provider`, `--approve-for-me`, `--color` | **no, after `resume`** (**LIVE**: `error: unexpected argument '-s' found`, exit 2) | They are accepted **before** `resume` and **do take effect** on the resumed thread. **LIVE:** `codex exec -s workspace-write -C /tmp/codex_work2 resume … <id> "…"` produced new `<permissions instructions>` (workspace-write) and `<cwd>/tmp/codex_work2</cwd>` in the next request. |

How the resumed thread is configured (exec/src/lib.rs `thread_resume_params_from_config`): it sends `model: config.model`, `cwd: config.cwd`, `approval_policy`, `sandbox` from the *current* invocation's config.
- The cwd of the resumed turn is therefore the cwd of the new process (or `-C`), not the original one.
- There is no cwd check for resume-by-UUID.

Recommended invocation for the bot:
```sh
# new thread
codex exec [-s workspace-write|--dangerously-bypass-approvals-and-sandbox] [-C DIR] \
    --json --skip-git-repo-check -o /path/last.txt \
    -m gpt-6-sol -c model_reasoning_effort="medium" \
    -c 'developer_instructions="…"' -- "$PROMPT" < /dev/null
# follow-up
codex exec [-s …] [-C DIR] resume --json --skip-git-repo-check -o /path/last.txt \
    -m gpt-6-sol -c model_reasoning_effort="medium" -c 'developer_instructions="…"' \
    "$THREAD_ID" -- "$PROMPT" < /dev/null
```
Long prompts can instead go through stdin: `... resume "$THREAD_ID" - < prompt.txt`, or `codex exec … - < prompt.txt` for a new thread.

### 2.4 Prompt from stdin, and prompts starting with `-`

These behaviours come from exec/src/lib.rs `resolve_prompt`, `resolve_root_prompt`, `read_prompt_from_stdin` and `prompt_with_stdin_context`.

**New session (`codex exec [PROMPT]`):**
- No prompt: reads stdin if it is piped; prints `Reading prompt from stdin...` to stderr. If stdin is a TTY it errors: `No prompt provided. Either specify one as an argument or pipe the prompt into stdin.` (exit 1).
- `-`: forces reading stdin as the prompt. **LIVE**: `printf '...' | codex exec --json … -` works.
- Positional prompt **and** non-TTY stdin: stdin is read to EOF (stderr `Reading additional input from stdin...`) and appended.
  - **LIVE** request text: `main prompt SCENARIO_PLAIN\n\n<stdin>\nEXTRA CONTEXT LINE\n</stdin>`.
  - Empty stdin (e.g. `/dev/null`) is ignored.
  - **An open-but-never-closed pipe blocks forever.**

**Resume (`resume <id> [PROMPT]`):**
- A prompt other than `-` is used as-is, without reading stdin.
- `-` or no prompt reads stdin (required-if-piped semantics).

**Encoding:** the prompt bytes must be UTF-8. A UTF-8 BOM is stripped. UTF-16 with a BOM is decoded. Otherwise it fails with `Failed to read prompt from stdin: input is not valid UTF-8 …`.

**Leading `-`:** a prompt beginning with `-` is parsed as options. **LIVE**: `codex exec … "-hello SCENARIO_PLAIN"` printed the short help and **exited 0** (`-h` in the cluster). Always put `--` before the prompt: `codex exec [opts] -- "$PROMPT"` and `codex exec resume [opts] "$ID" -- "$PROMPT"`. Both are **LIVE**-verified; `-dash resume …` reached the model verbatim.

**Slash commands:** `codex exec` does not interpret `/compact` or `/status`. The text is sent to the model verbatim (§7).

### 2.5 Exit codes (LIVE unless noted)

| Situation | Exit | stdout (`--json`) | stderr |
|---|---|---|---|
| turn completed | 0 | … `turn.completed` | warnings/logs |
| turn failed (HTTP 401/429/5xx, `response.failed`, context overflow) | 1 | … `turn.failed` | ERROR log lines |
| SIGINT (Ctrl-C) | 1 | no terminal event | – |
| SIGTERM | 143 (killed by signal; no handler) | no terminal event | – |
| unknown resume UUID | 1 | nothing | `Error: thread/resume: thread/resume failed: no rollout found for thread id … (code -32600)` |
| not a git repo, no `--skip-git-repo-check` | 1 | nothing | `Not inside a trusted directory and --skip-git-repo-check was not specified.` |
| clap usage error | 2 | nothing | `error: unexpected argument …` |
| stdout closed early (e.g. `\| head -1`) | 101 (panic writing stdout) | – | – |
| approval requests from the agent (command/file-change/permissions/request_user_input/dynamic tools) | unchanged by itself (SOURCE: exec replies with a JSON-RPC error `… is not supported in exec mode for thread …`; MCP elicitations are auto-cancelled; `error_seen` is set only if sending that reply fails) | – | – |

## 3. `--json` (JSONL) event schema

### 3.1 How exec produces it (SOURCE)
- `codex exec` runs an **in-process app-server**. It calls `thread/start` or `thread/resume`, then `turn/start`, and converts app-server `ServerNotification`s to JSONL.
  - Conversion: `codex-rs/exec/src/event_processor_with_jsonl_output.rs`.
  - Types: `codex-rs/exec/src/exec_events.rs`.
- Every line is written with `println!` to **stdout**, one JSON object per line. Nothing else goes to stdout in `--json` mode (**LIVE**). All warnings and logs go to stderr.
- Item ids are synthetic per process: `item_0`, `item_1`, … They restart at `item_0` on every invocation, including resume.
- Notifications that are **not** turned into JSONL, whether filtered by `should_process_notification` in exec/src/lib.rs or ignored by the processor:
  - `thread/tokenUsage/updated` is stored and used only for `turn.completed.usage`.
  - `account/rateLimits/updated`, `model/verification`, hooks, `turn/diff/updated`, `thread/status/changed`, the session-configured payload, and all streaming deltas.
  - **So there are no text deltas.** `agent_message` arrives only as `item.completed`.
- `ThreadItem` kinds that are dropped (`_ => None`): `UserMessage`, `HookPrompt`, `FunctionCallOutput`, `Plan`, `DynamicToolCall`, `SubAgentActivity`, `ImageView`, `Sleep`, `ImageGeneration`, `EnteredReviewMode`/`ExitedReviewMode`, and **`ContextCompaction`**. A compaction is invisible in JSONL.
- Other mapping rules:
  - `reasoning` items are emitted only on completion, and only if the summary text is non-empty.
  - Collab items for `send_message`, `followup_task`, `interrupt_agent` and `list_agents`, and any collab item with status `interrupted`, are dropped.
  - `ResumeAgent` maps to `wait`.
- Warnings, config warnings, deprecation notices and `model rerouted: A -> B (…)` become `item.completed` items of `type:"error"`. They are **not fatal**.

### 3.2 Types, verbatim (`codex-rs/exec/src/exec_events.rs` @ rust-v0.159.0)
```rust
/// Top-level JSONL events emitted by codex exec
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, TS)]
#[serde(tag = "type")]
pub enum ThreadEvent {
    /// Emitted when a new thread is started as the first event.
    #[serde(rename = "thread.started")]
    ThreadStarted(ThreadStartedEvent),
    /// Emitted when a turn is started by sending a new prompt to the model.
    /// A turn encompasses all events that happen while agent is processing the prompt.
    #[serde(rename = "turn.started")]
    TurnStarted(TurnStartedEvent),
    /// Emitted when a turn is completed. Typically right after the assistant's response.
    #[serde(rename = "turn.completed")]
    TurnCompleted(TurnCompletedEvent),
    /// Indicates that a turn failed with an error.
    #[serde(rename = "turn.failed")]
    TurnFailed(TurnFailedEvent),
    /// Emitted when a new item is added to the thread. Typically the item will be in an "in progress" state.
    #[serde(rename = "item.started")]
    ItemStarted(ItemStartedEvent),
    /// Emitted when an item is updated.
    #[serde(rename = "item.updated")]
    ItemUpdated(ItemUpdatedEvent),
    /// Signals that an item has reached a terminal state—either success or failure.
    #[serde(rename = "item.completed")]
    ItemCompleted(ItemCompletedEvent),
    /// Represents an unrecoverable error emitted directly by the event stream.
    #[serde(rename = "error")]
    Error(ThreadErrorEvent),
}

pub struct ThreadStartedEvent {
    /// The identified of the new thread. Can be used to resume the thread later.
    pub thread_id: String,
}
pub struct TurnStartedEvent {}
pub struct TurnCompletedEvent {
    pub usage: Usage,
}
pub struct TurnFailedEvent {
    pub error: ThreadErrorEvent,
}

/// Describes the usage of tokens during a turn.
pub struct Usage {
    /// The number of input tokens used during the turn.
    pub input_tokens: i64,
    /// The number of cached input tokens used during the turn.
    pub cached_input_tokens: i64,
    /// The number of input tokens written to the prompt cache during the turn.
    #[serde(default)]
    pub cache_write_input_tokens: i64,
    /// The number of output tokens used during the turn.
    pub output_tokens: i64,
    /// The number of reasoning output tokens used during the turn.
    pub reasoning_output_tokens: i64,
}

pub struct ItemStartedEvent { pub item: ThreadItem }
pub struct ItemCompletedEvent { pub item: ThreadItem }
pub struct ItemUpdatedEvent { pub item: ThreadItem }

/// Fatal error emitted by the stream.
pub struct ThreadErrorEvent {
    pub message: String,
}

/// Canonical representation of a thread item and its domain-specific payload.
pub struct ThreadItem {
    pub id: String,
    #[serde(flatten)]
    pub details: ThreadItemDetails,
}

/// Typed payloads for each supported thread item type.
#[serde(tag = "type", rename_all = "snake_case")]
pub enum ThreadItemDetails {
    AgentMessage(AgentMessageItem),     // "agent_message"
    Reasoning(ReasoningItem),           // "reasoning"
    CommandExecution(CommandExecutionItem), // "command_execution"
    FileChange(FileChangeItem),         // "file_change"
    McpToolCall(McpToolCallItem),       // "mcp_tool_call"
    CollabToolCall(CollabToolCallItem), // "collab_tool_call"
    WebSearch(WebSearchItem),           // "web_search"
    TodoList(TodoListItem),             // "todo_list"
    Error(ErrorItem),                   // "error"
}

pub struct AgentMessageItem { pub text: String }   // natural language, or JSON string when --output-schema
pub struct ReasoningItem { pub text: String }      // reasoning *summary* lines joined with "\n"

#[serde(rename_all = "snake_case")]
pub enum CommandExecutionStatus { #[default] InProgress, Completed, Failed, Declined }
pub struct CommandExecutionItem {
    pub command: String,
    pub aggregated_output: String,
    pub exit_code: Option<i32>,
    pub status: CommandExecutionStatus,
}

pub struct FileUpdateChange { pub path: String, pub kind: PatchChangeKind }
#[serde(rename_all = "snake_case")]
pub enum PatchApplyStatus { InProgress, Completed, Failed }   // app-server Declined -> Failed
pub struct FileChangeItem { pub changes: Vec<FileUpdateChange>, pub status: PatchApplyStatus }
#[serde(rename_all = "snake_case")]
pub enum PatchChangeKind { Add, Delete, Update }

#[serde(rename_all = "snake_case")]
pub enum McpToolCallStatus { #[default] InProgress, Completed, Failed }
#[serde(rename_all = "snake_case")]
pub enum CollabToolCallStatus { #[default] InProgress, Completed, Failed }
#[serde(rename_all = "snake_case")]
pub enum CollabTool { SpawnAgent, SendInput, Wait, CloseAgent }
#[serde(rename_all = "snake_case")]
pub enum CollabAgentStatus { PendingInit, Running, Interrupted, Completed, Errored, Shutdown, NotFound }
pub struct CollabAgentState { pub status: CollabAgentStatus, pub message: Option<String> }
pub struct CollabToolCallItem {
    pub tool: CollabTool,
    pub sender_thread_id: String,
    pub receiver_thread_ids: Vec<String>,
    pub prompt: Option<String>,
    pub agents_states: HashMap<String, CollabAgentState>,
    pub status: CollabToolCallStatus,
}

pub struct McpToolCallItemResult {
    pub content: Vec<JsonValue>,
    #[serde(rename = "_meta", skip_serializing_if = "Option::is_none")]
    pub meta: Option<JsonValue>,
    pub structured_content: Option<JsonValue>,
}
pub struct McpToolCallItemError { pub message: String }
pub struct McpToolCallItem {
    pub server: String,
    pub tool: String,
    #[serde(default)]
    pub arguments: JsonValue,
    pub result: Option<McpToolCallItemResult>,
    pub error: Option<McpToolCallItemError>,
    pub status: McpToolCallStatus,
}

pub struct WebSearchItem {
    pub id: String,
    pub query: String,
    pub action: WebSearchAction,   // codex_protocol::models::WebSearchAction
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub results: Option<Vec<JsonValue>>,
}

pub struct ErrorItem { pub message: String }
pub struct TodoItem { pub text: String, pub completed: bool }
pub struct TodoListItem { pub items: Vec<TodoItem> }
```
(All derive `Debug, Clone, Serialize, Deserialize, PartialEq, TS`. I stripped derives and doc comments that only repeat the variant names.)

`WebSearchAction` is serialized with a `type` tag. Observed values (**LIVE**) are `{"type":"search","query":"…"}` and `{"type":"other"}`; the enum also has `open_page {url}` and `find_in_page {url, pattern}`.

**Quirk (LIVE): `web_search` items contain the key `id` twice.** The outer one is `item_N`; the inner one is the provider's id (`ws_1`), for example `{"id":"item_2","type":"web_search","id":"ws_1",…}`. Python's `json.loads` keeps the **last** value (`ws_1`). Use that id to match `item.started` with `item.completed`.

**Contrary to the doc comment, `file_change` does emit `item.started`** (with status `in_progress`) before `item.completed` (**LIVE**).

**`todo_list`** comes from `turn/plan/updated`:
- It is `item.started` the first time, `item.updated` afterwards, and `item.completed` at turn end.
- With the current `code_mode_only` models the `update_plan` tool is not offered at all. **LIVE**, the fake model calling it got `unsupported call: update_plan`. Expect no `todo_list` items with the default models.

**`collab_tool_call` (sub-agents):**
- gpt-6-* models use multi-agent **v2**. Its tools are `collaboration.spawn_agent`, `send_message`, `followup_task`, `wait_agent`, `interrupt_agent` and `list_agents` (**LIVE**, seen in the request).
- Exec JSONL only surfaces `spawn_agent`, `send_input`, `wait`/`resume_agent` and `close_agent`. The others are dropped.
- Sub-agent threads are separate threads, and their events are not printed (exec only prints events of the primary thread/turn).
- To disable sub-agents: `-c agents.enabled=false`. Per agent 1's binary check, `--disable multi_agent` alone does not remove multi-agent from gpt-6-astra.

### 3.3 `turn.completed.usage` semantics (SOURCE + LIVE)
```rust
fn usage_from_last_total(&self) -> Usage {
    let Some(usage) = self.last_total_token_usage.as_ref() else { return Usage::default(); };
    Usage {
        input_tokens: usage.total.input_tokens,
        cached_input_tokens: usage.total.cached_input_tokens,
        cache_write_input_tokens: usage.total.cache_write_input_tokens,
        output_tokens: usage.total.output_tokens,
        reasoning_output_tokens: usage.total.reasoning_output_tokens,
    }
}
```
- `usage.total` is the thread's cumulative total, summed over all model requests including earlier turns and previous processes, because it is restored on resume.
- **LIVE**, same thread across 3 exec processes: `input_tokens` 1200 → 2400 → 3600.
- Per-turn usage = difference between consecutive `turn.completed` values, or read `last_token_usage` / `token_usage_record` from the rollout (§4).
- There is no `total_tokens` field.

### 3.4 Real captured JSONL (LIVE, codex-cli 0.159.0 → fake Responses API)
All runs: `CODEX_API_KEY=fake codex exec --json … -c 'openai_base_url="http://127.0.0.1:18080/v1"'`. The fake server returns HTTP 426 on the WebSocket upgrade, so Codex falls back to HTTPS SSE (see §12 about the stderr line this causes).

**(a) Plain answer**, bare binary without `codex-code-mode-host` (note the error item):
```
{"type":"thread.started","thread_id":"01a0eda6-ca1f-79e2-b8fc-4d6ad503a8c1"}
{"type":"item.completed","item":{"id":"item_0","type":"error","message":"Code Mode is unavailable because failed to spawn code-mode host /tmp/codex_bin/codex-code-mode-host: host executable was not found. Code mode will fail closed; enable `features.code_mode_host` and install `codex-code-mode-host`."}}
{"type":"turn.started"}
{"type":"item.completed","item":{"id":"item_1","type":"reasoning","text":"**Thinking** about the answer"}}
{"type":"item.completed","item":{"id":"item_2","type":"agent_message","text":"Hello! This is the fake model answering."}}
{"type":"turn.completed","usage":{"input_tokens":1200,"cached_input_tokens":200,"cache_write_input_tokens":0,"output_tokens":50,"reasoning_output_tokens":10}}
```
Thread ids are UUIDv7 (`01a0eda6-…-7…`). With the host installed, the error item disappears.

**(b) Shell command**, via code mode `exec` → nested `tools.exec_command`, with `--dangerously-bypass-approvals-and-sandbox`:
```
{"type":"thread.started","thread_id":"01a0edaa-8ead-7282-9541-c6bde7c8993c"}
{"type":"turn.started"}
{"type":"item.started","item":{"id":"item_0","type":"command_execution","command":"/bin/bash -lc 'echo hello-from-codex; ls /nonexistent_dir_xyz; echo done'","aggregated_output":"","exit_code":null,"status":"in_progress"}}
{"type":"item.completed","item":{"id":"item_0","type":"command_execution","command":"/bin/bash -lc 'echo hello-from-codex; ls /nonexistent_dir_xyz; echo done'","aggregated_output":"hello-from-codex\nls: cannot access '/nonexistent_dir_xyz': No such file or directory\ndone\n","exit_code":0,"status":"completed"}}
{"type":"item.completed","item":{"id":"item_1","type":"reasoning","text":"**Thinking** about the answer"}}
{"type":"item.completed","item":{"id":"item_2","type":"agent_message","text":"Command finished (saw 1 tool output(s))."}}
{"type":"turn.completed","usage":{"input_tokens":2150,"cached_input_tokens":300,"cache_write_input_tokens":0,"output_tokens":90,"reasoning_output_tokens":15}}
```

**(c) Non-zero exit** (`exit 3`):
```
{"type":"item.started","item":{"id":"item_0","type":"command_execution","command":"/bin/bash -lc 'exit 3'","aggregated_output":"","exit_code":null,"status":"in_progress"}}
{"type":"item.completed","item":{"id":"item_0","type":"command_execution","command":"/bin/bash -lc 'exit 3'","aggregated_output":"","exit_code":3,"status":"failed"}}
```

**(d) Same shell command inside the default `read-only` sandbox, on this VM where bwrap cannot create a user namespace.**
- No `command_execution` item at all. Only `reasoning` and `agent_message` appear.
- The model received this tool output: `{"exit_code":1,…,"output":"bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted\n"}`.
- Why (SOURCE, codex-rs/core/src/unified_exec/process_manager.rs + codex-rs/sandboxing/src/denial.rs):
  - A sandboxed process that fails and whose output contains `operation not permitted`, `permission denied`, `read-only file system`, `seccomp`, `sandbox`, `landlock` or `failed to write file` is classified as `SandboxDenied`.
  - Exit codes 2, 126 and 127 are excluded.
  - It is returned before the Begin event is emitted, so neither `item.started` nor `item.completed` appears.

**(e) Plan + shell + apply_patch + web search in one turn** (bypass mode):
```
{"type":"thread.started","thread_id":"01a0edaa-e7d5-7a30-97d1-434253371da2"}
{"type":"turn.started"}
{"type":"item.started","item":{"id":"item_0","type":"command_execution","command":"/bin/bash -lc 'pwd; echo all-scenario'","aggregated_output":"","exit_code":null,"status":"in_progress"}}
{"type":"item.completed","item":{"id":"item_0","type":"command_execution","command":"/bin/bash -lc 'pwd; echo all-scenario'","aggregated_output":"/tmp/codex_work2\nall-scenario\n","exit_code":0,"status":"completed"}}
{"type":"item.started","item":{"id":"item_1","type":"file_change","changes":[{"path":"/tmp/codex_work2/hello.txt","kind":"add"}],"status":"in_progress"}}
{"type":"item.completed","item":{"id":"item_1","type":"file_change","changes":[{"path":"/tmp/codex_work2/hello.txt","kind":"add"}],"status":"completed"}}
{"type":"item.started","item":{"id":"item_2","type":"web_search","id":"ws_1","query":"","action":{"type":"other"}}}
{"type":"item.completed","item":{"id":"item_2","type":"web_search","id":"ws_1","query":"codex cli jsonl","action":{"type":"search","query":"codex cli jsonl"}}}
{"type":"item.completed","item":{"id":"item_3","type":"agent_message","text":"All steps done."}}
{"type":"turn.completed","usage":{"input_tokens":5800,"cached_input_tokens":1300,"cache_write_input_tokens":0,"output_tokens":190,"reasoning_output_tokens":35}}
```
File-change paths are absolute. `update_plan` was rejected (`unsupported call: update_plan`), which also logged `ERROR codex_core::tools::router: error=unsupported call: update_plan` on stderr.

**(f) HTTP 401 from the API.** There are 5 retries (`error` events), then a fatal `error` + `turn.failed`, exit 1:
```
{"type":"thread.started","thread_id":"01a0edab-1cc6-78a2-8504-35669b2da72e"}
{"type":"turn.started"}
{"type":"error","message":"Reconnecting... 1/5 (unexpected status 401 Unauthorized: Incorrect API key provided: fake. You can find your API key at https://platform.openai.com/account/api-keys., url: http://127.0.0.1:18080/v1/responses)"}
{"type":"error","message":"Reconnecting... 2/5 (unexpected status 401 Unauthorized: …)"}
{"type":"error","message":"Reconnecting... 3/5 (…)"}
{"type":"error","message":"Reconnecting... 4/5 (…)"}
{"type":"error","message":"Reconnecting... 5/5 (…)"}
{"type":"error","message":"unexpected status 401 Unauthorized: Incorrect API key provided: fake. You can find your API key at https://platform.openai.com/account/api-keys., url: http://127.0.0.1:18080/v1/responses"}
{"type":"turn.failed","error":{"message":"unexpected status 401 Unauthorized: Incorrect API key provided: fake. You can find your API key at https://platform.openai.com/account/api-keys., url: http://127.0.0.1:18080/v1/responses"}}
```
Against the real api.openai.com (accidental, see Appendix A) the messages also carry `, cf-ray: …, request id: req_…`. First there were 5 WebSocket retries, then an `item.completed` error item `Falling back from WebSockets to HTTPS transport. unexpected status 401 Unauthorized: …`, then 5 HTTPS retries, then `turn.failed`.

**(g) HTTP 500.** 5 retries over about 25 s, then failure, exit 1:
```
{"type":"error","message":"Reconnecting... 1/5 (We’re currently experiencing high demand, which may cause temporary errors.)"}
… 2/5 … 5/5 …
{"type":"error","message":"We’re currently experiencing high demand, which may cause temporary errors."}
{"type":"turn.failed","error":{"message":"We’re currently experiencing high demand, which may cause temporary errors."}}
```

**(h) SSE `response.failed`** (`code: server_error`). This is retried too:
```
{"type":"error","message":"Reconnecting... 1/5 (stream disconnected before completion: synthetic response.failed error)"}
… 5/5 …
{"type":"error","message":"stream disconnected before completion: synthetic response.failed error"}
{"type":"turn.failed","error":{"message":"stream disconnected before completion: synthetic response.failed error"}}
```

**(i) `response.failed` with `code: context_length_exceeded`.** No retry:
```
{"type":"error","message":"Codex ran out of room in the model's context window. Start a new thread or clear earlier history before retrying."}
{"type":"turn.failed","error":{"message":"Codex ran out of room in the model's context window. Start a new thread or clear earlier history before retrying."}}
```

**(j) HTTP 429 `{"error":{"type":"usage_limit_reached","plan_type":"plus","resets_at":…}}`.** No retry, immediate:
```
{"type":"error","message":"You’ve hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro), visit https://chatgpt.com/codex/settings/usage to purchase more credits or try again at 4:57 PM."}
{"type":"turn.failed","error":{"message":"You’ve hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro), visit https://chatgpt.com/codex/settings/usage to purchase more credits or try again at 4:57 PM."}}
```
The time is rendered in the host's local time zone. Note the typographic apostrophe `’` in these messages.

**(k) SIGINT during streaming.** Exit 1 right away; no `turn.failed` or `turn.completed`:
```
{"type":"thread.started","thread_id":"01a0edab-a67b-7fe1-b9ab-a91537f501a2"}
{"type":"turn.started"}
```
If the SIGINT arrives while a command is running, you get `item.started` (`command_execution`, `in_progress`) with no matching `item.completed`. The child process (`bash -c …; sleep 30`) was killed (**LIVE**).

**(l) Model switch on resume** (`resume -m gpt-5.5 <id>` on a gpt-6-astra thread):
```
{"type":"thread.started","thread_id":"01a0edb0-b845-7982-bf2a-ace833e9dc14"}
{"type":"item.completed","item":{"id":"item_0","type":"error","message":"This session was recorded with model `gpt-6-astra` but is resuming with `gpt-5.5`. Consider switching back to `gpt-6-astra` as it may affect Codex performance."}}
{"type":"turn.started"}
{"type":"item.completed","item":{"id":"item_1","type":"reasoning","text":"**Thinking** about the answer"}}
{"type":"item.completed","item":{"id":"item_2","type":"agent_message","text":"Hello! This is the fake model answering."}}
{"type":"turn.completed","usage":{"input_tokens":4800,"cached_input_tokens":800,"cache_write_input_tokens":0,"output_tokens":200,"reasoning_output_tokens":40}}
```
Between `turn.started` and the answer, Codex sent a remote-compaction request: `input[-1] = {"type":"compaction_trigger"}`, header `x-codex-beta-features: remote_compaction_v2`. It expects exactly one output item `{"type":"compaction","encrypted_content":…}`. When my fake server first answered with a normal message, the turn failed with:
`{"type":"error","message":"Error running remote compact task: Fatal error: remote compaction v2 expected exactly one compaction output item, got 0 from 2 output items"}` followed by `turn.failed`.

---

## 4. Model name, rate limits, context window: not in `--json`; use rollout files or app-server

### 4.1 What `--json` gives you
Nothing about the model, the context window or rate limits (SOURCE, §3.1; LIVE). Alternatives:
- **Human mode** (no `--json`) prints a header to **stderr** (**LIVE**):
  ```
  OpenAI Codex v0.159.0
  --------
  workdir: /tmp/codex_empty
  model: gpt-6-astra
  provider: openai
  approval: never
  sandbox: read-only
  reasoning effort: none
  reasoning summaries: none
  session id: 01a0edb9-3657-77a1-b163-6be02f747b34
  --------
  user
  human SCENARIO_PLAIN
  ```
  The final message goes to stdout, and `tokens used\n1,050` goes to stderr. "reasoning effort: none" here only means none was configured; the request actually used the model default `low`.
- **Rollout JSONL files** (below).
- **app-server:** after `thread/resume` it immediately replays `thread/tokenUsage/updated` with `total`, `last` and `modelContextWindow`, and it sends `account/rateLimits/updated` notifications (§7).

### 4.2 Rollout files (LIVE + SOURCE)
- **Path:** `$CODEX_HOME/sessions/YYYY/MM/DD/rollout-YYYY-MM-DDTHH-MM-SS-<thread_uuid>.jsonl`. Directory and filename use **local time**; per-line `timestamp` is UTC `YYYY-MM-DDTHH:MM:SS.mmmZ`.
  - Source: codex-rs/rollout/src/recorder.rs `precompute_new_rollout_path`, codex-rs/rollout/src/rollout_file_name.rs.
  - **LIVE** example: `/tmp/codex_home_test/sessions/2026/09/29/rollout-2026-09-29T14-55-23-01a0eda9-cd7a-7d33-a6cd-868a670128c7.jsonl`.
- **Archived** threads move flat to `$CODEX_HOME/archived_sessions/<same filename>`.
- `--ephemeral` writes no file. **LIVE**: a later resume fails with `no rollout found`.
- **JSONL is still the canonical store.**
  - SQLite (`state_5.sqlite`, `thread_history_1.sqlite`, `logs_2.sqlite`, `goals_1.sqlite`, `memories_1.sqlite`, `queue_1.sqlite` in `$CODEX_HOME`, or `CODEX_SQLITE_HOME`) is an index/projection.
  - New exec threads use `"history_mode":"paginated"`: every line has an `ordinal`, and chat items are persisted as `event_msg`/`item_completed` TurnItems.
  - Old files may be legacy format. `codex migrate-rollouts [--apply]` converts them.
  - `.jsonl.zst` compression exists behind the off-by-default feature `local_thread_store_compression`, for files at least 7 days old.
- **Line envelope:** `{"timestamp": "...", "ordinal": N, "type": <kind>, "payload": {...}}`. Kinds: `session_meta`, `response_item`, `compacted`, `turn_context`, `token_usage_record`, `world_state`, `event_msg`, `inter_agent_communication`, `inter_agent_communication_metadata`, `retained_context`, `security_risk_score`, `realtime_item` (codex-rs/history/src/rollout_payload.rs).
- **Line-type counts (LIVE)** for a one-command exec run:
  ```
  1 session_meta, 1 world_state, 1 turn_context, 6 response_item/message, 1 response_item/reasoning,
  1 response_item/custom_tool_call, 1 response_item/custom_tool_call_output, 2 token_usage_record,
  1 event_msg/task_started, 3 event_msg/item_completed, 2 event_msg/token_count, 1 event_msg/task_complete
  ```
  A resumed/compacted thread additionally had `event_msg/thread_settings_applied` (written on each resume and at compaction), `compacted`, and `event_msg/item_completed` with `"item":{"type":"ContextCompaction","id":…}`.

**Real lines (LIVE; long fields removed):**
```json
{"type":"session_meta","payload":{"session_id":"01a0eda9-cd7a-7d33-a6cd-868a670128c7","id":"01a0eda9-cd7a-7d33-a6cd-868a670128c7","timestamp":"2026-09-29T14:55:23.516Z","cwd":"/tmp/codex_work","runtime_workspace_roots":["/tmp/codex_work"],"originator":"codex_exec","cli_version":"0.159.0","source":"exec","thread_source":"user","model_provider":"openai","history_mode":"paginated","context_window":{"window_id":"01a0eda9-cd7c-75f1-a81a-b2017f565cd4"}, "base_instructions": {…}, …}}

{"timestamp":"2026-09-29T14:55:23.594Z","ordinal":1,"type":"event_msg","payload":{"type":"task_started","turn_id":"01a0eda9-cdc2-7042-95cc-238ad4124f52","root_turn_id":"01a0eda9-cdc2-7042-95cc-238ad4124f52","started_at":1790693723,"model_context_window":258400,"collaboration_mode_kind":"default"}}

{"timestamp":"2026-09-29T14:55:23.607Z","ordinal":7,"type":"turn_context","payload":{"turn_id":"01a0eda9-cdc2-7042-95cc-238ad4124f52","root_turn_id":"01a0eda9-cdc2-7042-95cc-238ad4124f52","disabled_plugin_ids":[],"cwd":"/tmp/codex_work","workspace_roots":["/tmp/codex_work"],"current_date":"2026-09-29","timezone":"Etc/UTC","approval_policy":"never","approvals_reviewer":"user","sandbox_policy":{"type":"read-only"},"permission_profile":{"type":"managed","file_system":{"type":"restricted","entries":[{"path":{"type":"special","value":{"kind":"root"}},"access":"read"}]},"network":"restricted"},"active_permission_profile":{"id":":read-only"},"model":"gpt-6-astra","comp_hash":"3000","collaboration_mode":{"mode":"default","settings":{"model":"gpt-6-astra","reasoning_effort":null,"developer_instructions":null}},"multi_agent_version":"v2","realtime_active":false,"summary":"none"}}

{"timestamp":"2026-09-29T14:55:23.701Z","ordinal":11,"type":"token_usage_record","payload":{"thread_id":"01a0eda9-cd7a-7d33-a6cd-868a670128c7","turn_id":"01a0eda9-cdc2-7042-95cc-238ad4124f52","session_id":"01a0eda9-cd7a-7d33-a6cd-868a670128c7","root_turn_id":"01a0eda9-cdc2-7042-95cc-238ad4124f52","response_id":"resp_006","usage":{"input_tokens":950,"cached_input_tokens":100,"cache_write_input_tokens":0,"output_tokens":40,"reasoning_output_tokens":5,"total_tokens":990},"turn_token_usage":{…same…},"thread_token_usage":{…same…}}}

{"timestamp":"2026-09-29T14:55:24.013Z","ordinal":19,"type":"event_msg","payload":{"type":"token_count","info":{"total_token_usage":{"input_tokens":2150,"cached_input_tokens":300,"cache_write_input_tokens":0,"output_tokens":90,"reasoning_output_tokens":15,"total_tokens":2240},"last_token_usage":{"input_tokens":1200,"cached_input_tokens":200,"cache_write_input_tokens":0,"output_tokens":50,"reasoning_output_tokens":10,"total_tokens":1250},"model_context_window":258400},"rate_limits":{"limit_id":"codex","limit_name":null,"primary":{"used_percent":12.5,"window_minutes":300,"resets_at":1790697271},"secondary":{"used_percent":40.0,"window_minutes":10080,"resets_at":1790952871},"credits":{"has_credits":false,"unlimited":false,"balance":null},"individual_limit":null,"spend_control_reached":null,"plan_type":null,"rate_limit_reached_type":null}}}

{"timestamp":"2026-09-29T14:55:24.019Z","ordinal":20,"type":"event_msg","payload":{"type":"task_complete","turn_id":"01a0eda9-cdc2-7042-95cc-238ad4124f52","last_agent_message":"Command finished (saw 1 tool output(s)).","started_at":1790693723,"completed_at":1790693724,"duration_ms":425,"time_to_first_token_ms":85}}

{"type":"event_msg","payload":{"type":"thread_settings_applied","thread_id":"01a0edb0-…","thread_settings":{"model":"gpt-6-astra","model_provider_id":"openai","approval_policy":"never","approvals_reviewer":"user","permission_profile":{…},"active_permission_profile":{"id":":read-only"},"cwd":"/tmp/codex_repo/sub","runtime_workspace_roots":["/tmp/codex_repo/sub"],"collaboration_mode":{"mode":"default","settings":{"model":"gpt-6-astra","reasoning_effort":null,"developer_instructions":null}},"disabled_plugin_ids":[]}}}
```
In that run the fake server sent the headers `x-codex-primary-used-percent: 12.5`, `x-codex-primary-window-minutes: 300`, `x-codex-primary-reset-at: <unix>`, `x-codex-secondary-*`, `x-codex-credits-has-credits: false` and `x-codex-credits-unlimited: false`. They show up 1:1 in `rate_limits`.

**Header parsing** (SOURCE, codex-rs/codex-api/src/rate_limits.rs):
- Families: `x-codex-{primary,secondary}-{used-percent,window-minutes,reset-at}`, `x-codex-limit-name`, `x-codex-credits-{has-credits,unlimited,balance}`, plus other `x-<limit>-primary-used-percent` families.
- Over WebSocket they arrive in a `codex.rate_limits` message, which is the only source of `plan_type`.
- **There is no `resets_in_seconds` field.** Resets are absolute `resets_at` in Unix seconds.
- A window whose used-percent is 0 and which has no minutes and no reset is dropped.
- With several limit families, the last one processed wins in `token_count.rate_limits`. Check `limit_id == "codex"`.

**How to use these numbers:**
- **Model:** `turn_context.payload.model` (per turn) or `thread_settings_applied.thread_settings.model`.
- **Effort:** `turn_context.payload.effort`. It is absent or `null` when using the model default; the per-model default is `default_reasoning_level` in the catalog (§9).
- **Context usage:** `token_count.info.last_token_usage.total_tokens` is the current context size; `info.model_context_window` is the usable window (= catalog `context_window × 95%`, e.g. 272000 → 258400).
  - TUI formula (codex-rs/protocol/src/protocol.rs):
    ```rust
    const BASELINE_TOKENS: i64 = 12000;
    pub fn percent_of_context_window_remaining(&self, context_window: i64) -> i64 {
        if context_window <= BASELINE_TOKENS { return 0; }
        let effective_window = context_window - BASELINE_TOKENS;
        let used = (self.tokens_in_context_window() - BASELINE_TOKENS).max(0);   // tokens_in_context_window() = total_tokens
        let remaining = (effective_window - used).max(0);
        ((remaining as f64 / effective_window as f64) * 100.0).clamp(0.0, 100.0).round() as i64
    }
    // TUI applies it to info.last_token_usage with info.model_context_window
    ```
  - Auto-compaction threshold defaults to 90% of the raw `context_window` (e.g. 244800 for 272000), clamped.
- **Rate limits:** `token_count.rate_limits.primary.used_percent` (float 0-100; typically the 5-hour window, `window_minutes: 300`) and `.secondary` (weekly, `window_minutes: 10080`). The reset time is `resets_at` (Unix seconds). `plan_type` is present only if the server sent it.

### 4.3 Protocol structs, verbatim (codex-rs/protocol/src/protocol.rs @ rust-v0.159.0)
```rust
#[derive(Debug, Clone, Deserialize, Serialize, Default, PartialEq, Eq, JsonSchema, TS)]
pub struct TokenUsage {
    #[ts(type = "number")]
    pub input_tokens: i64,
    #[ts(type = "number")]
    pub cached_input_tokens: i64,
    #[serde(default)]
    #[ts(type = "number")]
    pub cache_write_input_tokens: i64,
    #[ts(type = "number")]
    pub output_tokens: i64,
    #[ts(type = "number")]
    pub reasoning_output_tokens: i64,
    #[ts(type = "number")]
    pub total_tokens: i64,
    /// Provider-reported units consumed from the shared rollout budget.
    #[serde(default, skip_serializing)]
    #[schemars(skip)]
    #[ts(skip)]
    pub codex_rollout_budget_units: Option<serde_json::Number>,
}

/// Best-effort Responses API usage observed for one completed response.
pub struct TokenUsageRecord {
    pub thread_id: ThreadId,
    pub turn_id: String,
    pub session_id: SessionId,
    pub root_turn_id: String,
    pub response_id: String,
    pub usage: TokenUsage,
    pub turn_token_usage: TokenUsage,
    pub thread_token_usage: TokenUsage,
}

pub struct TokenUsageInfo {
    pub total_token_usage: TokenUsage,
    pub last_token_usage: TokenUsage,
    // TODO(aibrahim): make this not optional
    #[ts(type = "number | null")]
    pub model_context_window: Option<i64>,
}

pub struct TokenCountEvent {
    pub info: Option<TokenUsageInfo>,
    pub rate_limits: Option<RateLimitSnapshot>,
}

pub struct RateLimitSnapshot {
    pub limit_id: Option<String>,
    pub limit_name: Option<String>,
    /// Normal model metadata for a quota alias; never a replacement for the request model.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub normal_model_slug: Option<String>,
    pub primary: Option<RateLimitWindow>,
    pub secondary: Option<RateLimitWindow>,
    pub credits: Option<CreditsSnapshot>,
    pub individual_limit: Option<SpendControlLimitSnapshot>,
    /// Backend-reported spend-control state. `None` is unavailable, not a sparse-update recovery.
    pub spend_control_reached: Option<bool>,
    pub plan_type: Option<crate::account::PlanType>,
    pub rate_limit_reached_type: Option<RateLimitReachedType>,
}

#[serde(rename_all = "snake_case")]
pub enum RateLimitReachedType {
    RateLimitReached,
    WorkspaceOwnerCreditsDepleted,
    WorkspaceMemberCreditsDepleted,
    WorkspaceOwnerUsageLimitReached,
    WorkspaceMemberUsageLimitReached,
}

pub struct RateLimitWindow {
    /// Percentage (0-100) of the window that has been consumed.
    pub used_percent: f64,
    /// Rolling window duration, in minutes.
    #[ts(type = "number | null")]
    pub window_minutes: Option<i64>,
    /// Unix timestamp (seconds since epoch) when the window resets.
    #[ts(type = "number | null")]
    pub resets_at: Option<i64>,
}

pub struct CreditsSnapshot {
    pub has_credits: bool,
    pub unlimited: bool,
    pub balance: Option<String>,
}

pub struct SpendControlLimitSnapshot {
    pub limit: String,
    pub used: String,
    pub remaining_percent: i32,
    pub resets_at: i64,
}
```
`PlanType` (codex-rs/protocol/src/account.rs, `rename_all = "lowercase"`) has these values: `free`, `go`, `plus`, `pro`, `prolite`, `promax`, `team`, `self_serve_business_prolite`, `self_serve_business_usage_based`, `business`, `ent26`, `enterprise_cbp_automation`, `enterprise_cbp_usage_based`, `enterprise`, `edu`, `edu_plus`, `edu_pro`; unknown values map to `Unknown`.

`TurnContextItem` fields (protocol.rs):
- `turn_id?`, `root_turn_id?`, `disabled_plugin_ids?`, `cwd`, `workspace_roots?`, `current_date?`, `timezone?`
- `approval_policy`, `approvals_reviewer?`, `sandbox_policy`, `permission_profile?`, `active_permission_profile?`, `network?`, `file_system_sandbox_policy?`
- **`model: String`**, `comp_hash?`, `personality?`, `collaboration_mode?`, `multi_agent_version?`, `multi_agent_mode?`, `realtime_active?`, `cyber_access_program?`, **`effort: Option<ReasoningEffort>`**, `summary` (compat-only).

`SessionMeta` has no model field. It holds: `session_id`, `id`, `forked_from_id?`, `parent_thread_id?`, `timestamp`, `cwd`, `runtime_workspace_roots?`, `originator`, `cli_version`, `source`, `thread_source?`, `agent_nickname?`, `agent_role?`, `agent_path?`, `model_provider`, `base_instructions`, `dynamic_tools?`, `memory_mode?`, `history_mode`, `history_base?`, `multi_agent_version?`, `context_window?`, plus `git {commit_hash, branch, repository_url}` on the line.

**Persisted `event_msg` types** (codex-rs/rollout/src/policy.rs):
- Always: `token_count`, `thread_goal_updated`, `thread_rolled_back`, `turn_aborted`, `task_started`/`turn_started`, `task_complete`/`turn_complete`, `thread_settings_applied`.
- In paginated mode: `item_completed`.
- In legacy mode: `user_message`, `agent_message`, `agent_reasoning`, `context_compacted`, `patch_apply_end`, `mcp_tool_call_end`, `web_search_end`, etc.
- **Not persisted:** errors, warnings, `exec_command_*`, deltas.

## 5. Config keys (`$CODEX_HOME/config.toml` or `-c key=value`)

### 5.1 Layering and precedence (SOURCE, config/src/loader/mod.rs)
From lowest to highest:
1. Built-in defaults (`config/defaults.toml`)
2. `/etc/codex/config.toml`
3. Cloud-managed layers (business/edu/enterprise only)
4. `$CODEX_HOME/config.toml`
5. `$CODEX_HOME/<name>.config.toml` (via `-p <name>`)
6. Project `.codex/config.toml`, only if the project is trusted
7. `-c` flags
8. For exec, harness overrides such as `approval_policy=never`

Unknown keys are ignored silently unless `--strict-config` is passed.

Built-in defaults, verbatim (`codex-rs/config/defaults.toml`):
```toml
include_permissions_instructions = true
include_apps_instructions = true
include_collaboration_mode_instructions = true
include_environment_context = true
cli_auth_credentials_store = "file"
mcp_oauth_credentials_store = "auto"
project_doc_max_bytes = 32768
project_doc_fallback_filenames = []
background_terminal_max_timeout = 300000
file_opener = "vscode"
hide_agent_reasoning = false
chatgpt_base_url = "https://chatgpt.com/backend-api/"
project_root_markers = [".git"]

[history]
persistence = "save-all"
```

### 5.2 Keys asked about
Sources for this table: `codex-rs/config/src/config_toml.rs`, `codex-rs/core/src/config/mod.rs`, `codex-rs/protocol/src/*`, and `/tmp/codex_rel/config-schema.json` (the release asset). "LIVE" means observed in the request my fake server received.

| Key | Values / type | Default | Notes |
|---|---|---|---|
| `model` | any slug string | unset: the catalog default, `gpt-6-astra` at this tag (**LIVE**) | `-m` is equivalent |
| `model_provider` | `openai` (built-in), `ollama`, `lmstudio`, `amazon-bedrock`, `amazon-bedrock-runtime`, or a key of `[model_providers]` | `openai` | Built-in ids cannot be redefined under `[model_providers]` |
| `openai_base_url` | string | unset | Overrides the built-in openai provider URL. Not honored from project config. This is what the tests (and my fake server) use |
| `model_reasoning_effort` | `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`, `ultra`, `persistent`, **or any other non-empty string** (`Custom`) | unset: model's `default_reasoning_level` (gpt-6-astra: `low`, **LIVE** `"reasoning":{"effort":"low"}`) | **LIVE**: `minimal`, `none`, `xhigh`, `max` and even `bogus` were all sent verbatim as `reasoning.effort`. **The client does not validate against the model's supported list**, so the bot must. `ultra` goes on the wire as the model's `multi_agent_reasoning_effort` (gpt-6-astra: `xhigh`) and enables proactive sub-agent delegation. `persistent` goes on the wire as `"disabled"` |
| `plan_mode_reasoning_effort` | same | unset | |
| `model_reasoning_summary` | `auto`, `concise`, `detailed`, `none` | model's `default_reasoning_summary` (`none` for all bundled models) | With `none`, reasoning summaries (and `reasoning` JSONL items) may be empty. My fake server always sent a summary |
| `model_verbosity` | `low`, `medium`, `high` | model's `default_verbosity` (gpt-6-astra: `low`, **LIVE** `"text":{"verbosity":"low"}`) | Only sent if the model supports verbosity |
| `sandbox_mode` | `read-only`, `workspace-write`, `danger-full-access` | `read-only`; `workspace-write` if the project has a trust entry | `-s` is equivalent |
| `sandbox_workspace_write.network_access` | bool | `false` | Also `writable_roots` (absolute paths), `exclude_tmpdir_env_var`, `exclude_slash_tmp`. `/tmp` and `$TMPDIR` are writable by default in workspace-write (**LIVE** header: `sandbox: workspace-write [workdir, /tmp, $TMPDIR]`) |
| `approval_policy` | `on-request` (alias `on-failure`), `never`, `{granular={…}}`; `untrusted` is now an **error** (`approval_policy = "untrusted" is no longer supported; remove this setting`) | `on-request` | **`codex exec` forces `never`** via a harness override that beats config, unless `approvals_reviewer = "auto_review"` (`--approve-for-me`) |
| `developer_instructions` | string | unset | See §6. Exists and appends; it does not replace the base prompt |
| `instructions` | string | unset | **Replaces** the base instructions (lowest priority of the replacement sources) |
| `model_instructions_file` | path | unset | **Replaces** the base instructions. Missing or empty file is a hard error. The source discourages using it |
| `experimental_instructions_file` | **does not exist** at this tag | — | Silently ignored as an unknown key |
| `model_context_window` | i64 | catalog (272000 for most models) | Clamped to `max_context_window` |
| `model_auto_compact_token_limit` | i64 | 90% of context window | Always clamped to ≤ 90% of the window. Related: `model_auto_compact_token_limit_scope` (`total`, `body_after_prefix`), `model_post_turn_compact_threshold_percent` (0 = off), `compact_prompt` |
| `project_doc_max_bytes` | usize | `32768` | Shared budget across all project AGENTS.md files; the last file is truncated to fit |
| `project_doc_fallback_filenames` | `[string]` | `[]` | e.g. `["CLAUDE.md"]`. Bare file names only |
| `hide_agent_reasoning` | bool | `false` | Human output only |
| `show_raw_agent_reasoning` | bool | `false` (`--oss` forces true) | |
| `web_search` | `disabled`, `cached`, `indexed`, `live` | `cached`; `live` with `--search`; auto-upgrades to `live` under danger-full-access | `tools.web_search = {context_size, allowed_domains, location}`. The features `web_search_request` and `web_search_cached` are **deprecated** |
| `shell_environment_policy` | `inherit = all/core/none`, `ignore_default_excludes`, `exclude`, `set`, `include_only`, … | inherit `all`, `ignore_default_excludes = true` | **Model-run commands inherit the whole service environment, including secrets.** Set `ignore_default_excludes = false` or `inherit = "core"` |
| `agents.enabled` | bool | `true` | `-c agents.enabled=false` disables sub-agents for gpt-6 (multi-agent v2). Also `agents.max_concurrent_threads_per_session`, `max_depth`, `default_subagent_model`, `default_subagent_reasoning_effort` |
| `notify` | `[argv…]` | unset | Runs a program with one JSON arg (`{"type":"agent-turn-complete",…}`) |
| `history.persistence` | `save-all`, `none` | `save-all` | `history.jsonl` is TUI-only |
| `cli_auth_credentials_store` | `file`, `keyring`, `auto`, `ephemeral` | `file` | §8 |
| `forced_login_method` | `chatgpt`, `api` | unset | §8 (footgun) |
| `projects."<abs path>".trust_level` | `trusted`, `untrusted` | none | §13 |
| `-p/--profile NAME` | loads `$CODEX_HOME/NAME.config.toml` as an extra layer | — | The legacy `profile = "x"` selector is now a **hard error**; legacy `[profiles.x]` tables are parsed but ignored |

`ModelProviderInfo` fields (codex-rs/model-provider-info/src/lib.rs, for custom `[model_providers.<id>]`):
- `name`, `base_url`, `model_catalog_url`, `env_key`, `env_key_instructions`
- `experimental_bearer_token`, `auth {command,args,timeout_ms,refresh_interval_ms,cwd}`, `gateway_oauth`, `aws`
- `wire_api` (only `"responses"`; `"chat"` gives the error ``wire_api = "chat"` is no longer supported.``)
- `query_params`, `http_headers`, `env_http_headers`
- `request_max_retries` (default 4, max 100), `stream_max_retries` (default 5), `stream_idle_timeout_ms` (default 300000), `websocket_connect_timeout_ms`
- `requires_openai_auth`, `supports_websockets`, `supports_standalone_web_search`

### 5.3 Feature flags (`features.<key>`, or `--enable/--disable <key>`)
The full `codex features list` output, **LIVE**, is 152 flags (agent 1 dump; reproduce with `CODEX_HOME=… codex features list`). The flags relevant to the bot:

| Key | Stage | Default |
|---|---|---|
| `code_mode` | under development | false (but the model catalog forces code mode via `tool_mode`) |
| `code_mode_only` | under development | false |
| `code_mode_host` | stable | true (needs the `codex-code-mode-host` binary) |
| `multi_agent` (alias `collab`) | stable | **true** |
| `multi_agent_v2` | stable | false (gpt-6 uses v2 anyway via catalog `multi_agent_version`) |
| `unified_exec` | stable | true |
| `shell_tool` | stable | true |
| `view_image` | stable | true |
| `image_generation` | stable | true |
| `web_search_request`, `web_search_cached` | deprecated | false (use top-level `web_search`) |
| `standalone_web_search` | under development | false |
| `goals` | stable | true |
| `memories` | stable | **false** |
| `hooks` | stable | true |
| `plugins` | stable | true |
| `apps` | stable | true |
| `shell_snapshot` | stable | true |
| `instant_interrupt` | under development | false |
| `use_legacy_landlock` | deprecated | false |
| `use_linux_sandbox_bwrap` | removed (no-op) | false |
| `worktrees` | stable | true |

Legacy aliases (features/src/legacy.rs): `collab`→`multi_agent`, `web_search`→`web_search_request`, `experimental_use_unified_exec_tool`→`unified_exec`, `codex_hooks`→`hooks`, `connectors`→`apps`, `memory_tool`→`memories`.

### 5.4 `-c` value parsing (verbatim, codex-rs/utils/cli/src/config_override.rs)
```rust
/// ... The `value` portion is parsed as TOML. If it fails to parse as TOML, the raw string is used as a literal.
#[arg(short = 'c', long = "config", value_name = "key=value", action = ArgAction::Append, global = true)]
pub raw_overrides: Vec<String>,
...
                // Only split on the *first* '=' so values are free to contain
                // the character.
                let mut parts = s.splitn(2, '=');
                ...
                // Attempt to parse as TOML. If that fails, treat it as a raw
                // string. This allows convenient usage such as
                // `-c model=o3` without the quotes.
                let value: Value = match parse_toml_value(value_str) {
                    Ok(v) => v,
                    Err(_) => {
                        // Strip leading/trailing quotes if present
                        let trimmed = value_str.trim().trim_matches(|c| c == '"' || c == '\'');
                        Value::String(trimmed.to_string())
                    }
                };
...
fn parse_toml_value(raw: &str) -> Result<Value, toml::de::Error> {
    let wrapped = format!("_x_ = {raw}");
    let table: toml::Table = toml::from_str(&wrapped)?;
    table.get("_x_").cloned().ok_or_else(|| SerdeError::custom("missing sentinel key"))
}
```
Parsing rules:
- The key is split naively on `.` (config/src/overrides.rs).
  - **LIVE:** `-c 'projects."/tmp/x".trust_level="trusted"'` had **no effect**, because the quotes stay part of the key segment.
  - Use an inline table at the parent level instead. **LIVE**, this works: `-c 'projects={"/tmp/codex_empty"={trust_level="trusted"}}'`.
- When the same key is repeated, the last value wins. **Across the `resume` subcommand boundary, the post-`resume` set replaces the pre-`resume` set** (§0.3).
- Values are trimmed. A value that fails TOML parsing has all of its leading and trailing `"`/`'` characters stripped.

Quoting table (agent 1, binary-verified with `codex debug prompt-input`; the Python `subprocess` argv equivalent is in brackets):

| Shell argument | Resulting string |
|---|---|
| `-c model=gpt-5.5` | `gpt-5.5` (unquoted is fine) |
| `-c 'developer_instructions="line1\nline2"'` [argv `developer_instructions="line1\nline2"` with a literal backslash-n] | `line1`⏎`line2` (TOML escape honored) |
| `-c developer_instructions="line1\nline2"` (shell strips the quotes) | literal `line1\nline2`, with **no** newline |
| `-c $'developer_instructions=line1\nline2'` [argv containing a real newline, unquoted] | real newline (raw fallback) |
| `-c "developer_instructions=Be concise and terse"` | `Be concise and terse` |
| `-c 'developer_instructions="quoted" tail'` | `quoted" tail` (gotcha) |
| `-c developer_instructions=true` | error: `invalid type: boolean `true`, expected a string in `developer_instructions`` |

**Recommendation for the bot (LIVE-verified):** pass the value as one argv element built like this (Python):
```python
arg = "developer_instructions=" + json.dumps(text, ensure_ascii=False).replace("\x7f", "\\u007f")
cmd += ["-c", arg]
```
Why this form:
- A JSON string literal is a valid TOML basic string as long as `ensure_ascii=False` is used. With the default `ensure_ascii=True`, emoji become surrogate `\ud83d…` escapes, which TOML rejects. U+007F must also be escaped by hand.
- Checked with `tomllib` and with the binary (`codex -c "$ARG" debug prompt-input hello`). Quotes, backslashes, `\n`, `\t`, Cyrillic, emoji and DEL all round-tripped exactly.
- If TOML parsing fails, Codex silently falls back to the raw string with outer quotes stripped. You then get literal `\n` sequences, not an error.

Alternatively, put the text in `config.toml` or a `-p` profile file.

---

## 6. Adding instructions to every run (`--append-system-prompt` equivalent) and AGENTS.md

### 6.1 How the request is assembled (LIVE, captured request for gpt-6-astra)
- `instructions` (the top-level field) is **not sent** for "Responses Lite" models (all gpt-6/5.6 presets; `use_responses_lite: true`).
- The `input` array is:
  ```
  #0 {type:"additional_tools", role:"developer", tools:[namespace functions{exec,wait,request_user_input,…}, clock{sleep}, collaboration{spawn_agent,…}]}
  #1 developer: "You are Codex, an agent based on GPT-6. …"             <- base instructions (from session_meta.base_instructions)
  #2 developer: [ <developer_instructions>, <skills_instructions>…, <permissions instructions>…, <collaboration_mode>… ]
  #3 developer: <multi_agent_role>…
  #4 developer: <multi_agent_mode>…
  #5 user:      [ "# AGENTS.md instructions for <cwd>\n\n<INSTRUCTIONS>\n…\n</INSTRUCTIONS>", "<environment_context>…</environment_context>" ]
  #6 user:      <the prompt>
  ```
- For `gpt-5.5` (not Lite), the base instructions go in the top-level `instructions` field and the tools in `tools` (**LIVE**).

### 6.2 Options compared

| Mechanism | Replaces the base prompt? | Where it lands | Behavior on resume |
|---|---|---|---|
| **`developer_instructions`** (`-c`, config.toml, or `-p` profile) | **No, it appends** | First content part of developer message #2, verbatim, with no wrapper tag (**LIVE**) | **LIVE:** passing a new value on `resume` has **no effect**; the old text stays in history. After any compaction (manual, auto, or model switch) the *current process's* value is re-injected (**LIVE**, V3 appeared after the model-switch compaction). A run without the flag after a compaction loses it entirely (**LIVE**). **Pass the same value on every invocation.** |
| `$CODEX_HOME/AGENTS.md` (or `AGENTS.override.md`, which wins) | No | User message `# AGENTS.md instructions for <cwd>` → `<INSTRUCTIONS>` block, before `--- project-doc ---` | Re-read every turn. A change is pushed again as `These AGENTS.md instructions replace all previously provided AGENTS.md instructions.` (**LIVE**) |
| Project `AGENTS.md` files | No | Same user message after `--- project-doc ---` | Changes detected on resume (**LIVE**). Skipped entirely for **untrusted** projects |
| `instructions` / `model_instructions_file` | **Yes, replaces** | Base instructions (#1 or the `instructions` field) | Config value beats the persisted `session_meta.base_instructions` on every request |
| `experimental_instructions_file` | n/a | — | Not a key at this tag |
| app-server `thread/start` / `thread/resume` params `developerInstructions` / `baseInstructions` | — | — | app-server only |

**Recommendation:** use `developer_instructions`.
- Set it in `$CODEX_HOME/config.toml` if it's static; pass it via `-c` on **every** exec, new and resume, if it's dynamic.
- If a mid-thread change must take effect immediately, write it to `$CODEX_HOME/AGENTS.md` instead. The trade-off is that it arrives as a user-role message.

### 6.3 AGENTS.md discovery (SOURCE codex-rs/core/src/agents_md.rs, codex-home/src/instructions/mod.rs; LIVE)
- **Global file:** `$CODEX_HOME/AGENTS.override.md`, else `$CODEX_HOME/AGENTS.md`. The first non-empty one is used, and it is trimmed. It is not counted against `project_doc_max_bytes`.
- **Project root:** the nearest ancestor containing a `project_root_markers` entry (default `[".git"]`). Without a marker, only the cwd is searched.
- **Per directory, from the project root down to the cwd:** the first of `AGENTS.override.md`, `AGENTS.md`, then `project_doc_fallback_filenames`.
- **Size budget:** `project_doc_max_bytes` (32768) is a shared budget. Untrusted projects get no project docs.
- **Joining:** global text + `\n\n--- project-doc ---\n\n` + project files joined with `\n\n`.
- **Wrapper:** the result is wrapped as follows (**LIVE**, repo with a root and a `sub/` AGENTS.md, run from `sub/`):
```
# AGENTS.md instructions for /tmp/codex_repo/sub

<INSTRUCTIONS>
GLOBAL_AGENTS_MARKER: codex home AGENTS.md

--- project-doc ---

PROJECT_AGENTS_MARKER: root AGENTS.md


SUBDIR_AGENTS_MARKER: sub AGENTS.md

</INSTRUCTIONS>
```

---

## 7. Manual compaction and status without the TUI

### 7.1 `codex exec` behavior
- **No slash commands.** The prompt is passed as `UserInput::Text` verbatim, so `/compact` or `/status` is just text sent to the model (SOURCE exec/src/lib.rs; slash commands exist only in codex-rs/tui/src/slash_command.rs).
- **No compaction flag.**
- **Compaction is invisible in `--json`:** the `ContextCompaction` item is dropped. Human mode prints `context compacted` to stderr.
- **Automatic compaction** runs pre-turn, mid-turn, or post-turn when active tokens reach `model_auto_compact_token_limit` (default 90% of the window).
  - For the OpenAI provider it uses **remote compaction v2**: a POST to `/responses` whose input ends with `{"type":"compaction_trigger"}` and header `x-codex-beta-features: remote_compaction_v2`, expecting one `{"type":"compaction","encrypted_content":…}` output item (**LIVE**).
  - Other providers use a local summarization turn.
- **A model switch on resume forces a compaction** (**LIVE**, §3.4 l).
- **Possible exec-only workaround (UNVERIFIED):** `codex exec resume <id> -c model_auto_compact_token_limit=<small N> "<prompt>"`. On resume, token info is seeded from the rollout, so the pre-turn check should compact first. It still requires a turn.

### 7.2 app-server over stdio: `thread/compact/start` (LIVE, full transcript)
- **Framing:** newline-delimited JSON. The server does not send `"jsonrpc":"2.0"` and does not need it (codex-rs/app-server-protocol/src/rpc.rs: "We do not do true JSON-RPC 2.0").
- **Ordering:** requests before `initialize` fail with `Not initialized`.
- **Loaded threads only:** `thread/compact/start` only works on a thread loaded in *this* server, so call `thread/resume` first. Use the same `CODEX_HOME` as exec.
- **Auth:** the standalone app-server **does not honor `CODEX_API_KEY`**; it uses `auth.json`.

Transcript (`/tmp/fake_openai/as_client.py`, `CODEX_HOME` with an api-key `auth.json` and `openai_base_url` in config.toml; `>>>` = sent, `<<<` = received):
```
>>> {"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "tg_bot_probe", "title": null, "version": "0.1.0"}}}
<<< {"id": 1, "result": {"userAgent": "tg_bot_probe/0.159.0 (Ubuntu 26.4.0; x86_64) dumb (tg_bot_probe; 0.1.0)", "codexHome": "/tmp/codex_home_as", "platformFamily": "unix", "platformOs": "linux"}}
>>> {"method": "initialized"}
>>> {"id": 2, "method": "thread/resume", "params": {"threadId": "01a0edb9-dcea-7382-846c-58c82bf3debb", "excludeTurns": true}}
<<< {"method": "configWarning", "params": {"summary": "Codex could not find bubblewrap on PATH. … Codex will use the bundled bubblewrap in the meantime.", "details": null}, "emittedAtMs": …}
<<< {"method": "remoteControl/status/changed", …}
<<< {"method": "thread/status/changed", "params": {"threadId": "01a0edb9-…", "status": {"type": "idle"}}, …}
<<< {"id": 2, "result": {"thread": {"id": "01a0edb9-dcea-7382-846c-58c82bf3debb", …, "historyMode": "paginated", "modelProvider": "openai", "model": "gpt-6-astra", "reasoningEffort": null, …, "path": "/tmp/codex_home_as/sessions/2026/09/29/rollout-…jsonl", …}, "model": …, …}}
<<< {"method": "thread/tokenUsage/updated", "params": {"threadId": "01a0edb9-…", "turnId": "01a0edb9-dcf9-…", "tokenUsage": {"total": {"totalTokens": 1250, "inputTokens": 1200, "cachedInputTokens": 200, "cacheWriteInputTokens": 0, "outputTokens": 50, "reasoningOutputTokens": 10}, "last": {"totalTokens": 1250, …}, "modelContextWindow": 258400}}, …}
<<< {"method": "thread/goal/cleared", "params": {"threadId": "01a0edb9-…"}, …}
>>> {"id": 3, "method": "thread/compact/start", "params": {"threadId": "01a0edb9-dcea-7382-846c-58c82bf3debb"}}
<<< {"id": 3, "result": {}}
<<< {"method": "thread/status/changed", "params": {"threadId": "…", "status": {"type": "active", "activeFlags": []}}, …}
<<< {"method": "turn/started", "params": {"threadId": "…", "turn": {"id": "01a0edba-4585-…", "items": [], "itemsView": "notLoaded", "status": "inProgress", "error": null, …}}, …}
<<< {"method": "item/started", "params": {"item": {"type": "contextCompaction", "id": "01a0edba-4593-…"}, "threadId": "…", "turnId": "01a0edba-4585-…", "startedAtMs": …}, …}
<<< {"method": "thread/tokenUsage/updated", "params": {…, "tokenUsage": {"total": {"totalTokens": 1250, …}, "last": {"totalTokens": 5363, "inputTokens": 0, …}, "modelContextWindow": 258400}}, …}
<<< {"method": "item/completed", "params": {"item": {"type": "contextCompaction", "id": "01a0edba-4593-…"}, "threadId": "…", "turnId": "01a0edba-4585-…", "completedAtMs": …}, …}
<<< {"method": "thread/status/changed", "params": {"threadId": "…", "status": {"type": "idle"}}, …}
<<< {"method": "turn/completed", "params": {"threadId": "…", "turn": {"id": "01a0edba-4585-…", "items": [], "itemsView": "notLoaded", "status": "completed", "error": null, "startedAt": 1790694802, "completedAt": 1790694802, "durationMs": 94}}, …}
>>> {"id": 4, "method": "account/rateLimits/read"}
<<< {"error": {"code": -32600, "message": "chatgpt authentication required to read rate limits"}, "id": 4}
```
(Closing stdin shuts the server down with exit 0.)

How to use it:
- **Minimal sequence:** `initialize` → `initialized` → `thread/resume {threadId, excludeTurns:true}` → `thread/compact/start {threadId}` → wait for `turn/completed` (status `completed`, `failed` or `interrupted`). `item/completed` with `item.type=="contextCompaction"` confirms the compaction happened.
- **`thread/compacted`** exists in the protocol but is deprecated and never emitted at this tag.
- **Params and results:** `ThreadCompactStartParams { thread_id }` (camelCase `threadId`), result `{}`. It aborts any running turn on that thread.
- **Context status without running a turn:** `thread/resume` alone replays `thread/tokenUsage/updated` with `total`, `last` and `modelContextWindow`. `last.totalTokens` is the current context size. The resume result also carries `model` and `reasoningEffort`.
- **Rate limits:**
  - `account/rateLimits/read` needs **ChatGPT auth**. It calls `GET https://chatgpt.com/backend-api/wham/usage` and returns `{rateLimits:{limitId, primary:{usedPercent (int), windowDurationMins, resetsAt}, secondary, credits, planType, …}, rateLimitsByLimitId, …}`.
  - The v2 wire names differ from the rollout names: `usedPercent` is an integer and the window is `windowDurationMins`.
  - With API-key auth it fails with `chatgpt authentication required to read rate limits`.
  - Live updates arrive as `account/rateLimits/updated`.

## 8. Authentication on a headless server (ChatGPT Plus/Pro)

### 8.1 Commands
`codex login --help` (verbatim, LIVE):
```
Manage login

Usage: codex login [OPTIONS] [COMMAND]

Commands:
  status  Show login status
  help    Print this message or the help of the given subcommand(s)

Options:
  -c, --config <key=value>
          Override a configuration value that would otherwise be loaded from `~/.codex/config.toml`.
          Use a dotted path (`foo.bar.baz`) to override nested values. The `value` portion is parsed
          as TOML. If it fails to parse as TOML, the raw string is used as a literal.
          
          Examples: - `-c model="o3"` - `-c 'sandbox_permissions=["disk-full-read-access"]'` - `-c
          shell_environment_policy.inherit=all`

      --with-api-key
          Read the API key from stdin (e.g. `printenv OPENAI_API_KEY | codex login --with-api-key`)

      --enable <FEATURE>
          Enable a feature (repeatable). Equivalent to `-c features.<name>=true`

      --with-access-token
          Read the access token from stdin (e.g. `printenv CODEX_ACCESS_TOKEN | codex login
          --with-access-token`)

      --disable <FEATURE>
          Disable a feature (repeatable). Equivalent to `-c features.<name>=false`

      --device-auth
          

  -h, --help
          Print help (see a summary with '-h')
```

**Browser login.** Plain `codex login` starts a browser flow with a local callback server on `127.0.0.1:1455` (or 1457) and prints:
```
Starting local login server on http://localhost:{port}.
If your browser did not open, navigate to this URL to authenticate:

{auth_url}

On a remote or headless machine? Use `codex login --device-auth` instead.
```
It does **not** auto-detect a headless host (codex-rs/cli/src/login.rs).

**Device code: `codex login --device-auth`** (SOURCE, codex-rs/login/src/device_code_auth.rs; not run, since it would contact auth.openai.com).
- It first **revokes and clears any existing login**.
- Then it POSTs `https://auth.openai.com/api/accounts/deviceauth/usercode`, prints the prompt below to **stdout** (ANSI colors are always included), and polls `…/deviceauth/token` for up to 15 minutes.
- Prompt text:
  ```
  Welcome to Codex [v0.159.0]
  OpenAI's command-line coding agent

  Follow these steps to sign in with ChatGPT using device code authorization:

  1. Open this link in your browser and sign in to your account
     https://auth.openai.com/codex/device

  2. Enter this one-time code (expires in 15 minutes)
     <CODE>

  Continue only if you started this login in Codex. If a website or another person gave you this code, cancel.
  ```
- Success: stderr `Successfully logged in`, exit 0.
- Failure: stderr `Error logging in with device code: {e}`, exit 1. Timeout: `device auth timed out after 15 minutes`. Server without device auth: `device code login is not enabled for this Codex server. Use the browser login or verify the server URL.`
- The format of the one-time code is **UNVERIFIED**.

**API key: `printenv OPENAI_API_KEY | codex login --with-api-key`.** It reads stdin and is **local only**, with no network call. **LIVE:**
```
$ echo "fake-key" | codex login --with-api-key      # stderr: "Reading API key from stdin..." / "Successfully logged in", exit 0
$ cat $CODEX_HOME/auth.json                          # mode 0600
{
  "auth_mode": "apikey",
  "OPENAI_API_KEY": "fake-key"
}
```
The removed `--api-key` flag now prints `The --api-key flag is no longer supported. Pipe the key instead, e.g. …` and exits 1.

**`codex login status`.** All output goes to **stderr**, nothing to stdout (SOURCE codex-rs/cli/src/login.rs; LIVE by agent 2 and me):

| State | stderr | exit |
|---|---|---|
| nothing stored | `Not logged in` | 1 |
| API key | `Logged in using an API key - sk-proj-***ABCDE` (first 8 chars + `***` + last 5; keys ≤ 13 chars print `***`, **LIVE**: `Logged in using an API key - ***`) | 0 |
| ChatGPT | `Logged in using ChatGPT` | 0 |
| access token / PAT | `Logged in using access token` / `Logged in using personal access token` | 0 |
| corrupt auth.json | `Error checking login status: …` | 1 |
| bad `CODEX_HOME` | `Error loading configuration: CODEX_HOME points to "…", but that path does not exist` | 1 |

`login status` **ignores `CODEX_API_KEY`**, even though exec uses it.

**`codex logout`:** prints `Successfully logged out` (exit 0), or `Not logged in` (exit 0) if nothing was stored. For ChatGPT logins it revokes the refresh token at `https://auth.openai.com/oauth/revoke`.

### 8.2 `auth.json` (verbatim, codex-rs/login/src/auth/storage.rs, token_data.rs)
```rust
/// Expected structure for $CODEX_HOME/auth.json.
pub struct AuthDotJson {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub auth_mode: Option<AuthMode>,
    #[serde(rename = "OPENAI_API_KEY")]
    pub openai_api_key: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tokens: Option<TokenData>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub last_refresh: Option<DateTime<Utc>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub agent_identity: Option<AgentIdentityStorage>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub personal_access_token: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub bedrock_api_key: Option<BedrockApiKeyAuth>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub bedrock_access_keys: Option<BedrockAccessKeysAuth>,
}
pub struct TokenData {
    /// Flat info parsed from the JWT in auth.json.
    #[serde(deserialize_with = "deserialize_id_token", serialize_with = "serialize_id_token")]
    pub id_token: IdTokenInfo,       // stored as the raw JWT string
    /// This is a JWT.
    pub access_token: String,
    pub refresh_token: String,
    pub account_id: Option<String>,
}
```
- **`auth_mode` values:** `apikey`, `chatgpt`, `chatgptAuthTokens`, `headers`, `agentIdentity`, `personalAccessToken`, `bedrockApiKey`, `bedrockAccessKeys`.
- **ChatGPT shape (SOURCE-derived; no real file captured):** `{"auth_mode":"chatgpt","OPENAI_API_KEY":null,"tokens":{"id_token":"<JWT>","access_token":"<JWT>","refresh_token":"…","account_id":"…"},"last_refresh":"<RFC3339>"}`.
- **Plan type:** read from the id_token claim `https://api.openai.com/auth.chatgpt_plan_type` (no signature check).
- **Storage** (`cli_auth_credentials_store`): `file` (the default), `keyring`, `auto` or `ephemeral`. Keep `file` on a server.
- **Refresh (SOURCE, codex-rs/login/src/auth/manager.rs):**
  - Proactive when the access-token JWT `exp` is within 5 minutes, or when `last_refresh` is more than 8 days old.
  - On a 401 it first reloads auth.json from disk, then refreshes against `https://auth.openai.com/oauth/token`.
  - New tokens are written back to auth.json, so **CODEX_HOME must be writable**.
  - Coordination is in-process only; there is **no cross-process file lock**, so avoid many concurrent codex processes refreshing at the same moment.
  - Permanent failures produce one of:
    - `Your access token could not be refreshed because your refresh token has expired. Please log out and sign in again.`
    - `… refresh token was already used …`
    - `… refresh token was revoked …`
    - `Your access token could not be refreshed. Please log out and sign in again.`
- **Copying auth.json** from a machine where you logged in works. Refresh writes back into it. A later `codex login` on the source machine revokes the old refresh token.

### 8.3 Precedence and billing (SOURCE + agent 2)
`load_auth` (codex-rs/login/src/auth/manager.rs) checks sources in this order:
1. **`CODEX_API_KEY`**, only when the caller sets `enable_codex_api_key_env`. **`codex exec` does** (exec/src/lib.rs `enable_codex_api_key_env: true`).
2. The ephemeral store.
3. `CODEX_ACCESS_TOKEN`.
4. auth.json / keyring.

Consequences:
- **`CODEX_API_KEY` beats a stored ChatGPT login in exec.** It uses `AuthMode::ApiKey` with base URL `https://api.openai.com/v1` instead of `https://chatgpt.com/backend-api/codex`, so billing switches to the API key. The one exception: workspace-policy bootstrap still uses the stored ChatGPT token for business/edu/enterprise plans.
- **`OPENAI_API_KEY` is NOT read by exec's auth.** It does not override a ChatGPT login and does not switch billing. It is only a TUI prefill and a realtime fallback. It is, however, inherited by model-run shell commands.
- The TUI, `login status` and the standalone app-server ignore `CODEX_API_KEY`.
- **Footgun (SOURCE):** `forced_login_method = "chatgpt"` together with `CODEX_API_KEY` in the environment makes `codex exec` print `ChatGPT login is required, but an API key is currently being used. Logging out.`, **delete auth.json**, and exit 1.
- **For a Plus/Pro bot:** make sure `CODEX_API_KEY` is not set in the service environment. Log in once (`codex login --device-auth`, or copy auth.json) into the service's `CODEX_HOME`.

### 8.4 `CODEX_HOME`
- Default `~/.codex`.
- If set, it **must already exist** and be a directory. It is canonicalized. Otherwise every command fails with `CODEX_HOME points to "…", but that path does not exist`.
- An empty value counts as unset.
- `$CODEX_HOME/.env` is loaded at startup, with `CODEX_*` keys ignored.
- Contents:
  - `config.toml`, `<profile>.config.toml`, `auth.json`, `.env`
  - `sessions/`, `archived_sessions/`, `session_index.jsonl`
  - `log/`
  - SQLite `state_5.sqlite`, `logs_2.sqlite`, `thread_history_1.sqlite`, `goals_1.sqlite`, `memories_1.sqlite`, `queue_1.sqlite` (or in `CODEX_SQLITE_HOME`)
  - `models_cache.json` (ChatGPT auth), `installation_id`, `shell_snapshots/`, `rules/`, `skills/.system/…` (bundled skills are written there, **LIVE**), `tmp/arg0/…` (helper symlinks), `packages/standalone/…` (installer)
- **Do not put `CODEX_HOME` under `/tmp`.** Release builds then refuse to create helper aliases and print on every start: `WARNING: proceeding, even though we could not create PATH aliases: Refusing to create helper binaries under temporary dir "/tmp" (codex_home: …)`. It still works.
- **Multiple bot users:** one `CODEX_HOME` per ChatGPT account.

---

## 9. Model list at this tag

**Source:** bundled catalog `codex-rs/models-manager/models.json`, embedded in the binary (`codex debug models --bundled` prints it).

**Remote catalog (ChatGPT auth only):**
- Fetched from `https://chatgpt.com/backend-api/codex/models?client_version=0.159.0` and cached in `$CODEX_HOME/models_cache.json` for 300 s. The `X-Models-Etag` response header on /responses triggers a refresh.
- For ChatGPT accounts the server list is authoritative when it contains visible models. **The live list per plan is UNVERIFIED** (no account).
- With an API key, only the bundled list is used (**LIVE**: no `/models` request was made).

**Default model:** the first picker-visible (`visibility:"list"`) model by `priority`, which is **`gpt-6-astra`**. With an API key, **LIVE**: the request used `"model":"gpt-6-astra"`, `"reasoning":{"effort":"low"}`. There is no hard-coded default slug.

| Slug | Display name | Description | Picker | Priority | Default effort | Supported efforts | Context / max | Tool mode | Responses Lite |
|---|---|---|---|---|---|---|---|---|---|
| `gpt-6-astra` | GPT-6-Astra | Frontier intelligence for the most demanding work. | list | 1 | **low** | low, medium, high, xhigh, max, ultra | 272000 / 872000 | code_mode_only | yes |
| `gpt-6-sol` | GPT-6-Sol | Workhorse model for coding and everyday work. | list | 2 | medium | low, medium, high, xhigh, max, ultra | 272000 / 872000 | code_mode_only | yes |
| `gpt-6-luna` | GPT-6-Luna | Fast and affordable model for easier tasks. | list | 3 | medium | low, medium, high, xhigh, max | 272000 / 872000 | code_mode_only | yes |
| `gpt-5.6-sol` | GPT-5.6-Sol | Older coding model for complex work. | list | 4 | low | low, medium, high, xhigh, max, ultra | 272000 / 872000 | code_mode_only | yes |
| `gpt-5.6-terra` | GPT-5.6-Terra | Older balanced model for straightforward work. | list | 7 | medium | low, medium, high, xhigh, max, ultra | 272000 / 872000 | code_mode_only | yes |
| `gpt-5.6-luna` | GPT-5.6-Luna | Older fast and efficient model. | list | 8 | medium | low, medium, high, xhigh, max | 272000 / 872000 | code_mode_only | yes |
| `gpt-daybreak-blue-latest` | Daybreak Blue | Latest frontier agentic coding model for broad defensive cybersecurity work. | hide | 10 | low | low … ultra | 272000 / 872000 | code_mode_only | yes |
| `gpt-daybreak-red-latest` | Daybreak Red | Cyber-permissive variant of our latest frontier agentic coding model for advanced, authorized cybersecurity research. | hide | 11 | medium | low … ultra | 372000 / 372000 | code_mode_only | yes |
| `gpt-5.5` | GPT-5.5 | Legacy coding model. | list | 12 | medium | low, medium, high, xhigh | 272000 / 272000 | direct (`exec_command`, `apply_patch`, … as normal tools) | no |
| `codex-auto-review` | Codex Auto Review | Automatic approval review model for Codex. | hide | 43 | medium | low … max | 272000 / 872000 | code_mode_only | yes |

**Update 2026-09-29 (after 0.159.0): GPT-6.1 Sol.** SOURCE: `codex-rs/models-manager/models.json` at openai/codex main, merge commit `b1e72963` of PR #49318 ("Add GPT-6.1 Sol as the default catalog model"), backported to `release/0.159` for 0.159.1 (#49323). New entry `gpt-6.1-sol`, display "GPT-6.1-Sol", "Latest workhorse model for coding and everyday work.", visibility `list`, **priority 1** (so it becomes the default model), default effort **low**, efforts low … ultra, context 272000 / 872000, `code_mode_only`, `minimal_client_version` **0.153.0** (0.159.0 can use it), service tier `priority` ("Fast", "2x speed, increased usage"), plans include plus/pro/business. The other priorities shift by one (gpt-6-astra 2, gpt-6-sol 3, gpt-6-luna 4, …). API docs: model ID `gpt-6.1-sol`.

**Effort descriptions** (identical for all models):
- `low`: "Fast responses with lighter reasoning"
- `medium`: "Balances speed and reasoning depth for everyday tasks"
- `high`: "Greater reasoning depth for complex problems"
- `xhigh`: "Extra high reasoning depth for complex problems"
- `max`: "Maximum reasoning depth for the hardest problems"
- `ultra`: "Maximum reasoning with automatic task delegation"

**Other catalog fields:**
- `default_verbosity`: `low` for all models except Daybreak Red (`high`).
- Multi-agent version: v2 for gpt-6-* and gpt-5.6-sol/terra; v1 for gpt-5.6-luna.
- `gpt-6-sol` and `gpt-6-luna` declare `default_service_tier: "priority"` (tier "Fast", "1.5x speed"). **LIVE** with API-key auth the request still carried `"service_tier": null`. The behavior under ChatGPT auth is **UNVERIFIED**. The config key `service_tier` exists.
- `model_context_window` as reported = context_window × 95% (e.g. 258400). The default auto-compaction threshold is 90% of context_window.

---

## 10. Linux sandbox

**Mechanism** (codex-rs/linux-sandbox/README.md, launcher.rs, linux_run_main.rs):
- **bubblewrap** isolates the filesystem: `--ro-bind / /`, writable roots via `--bind`, and `.git`/`.codex` inside writable roots re-bound read-only.
- It also uses `--unshare-user --unshare-pid`, plus `--unshare-net` when network access is off.
- A second stage (`codex-linux-sandbox --apply-seccomp-then-exec`) sets `PR_SET_NO_NEW_PRIVS` and installs **seccomp**, which blocks ptrace, io_uring and non-AF_UNIX sockets when the network is off.
- **Landlock** is legacy and deprecated (`features.use_legacy_landlock`). It is refused for filesystem-restricted policies: `filesystem-restricted execution requires bubblewrap to isolate app-server sockets`.
- `danger-full-access` uses **no sandbox**.
- **There is no fallback when user namespaces are unavailable:** "This path never falls back to legacy Landlock on failure."

**bwrap discovery:**
- Order: a system `bwrap` on PATH (must support `--perms`), then the bundled one (`codex-resources/bwrap` in the package or next to the exe; SHA-256 pinned). Otherwise it panics with `bubblewrap is unavailable: …` (§0.2).
- **Missing-bwrap warning:** `warning: Codex could not find bubblewrap on PATH. Install bubblewrap with your OS package manager. See the sandbox prerequisites: https://developers.openai.com/codex/concepts/sandboxing#prerequisites. Codex will use the bundled bubblewrap in the meantime.`
  - Printed by exec in human mode only (**LIVE**).
  - App-server sends it as a `configWarning` notification (**LIVE**).

**Known problems on VPS/containers (LIVE on this Ubuntu 26.04 VM, `apparmor_restrict_unprivileged_userns=1`):**
```
bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted     (default read-only, network off)
bwrap: setting up uid map: Permission denied                      (network on)
```
- The Ubuntu AppArmor profile `bwrap-userns-restrict` grants userns only to `/usr/bin/bwrap`, so the bundled copy is not exempt.
- Options:
  - `apt install bubblewrap` (Codex prefers PATH), **UNVERIFIED** here.
  - `sysctl kernel.apparmor_restrict_unprivileged_userns=0`, **UNVERIFIED**.
  - An external container/VM with `--dangerously-bypass-approvals-and-sandbox` or `-s danger-full-access`, verified: no bwrap is used.
- Docker defaults are **UNVERIFIED**; typically unprivileged userns inside Docker's seccomp profile fails similarly.

**What happens in the agent:** a sandbox-failed command is classified as a sandbox denial.
- Under exec's `never` approval it is not retried unsandboxed; the model just gets the bwrap error text.
- No `command_execution` item is emitted (§3.4 d).

**`--dangerously-bypass-approvals-and-sandbox`** (alias **`--yolo`**; SOURCE codex-rs/utils/cli/src/shared_options.rs, `alias = "yolo"`):
- In exec it sets `sandbox_mode=danger-full-access` and keeps approval `never`.
- It **also skips the git-repo check** (exec/src/lib.rs: "When --yolo … is set, also skip the git repo check").
- It does not bypass hook trust (`--dangerously-bypass-hook-trust` does that).
- **LIVE**, the model-visible text under bypass: `<permission_profile type="disabled"><file_system type="unrestricted" />`.

**`--approve-for-me`** (alias `--not-so-yolo`) sets `approvals_reviewer=auto_review`, `approval_policy=on-request`, `sandbox_mode=workspace-write`. A reviewer sub-agent decides escalations. It conflicts with `-s` and `--yolo`.

**`--full-auto` no longer exists** (`error: unexpected argument '--full-auto' found`, exit 2), and exec has no `-a`.

**Defaults in exec:**
- **approval `never`** (harness override, exec/src/lib.rs: "Default to never ask for approvals in headless mode").
- **sandbox `read-only`**, or `workspace-write` if the project has a trust entry (LIVE, §13).
- Under `never`, escalation requests return to the model as `approval policy is Never; reject command — you cannot ask for escalated permissions if the approval policy is Never`. Any approval request reaching exec is rejected (`… is not supported in exec mode …`).
- Model-visible permission text (**LIVE**, read-only): `` `sandbox_mode` is `read-only`: The sandbox only permits reading files. Network access is restricted.\nApproval policy is currently never. Do not provide the `sandbox_permissions` for any reason, commands will be rejected. ``
- The environment of sandboxed commands gets `CODEX_SANDBOX_NETWORK_DISABLED=1` when the network is off. `CODEX_SANDBOX` is never set on Linux (macOS only: `seatbelt`).

---

## 11. Stopping a running `codex exec`

- **SIGINT** (Ctrl-C, `kill -INT`). exec catches it with `tokio::signal::ctrl_c()` and sends `turn/interrupt` to its in-process server (exec/src/lib.rs).
  - **LIVE:** exit code **1** after ~0 s.
  - **No `turn.failed` or `turn.completed` is emitted**; the stream just ends after the last item. `TurnStatus::Interrupted` maps to no event, but it sets `error_seen` so the exit code is 1.
  - A running child command (`bash -lc '…sleep 30…'`) **was killed** (LIVE).
  - The session **remains resumable**. On the next `resume` the model sees a developer message (LIVE):
    ```
    <turn_aborted>
    The previous turn was interrupted on purpose. Any running unified exec processes may still be running in the background. If any tools/commands were aborted, they may have partially executed.
    </turn_aborted>
    ```
  - A partially streamed assistant text is not kept in history.
- **SIGTERM:** there is no handler, so the process dies immediately (**exit 143**) with no terminal event. **LIVE:** the rollout is intact up to the last persisted line and `resume` works. The next prompt simply follows the unanswered user message, with no `<turn_aborted>` marker.
- **Recommendation:** send SIGINT to the codex process, wait a few seconds, then SIGKILL the process group. Treat "stream ended without `turn.completed`/`turn.failed`" as *interrupted*. Keep the `thread_id` from `thread.started` for resuming.
- **Instant-steering alternative (not exec):** feature `instant_interrupt` (under development) and app-server `turn/steer`/`turn/interrupt`.

---

## 12. Environment variables and stdout/stderr

**stdout in `--json` mode:** only JSONL lines (**LIVE**, verified across all runs).

**stderr, in any mode (LIVE):**
- `Reading additional input from stdin...` / `Reading prompt from stdin...`
- `WARNING: proceeding, even though we could not create PATH aliases: …` (only when `CODEX_HOME` is under the temp dir)
- tracing logs. The default filter is `error,opentelemetry_sdk=off,opentelemetry_otlp=off`, changed with `RUST_LOG`. Seen live:
  - `2026-09-29T14:52:06.086660Z ERROR codex_api::endpoint::responses_websocket: failed to connect to websocket: HTTP error: 426 Upgrade Required, url: ws://127.0.0.1:18080/v1/responses` (only because my fake server refuses WebSockets)
  - `ERROR codex_core::tools::router: error=unsupported call: update_plan`
- `Error: …` fatal errors before the turn (bad resume id, config errors, `Not inside a trusted directory …`)
- `Warning: no last agent message; wrote empty content to <file>` (`-o` with no message)
- In human mode: the whole transcript plus `tokens used\nN`; the final message goes to stdout.

Use `--color never` for plain stderr.

**Environment variables that matter for a service** (SOURCE; from agent 2 plus my checks):

| Var | Effect |
|---|---|
| `CODEX_HOME` | config/state dir; must exist; not under /tmp |
| `CODEX_SQLITE_HOME` | where the *.sqlite files go (default `CODEX_HOME`) |
| `CODEX_API_KEY` | **exec only:** API-key auth that overrides auth.json and switches billing to the API |
| `OPENAI_API_KEY` | **not** used by exec auth; only inherited by child commands |
| `CODEX_ACCESS_TOKEN` | access token / PAT auth (network-validated) |
| `OPENAI_BASE_URL` | **not** used for Codex's own requests. Use config `openai_base_url` (or a custom `model_providers.<id>.base_url`) |
| `OPENAI_ORGANIZATION`, `OPENAI_PROJECT` | sent as headers to the openai provider |
| `RUST_LOG` | stderr log filter for exec |
| `NO_COLOR` | only read directly by `codex doctor`; for exec use `--color never` |
| `HTTPS_PROXY` / `HTTP_PROXY` / `ALL_PROXY` / `NO_PROXY` | outbound proxy |
| `CODEX_CA_CERTIFICATE`, `SSL_CERT_FILE` | custom CA bundle |
| `CODEX_INTERNAL_ORIGINATOR_OVERRIDE` | `originator` header (exec default `codex_exec`) |
| `CODEX_REFRESH_TOKEN_URL_OVERRIDE`, `CODEX_REVOKE_TOKEN_URL_OVERRIDE` | auth endpoint overrides |
| `TMPDIR` | affects the "CODEX_HOME under temp" check and the workspace-write writable roots |
| `TRACEPARENT` | OTEL parent span |
| `CODEX_MANAGED_BY_NPM` / `_PNPM` / `_BUN` | set by the npm shim |

**Env seen by model-run commands:**
- The whole service environment is inherited by default (`shell_environment_policy.inherit=all`, `ignore_default_excludes=true`), **including `TELEGRAM_BOT_TOKEN` and other secrets**.
- Codex adds `CODEX_THREAD_ID`, `CODEX_SESSION_ID`, `CODEX_VERSION`, `CODEX_PERMISSION_PROFILE`, `CODEX_CI=1`, `NO_COLOR=1`, `TERM=dumb`, `PAGER=cat`, … and `CODEX_SANDBOX_NETWORK_DISABLED=1` when the network is off.
- Recommended: `-c shell_environment_policy.ignore_default_excludes=false`, which drops `*KEY*`, `*SECRET*` and `*TOKEN*`, or an explicit `exclude`/`inherit="core"`.

**Background network activity seen in a fresh `CODEX_HOME` (LIVE):**
- Git clones of `https://github.com/openai/plugins` into `$CODEX_HOME/.tmp/plugins-clone-*` (feature `plugins`, stable, on by default; not tested with `--disable plugins`).
- A remote plugin sync. With API-key auth this logs `WARN codex_core_plugins::manager: remote installed plugin bundle sync failed error=chatgpt authentication required for remote plugin catalog; api key auth is not supported` (only with RUST_LOG).
- A "prewarm" WebSocket request (`request_kind: "prewarm"` in `x-codex-turn-metadata`) before the first turn.

---

## 13. Not a git repo; untrusted cwd

- **Git check** (exec/src/lib.rs):
  ```rust
  if !skip_git_repo_check && !dangerously_bypass_approvals_and_sandbox && get_git_repo_root(&default_cwd).is_none() {
      eprintln!("Not inside a trusted directory and --skip-git-repo-check was not specified.");
      std::process::exit(1);
  }
  ```
  - **LIVE:** exit 1 with that stderr line.
  - Despite the wording, it only checks for a **git repo**. A trust entry does not satisfy it.
  - `--skip-git-repo-check` or `--dangerously-bypass-approvals-and-sandbox` skips it.
  - It runs *after* stdin is read.
- **Trust entries:** `[projects."<abs path>"] trust_level = "trusted"|"untrusted"` in `$CODEX_HOME/config.toml`, looked up by cwd and then by git root.

**What trust changes (LIVE):**
- **Default sandbox:** with no `-s`, a directory with any trust entry defaults to `workspace-write`; otherwise `read-only`.
  - **LIVE**, the stderr header of three runs in `/tmp/codex_empty`:
    - no entry → `sandbox: read-only`
    - `-c 'projects={"/tmp/codex_empty"={trust_level="trusted"}}'` → `sandbox: workspace-write [workdir, /tmp, $TMPDIR]`
    - config.toml entry → `workspace-write`
  - The dotted form `-c 'projects."/tmp/codex_empty".trust_level="trusted"'` did **not** work (§5.4).
- **Project config:** project `.codex/config.toml`, hooks, execpolicy rules and project AGENTS.md are loaded only for trusted projects. Untrusted projects get a warning: `To load project-local config, hooks, and exec policies, add … as a trusted project in …`.
- **Keys never read from project config:** `openai_base_url`, `model_provider(s)`, `notify`, `profile(s)`, `otel`, … are ignored even when trusted.

**Side effect: Codex writes trust entries itself.**
- **LIVE**, found in my test `config.toml`: `[projects."/tmp/codex_repo"]\ntrust_level = "trusted"`.
- Source: codex-rs/app-server/src/request_processors/thread_processor.rs, used by exec's `thread/start`.
- It happens when all of these hold:
  - a thread is started with a cwd inside a **project** (a git root or other `project_root_markers`, so "projectless" dirs are excluded);
  - the project has no trust entry;
  - the effective permissions can write the cwd (`workspace-write`, `danger-full-access` or `--yolo`).
- Codex then **persists `trust_level = "trusted"` for the git root into `$CODEX_HOME/config.toml`**.
- After that, the project's `.codex/config.toml`, hooks and rules become active, and later runs default to `workspace-write`.
- If the bot manages `config.toml` itself, expect this mutation. Pre-seed an explicit trust entry to control it.

---

## Appendix A: fake-server method and one incident

**Setup.**
- `/tmp/fake_openai/server.py` answers:
  - `GET /v1/models` with `{"models":[]}`;
  - `GET /v1/responses` (WebSocket upgrade) with **426**, so Codex falls back to HTTPS;
  - `POST /v1/responses` with SSE (`response.created`, `response.output_item.added/done`, `response.output_text.delta`, `response.reasoning_summary_text.delta`, `response.completed` with `usage` and `x-codex-*` rate-limit headers);
  - code-mode `exec` custom tool calls (`text(JSON.stringify(await tools.exec_command({cmd: …})))`), `apply_patch`, `web_search_call`, `response.failed`, 401/429/500, slow streams, and `compaction_trigger` → a `compaction` item.
- Invocation: `CODEX_API_KEY=fake codex exec --json … -c 'openai_base_url="http://127.0.0.1:18080/v1"' "… SCENARIO_X"`, the same way the upstream tests do it (codex-rs/core/tests/common/test_codex_exec.rs).
- Captured request bodies are in `/tmp/fake_openai/requests_18080/*.json`. They show the exact prompt layout, the tools (`additional_tools` input item for Lite models), the `x-codex-turn-metadata` header (includes `"sandbox":"seccomp","sandbox_mode":"read-only","model":"gpt-6-astra","reasoning_effort":"low"`), and `prompt_cache_key` = thread id.

**Incident.** One run had `-c openai_base_url=…` before `resume` and another `-c` after it. The first `-c` was dropped (§0.3), and Codex sent requests with the key string `fake` to the real `api.openai.com` / `wss://api.openai.com`, which returned 401. No login was attempted and no real credential exists on this machine. It is recorded here because it demonstrates the `-c` gotcha. `/tmp/codex_home_test/config.toml` contains an auto-written trust entry for `/tmp/codex_repo` (§13).

## Appendix B: recommended bot integration checklist
1. **Install:** `curl -fsSL https://chatgpt.com/codex/install.sh | CODEX_NON_INTERACTIVE=1 sh`, or unpack `codex-package-x86_64-unknown-linux-musl.tar.gz` and run `<pkg>/bin/codex`. Verify with `codex --version` → `codex-cli 0.159.0`.
2. **Home and auth:** set `CODEX_HOME=/var/lib/<bot>/codex` (exists, not in /tmp) and run `codex login --device-auth` once. Do **not** set `CODEX_API_KEY` (it would switch billing). Check with `codex login status` (stderr, exit 0/1).
3. **Spawning:** `stdin=DEVNULL`, `--json`, `--skip-git-repo-check`, `-o <file>`, `--` before the prompt. Put all `-c` flags **after** `resume` when resuming, and all non-global flags (`-s`, `-C`, `--add-dir`) **before** `resume`.
4. **Parsing:**
   - `thread.started.thread_id` is the session id. Compare it on resume to detect silent new threads.
   - Render `item.completed` for `agent_message`, `command_execution`, `file_change`, `web_search`, `mcp_tool_call` and `error` (non-fatal).
   - Treat top-level `error` as a status message.
   - Finish on `turn.completed` or `turn.failed`, and treat EOF without either as interrupted.
   - `usage` is cumulative.
5. **Status/context:** tail the rollout file `$CODEX_HOME/sessions/**/rollout-*-<thread_id>.jsonl` for the last `token_count` (context = `last_token_usage.total_tokens` / `model_context_window`; rate limits = `rate_limits.primary/secondary.used_percent`, `resets_at`) and the last `turn_context.model`/`effort`. Or use app-server `thread/resume`.
6. **Compaction:** app-server `thread/compact/start`, or rely on auto-compaction.
7. **Instructions:** pass `-c developer_instructions=<json.dumps(text, ensure_ascii=False)>` on every run, or use `$CODEX_HOME/AGENTS.md`.
8. **Sandbox:** decide explicitly between `-s workspace-write` (needs working userns) and `--dangerously-bypass-approvals-and-sandbox` inside your own container. Scrub the service env via `shell_environment_policy`.
