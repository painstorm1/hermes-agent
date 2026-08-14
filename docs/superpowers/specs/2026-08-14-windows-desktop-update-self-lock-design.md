# Windows Desktop Self-Update Lock Design

Date: 2026-08-14

## Goal

Make the Windows Desktop **Update** action complete without deleting the packaged Desktop app or stranding the `fn_cool` gateway when Python dependencies are refreshed.

The change must preserve all user-owned Hermes state: databases, conversations, memories, profiles, configuration, credentials, skills, cron jobs, and messaging state.

## Confirmed Failure Chain

The incident is a deterministic interaction between three existing behaviors:

1. `scripts/desktop-update/windows.ps1` proves that `venv\Scripts\hermes.exe` is unlocked and then launches that same executable to run `hermes update`.
2. The update refreshes the editable Python package. On Windows, `uv pip install` must replace `venv\Scripts\hermes.exe`, but the running updater process still holds that launcher open. The install fails with OS error 32.
3. A broad `CalledProcessError` handler labels the dependency failure as a Git failure and falls back to the source ZIP updater. The ZIP updater atomically replaces the top-level `apps` directory. GitHub source ZIPs do not contain ignored `apps/desktop/release` or `apps/desktop/dist`, so the packaged `Hermes.exe` is deleted. The ZIP path does not rebuild Desktop, and the later installed-app gate can no longer detect that Desktop was previously installed.

The current recurrence produced this exact chain in `desktop-update-handoff.log`: self-lock, dependency failure, ZIP fallback, top-level replacement, and a missing Desktop executable.

## Scope

### In scope

- Eliminate the Desktop handoff's self-lock of `hermes.exe`.
- Restrict ZIP fallback to actual Git command failures.
- Preserve the previously packaged Desktop artifacts across a ZIP source replacement.
- Rebuild Desktop after both Git and ZIP updates when Desktop was installed before the update.
- Make the handoff's success verdict depend on an executable that actually exists, not an output-string match alone.
- Add behavior-based regression tests.
- Repair and rebuild the current installation after the patch is verified.
- Keep a recoverable, committed fix branch and an exported patch outside the live checkout.

### Out of scope

- Moving Desktop installation artifacts to a new global install directory.
- Changing user configuration, profiles, `state.db`, memory, skills, or scheduled job definitions.
- Sending Slack, Telegram, or other messaging traffic.
- Publishing or pushing an upstream branch without separate authorization.
- Reworking unrelated gateway watchdog/runtime topology.

## Design

### 1. Use the venv Python module entry point

The PowerShell handoff will invoke:

```text
venv\Scripts\python.exe -m hermes_cli.main update ...
```

instead of:

```text
venv\Scripts\hermes.exe update ...
```

The console entry point already dispatches to `hermes_cli.main:main`, so behavior and exit codes remain the same. Only the replaceable launcher is removed from the running process chain.

`Invoke-HermesStep` will set `WorkingDirectory` to `InstallRoot`, ensuring the module is loaded from the checkout being updated. The Desktop rebuild retry will use the same Python-module entry point.

The existing preflight checks stay in place. They still protect terminal-driven updates and detect other venv holders.

### 2. Classify fallback failures by the command that failed

ZIP fallback is intended for Windows Git/NTFS failures. A Python dependency install, Node install, build, or validation failure must not trigger a destructive source replacement.

A small pure classifier will inspect `CalledProcessError.cmd` and return true only when the failed executable is Git. The outer update handler will:

- use ZIP fallback for a Git command failure on Windows;
- report and return failure for non-Git subprocess failures;
- keep the existing non-Windows failure behavior.

This prevents a dependency error from being mislabeled as `Git update failed` and blocks the incident's destructive second stage.

### 3. Preserve only explicit Desktop artifacts inside ZIP staging

Before ZIP replacement, the updater records whether Desktop was installed. While constructing the staged replacement for the top-level `apps` entry, it copies only these existing local artifacts into the staged `apps` tree:

- `apps/desktop/release`
- `apps/desktop/dist`

No tracked source is merged from the old tree, and arbitrary ignored files are not preserved. This keeps upstream deletions effective and avoids carrying stale executable content outside the explicit allowlist.

Preservation occurs before the atomic commit, so a staging or rename failure retains the current all-old-or-all-new rollback contract. The free-space estimate includes preserved artifact bytes.

### 4. Share Desktop rebuild policy across Git and ZIP paths

The updater snapshots `desktop_was_installed` before either update path can alter artifacts. A shared helper will rebuild Desktop when:

- Desktop was installed before the update;
- Desktop source still exists; and
- a Node/npm runtime is available.

The helper will use the existing content-hash check so an unchanged build remains fast. Missing output is a rebuild reason even if post-update artifact discovery would otherwise return false.

Both Git and ZIP update paths call the same helper. People who never installed Desktop are not forced into an Electron build.

### 5. Make handoff completion truthful

