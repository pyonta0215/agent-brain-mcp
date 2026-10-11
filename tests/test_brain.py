import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("BRAIN_RESOURCE", "https://brain.example.com/mcp")
os.environ.setdefault("BRAIN_AUTH_BASE", "https://auth.example.com")

import handler as lambda_handler  # noqa: E402
from brain import guard, migrate, model  # noqa: E402
from brain.frontmatter import parse_text  # noqa: E402
from brain.store import ConflictError, DynamoStore, MemoryStore, from_item, to_item  # noqa: E402
from brain.tools import WRITE_TOOLS, Brain  # noqa: E402

TODAY = date(2026, 9, 25)
RESOURCE = "https://brain.example.com/mcp"
LEAK_MARKER = "LEAK" + "-MARKER-" + "x" * 20


class Clock:
    def __init__(self):
        self.n = 0

    def __call__(self):
        self.n += 1
        return f"2026-09-25T00:{self.n // 60:02d}:{self.n % 60:02d}Z"


def brain(store=None, agent="claude-code"):
    return Brain(store or MemoryStore(), agent=agent, clock=Clock(), today=lambda: TODAY,
                 projects={"my-app": {"repo": "example/my-app", "read_first": ["docs/adr/"]}})


def decision(**kw):
    args = {"project": "my-app", "topic": "github-write-scope", "title": "GitHub への書き込みは Issue 起票だけにする",
            "context": "Finding を次の行動へつなぐには書き込みが要る", "choice": "Issue の起票だけ許す",
            "why": "コード変更は既存のエージェントに任せる", "rejected": "PR 作成まで許す案は権限が広すぎる"}
    args.update(kw)
    return args


def action(**kw):
    args = {"project": "my-app", "summary": "Overview の対象をそろえた", "changed": "Repository 一覧を週次点検と同じにした",
            "verified": "npm run check", "outcome": "done"}
    args.update(kw)
    return args


class ModelAndMigrationTest(unittest.TestCase):
    def test_verify_reports_missing_and_changed_entries(self):
        rows = [("knowledge/ai/a.md", model.from_markdown(
            {"id": "a-note", "title": "A", "type": "knowledge", "domain": "ai", "status": "active"}, "body",
            at="t", by="import"))]
        store = MemoryStore()
        self.assertEqual(migrate.verify(store, rows)["missing"], ["a-note"])
        migrate.import_entries(store, rows)
        rows[0][1]["body"] = "changed"
        self.assertEqual(migrate.verify(store, rows)["differs"], ["a-note"])

    def test_generated_ids_do_not_collide_within_one_second(self):
        ids = {model.new_id("action", "my-app") for _ in range(5000)}
        self.assertEqual(len(ids), 5000)
        self.assertTrue(all(model.ID_RE.fullmatch(i) and not guard.find(i) for i in ids))

    def test_memory_cannot_be_active_and_superseded_needs_successor(self):
        entry = {"id": "x-memory", "kind": "memory", "status": "active", "title": "t"}
        with self.assertRaises(model.EntryError):
            model.validate(entry)
        with self.assertRaises(model.EntryError):
            model.validate({"id": "old-one", "kind": "decision", "status": "superseded", "title": "t"})


