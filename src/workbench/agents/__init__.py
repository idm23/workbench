"""Driving an agent, behind one seam.

The public surface is the protocol types, the prompts, and `get_backend`.
Importing this package deliberately does not import any backend implementation
— and so does not import any agent SDK — because the web process resolves
backend names far more often than it runs one. Only `workbench.agents.claude`,
reached through the registry at the moment a run actually starts, pulls an SDK
into the process.
"""

from workbench.agents.prompts import execute_prompt, plan_prompt, prompt_for
from workbench.agents.protocol import (
    AgentEvent,
    AgentFailed,
    AgentFinished,
    AgentOutcome,
    AgentRequest,
    AgentStream,
    AgentUnavailable,
    Backend,
)
from workbench.agents.registry import UnknownBackend, available_backends, get_backend

__all__ = [
    "AgentEvent",
    "AgentFailed",
    "AgentFinished",
    "AgentOutcome",
    "AgentRequest",
    "AgentStream",
    "AgentUnavailable",
    "Backend",
    "UnknownBackend",
    "available_backends",
    "execute_prompt",
    "get_backend",
    "plan_prompt",
    "prompt_for",
]
