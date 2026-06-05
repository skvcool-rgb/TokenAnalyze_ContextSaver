# Vendored: KOS-Memory ("KASM")

This `kasm/` directory is a vendored copy of **KOS-MemoryV4** — the per-project-isolated memory layer for
Claude Code (local BM25 + grep over a per-project SQLite store; pure stdlib; no embeddings, no network).

- **Source:** https://github.com/skvcool-rgb/KOS-MemoryV4
- **Commit:** `f4f36f994303ef2f85e0d4dfc70d4119a446917c`
- **License:** MIT (same as this bundle)
- **Excluded from the copy:** `.git/`, `__pycache__/`, `*.pyc`

## Why it's bundled here

The watch panel and statusline in this suite read KASM's per-project state (chunk count, mode, daily recall
budget) so you can *see* the memory layer working. Bundling KASM makes the suite self-contained — but it
remains an independent project.

## Per-project isolation (the headline property)

KOS-MemoryV4 stores each project's memory in `<project>/.kos-memory/chunks.db`; cross-project pins live
separately under `~/.config/kos-memory/user/` (`%APPDATA%\kos-memory\user\` on Windows). A recall resolves
to exactly one project's DB — never a merge across projects. This was verified end-to-end (write-A / recall-B
adversarial test: a project cannot surface another project's chunks, and a user-level pin does not bleed into
a project recall).

## Install / test KASM on its own

```bash
cd kasm
python scripts/install.py                 # registers the plugin + hooks (its own settings.json block)
python -m unittest discover -s tests       # ~399 tests
```

## Re-syncing

To update this vendored copy, re-clone the source at a newer commit and replace this directory (keep this
file, bump the commit hash). Nothing in the bundle patches KASM's sources — it only *reads* its data files.
