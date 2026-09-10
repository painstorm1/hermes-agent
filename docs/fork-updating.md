# Updating a customized fork from a stable release

A GitHub source update is not a packaged Runtime update. Keep source integration,
Python dependency verification, Desktop packaging, and operational cutover as
separate gates. A successful fetch, clean textual merge, or version banner is
not proof that custom behavior or the installed application was preserved.

## Prepare an isolated candidate

1. Identify the canonical development repository and its Git common directory.
   Use one task worktree there; do not develop in a running installation.
2. Pin the official stable tag, annotated tag object, peeled commit, fork base,
   and merge base. Do not substitute a moving upstream branch for that release.
3. Export existing tracked changes and required untracked files to private
   storage with hashes. Keep Runtime dirtiness separate from task-worktree
   dirtiness. Do not stash/reset someone else's changes or hide them with
   `skip-worktree`.
4. On a case-insensitive filesystem, inspect the Git tree for case-folded path
   collisions. Preserve both source blobs and the checkout bytes before any
   structural resolution. A fresh-checkout discrepancy can be caused by Git
   paths that Windows cannot represent simultaneously; it is not permission to
   discard an unrelated Runtime patch.
5. Inventory fork-only commits and behavior. Resolve overlapping code according
   to the stable release's actual module boundaries, including every caller.
   Never use a blanket ours/theirs choice to make conflicts disappear.

## Verify the exact final bytes

Use a worktree-local virtual environment and the pinned dependency manifest.
Never install native dependencies into a running Runtime as a test. Use an
isolated `HERMES_HOME`, remove inherited secrets, disable lazy installs, and
block external network access for tests. Windows contributors can invoke pytest
through that environment with `-o addopts=` when the POSIX test runner cannot
activate the Windows virtual environment.

Require focused coverage of custom route budgets, concurrency and MCP refresh,
completion reserve, cron enqueue/ownership/delivery, profile routing, platform
callbacks, and Windows updater recovery. Run relevant stable regressions too.
Verify syntax, installed dependency compatibility, CLI version, and gateway
construction without connecting adapters. An installed Desktop also needs its
own build/package gate; a renderer build does not validate a distributable EXE.

Keep machine-specific logs outside tracked source. Record skipped, failed,
timed-out and unexecuted gates explicitly. For an uncommitted candidate, identify
both its Git tree and binary diff digest: the CLI's reported commit still refers
to the unchanged worktree HEAD, not to the candidate tree.

## Cut over only from an external operator shell

Operational deployment is a separate authorized action after independent QA.
An updater running beneath a live gateway must not stop or restart that parent.
Do not bypass self-target lifecycle guards. Close the affected Desktop/CLI
processes and stop the gateway from a genuinely external shell before replacing
Python/native packages. Preserve application packages and profile/state backups
separately from Git source. Rebuild/package as required and verify the exact
source, interpreter, dependency, package, launcher and gateway identities after
restart. Never describe a pushed source branch as an updated Runtime.

## Rollback

Retain the previous source commit, dependency state, package and private data
backup until cutover verification closes. Git reset alone cannot restore an
untracked Desktop package or reverse a database migration. Restore tracked
source before restoring untracked build artifacts; later cleanup must not erase
the restored package. Preserve original dirty changes for explicit review rather
than blindly reapplying them to different source. Validate the rollback package
and Runtime identities before restarting services.
