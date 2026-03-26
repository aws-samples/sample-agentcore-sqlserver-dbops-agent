"""Agent with AgentCore Memory integration.

This version adds cross-session memory so the agent retains findings
from previous investigations. Replace agent.py with this file after
creating a memory resource (see README).
"""

from strands import Agent, tool
from strands.models import BedrockModel
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from bedrock_agentcore.memory import MemoryClient
from bedrock_agentcore.memory.integrations.strands.config import AgentCoreMemoryConfig, RetrievalConfig
from bedrock_agentcore.memory.integrations.strands.session_manager import AgentCoreMemorySessionManager
import os

app = BedrockAgentCoreApp()

AWS_REGION = os.getenv("AWS_REGION", "us-west-2")
MEMORY_ID = os.getenv("AGENTCORE_MEMORY_ID")

model = BedrockModel(
    model_id=os.getenv("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-20250514-v1:0"),
    region_name=AWS_REGION,
    temperature=0.3,
)

# Import tools from agent.py — keep a single source of truth
from agent import (
    get_deadlock_graphs,
    get_blocking_chains,
    get_session_details,
    get_blocked_process_reports,
    send_diagnostic_report,
)

SYSTEM_PROMPT = """You are a SQL Server DBOps Agent for Amazon RDS with persistent memory.

When investigating issues:
1. Check memory for prior findings on the same tables or sessions before running tools
2. Start with the symptoms — don't run every tool on every question
3. Use the deadlock tool when applications report error 1205 or transaction failures
4. Use blocking chain tools when applications report timeouts or lock waits
5. Use blocked process reports for historical blocking analysis when current blocking has resolved
6. Correlate findings across data sources and past investigations before giving your assessment
7. Provide severity (Critical/Warning/Info) and specific, actionable recommendations
8. When sending diagnostic reports via SNS, include: affected session IDs, SQL statements,
   wait types, duration, host/IP, login name, root cause, and specific remediation steps"""

TOOLS = [
    get_deadlock_graphs,
    get_blocking_chains,
    get_session_details,
    get_blocked_process_reports,
    send_diagnostic_report,
]


def build_session_manager(session_id="default", actor_id="dbops-agent"):
    """Build a memory session manager with semantic + summary retrieval strategies."""
    if not MEMORY_ID:
        return None

    # Dynamically fetch strategy IDs from the memory resource
    strategies = MemoryClient(region_name=AWS_REGION).get_memory_strategies(MEMORY_ID)
    strategy_map = {s['type']: s['strategyId'] for s in strategies}
    semantic_id = strategy_map.get('SEMANTIC', '')
    summary_id = strategy_map.get('SUMMARIZATION', '')

    config = AgentCoreMemoryConfig(
        memory_id=MEMORY_ID,
        session_id=session_id,
        actor_id=actor_id,
        retrieval_config={
            "/strategies/{memoryStrategyId}/actors/{actorId}/": RetrievalConfig(
                top_k=5, relevance_score=0.3, strategy_id=semantic_id
            ),
            "/strategies/{memoryStrategyId}/actors/{actorId}/sessions/{sessionId}/": RetrievalConfig(
                top_k=3, relevance_score=0.3, strategy_id=summary_id
            ),
        },
    )
    return AgentCoreMemorySessionManager(
        agentcore_memory_config=config,
        region_name=AWS_REGION,
    )


# Fallback agent for when memory is not configured
agent = Agent(system_prompt=SYSTEM_PROMPT, model=model, tools=TOOLS)


@app.entrypoint
def handler(payload, context=None):
    user_input = payload.get("prompt", "")
    session_id = getattr(context, 'session_id', None) or payload.get("session_id", "default")
    sm = build_session_manager(session_id=session_id)

    if sm:
        with sm:
            memory_agent = Agent(
                system_prompt=SYSTEM_PROMPT,
                model=model,
                tools=TOOLS,
                session_manager=sm,
            )
            response = memory_agent(user_input)
            return response.message["content"][0]["text"]
    else:
        response = agent(user_input)
        return response.message["content"][0]["text"]


if __name__ == "__main__":
    app.run()