class GuardTest(unittest.TestCase):
    samples = {
        "AWS Access Key ID": "AK" + "IA" + "A" * 16,
        "GitHub token": "gh" + "p_" + "g" * 16,
        "Anthropic API key": "sk-" + "ant-" + "a" * 20,
        "OpenAI API key": "sk-" + "o" * 32,
        "Google API key": "AI" + "za" + "G" * 35,
        "Slack token": "xo" + "xb-" + "s" * 10,
        "Private key block": "-----BEGIN " + "PRIVATE KEY-----",
        "AWS account id": "1" * 12,
        "JWT": "ey" + "J" + "j" * 10 + "." + "ey" + "J" + "k" * 10 + ".",
        "Bearer token": "Be" + "arer " + "b" * 24,
        "Secret assignment": "api_" + "key=" + "c" * 16,
        "Cookie header": "Co" + "okie: session=" + "d" * 16,
        "Email address": "someone" + "@" + "mail.test.dev",
    }

    def test_every_scanner_sample_is_detected(self):
        for name, value in self.samples.items():
            self.assertTrue(any(name in hit for hit in guard.find({"x": value})), name)

    def test_benign_text_passes_and_values_are_not_echoed(self):
        self.assertEqual(guard.find({"a": "api_key=short-placeholder", "b": "noreply@example.com"}), [])
        hits = guard.find({"nested": [{"body": self.samples["GitHub token"]}]})
        self.assertEqual(hits, ["nested[0].body: GitHub token"])

    def test_every_write_tool_rejects_secrets_without_writing(self):
        for tool, args in (
            ("brain_record_decision", decision(why="使うキーは " + self.samples["Anthropic API key"])),
            ("brain_record_memory", {"claim": "id " + self.samples["AWS account id"], "applies_when": "a",
                                     "evidence": "b"}),
            ("brain_log_action", action(changed=self.samples["Bearer token"])),
        ):
            store = MemoryStore()
            b = brain(store)
            with self.assertRaisesRegex(Exception, "秘密情報"):
                b.call(tool, args)
            self.assertEqual(store.items, {}, tool)


