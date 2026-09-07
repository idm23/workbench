"""Registering a node, and deciding which address to reach it on.

The table is the dull half. What is worth testing is the addressing: a node
advertises every route to itself in preference order and the head *probes*
rather than trusts, because which route works is a property of where the
asking happens — and on the two machines this was built for, that answer has
already changed once mid-project.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from workbench import nodes
from workbench.app import app
from workbench.database.db import get_db, make_engine
from workbench.database.models import Base, Node
from workbench.nodes import (
    INFERENCE,
    Registration,
    candidates,
    inference_url,
    known_nodes,
    register,
    url_for,
)


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKBENCH_DB", str(tmp_path / "data" / "test.db"))
    engine = make_engine(f"sqlite+pysqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


@pytest.fixture
def client(tmp_path, monkeypatch):
    """The app over the same database, for the registration route."""
    monkeypatch.setenv("WORKBENCH_DB", str(tmp_path / "data" / "api.db"))
    engine = make_engine(f"sqlite+pysqlite:///{tmp_path / 'api.db'}")
    Base.metadata.create_all(engine)

    def override():
        session = Session(engine)
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def a_node(**overrides) -> Registration:
    fields = {
        "name": "homebox-node-1",
        "addresses": ["192.168.1.155", "100.120.132.42"],
        "capabilities": [INFERENCE],
        "model": "qwen2.5-coder:7b",
        "gpu": "NVIDIA GeForce RTX 3070 Laptop GPU, 8192 MiB",
    }
    return Registration(**(fields | overrides))


def test_a_node_registers_itself(db):
    node = register(db, a_node())

    assert node.name == "homebox-node-1"
    assert node.addresses == ["192.168.1.155", "100.120.132.42"]
    assert node.last_seen_at is not None


def test_registering_twice_updates_rather_than_duplicates(db):
    """A node reinstalled or re-addressed is the same node. A second row for it
    would leave the head probing an address nothing listens on."""
    register(db, a_node())
    register(db, a_node(addresses=["192.168.1.200"], model="qwen3:8b"))

    assert len(known_nodes(db)) == 1
    assert known_nodes(db)[0].addresses == ["192.168.1.200"]
    assert known_nodes(db)[0].model == "qwen3:8b"


def test_a_remembered_route_that_is_gone_is_forgotten(db):
    """The case that would otherwise hang every run: a node that changed
    network, still holding a `last_good_address` on the old one, which the head
    tries first and waits out."""
    node = register(db, a_node())
    node.last_good_address = "192.168.1.155"
    db.commit()

    register(db, a_node(addresses=["10.0.0.9"]))

    assert known_nodes(db)[0].last_good_address is None


def test_a_remembered_route_that_still_exists_is_kept(db):
    node = register(db, a_node())
    node.last_good_address = "192.168.1.155"
    db.commit()

    register(db, a_node())

    assert known_nodes(db)[0].last_good_address == "192.168.1.155"


def test_what_answered_last_is_tried_first(db):
    node = register(db, a_node())
    node.last_good_address = "100.120.132.42"

    assert candidates(node) == ["100.120.132.42", "192.168.1.155"]


def test_otherwise_the_order_the_node_gave_is_the_order(db):
    """LAN first, tailnet second, because that is how the node listed them."""
    node = register(db, a_node())

    assert candidates(node) == ["192.168.1.155", "100.120.132.42"]


def test_the_first_address_that_answers_wins_and_is_remembered(db, monkeypatch):
    register(db, a_node())
    tried: list[str] = []

    def answers(url: str) -> bool:
        tried.append(url)
        return url == url_for("100.120.132.42")

    monkeypatch.setattr(nodes, "_answers", answers)

    assert inference_url(db) == url_for("100.120.132.42")
    assert tried == [url_for("192.168.1.155"), url_for("100.120.132.42")]
    assert known_nodes(db)[0].last_good_address == "100.120.132.42"


def test_the_remembered_route_costs_one_request(db, monkeypatch):
    node = register(db, a_node())
    node.last_good_address = "100.120.132.42"
    db.commit()
    tried: list[str] = []

    monkeypatch.setattr(nodes, "_answers", lambda url: tried.append(url) or True)

    inference_url(db)

    assert tried == [url_for("100.120.132.42")]


def test_a_node_that_cannot_do_it_is_not_asked(db, monkeypatch):
    register(db, a_node(capabilities=["storage"]))
    monkeypatch.setattr(nodes, "_answers", lambda _url: pytest.fail("probed the wrong node"))

    assert inference_url(db) is None


def test_no_nodes_is_an_answer_not_a_failure(db):
    """A single machine is a perfectly good Workbench: the caller falls back to
    its own configuration."""
    assert inference_url(db) is None


def test_a_node_that_answers_nowhere_is_reported_as_none(db, monkeypatch, caplog):
    register(db, a_node())
    monkeypatch.setattr(nodes, "_answers", lambda _url: False)

    with caplog.at_level("WARNING"):
        assert inference_url(db) is None

    assert "did not answer" in caplog.text


def test_registering_over_http(client):
    """The call a node's installer makes, and its deploy timer repeats."""
    response = client.post(
        "/api/nodes",
        json={
            "name": "homebox-node-1",
            "addresses": ["192.168.1.155", "100.120.132.42"],
            "capabilities": ["inference"],
            "model": "qwen2.5-coder:7b",
            "gpu": "RTX 3070 Laptop, 8192 MiB",
        },
    )

    assert response.status_code == 200
    assert response.json()["name"] == "homebox-node-1"
    assert response.json()["addresses"] == ["192.168.1.155", "100.120.132.42"]
    assert client.get("/api/nodes").json()[0]["gpu"] == "RTX 3070 Laptop, 8192 MiB"


