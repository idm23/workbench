"""The buttons a head presses on a node.

The listener is unauthenticated on the LAN, so what is worth pinning is that a
request can only choose *whether* a fixed thing happens, never what: two verbs,
each a `systemctl` call on a unit named by the node itself.
"""

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from workbench import node_control
from workbench.config import CLIENT_UNIT_NAME, steam_up_unit_name


@pytest.fixture
def node(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKBENCH_DB", str(tmp_path / "data" / "workbench.db"))
    monkeypatch.setenv("WORKBENCH_ROLE", "node")
    monkeypatch.setenv("WORKBENCH_GAMING_USER", "ian")
    calls: list[tuple[str, str]] = []

    def fake(verb: str, unit: str) -> str | None:
        calls.append((verb, unit))
        return None

    monkeypatch.setattr(node_control, "_systemctl", fake)
    return calls


def test_a_client_starts_and_stops_its_stream(node, monkeypatch):
    monkeypatch.setenv("WORKBENCH_CAPABILITIES", "client")

    assert node_control.press("start")["ok"]
    assert node_control.press("stop")["ok"]
    assert node == [("start", CLIENT_UNIT_NAME), ("stop", CLIENT_UNIT_NAME)]


def test_a_gaming_node_starts_steam_and_never_stops_it(node, monkeypatch):
    """Stopping Steam would end a game somebody is in the middle of."""
    monkeypatch.setenv("WORKBENCH_CAPABILITIES", "inference,gaming")

    node_control.press("start")
    node_control.press("stop")

    assert node == [("start", f"{steam_up_unit_name()}.service")]


def test_a_node_that_is_both_starts_steam_before_the_stream(node, monkeypatch):
    monkeypatch.setenv("WORKBENCH_CAPABILITIES", "gaming,client")

    node_control.press("start")

    assert node == [("start", f"{steam_up_unit_name()}.service"), ("start", CLIENT_UNIT_NAME)]


def test_a_failure_is_reported_and_the_rest_still_tried(node, monkeypatch):
    monkeypatch.setenv("WORKBENCH_CAPABILITIES", "gaming,client")
    monkeypatch.setattr(
        node_control,
        "_systemctl",
        lambda verb, unit: "Access denied" if "steam" in unit else node.append((verb, unit)),
    )

    outcome = node_control.press("start")

    assert outcome["ok"] is False
    assert outcome["failed"] == ["Access denied"]
    assert node == [("start", CLIENT_UNIT_NAME)]


@pytest.fixture
def listening(node, monkeypatch):
    monkeypatch.setenv("WORKBENCH_CAPABILITIES", "client")
    server = ThreadingHTTPServer(("127.0.0.1", 0), node_control.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def _post(url: str) -> tuple[int, dict]:
    request = urllib.request.Request(url, data=b"", method="POST")
    try:
        with urllib.request.urlopen(request, timeout=5) as answer:
            return answer.status, json.loads(answer.read())
    except urllib.error.HTTPError as error:
        with error:
            return error.code, json.loads(error.read())


def test_the_listener_presses_a_button(listening, node):
    status, body = _post(f"{listening}/stream/start")

    assert status == 200 and body["ok"]
    assert node == [("start", CLIENT_UNIT_NAME)]


@pytest.mark.parametrize("path", ["/stream/restart", "/stream/../stream/start", "/other/start"])
def test_anything_else_is_refused_and_runs_nothing(listening, node, path):
    status, _ = _post(f"{listening}{path}")

    assert status == 404
    assert node == []


def test_a_node_with_nothing_to_stream_says_so(listening, node, monkeypatch):
    monkeypatch.setenv("WORKBENCH_CAPABILITIES", "inference")

    status, body = _post(f"{listening}/stream/start")

    assert status == 409
    assert "nothing to stream" in body["failed"][0]
