"""Hook tests — invoke each hook script as a subprocess with simulated env."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))


def _run_hook(hook_name: str, *, env_overrides: dict | None = None,
              stdin_data: str = "", project_dir: str = "") -> subprocess.CompletedProcess:
    """Run hooks/<hook_name>.py in a subprocess with given env + stdin."""
    env = os.environ.copy()
    env.pop("CLAUDE_TRANSCRIPT_PATH", None)
    env.pop("CLAUDE_PROJECT_DIR", None)
    env.pop("CLAUDE_SESSION_ID", None)
    if project_dir:
        env["CLAUDE_PROJECT_DIR"] = project_dir
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        [sys.executable, str(PLUGIN_ROOT / "hooks" / f"{hook_name}.py")],
        input=stdin_data,
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )


def _seed_chunk(project_dir: str) -> None:
    """Helper: create a minimal chunks.db so SessionStart has something to print."""
    from lib.paths import FILE_CHUNKS_DB, ensure_kos_dir
    from lib.store import Store
    kos = ensure_kos_dir(project_dir, user_level=False)
    s = Store(kos / FILE_CHUNKS_DB)
    s.add_chunk(text="seed chunk for hook test",
                session_id="hooktest", ts=int(time.time()))
    s.upsert_session("hooktest", started_at=int(time.time()),
                     chunk_count=1, summary="seed", tags=["seed"])
    s.close()


class SessionStartBackupModeTests(unittest.TestCase):
    """v4.0 legacy behavior — explicit KOS_MEMORY_MODE=backup."""

    def _backup_env(self):
        return {"KOS_MEMORY_MODE": "backup"}

    def test_silent_when_no_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = _run_hook("SessionStart", project_dir=tmp,
                          env_overrides=self._backup_env())
            self.assertEqual(r.returncode, 0)
            self.assertEqual(r.stdout.strip(), "")

    def test_prints_marker_when_db_has_chunks(self):
        with tempfile.TemporaryDirectory() as tmp:
            _seed_chunk(tmp)
            r = _run_hook("SessionStart", project_dir=tmp,
                          env_overrides=self._backup_env())
            self.assertEqual(r.returncode, 0)
            self.assertIn("[kos-memory BACKUP]", r.stdout)
            self.assertIn("chunks", r.stdout)


class SessionStartPrimaryModeTests(unittest.TestCase):
    """v4.1 default behavior — KOS_MEMORY_MODE=primary."""

    def _primary_env(self):
        return {"KOS_MEMORY_MODE": "primary"}

    def test_silent_when_no_db_and_no_memory_md(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Isolate user home so the user-global CLAUDE.md doesn't leak in
            r = _run_hook("SessionStart", project_dir=tmp,
                          env_overrides={**self._primary_env(),
                                         "HOME": tmp, "USERPROFILE": tmp})
            self.assertEqual(r.returncode, 0)
            self.assertEqual(r.stdout.strip(), "")

    def test_emits_primary_block_with_chunks(self):
        with tempfile.TemporaryDirectory() as tmp:
            _seed_chunk(tmp)
            r = _run_hook("SessionStart", project_dir=tmp,
                          env_overrides={**self._primary_env(),
                                         "HOME": tmp, "USERPROFILE": tmp})
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            self.assertIn("[kos-memory PRIMARY]", r.stdout)
            # Catalog section must appear
            self.assertIn("session catalog", r.stdout.lower())
            # Drift section must appear (even if empty)
            self.assertIn("Drift", r.stdout)

    def test_emits_memory_md_when_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            _seed_chunk(tmp)
            (Path(tmp) / "MEMORY.md").write_text(
                "# Project notes\n\n## Decisions\n\n- chose Postgres\n",
                encoding="utf-8",
            )
            r = _run_hook("SessionStart", project_dir=tmp,
                          env_overrides={**self._primary_env(),
                                         "HOME": tmp, "USERPROFILE": tmp})
            self.assertEqual(r.returncode, 0)
            self.assertIn("[kos-memory PRIMARY]", r.stdout)
            self.assertIn("MEMORY.md", r.stdout)
            # Heading skeleton appears (heading_only=True at SessionStart)
            self.assertIn("Project notes", r.stdout)


class UserPromptSubmitBackupModeTests(unittest.TestCase):
    def _backup_env(self):
        return {"KOS_MEMORY_MODE": "backup"}

    def test_silent_when_no_trigger(self):
        payload = json.dumps({"prompt": "write a sort function"})
        r = _run_hook("UserPromptSubmit", stdin_data=payload,
                      env_overrides=self._backup_env())
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), "")

    def test_emits_hint_on_trigger_phrase(self):
        triggers = [
            "what did we discuss yesterday",
            "where did we leave off",
            "as we discussed",
            "remember when we built that thing",
            "since last week we changed it",
        ]
        for trigger in triggers:
            payload = json.dumps({"prompt": trigger})
            r = _run_hook("UserPromptSubmit", stdin_data=payload,
                          env_overrides=self._backup_env())
            self.assertEqual(r.returncode, 0, msg=f"trigger={trigger!r}")
            self.assertIn(
                "[kos-memory hint]", r.stdout,
                msg=f"no hint for trigger={trigger!r}",
            )

    def test_skips_slash_command(self):
        # Slash commands have their own handler; hook should not duplicate
        payload = json.dumps({"prompt": "/recall what did we discuss"})
        r = _run_hook("UserPromptSubmit", stdin_data=payload,
                      env_overrides=self._backup_env())
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), "")


class UserPromptSubmitPrimaryModeTests(unittest.TestCase):
    """In primary mode, triggers cause inline auto-recall."""

    def _primary_env(self):
        return {"KOS_MEMORY_MODE": "primary"}

    def test_silent_when_no_trigger(self):
        payload = json.dumps({"prompt": "write a sort function"})
        r = _run_hook("UserPromptSubmit", stdin_data=payload,
                      env_overrides=self._primary_env())
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), "")

    def test_silent_when_no_data_and_no_memory_md(self):
        # Primary mode shouldn't generate noise when there's nothing to recall
        with tempfile.TemporaryDirectory() as tmp:
            payload = json.dumps({"prompt": "where did we leave off",
                                  "cwd": tmp})
            r = _run_hook("UserPromptSubmit", stdin_data=payload,
                          project_dir=tmp,
                          env_overrides={**self._primary_env(),
                                         "HOME": tmp, "USERPROFILE": tmp})
            self.assertEqual(r.returncode, 0)
            self.assertEqual(r.stdout.strip(), "")

    def test_auto_recalls_inline_with_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            from lib.paths import FILE_CHUNKS_DB, ensure_kos_dir
            from lib.store import Store
            kos = ensure_kos_dir(tmp, user_level=False)
            s = Store(kos / FILE_CHUNKS_DB)
            s.add_chunk(text="we chose Postgres for the user table",
                        session_id="recent", ts=int(time.time()))
            s.upsert_session("recent", started_at=int(time.time()),
                             chunk_count=1, summary="db decision",
                             tags=["db"])
            s.close()

            payload = json.dumps({"prompt": "what did we decide about postgres",
                                  "cwd": tmp})
            r = _run_hook("UserPromptSubmit", stdin_data=payload,
                          project_dir=tmp,
                          env_overrides={**self._primary_env(),
                                         "HOME": tmp, "USERPROFILE": tmp})
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            self.assertIn("[kos-memory PRIMARY] Auto-recall", r.stdout)
            # The actual passage text leaked through
            self.assertIn("Postgres", r.stdout)

    def test_skips_slash_command(self):
        payload = json.dumps({"prompt": "/recall postgres"})
        r = _run_hook("UserPromptSubmit", stdin_data=payload,
                      env_overrides=self._primary_env())
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), "")

    def test_handles_empty_payload(self):
        r = _run_hook("UserPromptSubmit", stdin_data="")
        self.assertEqual(r.returncode, 0)

    def test_handles_malformed_json(self):
        r = _run_hook("UserPromptSubmit", stdin_data="{not valid")
        self.assertEqual(r.returncode, 0)

    def test_handles_missing_prompt_key(self):
        payload = json.dumps({"other_key": "data"})
        r = _run_hook("UserPromptSubmit", stdin_data=payload)
        self.assertEqual(r.returncode, 0)


class UserPromptSubmitBuildStatusTests(unittest.TestCase):
    """v5.0: build-status questions auto-trigger reality_sync verdict."""

    def _primary_env(self):
        return {"KOS_MEMORY_MODE": "primary"}

    def test_is_X_built_fires_build_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = json.dumps({"prompt": "is the auth module built",
                                  "cwd": tmp})
            r = _run_hook("UserPromptSubmit", stdin_data=payload,
                          project_dir=tmp,
                          env_overrides={**self._primary_env(),
                                         "HOME": tmp, "USERPROFILE": tmp})
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            self.assertIn("Build-status check fired", r.stdout)
            self.assertIn("[reality check]", r.stdout)

    def test_did_we_ship_fires_build_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = json.dumps({"prompt": "did we ship the new dashboard",
                                  "cwd": tmp})
            r = _run_hook("UserPromptSubmit", stdin_data=payload,
                          project_dir=tmp,
                          env_overrides={**self._primary_env(),
                                         "HOME": tmp, "USERPROFILE": tmp})
            self.assertEqual(r.returncode, 0)
            self.assertIn("Build-status check fired", r.stdout)

    def test_status_of_X_fires_build_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = json.dumps({"prompt": "what's the status of payment integration",
                                  "cwd": tmp})
            r = _run_hook("UserPromptSubmit", stdin_data=payload,
                          project_dir=tmp,
                          env_overrides={**self._primary_env(),
                                         "HOME": tmp, "USERPROFILE": tmp})
            self.assertEqual(r.returncode, 0)
            self.assertIn("Build-status check fired", r.stdout)

    def test_past_tense_recall_takes_priority(self):
        # "where did we leave off" must NOT trigger build-status
        # (it's a past-tense recall trigger)
        with tempfile.TemporaryDirectory() as tmp:
            payload = json.dumps({"prompt": "where did we leave off",
                                  "cwd": tmp})
            r = _run_hook("UserPromptSubmit", stdin_data=payload,
                          project_dir=tmp,
                          env_overrides={**self._primary_env(),
                                         "HOME": tmp, "USERPROFILE": tmp})
            self.assertEqual(r.returncode, 0)
            # No store + no MEMORY.md = silent (recall path)
            self.assertEqual(r.stdout.strip(), "")

    def test_build_status_silent_in_backup_mode(self):
        # In backup mode, build-status triggers don't fire — consistency
        # with v4.0 behavior
        with tempfile.TemporaryDirectory() as tmp:
            payload = json.dumps({"prompt": "is the auth module built",
                                  "cwd": tmp})
            r = _run_hook("UserPromptSubmit", stdin_data=payload,
                          project_dir=tmp,
                          env_overrides={"KOS_MEMORY_MODE": "backup",
                                         "HOME": tmp, "USERPROFILE": tmp})
            self.assertEqual(r.returncode, 0)
            self.assertNotIn("Build-status check", r.stdout)


class SessionStartV5ReconciliationTests(unittest.TestCase):
    """v5.0: SessionStart in primary mode includes Live state +
    Reconciliation sections."""

    def test_emits_live_state_when_git_repo(self):
        import subprocess as sp
        with tempfile.TemporaryDirectory() as tmp:
            # Init a git repo
            sp.run(["git", "init", "-b", "main"], cwd=tmp,
                   capture_output=True, check=True)
            sp.run(["git", "config", "user.email", "t@t"], cwd=tmp,
                   capture_output=True)
            sp.run(["git", "config", "user.name", "t"], cwd=tmp,
                   capture_output=True)
            (Path(tmp) / "f.py").write_text("# f", encoding="utf-8")
            sp.run(["git", "add", "."], cwd=tmp, capture_output=True)
            sp.run(["git", "commit", "-m", "init"], cwd=tmp,
                   capture_output=True, check=True)

            _seed_chunk(tmp)
            r = _run_hook("SessionStart", project_dir=tmp,
                          env_overrides={"KOS_MEMORY_MODE": "primary",
                                         "HOME": tmp, "USERPROFILE": tmp})
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            self.assertIn("[kos-memory PRIMARY]", r.stdout)
            self.assertIn("Live project state", r.stdout)
            self.assertIn("branch:", r.stdout)
            self.assertIn("Build-status reconciliation", r.stdout)

    def test_authority_order_block_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            _seed_chunk(tmp)
            r = _run_hook("SessionStart", project_dir=tmp,
                          env_overrides={"KOS_MEMORY_MODE": "primary",
                                         "HOME": tmp, "USERPROFILE": tmp})
            self.assertEqual(r.returncode, 0)
            self.assertIn("Authority order", r.stdout)
            self.assertIn("filesystem + git", r.stdout)
            self.assertIn("BEFORE claiming", r.stdout)


class StopHookTests(unittest.TestCase):
    def _make_transcript(self, dir_path: Path, lines: list[dict]) -> Path:
        p = dir_path / "transcript.jsonl"
        p.write_text(
            "\n".join(json.dumps(l) for l in lines),
            encoding="utf-8",
        )
        return p

    def test_no_transcript_exits_silently(self):
        # No transcript anywhere
        with tempfile.TemporaryDirectory() as tmp:
            # Set HOME so the fallback search finds nothing
            r = _run_hook(
                "Stop",
                project_dir=tmp,
                env_overrides={
                    "HOME": tmp,
                    "USERPROFILE": tmp,
                    "APPDATA": tmp,
                    "CLAUDE_TRANSCRIPT_PATH": "",
                },
            )
            self.assertEqual(r.returncode, 0)

    def test_ingests_from_transcript(self):
        with tempfile.TemporaryDirectory() as tmp:
            tdir = Path(tmp)
            transcript = self._make_transcript(tdir, [
                {"type": "user", "message": {
                    "role": "user", "content": "fix the auth bug"}},
                {"type": "assistant", "message": {
                    "role": "assistant", "content": "I'll look at the auth module"}},
                {"type": "user", "message": {
                    "role": "user", "content": "thanks, that worked"}},
            ])
            r = _run_hook("Stop", project_dir=tmp, env_overrides={
                "CLAUDE_TRANSCRIPT_PATH": str(transcript),
                "CLAUDE_SESSION_ID": "test-session",
                "HOME": tmp,
                "USERPROFILE": tmp,
                "APPDATA": tmp,
            })
            self.assertEqual(r.returncode, 0, msg=f"stderr={r.stderr}")
            # Verify the WAL log was appended
            log = tdir / ".kos-memory" / "ingest_log.jsonl"
            self.assertTrue(log.exists(), msg="WAL log not appended")
            log_lines = log.read_text(encoding="utf-8").strip().splitlines()
            self.assertGreater(len(log_lines), 0)
            entry = json.loads(log_lines[0])
            self.assertEqual(entry["session_id"], "test-session")
            self.assertEqual(entry["kind"], "session_end")

            # Verify chunks landed
            from lib.paths import FILE_CHUNKS_DB
            from lib.store import Store
            store = Store(tdir / ".kos-memory" / FILE_CHUNKS_DB)
            try:
                self.assertGreater(store.count(), 0)
            finally:
                store.close()


class PreCompactHookTests(unittest.TestCase):
    def test_silent_on_manual_trigger(self):
        # PreCompact must not act on manual /compact — only auto-compact
        payload = json.dumps({
            "trigger": "manual",
            "session_id": "s1",
            "transcript_path": "",
            "cwd": "",
        })
        r = _run_hook("PreCompact", stdin_data=payload)
        self.assertEqual(r.returncode, 0)

    def test_handles_missing_transcript_gracefully(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = json.dumps({
                "trigger": "auto",
                "session_id": "s1",
                "transcript_path": str(Path(tmp) / "missing.jsonl"),
                "cwd": tmp,
            })
            r = _run_hook("PreCompact", stdin_data=payload, project_dir=tmp)
            self.assertEqual(r.returncode, 0)

    def test_ingests_on_auto_trigger_with_transcript(self):
        with tempfile.TemporaryDirectory() as tmp:
            tdir = Path(tmp)
            t = tdir / "transcript.jsonl"
            t.write_text("\n".join([
                json.dumps({"type": "user", "message": {
                    "role": "user", "content": "what about the catalog"}}),
                json.dumps({"type": "assistant", "message": {
                    "role": "assistant", "content": "the catalog has 3 sections"}}),
            ]), encoding="utf-8")

            payload = json.dumps({
                "trigger": "auto",
                "session_id": "compact-session",
                "transcript_path": str(t),
                "cwd": tmp,
            })
            r = _run_hook("PreCompact", stdin_data=payload, project_dir=tmp)
            self.assertEqual(r.returncode, 0, msg=f"stderr={r.stderr}")

            # WAL entry tagged kind=pre_compact
            log = tdir / ".kos-memory" / "ingest_log.jsonl"
            self.assertTrue(log.exists())
            entry = json.loads(log.read_text(encoding="utf-8").strip().splitlines()[0])
            self.assertEqual(entry["kind"], "pre_compact")
            self.assertEqual(entry["session_id"], "compact-session")

    def test_never_blocks_compaction(self):
        # Even when something goes wrong inside, PreCompact must exit 0
        payload = json.dumps({
            "trigger": "auto",
            "session_id": "s1",
            "transcript_path": "/nonexistent/path",
            "cwd": "/also/nonexistent",
        })
        r = _run_hook("PreCompact", stdin_data=payload)
        self.assertEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
