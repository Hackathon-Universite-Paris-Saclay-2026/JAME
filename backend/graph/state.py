"""Shared state definition for the Multi-Agent Software Factory.

The AgentState flows through the LangGraph pipeline. Each agent reads
from and writes to specific keys, and appends its reasoning trace so
the orchestrator can expose a full Plan / Act / Reason log.
"""