class ToolsTest(unittest.TestCase):
    def test_decision_round_trip_similar_conflict_and_supersede(self):
        store = MemoryStore()
        b = brain(store)
        first = b.call("brain_record_decision", decision())
        second = b.call("brain_record_decision", decision())
        self.assertIn(first["id"], [s["id"] for s in second["similar"]])
        found = b.call("brain_search", {"query": "GitHub 書き込み Issue", "project": "my-app"})["results"]
        self.assertTrue(all("conflict" in r.get("flags", []) for r in found if r["kind"] == "decision"))
        third = b.call("brain_record_decision", decision(title="Issue とコメントだけ書き込む",
                                                         supersedes=[first["id"], second["id"]]))
        self.assertEqual(store.get(first["id"])["status"], "superseded")
        self.assertEqual(store.get(first["id"])["superseded_by"], third["id"])
        self.assertEqual(store.get(first["id"])["rev"], 2)
        found = b.call("brain_search", {"query": "GitHub 書き込み Issue"})["results"]
        self.assertEqual([r["id"] for r in found if r["kind"] == "decision"], [third["id"]])
        self.assertNotIn("flags", found[0])
        history = b.call("brain_get", {"id": first["id"], "include_history": True})["history"]
        self.assertEqual([h["rev"] for h in history], [1, 2])

    def test_context_hands_over_state_and_puts_failures_first(self):
        store = MemoryStore()
        writer = brain(store, agent="codex")
        for i in range(12):
            writer.call("brain_log_action", action(summary=f"作業 {i}"))
        writer.call("brain_log_action", action(summary="デプロイ失敗", outcome="failed",
                                               why_failed="arm64 のイメージが無かった"))
        writer.call("brain_record_decision", decision())
        writer.call("brain_log_action", action(summary="最後の作業", next="Issue #113 を着手", goal="週次点検を安定させる"))

        reader = brain(store, agent="claude-code")
        ctx = reader.call("brain_context", {"project": "my-app"})
        self.assertEqual(ctx["recent_actions"][0]["title"], "デプロイ失敗")
        self.assertEqual(len(ctx["recent_actions"]), 10)
        self.assertEqual(ctx["state"]["next"], "Issue #113 を着手")
        self.assertEqual(ctx["state"]["updated_by"], "codex")
        self.assertNotIn("flags", ctx["state"])
        self.assertEqual(len(ctx["decisions"]), 1)
        self.assertEqual(ctx["read_first_in_repo"], ["docs/adr/"])
        stale = Brain(store, agent="x", today=lambda: date(2026, 10, 20)).call("brain_context", {"project": "my-app"})
        self.assertEqual(stale["state"]["flags"], ["stale"])

    def test_failed_action_requires_reason_and_state_keeps_unspecified_fields(self):
        b = brain()
        with self.assertRaisesRegex(Exception, "why_failed"):
            b.call("brain_log_action", action(outcome="failed"))
        b.call("brain_log_action", action(goal="目的A", next="次B", blockers="待ちC"))
        b.call("brain_log_action", action(next="次D"))
        state = b.call("brain_context", {"project": "my-app"})["state"]
        self.assertEqual((state["goal"], state["next"], state["blockers"]), ("目的A", "次D", "待ちC"))

    def test_memory_is_candidate_until_promoted_with_expected_rev(self):
        store = MemoryStore()
        b = brain(store)
        mem = b.call("brain_record_memory", {"claim": "Query は 1MB で打ち切られる", "applies_when": "DynamoDB",
                                             "evidence": "実測"})
        self.assertEqual(mem["status"], "candidate")
        self.assertEqual(b.call("brain_search", {"query": "Query 1MB 打ち切り"})["results"], [])
        with self.assertRaisesRegex(Exception, "版が古い"):
            b.call("brain_update_entry", {"id": mem["id"], "expected_rev": 9, "reason": "r",
                                          "changes": {"status": "active"}})
        with self.assertRaisesRegex(Exception, "candidate"):
            b.call("brain_update_entry", {"id": mem["id"], "expected_rev": 1, "reason": "r",
                                          "changes": {"status": "active"}})
        done = b.call("brain_update_entry", {"id": mem["id"], "expected_rev": 1, "reason": "本人が採用",
                                             "changes": {"kind": "knowledge", "status": "active", "domain": "engineering",
                                                         "confidence": "high"}})
        self.assertEqual((done["rev"], done["kind"]), (2, "knowledge"))
        self.assertEqual(b.call("brain_search", {"query": "Query 1MB 打ち切り"})["results"][0]["id"], mem["id"])
        with self.assertRaisesRegex(Exception, "変更できない"):
            b.call("brain_update_entry", {"id": mem["id"], "expected_rev": 2, "reason": "r",
                                          "changes": {"created_by": "someone"}})

    def test_context_stays_small_with_many_records(self):
        store = MemoryStore()
        rows = [(f"knowledge/user/pref-{i}.md", model.from_markdown(
            {"id": f"pref-{i}", "title": f"判断基準 {i}", "type": "knowledge", "domain": "user", "status": "active"},
            "本文" * 200, at="t", by="import")) for i in range(30)]
        migrate.import_entries(store, rows)
        b = brain(store)
        for i in range(40):
            b.call("brain_log_action", action(summary=f"作業 {i} " + "説明" * 30))
        for i in range(20):
            b.call("brain_record_decision", decision(topic=f"topic-{i}", title=f"判断 {i} " + "理由" * 30))
        ctx = json.dumps(b.call("brain_context", {"project": "my-app"}), ensure_ascii=False)
        self.assertLess(len(ctx), 9000)
        self.assertTrue(json.loads(ctx)["user_preferences"])


class FakeDynamo:
    def __init__(self, pages=None, fail=None):
        self.pages = pages or []
        self.calls = []
        self.fail = fail

    def query(self, **kw):
        self.calls.append(kw)
        index = 0 if "ExclusiveStartKey" not in kw else int(kw["ExclusiveStartKey"]["n"]["N"])
        page = {"Items": self.pages[index]}
        if index + 1 < len(self.pages):
            page["LastEvaluatedKey"] = {"n": {"N": str(index + 1)}}
        return page

    def transact_write_items(self, **kw):
        self.calls.append(kw)
        if self.fail:
            raise self.fail


class TransactionCanceledException(Exception):
    pass


