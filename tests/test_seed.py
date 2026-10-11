import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("BRAIN_RESOURCE", "https://brain.example.com/mcp")
os.environ.setdefault("BRAIN_AUTH_BASE", "https://auth.example.com")

from brain import migrate  # noqa: E402
from brain.store import MemoryStore  # noqa: E402
from brain.tools import Brain  # noqa: E402


class SeedTest(unittest.TestCase):
    def test_seed_imports_cleanly_and_shapes_the_first_context(self):
        rows = migrate.load_markdown(ROOT / "examples" / "seed", at="2026-10-11T00:00:00Z")
        self.assertEqual(migrate.check(rows), [])
        store = MemoryStore()
        report = migrate.import_entries(store, rows)
        self.assertEqual(len(report["created"]), len(rows))
        self.assertTrue(migrate.verify(store, rows)["ok"])

        brain = Brain(store, agent="claude-code")
        context = brain.call("brain_context", {"project": "my-app"})
        self.assertEqual([p["id"] for p in context["user_preferences"]], ["knowledge-owner-judgment-criteria"])
        hits = brain.call("brain_search", {"query": "書き戻す基準"})["results"]
        self.assertEqual(hits[0]["id"], "playbook-write-back-criteria")

    def test_example_projects_file_is_valid_for_the_handler(self):
        import json

        projects = json.loads((ROOT / "projects.example.json").read_text(encoding="utf-8"))
        self.assertEqual(projects["old-app-name"], {"alias_of": "my-app"})


if __name__ == "__main__":
    unittest.main()
