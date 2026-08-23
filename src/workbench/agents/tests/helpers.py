"""Driving an async stream from a synchronous test.

`asyncio.run` rather than pytest-asyncio: one helper is cheaper than a plugin,
and every test here wants the same thing — the whole stream, as a list.
"""

import asyncio
from typing import Any

from workbench.agents.protocol import AgentStream


def drain(stream: AgentStream) -> list[Any]:
    """Everything a backend yields, in order."""

    async def collect() -> list[Any]:
        return [item async for item in stream]

    return asyncio.run(collect())
