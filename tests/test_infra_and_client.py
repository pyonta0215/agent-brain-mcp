import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = (ROOT / "infra" / "template.yaml").read_text(encoding="utf-8")
CLIENT = ROOT / "client" / "brain_auth_header.py"
MCP_URL = "https://${McpApi}.execute-api.${AWS::Region}.${AWS::URLSuffix}/mcp"


def resource(name):
    return TEMPLATE.split(f"\n  {name}:\n", 1)[1].split("\n\n  ", 1)[0]


class TemplateTest(unittest.TestCase):
    def test_data_is_retained_and_recoverable(self):
        table = resource("BrainTable")
        for text in ("DeletionPolicy: Retain", "DeletionProtectionEnabled: true", "PointInTimeRecoveryEnabled: true",
                     "BillingMode: PAY_PER_REQUEST", "IndexName: gsi1", "IndexName: gsi2", "SSEEnabled: true"):
            self.assertIn(text, table)

    def test_mcp_route_needs_a_jwt_from_this_stack_users(self):
        route = resource("McpRoute")
        self.assertIn("AuthorizationType: JWT", route)
        self.assertIn(f"{MCP_URL}/read", route)
        self.assertIn("- openid", route)
        auth = resource("McpApiAuthorizer")
        for client in ("ConnectorClient", "WriterAgentClient", "ReaderAgentClient"):
            self.assertIn(f"!Ref {client}", auth)
        self.assertIn(f"{MCP_URL}'", auth)
        self.assertIn("/${UserPool}'", auth)

    def test_only_discovery_documents_are_anonymous(self):
        discovery = resource("DiscoveryRoute")
        self.assertIn("RouteKey: 'GET /.well-known/{proxy+}'", discovery)
        self.assertIn("AuthorizationType: NONE", discovery)
        self.assertEqual(TEMPLATE.count("AuthorizationType: NONE"), 1)

    def test_users_cannot_sign_themselves_up(self):
        self.assertIn("AllowAdminCreateUserOnly: true", resource("UserPool"))
        connector = resource("ConnectorClient")
        self.assertIn("GenerateSecret: false", connector)
        self.assertIn("AllowedOAuthFlows: [code]", connector)

    def test_reader_client_cannot_write(self):
        self.assertIn(f"{MCP_URL}/write", resource("WriterAgentClient"))
        reader = resource("ReaderAgentClient")
        self.assertIn(f"{MCP_URL}/read", reader)
        self.assertNotIn("/write", reader)
        for client in ("WriterAgentClient", "ReaderAgentClient"):
            self.assertIn("AllowedOAuthFlows: [client_credentials]", resource(client))

    def test_function_has_no_scan_or_delete_and_is_capped(self):
        role = resource("McpFunctionRole")
        self.assertNotIn("Scan", role)
        self.assertNotIn("Delete", role)
        self.assertNotIn("dynamodb:*", role)
        function = resource("McpFunction")
        self.assertIn("ReservedConcurrentExecutions: 2", function)
        self.assertIn(f"BRAIN_RESOURCE: !Sub '{MCP_URL}'", function)
        self.assertIn("INTERACTIVE_CLIENTS: !Ref ConnectorClient", function)
        self.assertIn('"${ReaderAgentClient}":"${ReaderAgentName}"', function)
        self.assertIn("RetentionInDays: 30", resource("McpFunctionLogGroup"))


class TokenHandler(BaseHTTPRequestHandler):
    calls = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers["content-length"])).decode()
        TokenHandler.calls.append((self.headers.get("authorization"), body))
        payload = json.dumps({"access_token": f"token-{len(TokenHandler.calls)}", "expires_in": 3600}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


class ClientTest(unittest.TestCase):
    def test_fetches_once_then_reuses_cached_token(self):
        server = HTTPServer(("127.0.0.1", 0), TokenHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                env = {**os.environ, "TMPDIR": tmp, "BRAIN_CLIENT_ID": "cid", "BRAIN_CLIENT_SECRET": "sec",
                       "BRAIN_TOKEN_URL": f"http://127.0.0.1:{server.server_port}/oauth2/token",
                       "BRAIN_RESOURCE": "https://brain.example.com/mcp",
                       "NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"}
                first = subprocess.run([sys.executable, str(CLIENT)], env=env, capture_output=True, text=True, check=True)
                second = subprocess.run([sys.executable, str(CLIENT), "--raw"], env=env, capture_output=True, text=True,
                                        check=True)
                missing = subprocess.run([sys.executable, str(CLIENT)], env={**env, "BRAIN_CLIENT_SECRET": ""},
                                         capture_output=True, text=True)
                cache_mode = [oct(p.stat().st_mode & 0o777) for p in Path(tmp).glob("agent-brain-*.json")]
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(json.loads(first.stdout), {"Authorization": "Bearer token-1"})
        self.assertEqual(second.stdout.strip(), "token-1")
        self.assertEqual(len(TokenHandler.calls), 1)
        self.assertIn("grant_type=client_credentials", TokenHandler.calls[0][1])
        self.assertTrue(TokenHandler.calls[0][0].startswith("Basic "))
        self.assertEqual(missing.returncode, 1)
        self.assertNotIn("sec", missing.stderr)
        self.assertEqual(cache_mode, ["0o600"])


if __name__ == "__main__":
    unittest.main()
