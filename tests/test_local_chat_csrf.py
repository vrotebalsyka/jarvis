from __future__ import annotations

import contextlib
import http.client
import ipaddress
import json
import re
import sys
import threading
import unittest
from html.parser import HTMLParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import local_chat_gateway as local


class MetaElements(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.elements = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag == "meta":
            self.elements.append(dict(attrs))


def frontend_csrf(html):
    """Resolve the selector actually used by the page, not a test-only selector."""
    match = re.search(r"const c=document\.querySelector\('([^']+)'\)\.content", html)
    if match is None:
        raise AssertionError("Missing frontend CSRF selection")
    selector = match.group(1)
    elements = MetaElements(html).elements
    if selector == "meta":
        return elements[0].get("content", "")
    if selector == "meta[name=home-butler-csrf]":
        matches = [element for element in elements if element.get("name") == "home-butler-csrf"]
        if len(matches) == 1:
            return matches[0].get("content", "")
    raise AssertionError("Unexpected or ambiguous CSRF selector")


@contextlib.contextmanager
def endpoint(*, auth=False):
    calls = []
    app = local.ChatApplication(
        answerer=lambda question, context, history: calls.append(question) or "test reply",
        context_factory=lambda: {}, lan_access_key="test-owner-key-1234567890",
    )
    handler = local._handler("CsrfTestHandler", app, set(),
                             (ipaddress.ip_network("127.0.0.0/8"),), auth)
    server = local.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    handler.allowed_hosts = {f"127.0.0.1:{server.server_port}"}
    worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    worker.start()
    try:
        yield server.server_port, app, calls
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def request(port, method, path, document=None, token=None):
    headers = {}
    if token is not None:
        headers["X-Home-Butler-CSRF"] = token
    body = json.dumps(document).encode() if document is not None else None
    if body is not None:
        headers["Content-Type"] = "application/json"
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        return response.status, response.read().decode()
    finally:
        connection.close()


class LocalChatCsrfTests(unittest.TestCase):
    def test_both_templates_select_named_meta_not_charset_or_viewport(self):
        for template in (local.HTML, local.LOGIN_HTML):
            with self.subTest(login=template == local.LOGIN_HTML):
                html = template.replace("__CSRF__", "test-csrf-token")
                self.assertIn("charset", MetaElements(html).elements[0])
                self.assertEqual(frontend_csrf(html), "test-csrf-token")

    def test_chat_page_token_reaches_answerer(self):
        with endpoint() as (port, app, calls):
            status, html = request(port, "GET", "/")
            self.assertEqual(status, 200)
            token = frontend_csrf(html)
            self.assertTrue(token == app.csrf_token, "Page CSRF does not match server")
            status, body = request(port, "POST", "/api/chat", {"message": "привет"}, token)
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body), {"answer": "test reply"})
            self.assertEqual(calls, ["привет"])

    def test_missing_empty_and_stale_tokens_remain_rejected(self):
        with endpoint() as (port, app, calls):
            for token in (None, "", "stale-token"):
                for path, document in (("/api/chat", {"message": "привет"}),
                                       ("/api/login", {"key": app.lan_access_key})):
                    with self.subTest(path=path, token=token):
                        status, body = request(port, "POST", path, document, token)
                        self.assertEqual(status, 403)
                        self.assertEqual(json.loads(body)["error"], "Сессия устарела.")
            self.assertEqual(calls, [])

    def test_login_page_token_works_without_bypassing_owner_auth(self):
        with endpoint(auth=True) as (port, app, calls):
            status, html = request(port, "GET", "/")
            self.assertEqual(status, 200)
            token = frontend_csrf(html)
            self.assertTrue(token == app.csrf_token, "Login CSRF does not match server")
            status, _ = request(port, "POST", "/api/chat", {"message": "привет"}, token)
            self.assertEqual(status, 401)
            status, _ = request(port, "POST", "/api/login", {"key": "incorrect-owner-key-12345"}, token)
            self.assertEqual(status, 403)
            status, body = request(port, "POST", "/api/login", {"key": app.lan_access_key}, token)
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body), {"ok": True})
            self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