class DynamoStoreTest(unittest.TestCase):
    def test_all_pages_are_read(self):
        item = lambda i: to_item({"pk": f"ENTRY#e{i}", "sk": "META", "id": f"e{i}", "rev": 1})
        client = FakeDynamo(pages=[[item(1), item(2)], [item(3)], [item(4)]])
        ids = [e["id"] for e in DynamoStore(client, "t").all_entries()]
        self.assertEqual(ids, ["e1", "e2", "e3", "e4"])
        self.assertEqual(len(client.calls), 3)
        self.assertNotIn("pk", DynamoStore(client, "t").all_entries()[0])

    def test_serialization_round_trip_and_conflict(self):
        data = {"a": "x", "n": 3, "l": ["p", {"q": None}], "b": True, "e": ""}
        self.assertEqual(from_item(to_item(data)), data)
        with self.assertRaises(TypeError):
            to_item({"f": 1.5})
        entry = {"id": "e1", "kind": "action", "project": "my-app", "created_at": "t", "updated_at": "t",
                 "updated_by": "a", "rev": 1}
        client = FakeDynamo(fail=TransactionCanceledException("ConditionalCheckFailed"))
        with self.assertRaises(ConflictError):
            DynamoStore(client, "t").commit([(entry, None)], "r")
        ops = client.calls[0]["TransactItems"]
        self.assertEqual(ops[0]["Put"]["ConditionExpression"], "attribute_not_exists(pk)")
        self.assertEqual(ops[0]["Put"]["Item"]["gsi2pk"], {"S": "PROJECT#my-app"})
        self.assertEqual(ops[1]["Put"]["Item"]["sk"], {"S": "REV#000001"})


