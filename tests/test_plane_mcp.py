"""Offline Plane MCP tests. Run: python3 -m unittest discover -s tests -p test_plane_mcp.py -v"""

import http.client
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import urllib.parse
import urllib.response

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import claude_codex as runtime
import plane_mcp as plane

SECRET = "plane-test-secret-never-print-123"
PROJECT = "12345678-1234-1234-1234-123456789abc"
WORK_ITEM = "abcdefab-abcd-abcd-abcd-abcdefabcdef"
PAGE = "fedcba98-7654-4321-8765-0123456789ab"


def request(method, params=None, request_id=1):
    value = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        value["params"] = params
    return value


def initialize(version=None):
    return request("initialize", {"protocolVersion": version or plane.PROTOCOL_VERSIONS[0],
                                  "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}})


def ready(server):
    result = server.handle(initialize())
    assert "result" in result, result
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


class RecordingBytesIO(io.BytesIO):
    def __init__(self, data):
        super().__init__(data)
        self.read_sizes = []
        self.line_sizes = []

    def read(self, size=-1):
        self.read_sizes.append(size)
        return super().read(size)

    def readline(self, size=-1):
        self.line_sizes.append(size)
        return super().readline(size)


class OfflineTests(unittest.TestCase):
    def setUp(self):
        # Patch the transport, not the URL builder or redirect/error handlers.
        # Any unconfigured network request fails rather than contacting Plane.
        transport = patch.object(plane.urllib.request.HTTPSHandler, "https_open", autospec=True,
                                 side_effect=AssertionError("Unexpected network request"))
        self.transport = transport.start()
        self.addCleanup(transport.stop)
        self.server = plane.PlaneServer(SECRET)

    def reply(self, body=b"{}", status=200, headers=None, url=None):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        stream = RecordingBytesIO(body)

        def respond(handler, req):
            response = urllib.response.addinfourl(stream, headers or {}, url or req.full_url, status)
            response.msg = "Remote reason " + SECRET
            return response

        self.transport.side_effect = respond
        return stream

    def assert_rpc_error(self, result, code):
        self.assertEqual(result["jsonrpc"], "2.0")
        self.assertEqual(result["error"]["code"], code)
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertNotIn("result", result)


class CredentialTests(OfflineTests):
    def test_key_normalization_and_visible_ascii(self):
        for value in (SECRET, "  " + SECRET + "\n", "\r\n\t" + SECRET + "\t\r\n", " " + SECRET + " "):
            self.assertEqual(plane.validate_api_key(value), SECRET)
        for value in ("x", "x" * 4096, "!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~"):
            self.assertEqual(plane.validate_api_key(value), value)
        for value in (None, False, 123, 1.2, [], {}, b"token", "", " \n\t ", "x" * 4097,
                      SECRET + " space", SECRET + "\r\nHeader: injected", SECRET + "\x00", SECRET + "\x7f",
                      SECRET + "é", SECRET + "\ud800", SECRET + " more"):
            with self.subTest(value_type=type(value).__name__), self.assertRaises(runtime.SetupError) as error:
                plane.validate_api_key(value)
            self.assertNotIn(SECRET, str(error.exception))

    def test_exact_credential_file_and_absolute_path(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "plane-credentials.json"
            path.write_text(json.dumps({"workspace": "peppy", "api_key": " " + SECRET + "\n"}))
            self.assertEqual(plane.load_credentials(path), SECRET)
            self.assertEqual(plane.load_credentials(str(path)), SECRET)
            for bad in ("plane-credentials.json", "~/plane-credentials.json", None, 123, [], Path(temp),
                        Path(temp) / SECRET):
                with self.subTest(path_type=type(bad).__name__), self.assertRaises(runtime.SetupError) as error:
                    plane.load_credentials(bad)
                self.assertNotIn(SECRET, str(error.exception))
        self.transport.assert_not_called()

    def test_invalid_credentials_are_redacted(self):
        values = [[], None, SECRET, {"api_key": SECRET}, {"workspace": "peppy"},
                  {"workspace": "other", "api_key": SECRET}, {"workspace": ["peppy"], "api_key": SECRET},
                  {"workspace": "peppy", "api_key": SECRET, "url": "https://evil.invalid"},
                  {"workspace": "peppy", "api_key": {"secret": SECRET}},
                  {"workspace": "peppy", "api_key": SECRET + "\nunsafe"}]
        bodies = [json.dumps(value).encode() for value in values]
        bodies += [SECRET.encode(), b'\xff' + SECRET.encode(),
                   ('{"workspace":"peppy","api_key":"' + SECRET + '","api_key":"other"}').encode(),
                   b'{"workspace":"peppy","api_key":NaN}', b"[" * 2000 + SECRET.encode(),
                   b" " * (plane.MAX_CREDENTIAL_BYTES + 1)]
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / SECRET
            for body in bodies:
                with self.subTest(body_size=len(body)):
                    path.write_bytes(body)
                    with self.assertRaises(runtime.SetupError) as error:
                        plane.load_credentials(path)
                    self.assertNotIn(SECRET, str(error.exception))
                    self.assertNotIn("Traceback", str(error.exception))


class ToolTests(OfflineTests):
    def test_only_read_only_tools_with_closed_schemas(self):
        ready(self.server)
        tools = self.server.handle(request("tools/list"))["result"]["tools"]
        self.assertEqual([tool["name"] for tool in tools],
                         ["list_projects", "list_work_items", "get_work_item", "list_pages", "get_page"])
        for tool in tools:
            self.assertEqual(tool["annotations"], {"readOnlyHint": True})
            self.assertEqual(tool["inputSchema"]["type"], "object")
            self.assertIs(tool["inputSchema"]["additionalProperties"], False)
        for tool in (tool for tool in tools if tool["name"].startswith("list_")):
            schema = tool["inputSchema"]["properties"]["per_page"]
            self.assertEqual(schema["default"], plane.DEFAULT_PER_PAGE)
            self.assertEqual(schema["maximum"], 100)
        self.transport.assert_not_called()

    def test_every_request_is_fixed_workspace_get_with_only_api_auth(self):
        cases = [("list_projects", {}, "projects/?per_page=100"),
                 ("list_work_items", {"project_id": PROJECT.upper()}, f"projects/{PROJECT}/work-items/?per_page=100"),
                 ("get_work_item", {"project_id": PROJECT, "work_item_id": WORK_ITEM.upper()},
                  f"projects/{PROJECT}/work-items/{WORK_ITEM}/"),
                 ("list_pages", {}, "pages/?per_page=100"),
                 ("list_pages", {"project_id": PROJECT.upper()}, f"projects/{PROJECT}/pages/?per_page=100"),
                 ("get_page", {"page_id": PAGE.upper()}, f"pages/{PAGE}/"),
                 ("get_page", {"project_id": PROJECT, "page_id": PAGE}, f"projects/{PROJECT}/pages/{PAGE}/")]
        for name, arguments, suffix in cases:
            with self.subTest(tool=name, suffix=suffix):
                self.transport.reset_mock()
                body = {"results": [{"id": PROJECT}], "next_cursor": "100:1:0", "prev_cursor": "100:-1:0",
                        "next_page_results": True, "total_results": 321,
                        "next": "https://evil.invalid/mutate", "previous": "https://api.plane.so/other"}
                stream = self.reply(body)
                self.assertEqual(self.server.call_tool(name, arguments), body)
                self.transport.assert_called_once()
                req = self.transport.call_args.args[1]
                self.assertEqual(req.full_url, "https://api.plane.so/api/v1/workspaces/peppy/" + suffix)
                self.assertEqual(req.get_method(), "GET")
                self.assertIsNone(req.data)
                self.assertEqual(req.timeout, plane.HTTP_TIMEOUT)
                self.assertEqual(dict(req.header_items()), {"Host": "api.plane.so", "X-api-key": SECRET,
                                                          "Accept": "application/json", "User-agent": plane.SERVER_NAME + "/1.0"})
                limit = plane.MAX_PAGE_RESPONSE_BYTES if name == "get_page" else plane.MAX_RESPONSE_BYTES
                self.assertEqual(stream.read_sizes, [limit + 1])
                self.assertTrue(stream.closed)

    def test_pagination_is_explicit_bounded_and_query_encoded(self):
        for name, base_args in (("list_projects", {}), ("list_work_items", {"project_id": PROJECT}),
                                ("list_pages", {}), ("list_pages", {"project_id": PROJECT})):
            for per_page in (1, 40, 100):
                for cursor in ("100:1:0", "YWJjZA==", "a/b+c", "https://evil.invalid/?method=DELETE&workspace=other#x",
                               "../%2f?url=https://evil.invalid"):
                    with self.subTest(tool=name, per_page=per_page, cursor=cursor):
                        self.reply()
                        self.server.call_tool(name, {**base_args, "cursor": cursor, "per_page": per_page})
                        req = self.transport.call_args.args[1]
                        parsed = urllib.parse.urlsplit(req.full_url)
                        self.assertEqual(parsed.scheme, "https")
                        self.assertEqual(parsed.netloc, "api.plane.so")
                        self.assertTrue(parsed.path.startswith("/api/v1/workspaces/peppy/"))
                        self.assertEqual(parsed.fragment, "")
                        self.assertEqual(urllib.parse.parse_qs(parsed.query), {"per_page": [str(per_page)], "cursor": [cursor]})

    def test_page_filters_are_enumerated_bounded_and_query_encoded(self):
        for page_type in plane.PAGE_TYPES:
            for search in ("a", "Robotics workflow", "é & ü", "../pages/?type=all#x", "x" * plane.MAX_SEARCH_LENGTH):
                with self.subTest(page_type=page_type, search=search[:20]):
                    self.reply()
                    self.server.call_tool("list_pages", {"type": page_type, "search": search})
                    query = urllib.parse.urlsplit(self.transport.call_args.args[1].full_url).query
                    self.assertEqual(urllib.parse.parse_qs(query),
                                     {"per_page": ["100"], "type": [page_type], "search": [search]})
        self.transport.reset_mock()
        for value in (None, True, 1, [], {}, ["all"], "", "All", "all ", "deleted", "all&type=archived", SECRET):
            with self.subTest(value_type=type(value).__name__), self.assertRaises(plane.RpcError) as error:
                self.server.call_tool("list_pages", {"type": value})
            self.assertNotIn(SECRET, str(error.exception))
        for value in (None, True, 1, [], {}, "", "x" * (plane.MAX_SEARCH_LENGTH + 1), "a\tb", "a\r\nb",
                      "a\x00b", "\x7f", "\ud800"):
            with self.subTest(value_type=type(value).__name__), self.assertRaises(plane.RpcError):
                self.server.call_tool("list_pages", {"search": value})
        self.transport.assert_not_called()

    def test_page_reads_return_one_compact_html_body(self):
        html = ('<h2 class="editor-heading-block" data-spacing-group="heading" data-id="1">Goal &amp; plan</h2>'
                '<p class="editor-paragraph-block" data-id="2">a &lt;b&gt; "c" &#39;d&#39;<br>'
                ' class="kept" text</p><pre class="" data-id="3"><code class="rounded-sm language-mermaid" spellcheck="false">'
                'A --&gt; B</code></pre><table data-id="4"><tbody><tr style=""><td colspan="1" rowspan="2" colwidth="150" '
                'style="">cell</td></tr></tbody></table><ul data-type="taskList" data-tight="true"><li data-type="taskItem">'
                '<input type="checkbox" checked><a href="https://example.invalid/?a=1&amp;b=&quot;2&quot;" target="_blank" '
                'rel="noopener noreferrer" class="underline">link</a></li></ul><image-component src="asset" width="775px" '
                'height="224px" aspectratio="3.4" alignment="left" status="uploaded"></image-component><hr/>')
        body = {"id": PAGE, "name": "Wiki", "parent_id": None, "projects": [], "description_html": html,
                "description_stripped": "Goal & plan", "description": {"type": "doc"},
                "description_json": {"type": "doc"}, "description_binary": "AAEC"}
        self.reply(body)
        page = self.server.call_tool("get_page", {"page_id": PAGE})
        self.assertEqual(page, {"id": PAGE, "name": "Wiki", "parent_id": None, "projects": [], "description_html": (
            '<h2>Goal &amp; plan</h2><p>a &lt;b&gt; "c" \'d\'<br> class="kept" text</p>'
            '<pre><code class="language-mermaid">A --&gt; B</code></pre>'
            '<table><tbody><tr><td rowspan="2">cell</td></tr></tbody></table>'
            '<ul data-type="taskList"><li data-type="taskItem"><input type="checkbox" checked>'
            '<a href="https://example.invalid/?a=1&amp;b=&quot;2&quot;">link</a></li></ul>'
            '<image-component src="asset"></image-component><hr/>')})
        for description in (None, "", "plain text only"):
            with self.subTest(description=description):
                self.reply({"id": PAGE, "description_html": description, "description_binary": "AAEC"})
                self.assertEqual(self.server.call_tool("get_page", {"page_id": PAGE}),
                                 {"id": PAGE, "description_html": description})
        # Only page reads are reshaped; listings keep every field Plane returns.
        listing = {"results": [{"id": PAGE, "description_html": '<p data-id="1">x</p>', "description_binary": "AAEC"}]}
        self.reply(listing)
        self.assertEqual(self.server.call_tool("list_pages", {}), listing)

    def test_unknown_mutation_and_arbitrary_parameters_never_reach_transport(self):
        for name in ("create_project", "create_work_item", "update_work_item", "delete_work_item", "request", "GET",
                     "projects/", "list_projects/../delete", SECRET, None, [], {}):
            with self.subTest(tool_type=type(name).__name__), self.assertRaises(plane.RpcError) as error:
                self.server.call_tool(name, {})
            self.assertEqual(error.exception.code, -32602)
            self.assertNotIn(SECRET, str(error.exception))
        cases = [("list_projects", {}, {"project_id", "work_item_id"}),
                 ("list_work_items", {"project_id": PROJECT}, {"work_item_id"}),
                 ("get_work_item", {"project_id": PROJECT, "work_item_id": WORK_ITEM}, {"cursor", "per_page"}),
                 ("list_pages", {"project_id": PROJECT}, {"page_id", "work_item_id"}),
                 ("get_page", {"page_id": PAGE}, {"cursor", "per_page", "type", "search", "work_item_id"})]
        common = {"method", "path", "url", "base_url", "endpoint", "workspace", "workspace_slug", "headers", "body",
                  "data", "api_key", "authorization", "query", "limit", "_meta", "additionalProperties", SECRET}
        for name, args, extra in cases:
            for key in common | extra:
                with self.subTest(tool=name, key_type=type(key).__name__), self.assertRaises(plane.RpcError) as error:
                    self.server.call_tool(name, {**args, key: SECRET})
                self.assertNotIn(SECRET, str(error.exception))
        self.transport.assert_not_called()

    def test_arguments_are_objects_and_required_ids_are_present(self):
        for name in ("list_projects", "list_work_items", "get_work_item", "list_pages", "get_page"):
            for value in (None, False, [], ["cursor"], 0, "{}"):
                with self.subTest(tool=name, value_type=type(value).__name__), self.assertRaises(plane.RpcError):
                    self.server.call_tool(name, value)
        for name, arguments in (("list_work_items", {}), ("get_work_item", {}),
                                ("get_work_item", {"project_id": PROJECT}),
                                ("get_work_item", {"work_item_id": WORK_ITEM}),
                                ("get_page", {}), ("get_page", {"project_id": PROJECT})):
            with self.subTest(tool=name), self.assertRaises(plane.RpcError):
                self.server.call_tool(name, arguments)
        self.transport.assert_not_called()

    def test_uuid_validation_rejects_traversal_encoded_separators_and_other_types(self):
        bad_ids = [None, 1, True, [], {}, "", "..", "../other", PROJECT + "/", PROJECT + "%2fother", "%2e%2e",
                   PROJECT + "%5cother", PROJECT + "\\other", PROJECT + "?method=POST", PROJECT + "#x",
                   "https://evil.invalid", PROJECT + "\n", " " + PROJECT, PROJECT.replace("-", ""),
                   "{" + PROJECT + "}", "urn:uuid:" + PROJECT, PROJECT[:-1] + "g", SECRET]
        for value in bad_ids:
            for name, arguments in (("list_work_items", {"project_id": value}),
                                    ("get_work_item", {"project_id": value, "work_item_id": WORK_ITEM}),
                                    ("get_work_item", {"project_id": PROJECT, "work_item_id": value}),
                                    ("list_pages", {"project_id": value}),
                                    ("get_page", {"page_id": value}),
                                    ("get_page", {"project_id": value, "page_id": PAGE})):
                with self.subTest(tool=name, value_type=type(value).__name__), self.assertRaises(plane.RpcError) as error:
                    self.server.call_tool(name, arguments)
                self.assertNotIn(SECRET, str(error.exception))
        self.transport.assert_not_called()

    def test_pagination_types_and_bounds_are_enforced_in_direct_calls(self):
        for name, base in (("list_projects", {}), ("list_work_items", {"project_id": PROJECT}), ("list_pages", {})):
            for value in (None, True, False, 0, -1, 101, 1.0, 1.5, "10", [], {}, float("inf")):
                with self.subTest(tool=name, value_type=type(value).__name__), self.assertRaises(plane.RpcError):
                    self.server.call_tool(name, {**base, "per_page": value})
            for value in (None, True, 1, [], {}, "", "x" * 1025, "a b", "a\tb", "a\r\nb", "a\x00b", "\x7f", "é"):
                with self.subTest(tool=name, value_type=type(value).__name__), self.assertRaises(plane.RpcError):
                    self.server.call_tool(name, {**base, "cursor": value})
        self.transport.assert_not_called()

    def test_redirects_are_not_followed_and_error_bodies_are_not_read(self):
        for status in (300, 301, 302, 303, 304, 307, 308):
            for target in ("https://evil.invalid/steal", "http://api.plane.so/insecure", "file:///tmp/secret",
                           "https://api.plane.so/api/v1/workspaces/other/projects/"):
                with self.subTest(status=status, target=target):
                    self.transport.reset_mock()
                    stream = self.reply(SECRET.encode(), status, {"Location": target})
                    with self.assertRaises(plane.ToolError) as error:
                        self.server.call_tool("list_projects", {})
                    self.assertIn("redirect", str(error.exception))
                    self.assertNotIn(SECRET, str(error.exception))
                    self.assertNotIn(target, str(error.exception))
                    self.transport.assert_called_once()
                    self.assertEqual(stream.read_sizes, [])
                    self.assertTrue(stream.closed)

    def test_unexpected_final_response_url_is_rejected(self):
        stream = self.reply(url="https://evil.invalid")
        with self.assertRaises(plane.ToolError):
            self.server.call_tool("list_projects", {})
        self.assertEqual(stream.read_sizes, [])
        self.assertTrue(stream.closed)

    def test_http_errors_are_tool_errors_without_remote_bodies_or_reasons(self):
        ready(self.server)
        for status in (400, 401, 403, 404, 429, 500, 503):
            with self.subTest(status=status):
                stream = self.reply(("Remote body " + SECRET).encode(), status)
                result = self.server.handle(request("tools/call", {"name": "list_projects"}))["result"]
                self.assertTrue(result["isError"])
                self.assertIn(str(status), result["content"][0]["text"])
                self.assertNotIn(SECRET, json.dumps(result))
                self.assertNotIn("Remote", json.dumps(result))
                self.assertEqual(stream.read_sizes, [])
                self.assertTrue(stream.closed)

    def test_verification_is_one_bounded_read_that_names_a_rejected_credential(self):
        stream = self.reply({"results": [], "total_results": 0})
        self.assertIsNone(plane.verify_api_key(SECRET))
        self.transport.assert_called_once()
        req = self.transport.call_args.args[1]
        self.assertEqual(req.full_url, plane.BASE_URL + "projects/?per_page=1")
        self.assertEqual(req.get_method(), "GET")
        self.assertTrue(stream.closed)
        # Only a rejected credential is an AuthError; anything else leaves the token unjudged.
        for status, credential in ((401, True), (403, True), (404, False), (429, False), (500, False)):
            with self.subTest(status=status):
                self.reply(("Remote body " + SECRET).encode(), status)
                with self.assertRaises(plane.ToolError) as error:
                    plane.verify_api_key(SECRET)
                self.assertIs(isinstance(error.exception, plane.AuthError), credential)
                self.assertNotIn(SECRET, str(error.exception))
                self.assertNotIn("Remote", str(error.exception))
        self.transport.side_effect = urllib.error.URLError(SECRET)
        with self.assertRaises(plane.ToolError) as error:
            plane.verify_api_key(SECRET)
        self.assertNotIsInstance(error.exception, plane.AuthError)
        self.assertNotIn(SECRET, str(error.exception))

    def test_network_failures_and_timeouts_are_redacted(self):
        ready(self.server)
        errors = [TimeoutError(SECRET), urllib.error.URLError(TimeoutError(SECRET)),
                  urllib.error.URLError(SECRET), OSError(SECRET), http.client.HTTPException(SECRET),
                  http.client.IncompleteRead(SECRET.encode())]
        for error in errors:
            with self.subTest(error_type=type(error).__name__):
                self.transport.side_effect = error
                response = self.server.handle(request("tools/call", {"name": "list_projects"}))
                self.assertTrue(response["result"]["isError"])
                self.assertNotIn(SECRET, json.dumps(response))
                if isinstance(error, TimeoutError) or isinstance(getattr(error, "reason", None), TimeoutError):
                    self.assertIn("timed out", response["result"]["content"][0]["text"])
        self.transport.side_effect = RuntimeError(SECRET)
        self.assert_rpc_error(self.server.handle(request("tools/call", {"name": "list_projects"})), -32603)

    def test_bad_oversized_or_non_object_json_is_redacted(self):
        bodies = [b"", SECRET.encode(), SECRET.encode() + b"\xff", json.dumps(SECRET).encode(), b"null", b"[]", b"true", b"42",
                  b'{"a":NaN}', b'{"a":Infinity}', b'{"a":-Infinity}', b'{"a":1e10000}', b'{"a":1,"a":2}',
                  b'{"a":' + b"[" * 2000 + b"0" + b"]" * 2000 + b"}"]
        for body in bodies:
            with self.subTest(body_size=len(body)):
                stream = self.reply(body)
                with self.assertRaises(plane.ToolError) as error:
                    self.server.call_tool("list_projects", {})
                self.assertIn("invalid JSON", str(error.exception))
                self.assertNotIn(SECRET, str(error.exception))
                self.assertTrue(stream.closed)
        for body in (b" " * (plane.MAX_RESPONSE_BYTES + 1), b'{"a":"' + b"x" * plane.MAX_RESPONSE_BYTES + b'"}'):
            stream = self.reply(body)
            with self.assertRaises(plane.ToolError) as error:
                self.server.call_tool("list_projects", {})
            self.assertIn("size limit", str(error.exception))
            self.assertEqual(stream.read_sizes, [plane.MAX_RESPONSE_BYTES + 1])
        # Page reads carry every copy of the body, so only they get the larger limit.
        self.reply(b'{"description_binary":"' + b"x" * plane.MAX_RESPONSE_BYTES + b'"}')
        self.assertEqual(self.server.call_tool("get_page", {"page_id": PAGE}), {})
        stream = self.reply(b" " * (plane.MAX_PAGE_RESPONSE_BYTES + 1))
        with self.assertRaises(plane.ToolError) as error:
            self.server.call_tool("get_page", {"page_id": PAGE})
        self.assertIn("size limit", str(error.exception))
        self.assertEqual(stream.read_sizes, [plane.MAX_PAGE_RESPONSE_BYTES + 1])

    def test_credentials_echoed_in_successful_json_are_redacted(self):
        for api_key in (SECRET, 'test-"quoted\\key', "x"):
            with self.subTest(key_length=len(api_key)):
                server = plane.PlaneServer(api_key)
                ready(server)
                self.reply({"results": [{"description": "before " + api_key + " after", api_key: [api_key]}],
                            "next_cursor": "100:1:0"})
                response = server.handle(request("tools/call", {"name": "list_projects"}))
                self.assertFalse(response["result"]["isError"])
                body = json.loads(response["result"]["content"][0]["text"])
                self.assertEqual(body["results"], [{"description": "before [REDACTED] after", "[REDACTED]": ["[REDACTED]"]}])
                # Even the one-character key only redacts string values, not JSON syntax.
                self.assertEqual(body.get("next_cursor", body.get("ne[REDACTED]t_cursor")), "100:1:0")

    def test_environment_cannot_override_endpoint_auth_or_proxy_routing(self):
        with patch.dict(os.environ, {"PLANE_API_URL": "http://evil.invalid", "PLANE_BASE_URL": "http://evil.invalid",
                                     "PLANE_WORKSPACE": "other", "PLANE_API_KEY": "wrong-key",
                                     "HTTP_PROXY": "http://evil.invalid", "HTTPS_PROXY": "http://evil.invalid",
                                     "http_proxy": "http://evil.invalid", "https_proxy": "http://evil.invalid"}):
            server = plane.PlaneServer(SECRET)
            self.reply()
            server.call_tool("list_projects", {})
        req = self.transport.call_args.args[1]
        self.assertEqual(req.host, "api.plane.so")
        self.assertIsNone(req._tunnel_host)
        self.assertTrue(req.full_url.startswith(plane.BASE_URL))
        self.assertEqual(req.get_header("X-api-key"), SECRET)


class ProtocolTests(OfflineTests):
    def run_lines(self, lines):
        incoming = RecordingBytesIO(lines)
        outgoing = io.StringIO()
        status = plane.serve(self.server, incoming, outgoing)
        responses = [json.loads(line) for line in outgoing.getvalue().splitlines()]
        self.assertNotIn(SECRET, outgoing.getvalue())
        return status, responses, incoming

    def test_supported_version_negotiation_and_fallback(self):
        for version in (*plane.PROTOCOL_VERSIONS, "2099-01-01", "2020-01-01"):
            with self.subTest(version=version):
                server = plane.PlaneServer(SECRET)
                response = server.handle(initialize(version))
                result = response["result"]
                self.assertEqual(result["protocolVersion"], version if version in plane.PROTOCOL_VERSIONS else plane.PROTOCOL_VERSIONS[0])
                self.assertEqual(result["serverInfo"]["name"], plane.SERVER_NAME)
                self.assertEqual(result["capabilities"], {"tools": {}})
                self.assert_rpc_error(server.handle(initialize(version)), -32600)
        self.transport.assert_not_called()

    def test_initialize_requires_well_typed_protocol_fields(self):
        valid = initialize()["params"]
        cases = [{}, {key: value for key, value in valid.items() if key != "protocolVersion"},
                 {**valid, "workspace": "other"}, {**valid, "_meta": []}]
        for key in ("protocolVersion", "capabilities", "clientInfo"):
            for value in (None, True, 1, [], ""):
                cases.append({**valid, key: value})
        for info in ({}, {"name": "client"}, {"version": "1"}, {"name": "", "version": "1"},
                     {"name": 123, "version": "1"}, {"name": "client", "version": False}):
            cases.append({**valid, "clientInfo": info})
        for params in cases:
            with self.subTest(params_type=type(params).__name__):
                self.assert_rpc_error(self.server.handle(request("initialize", params)), -32602)
                self.assertFalse(self.server.initialized)
        self.transport.assert_not_called()

    def test_initialization_lifecycle_and_ping(self):
        self.assertEqual(self.server.handle(request("ping"))["result"], {})
        self.server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertFalse(self.server.ready)
        self.assert_rpc_error(self.server.handle(request("tools/list")), -32002)
        self.assert_rpc_error(self.server.handle(request("tools/call", {"name": "list_projects"})), -32002)
        self.server.handle(initialize())
        self.assert_rpc_error(self.server.handle(request("tools/list")), -32002)
        for params in (None, [], {"unknown": True}):
            self.server.handle({"jsonrpc": "2.0", "method": "notifications/initialized", "params": params})
            self.assertFalse(self.server.ready)
        self.server.handle({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {"_meta": {}}})
        self.assertTrue(self.server.ready)
        self.assertIn("tools", self.server.handle(request("tools/list"))["result"])
        self.assertEqual(self.server.handle(request("ping", {"_meta": {}}))["result"], {})
        self.transport.assert_not_called()

    def test_notifications_never_reply_or_execute_tools(self):
        self.assertIsNone(self.server.handle({"jsonrpc": "2.0", "method": "initialize", "params": initialize()["params"]}))
        self.assertFalse(self.server.initialized)
        ready(self.server)
        for method in ("tools/call", "tools/list", "ping", "initialize", "notifications/cancelled", SECRET):
            for params in ({"name": "list_projects", "arguments": {}}, {}, [], None):
                with self.subTest(method=method, params_type=type(params).__name__):
                    self.assertIsNone(self.server.handle({"jsonrpc": "2.0", "method": method, "params": params}))
        self.transport.assert_not_called()

    def test_request_ids_and_malformed_envelopes(self):
        for request_id in (0, -1, 123, "", "client-id", "\ud800"):
            response = self.server.handle(request("ping", request_id=request_id))
            self.assertEqual(response["id"], request_id)
            self.assertEqual(response["result"], {})
        for request_id in (None, True, False, 1.0, [], {}):
            response = self.server.handle(request("ping", request_id=request_id))
            self.assert_rpc_error(response, -32600)
            self.assertIsNone(response["id"])
        invalid = [None, True, [], [request("ping")], "message", 1, {}, {"jsonrpc": "2.0"},
                   {"jsonrpc": "1.0", "id": 1, "method": "ping"}, {"id": 1, "method": "ping"},
                   {"jsonrpc": "2.0", "id": 1, "method": ""}, {"jsonrpc": "2.0", "id": 1, "method": 123},
                   {**request("ping"), "extra": SECRET}, {"jsonrpc": "2.0", "id": 1, "result": {}}]
        for value in invalid:
            with self.subTest(value_type=type(value).__name__):
                self.assert_rpc_error(self.server.handle(value), -32600)
        for params in (None, [], True, "x", 123):
            message = {**request("ping"), "params": params}
            self.assert_rpc_error(self.server.handle(message), -32602)
        self.transport.assert_not_called()

    def test_invalid_tool_calls_and_protocol_params(self):
        ready(self.server)
        invalid = [{}, {"name": None}, {"name": []}, {"name": {}}, {"name": SECRET},
                   {"name": "list_projects", "arguments": None}, {"name": "list_projects", "arguments": []},
                   {"name": "list_projects", "arguments": {"method": "POST"}},
                   {"name": "get_work_item", "arguments": {"project_id": PROJECT, "work_item_id": WORK_ITEM, "per_page": 1}},
                   {"name": "list_projects", "workspace": "other"}, {"name": "list_projects", "_meta": []}]
        for params in invalid:
            self.assert_rpc_error(self.server.handle(request("tools/call", params)), -32602)
        for method in ("tools/list", "ping"):
            for params in ({"workspace": "other"}, {"cursor": "x"}, {"_meta": None}):
                self.assert_rpc_error(self.server.handle(request(method, params)), -32602)
        for method in ("delete", "resources/list", "notifications/initialized", SECRET):
            self.assert_rpc_error(self.server.handle(request(method)), -32601)
        self.transport.assert_not_called()

    def test_successful_tool_result_preserves_pagination_in_json_text(self):
        ready(self.server)
        body = {"results": [{"id": PROJECT, "name": "Test é"}], "next_cursor": "100:1:0",
                "next_page_results": True, "count": 250}
        self.reply(body)
        response = self.server.handle(request("tools/call", {"name": "list_projects", "_meta": {"progressToken": 2}}, "call-1"))
        self.assertEqual(response["id"], "call-1")
        result = response["result"]
        self.assertIs(result["isError"], False)
        self.assertEqual(result["content"][0]["type"], "text")
        self.assertEqual(json.loads(result["content"][0]["text"]), body)
        self.transport.assert_called_once()

    def test_newline_protocol_flushes_and_keeps_processing_after_bad_json(self):
        messages = [initialize(), {"jsonrpc": "2.0", "method": "notifications/initialized"}, request("tools/list", request_id=2),
                    {"jsonrpc": "2.0", "method": "notifications/unknown"}, request("ping", request_id="last")]
        lines = b"not-json\n" + b"\n".join(json.dumps(value).encode() for value in messages) + b"\n"
        status, responses, incoming = self.run_lines(lines)
        self.assertEqual(status, 0)
        self.assertEqual(len(responses), 4)
        self.assert_rpc_error(responses[0], -32700)
        self.assertEqual([value["id"] for value in responses[1:]], [1, 2, "last"])
        self.assertEqual(responses[-1]["result"], {})
        self.assertEqual(set(incoming.line_sizes), {plane.MAX_FRAME_BYTES + 1})
        self.transport.assert_not_called()

    def test_malformed_json_utf8_constants_duplicates_and_deep_nesting(self):
        lines = [b"", b"{", b"\xff" + SECRET.encode(),
                 b'{"jsonrpc":"2.0","method":"ping","id":NaN}',
                 b'{"jsonrpc":"2.0","method":"ping","id":Infinity}',
                 b'{"jsonrpc":"2.0","method":"ping","id":1,"id":2}',
                 b"[" * 2000 + b"0" + b"]" * 2000]
        for line in lines:
            with self.subTest(line_size=len(line)):
                status, responses, _ = self.run_lines(line + b"\n" + json.dumps(request("ping")).encode() + b"\n")
                self.assertEqual(status, 0)
                self.assert_rpc_error(responses[0], -32700)
                self.assertEqual(responses[1]["result"], {})

    def test_oversized_frame_stops_without_unbounded_drain(self):
        for frame in (b" " * (plane.MAX_FRAME_BYTES + 100), "é".encode() * plane.MAX_FRAME_BYTES):
            status, responses, incoming = self.run_lines(frame + b"\n" + json.dumps(request("ping")).encode() + b"\n")
            self.assertEqual(status, 1)
            self.assertEqual(len(responses), 1)
            self.assert_rpc_error(responses[0], -32700)
            self.assertIn("size limit", responses[0]["error"]["message"])
            self.assertEqual(incoming.tell(), plane.MAX_FRAME_BYTES + 1)
            self.assertEqual(incoming.line_sizes, [plane.MAX_FRAME_BYTES + 1])
        self.transport.assert_not_called()

    def test_empty_stdin_and_eof_without_final_newline(self):
        self.assertEqual(self.run_lines(b"")[:2], (0, []))
        status, responses, _ = self.run_lines(json.dumps(request("ping")).encode())
        self.assertEqual(status, 0)
        self.assertEqual(responses[0]["result"], {})


class CommandTests(unittest.TestCase):
    def run_command(self, *args, input=""):
        return subprocess.run([sys.executable, str(ROOT / "scripts" / "plane_mcp.py"), *args],
                              input=input, text=True, capture_output=True, timeout=10, cwd=ROOT)

    def test_real_stdio_process_handshake_tools_list_and_no_secret_output(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "plane-credentials.json"
            path.write_text(json.dumps({"workspace": "peppy", "api_key": SECRET}))
            messages = [initialize("2024-11-05"), {"jsonrpc": "2.0", "method": "notifications/initialized"},
                        request("tools/list", request_id=2), request("ping", request_id="done"),
                        request("tools/call", {"name": "delete_work_item", "arguments": {}}, 4)]
            result = self.run_command("--credentials", str(path), input="".join(json.dumps(value) + "\n" for value in messages))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertNotIn(SECRET, result.stdout)
        responses = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(len(responses), 4)
        self.assertEqual(responses[0]["result"]["protocolVersion"], "2024-11-05")
        self.assertEqual(len(responses[1]["result"]["tools"]), len(plane.TOOLS))
        self.assertEqual(responses[2]["result"], {})
        self.assertEqual(responses[3]["error"]["code"], -32602)

    def test_cli_credentials_and_unknown_flags_fail_with_clean_stdout(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / SECRET
            path.write_bytes(SECRET.encode() + b"\xff")
            cases = [(), ("--credentials",), ("--credentials", "relative.json"), ("--credentials", str(path)),
                     ("--credentials", str(path / "missing")), ("--cred", str(path))]
            for flag in ("--url", "--endpoint", "--workspace", "--method", "--api-key", "--header", "--body"):
                cases.append(("--credentials", str(path), flag, SECRET))
            for args in cases:
                with self.subTest(flag=args[0] if args else "missing"):
                    result = self.run_command(*args)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(result.stdout, "")
                    self.assertNotIn(SECRET, result.stderr)
                    self.assertNotIn("Traceback", result.stderr)
                    self.assertTrue(result.stderr)


if __name__ == "__main__":
    unittest.main()