def test_re_registering_over_http_is_idempotent(client):
    """The common call is the tenth one, not the first — a node's deploy timer
    makes this every five minutes."""
    body = {
        "name": "homebox-node-1",
        "addresses": ["192.168.1.155"],
        "capabilities": ["inference"],
    }
    client.post("/api/nodes", json=body)
    client.post("/api/nodes", json=body)

    assert len(client.get("/api/nodes").json()) == 1


def test_a_registration_must_say_where_and_what(client):
    """An address list is the whole point of the row; a node with none is
    unreachable, and one with no capability is unusable."""
    for body in (
        {"name": "n", "addresses": [], "capabilities": ["inference"]},
        {"name": "n", "addresses": ["10.0.0.1"], "capabilities": []},
    ):
        assert client.post("/api/nodes", json=body).status_code == 422


def test_a_node_in_the_database_is_a_node_on_the_page(db):
    register(db, a_node())
    register(db, a_node(name="homebox-node-2"))

    assert [node.name for node in known_nodes(db)] == ["homebox-node-1", "homebox-node-2"]


def test_the_services_page_lists_a_node(client, monkeypatch):
    """Including the address that is actually working, because "which route is
    it using" is the question someone asks when a node goes quiet."""
    monkeypatch.setattr("workbench.app.page_warnings", lambda: [])
    client.post(
        "/api/nodes",
        json={
            "name": "homebox-node-1",
            "addresses": ["192.168.1.155", "100.120.132.42"],
            "capabilities": ["inference"],
            "model": "qwen2.5-coder:7b",
            "gpu": "RTX 3070 Laptop, 8192 MiB",
        },
    )

    page = client.get("/services").text

    assert "homebox-node-1" in page
    assert "192.168.1.155" in page
    assert "qwen2.5-coder:7b" in page


def test_the_services_page_says_how_to_add_one(client, monkeypatch):
    """Not an error state: a single machine is a perfectly good Workbench, and
    this panel is how someone learns a second one is possible."""
    monkeypatch.setattr("workbench.app.page_warnings", lambda: [])

    assert "--role=node" in client.get("/services").text


def test_the_url_is_built_from_the_address(db):
    """A node advertises addresses, not URLs: the port and path are fixed by
    the installer, which is what lets the head build one it can trust."""
    assert url_for("192.168.1.155") == "http://192.168.1.155:11434/v1"


def test_a_node_row_is_readable(db):
    assert "homebox-node-1" in repr(register(db, a_node()))


def test_nodes_are_typed_as_the_model(db):
    assert isinstance(register(db, a_node()), Node)