class HandlerTest(unittest.TestCase):
    def setUp(self):
        import os
        self.env = {"AGENT_CLIENTS": json.dumps({"rwclient": "claude-code", "roclient": "reader",
                                                 "connector": "claude-connector"}),
                    "INTERACTIVE_CLIENTS": "connector"}
        self._old = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)
        lambda_handler._CACHE.clear()
        self.store = MemoryStore()

    def tearDown(self):
        import os
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def call(self, body, *, client="rwclient", scopes=("read", "write"), token_use="access", method="POST",
             path="/mcp", scope_text=None, aud=None, anonymous=False):
        claims = {"token_use": token_use, "client_id": client,
                  "scope": scope_text if scope_text is not None else " ".join(f"{RESOURCE}/{s}" for s in scopes)}
        if aud is not None:
            claims["aud"] = aud
        context = {"http": {"method": method}}
        if not anonymous:
            context["authorizer"] = {"jwt": {"claims": claims}}
        event = {
            "rawPath": path,
            "headers": {"authorization": "Be" + "arer " + LEAK_MARKER},
            "requestContext": context,
            "body": json.dumps(body) if body is not None else None,
        }
        out = io.StringIO()
        with redirect_stdout(out):
            result = lambda_handler.handler(event, None, store=self.store)
        self.assertNotIn(LEAK_MARKER, out.getvalue())
        self.log = json.loads(out.getvalue().strip().splitlines()[-1])
        return result

    def rpc(self, method, params=None, msg_id=1, **kw):
        return self.call({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}}, **kw)

    def test_rejects_missing_or_wrong_identity(self):
        self.assertEqual(self.rpc("ping", client="unknown")["statusCode"], 401)
        self.assertEqual(self.rpc("ping", token_use="id")["statusCode"], 401)
        self.assertEqual(self.rpc("ping", scopes=())["statusCode"], 401)
        self.assertEqual(self.call(None, method="GET")["statusCode"], 405)
        self.assertEqual(self.call(None, path="/other")["statusCode"], 404)

    def test_initialize_list_and_notification(self):
        init = json.loads(self.rpc("initialize", {"protocolVersion": "2025-06-18"})["body"])
        self.assertEqual(init["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(self.call({"jsonrpc": "2.0", "method": "notifications/initialized"})["statusCode"], 202)
        names = {t["name"] for t in json.loads(self.rpc("tools/list")["body"])["result"]["tools"]}
        self.assertTrue(WRITE_TOOLS <= names)
        ro = {t["name"] for t in json.loads(self.rpc("tools/list", client="roclient", scopes=("read",))["body"])
              ["result"]["tools"]}
        self.assertFalse(WRITE_TOOLS & ro)
        self.assertEqual(json.loads(self.rpc("nope")["body"])["error"]["code"], -32601)

    def test_read_only_token_cannot_write_and_agent_comes_from_token(self):
        res = self.rpc("tools/call", {"name": "brain_log_action", "arguments": action()},
                       client="roclient", scopes=("read",))
        self.assertEqual(res["statusCode"], 403)
        self.assertEqual(self.store.items, {})
        ok = json.loads(self.rpc("tools/call", {"name": "brain_log_action",
                                                "arguments": {**action(), "agent": "spoofed"}})["body"])
        saved = self.store.get(ok["result"]["structuredContent"]["id"])
        self.assertEqual(saved["created_by"], "claude-code")
        self.assertEqual((self.log["agent"], self.log["tool"], self.log["project"]),
                         ("claude-code", "brain_log_action", "my-app"))

    def test_discovery_documents_are_public_and_point_at_the_auth_server(self):
        for path in ("/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp"):
            prm = self.call(None, method="GET", path=path, anonymous=True)
            self.assertEqual(prm["statusCode"], 200)
            body = json.loads(prm["body"])
            self.assertEqual(body["resource"], "https://brain.example.com/mcp")
            self.assertEqual(body["authorization_servers"], ["https://brain.example.com"])
        meta = json.loads(self.call(None, method="GET", path="/.well-known/oauth-authorization-server",
                                    anonymous=True)["body"])
        self.assertEqual(meta["issuer"], "https://brain.example.com")
        self.assertEqual(meta["code_challenge_methods_supported"], ["S256"])
        self.assertIn("none", meta["token_endpoint_auth_methods_supported"])
        self.assertTrue(meta["authorization_endpoint"].startswith("https://auth.example.com/"))
        denied = self.rpc("ping", anonymous=True)
        self.assertEqual(denied["statusCode"], 401)
        self.assertIn('resource_metadata="https://brain.example.com/.well-known/oauth-protected-resource"',
                      denied["headers"]["www-authenticate"])

    def test_connector_login_gets_read_and_write_only_for_this_resource(self):
        ok = self.rpc("tools/list", client="connector", scope_text="openid", aud="https://brain.example.com/mcp")
        names = {t["name"] for t in json.loads(ok["body"])["result"]["tools"]}
        self.assertTrue(WRITE_TOOLS <= names)
        self.assertEqual(self.rpc("ping", client="connector", scope_text="openid")["statusCode"], 200)
        self.assertEqual(self.rpc("ping", client="connector", scope_text="openid",
                                  aud="https://other.example.com/")["statusCode"], 401)
        self.assertEqual(self.rpc("ping", client="connector", scope_text="email")["statusCode"], 401)
        self.assertEqual(self.rpc("ping", client="rwclient", scope_text="openid")["statusCode"], 401)
        saved = json.loads(self.rpc("tools/call", {"name": "brain_log_action", "arguments": action()},
                                    client="connector", scope_text="openid")["body"])
        self.assertEqual(self.store.get(saved["result"]["structuredContent"]["id"])["created_by"], "claude-connector")

    def test_tool_errors_are_results_not_protocol_errors(self):
        body = json.loads(self.rpc("tools/call", {"name": "brain_get", "arguments": {"id": "missing-id"}})["body"])
        self.assertTrue(body["result"]["isError"])


if __name__ == "__main__":
    unittest.main()
