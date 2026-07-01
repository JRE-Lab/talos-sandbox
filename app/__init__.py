"""TALOS Sandbox — application package.

A judge-facing demo of the real TALOS multi-agent rollout system driven against a
**simulated fleet**. Default mode is replay (recorded real-agent runs, zero external
deps); optional mode is live (real OpenAI agents against the sim fleet, guardrailed).

The honesty rule is load-bearing: the fleet is simulated, the agents are real.
"""

__version__ = "1.0.0"