After a successful `hermes update`, the Windows handoff always runs
`desktop --build-only` through the Python-module entry point. The existing
content-hash stamp makes this a fast no-op when the Python updater already
produced the current package. If that validation build fails or
`RelaunchExe` is still absent, the handoff performs one forced build and
checks again.

This replaces reliance on the exact text `Desktop build failed`, which is
brittle and currently does not match all emitted variants. A nonzero final
build or a still-missing executable is a failed handoff, not
`Update complete`.

If the update itself fails, the explicitly preserved prior package remains available for relaunch.

## Data Safety

The implementation does not write to user databases or configuration. Development and tests run in a linked worktree. Regression tests use temporary `HERMES_HOME` and temporary install trees.

Live recovery changes only:

- updater source files and tests;
- the editable Python installation in the existing venv;
- generated Desktop build artifacts;
- normal Desktop/gateway process state during stop and restart.

Before live repair, the current SHA, clean status, absence of Hermes processes,
configuration hashes, database integrity, and relevant user-state metadata are
recorded. Dependency repair and Desktop build must not alter user state; that
is verified again before either runtime is launched. After Desktop and the
gateway resume, normal operational metadata such as logs, connection status,
or session heartbeat timestamps may advance. Those expected runtime writes are
reported separately and are not treated as configuration or user-content
changes.

## Test Strategy

Tests follow red-green-refactor and exercise behavior rather than reading source text.

### Windows handoff integration

A Windows-only test executes the real PowerShell handoff against a temporary install root with a fake `hermes_cli.main`. The fake module records argv and attempts an exclusive open of `venv\Scripts\hermes.exe` while the updater child is running.

The current implementation fails because the launcher is executing. The fixed implementation succeeds, proving that the handoff uses Python-module execution and leaves the launcher replaceable.

### Failure classification

Pure tests construct `CalledProcessError` instances for Git, uv/pip, npm, and Python commands. Only Git failures qualify for ZIP fallback.

### ZIP artifact preservation

Behavioral tests use temporary live and extracted trees and exercise the real stage/commit orchestration. They verify:

- new tracked source replaces old tracked source;
- source deleted upstream does not survive;
- existing `release/Hermes.exe` and `dist/index.html` survive;
- a fresh non-Desktop install gains no artifacts;
- injected rename failure restores the old tree and artifacts;
- preserved bytes participate in the disk-space check;
- no staging or backup litter remains.

### Rebuild policy and handoff truth

Tests verify that a pre-update installed Desktop is rebuilt even when artifacts are missing after update, while a never-installed Desktop is skipped. The handoff must reject success when its relaunch executable is absent after the recovery build.

### Verification commands

Use the repository-required `scripts/run_tests.sh` wrapper for Python tests and the existing Desktop test/build commands for Electron code. Relevant targeted tests run first, followed by broader update and Desktop checks.

## Local Delivery and Persistence

Implementation is committed on `fix/windows-desktop-self-update-lock-20260814` in an isolated worktree. After tests pass:

1. export the implementation commit as a patch under `D:\hermes\backups`;
2. apply the tested source changes to the live managed checkout without adding a divergent commit to live `main`;
3. rely on the existing `updates.non_interactive_local_changes: stash` behavior for ordinary Git updates;
4. retain the external patch and worktree branch as recovery sources if a future ZIP replacement or upstream conflict removes the local fix.

When upstream ships an equivalent verified fix, the local patch can be removed and the live checkout returned to clean `origin/main`.

## Current Installation Recovery

After the patch passes its tests:

1. confirm the live checkout is clean and still equals `origin/main`;
2. keep the Desktop and Hermes gateway processes stopped;
3. repair editable dependencies with the venv Python module path, never the `hermes.exe` shim;
4. run `python.exe -m hermes_cli.main desktop --force-build --build-only`;
5. verify `Hermes.exe`, `app.asar`, install stamp, PE integrity, and hashes;
6. before launch, verify configuration hashes, database integrity, and
   user-state metadata are unchanged by repair/build;
7. launch Desktop and verify a real window and successful backend startup;
8. start the existing `Hermes_Gateway_fn_cool` task and verify its process,
   API port, and configured channel connections without sending messages;
9. after launch, verify live source status and distinguish expected runtime
   metadata writes from configuration or user-content changes.

If dependency repair or Desktop build fails, stop and preserve the full output. Do not repeat the update loop or replace user data.

## Success Criteria

- Desktop manual update no longer executes the replaceable `hermes.exe` shim.
- Non-Git failures cannot trigger ZIP fallback.
- ZIP source replacement cannot remove an existing packaged Desktop app.
- A previously installed Desktop is rebuilt on both update paths.
- The handoff cannot report success without a relaunchable executable.
- Targeted tests and broader relevant tests pass.
- Rebuilt Desktop launches, and `fn_cool` gateway returns healthy.
- Repair/build leaves user databases and state untouched; configuration,
  profiles, memories, jobs, and user content remain intact after runtime
  restart, apart from explicitly reported normal operational metadata.
