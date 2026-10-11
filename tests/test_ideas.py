import json
import os
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("BRAIN_RESOURCE", "https://brain.example.com/mcp")
os.environ.setdefault("BRAIN_AUTH_BASE", "https://auth.example.com")

from brain import ideas, migrate, model  # noqa: E402
from brain.store import MemoryStore  # noqa: E402
from brain.tools import Brain, ToolError  # noqa: E402

TODAY = date(2026, 10, 2)


def brain(store=None):
    projects = {"brain": {"repo": "example/brain"}, "old-name": {"alias_of": "brain"}}
    return Brain(store or MemoryStore(), agent="claude-code", clock=lambda: "2026-10-02T00:00:00Z",
                 today=lambda: TODAY, projects=projects)


class IdeaModelTest(unittest.TestCase):
    def test_create_starts_open_and_id_has_the_idea_prefix(self):
        entry = ideas.create(title="  家計簿の  月次まとめ ", body="メモ\r\n二行目", at="t", by="claude-code")
        self.assertTrue(entry["id"].startswith("idea-"))
        self.assertEqual((entry["kind"], entry["status"], entry["stage"]), ("idea", "active", "open"))
        self.assertEqual(entry["title"], "家計簿の 月次まとめ")
        self.assertEqual(entry["body"], "メモ\n二行目")
        self.assertNotIn("project", entry)

    def test_dropping_an_idea_deprecates_it_and_keeps_the_old_entry_unchanged(self):
        entry = ideas.create(title="案", project="my-app", at="t", by="claude-code")
        dropped = ideas.update(entry, {"stage": "dropped", "project": ""}, at="t2", by="claude-code")
        self.assertEqual((dropped["stage"], dropped["status"], dropped["rev"]), ("dropped", "deprecated", 2))
        self.assertNotIn("project", dropped)
        self.assertEqual((entry["stage"], entry["project"]), ("open", "my-app"))

    def test_invalid_input_is_rejected(self):
        with self.assertRaises(model.EntryError):
            ideas.create(title=" ", at="t", by="k")
        with self.assertRaises(model.EntryError):
            ideas.create(title="x", project="Bad Project", at="t", by="k")
        entry = ideas.create(title="x", at="t", by="k")
        with self.assertRaises(model.EntryError):
            ideas.update(entry, {"stage": "asked"}, at="t", by="k")
        with self.assertRaises(model.EntryError):
            ideas.update(entry, {"kind": "decision"}, at="t", by="k")

    def test_stage_is_only_for_ideas_and_must_match_status(self):
        with self.assertRaises(model.EntryError):
            model.validate({"id": "x-1", "kind": "knowledge", "status": "active", "title": "t", "stage": "open"})
        with self.assertRaises(model.EntryError):
            model.validate({"id": "idea-1", "kind": "idea", "status": "active", "title": "t", "stage": "dropped"})


class IdeaToolTest(unittest.TestCase):
    def test_record_idea_then_search_only_when_asked_for(self):
        b = brain()
        out = b.call("brain_record_idea", {"title": "読書メモから記事の種を拾う", "project": "brain"})
        hits = b.call("brain_search", {"query": "読書メモ 記事"})["results"]
        self.assertEqual(hits, [])
        hits = b.call("brain_search", {"query": "読書メモ 記事", "kinds": ["idea"]})["results"]
        self.assertEqual([h["id"] for h in hits], [out["id"]])
        self.assertEqual(hits[0]["stage"], "open")

    def test_context_lists_open_ideas_and_old_project_name_is_an_alias(self):
        b = brain()
        first = b.call("brain_record_idea", {"title": "アイデア帳を作る", "project": "old-name"})
        self.assertEqual(b.store.get(first["id"])["project"], "brain")
        ctx = b.call("brain_context", {"project": "old-name"})
        self.assertEqual(ctx["project"], "brain")
        self.assertEqual([i["id"] for i in ctx["open_ideas"]], [first["id"]])

        rev = b.call("brain_get", {"id": first["id"]})["entry"]["rev"]
        done = b.call("brain_update_entry", {"id": first["id"], "expected_rev": rev, "reason": "作った",
                                             "changes": {"stage": "dropped"}})
        self.assertEqual((done["stage"], done["status"]), ("dropped", "deprecated"))
        self.assertEqual(b.call("brain_context", {"project": "brain"})["open_ideas"], [])

    def test_secret_like_text_is_not_saved(self):
        with self.assertRaises(ToolError):
            brain().call("brain_record_idea", {"title": "連絡先 someone@" + "gmail.com に送る"})


class RenameProjectTest(unittest.TestCase):
    def test_rename_moves_entries_and_state_without_changing_ids(self):
        b = Brain(MemoryStore(), agent="codex", clock=lambda: "2026-10-02T00:00:00Z", today=lambda: TODAY)
        logged = b.call("brain_log_action", {"project": "old-name", "summary": "s", "changed": "c",
                                             "verified": "v", "outcome": "done", "next": "次"})
        b.call("brain_record_memory", {"claim": "c", "applies_when": "a", "evidence": "e"})  # 案件なしは触らない

        dry = migrate.rename_project(b.store, "old-name", "brain", at="t")
        self.assertEqual((dry["entries"], dry["state"], dry["applied"]), (1, True, False))
        self.assertEqual(b.store.project_entries("brain"), [])

        migrate.rename_project(b.store, "old-name", "brain", at="t", apply=True)
        self.assertEqual(b.store.project_entries("old-name"), [])
        moved = b.store.project_entries("brain")
        self.assertEqual([e["id"] for e in moved], [logged["id"]])
        self.assertEqual(moved[0]["rev"], 2)
        self.assertEqual(len(b.store.history(logged["id"])), 2)
        self.assertEqual(b.store.get_state("brain")["next"], "次")
        self.assertIsNone(b.store.get_state("old-name"))

    def test_rename_refuses_to_overwrite_an_existing_state(self):
        store = MemoryStore()
        store.put_state("old-name", {"next": "a"})
        store.put_state("new-name", {"next": "b"})
        with self.assertRaises(model.EntryError):
            migrate.rename_project(store, "old-name", "new-name", at="t", apply=True)



if __name__ == "__main__":
    unittest.main()
