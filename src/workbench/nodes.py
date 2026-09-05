"""Which machine answers, and how this one reaches it.

A head knows about its nodes because they tell it — a node registers itself at
the end of its install and on every deploy, so adding a machine is a matter of
running the installer on that machine and nothing at all on the head.

The interesting part is not the table but the addressing. A node advertises
every route to itself in preference order, LAN first, and the head *probes*
rather than trusts: which route works is a property of where you are asking
from, not of the node, and the pair of machines this was built on has already
had the answer change once mid-project. So the head tries the address that
worked last, then each one in order, and writes down which answered.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from workbench.database.models import Node

logger = logging.getLogger(__name__)

#: The capability a node advertises when it can serve a model. A plain string
#: for the same reason `runs.backend` is one: the set is open, and the second
#: capability should not need a migration.
INFERENCE = "inference"

#: How long to wait for a node to answer a probe. Short: this runs before a run
#: starts and on a page render, and a node that is asleep should cost a moment
#: rather than a minute.
PROBE_TIMEOUT_SECONDS = 3.0

#: Where an OpenAI-compatible server answers on a node, and the path that
#: proves it is up. Both fixed by the installer, which is what lets a node
#: advertise addresses rather than URLs.
INFERENCE_PORT = 11434
INFERENCE_PATH = "/v1"


@dataclass(frozen=True)
class Registration:
    """What a node says about itself. Plain data, straight off the wire."""

    name: str
    addresses: list[str]
    capabilities: list[str]
    model: str | None = None
    gpu: str | None = None


def register(db: Session, incoming: Registration) -> Node:
    """Record a node, or update what is already known about it.

    Keyed on the name, which is the hostname: a node reinstalled, re-addressed
    or moved to another network is the same node, and a second row for it would
    leave the head probing an address nothing listens on.

    `last_good_address` survives a re-registration only if it is still one of
    the addresses offered. A node that changed networks must not keep a
    remembered route that no longer exists — that is precisely the case where
    the first probe would hang until it timed out, on every run.
    """
    node = db.execute(select(Node).where(Node.name == incoming.name)).scalar_one_or_none()
    if node is None:
        node = Node(name=incoming.name)
        db.add(node)

    node.addresses = list(incoming.addresses)
    node.capabilities = list(incoming.capabilities)
    node.model = incoming.model
    node.gpu = incoming.gpu
    node.last_seen_at = datetime.now(UTC)
    if node.last_good_address not in node.addresses:
        node.last_good_address = None

    db.commit()
    db.refresh(node)
    return node


def url_for(address: str) -> str:
    """The endpoint on a node, from an address it advertised."""
    return f"http://{address}:{INFERENCE_PORT}{INFERENCE_PATH}"


def _answers(url: str) -> bool:
    """Whether something at this URL is serving models right now."""
    try:
        response = httpx.get(f"{url}/models", timeout=PROBE_TIMEOUT_SECONDS)
    except httpx.HTTPError:
        return False
    return response.status_code == 200


def candidates(node: Node) -> list[str]:
    """Addresses to try, best first: the one that worked, then the rest in the
    order the node offered them."""
    ordered = [address for address in node.addresses if isinstance(address, str)]
    if node.last_good_address:
        ordered = [node.last_good_address] + [
            address for address in ordered if address != node.last_good_address
        ]
    return ordered


def known_nodes(db: Session) -> list[Node]:
    """Every registered node, for the page that lists them.

    A plain read with no probing: rendering a page must not wait on a machine
    that is asleep, and `last_seen_at` already says whether one is likely to
    be there. Whether a node answers *right now* is the doctor's question, and
    it costs a network round trip to ask.
    """
    return list(db.execute(select(Node).order_by(Node.name)).scalars().all())


def inference_url(db: Session) -> str | None:
    """A node that will serve a model right now, or None.

    None is an ordinary answer, not a failure: a machine with no nodes serves
    its own inference or none at all, and the caller falls back to whatever
    `config.inference_base_url()` says. That fallback is what keeps a
    single-machine install working with no rows in this table at all.

    Probing costs one HTTP request in the common case, because the address that
    answered last is tried first and written back when it answers again.
    """
    nodes = db.execute(select(Node).order_by(Node.name)).scalars().all()
    for node in nodes:
        if INFERENCE not in (node.capabilities or []):
            continue
        for address in candidates(node):
            url = url_for(address)
            if not _answers(url):
                continue
            if node.last_good_address != address:
                node.last_good_address = address
                db.commit()
                logger.info("Node %s answers at %s.", node.name, address)
            return url
        logger.warning("Node %s did not answer on any of %s.", node.name, node.addresses)
    return None
