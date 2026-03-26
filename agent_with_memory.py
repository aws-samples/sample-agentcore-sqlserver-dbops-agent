"""Agent with AgentCore Memory integration.

This version adds cross-session memory so the agent retains findings
from previous investigations. Replace agent.py with this file after
creating a memory resource (see README).
"""

from strands import Agent, tool
from strands.models import BedrockModel
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from bedrock_agentcore.memory import AgentCoreMemorySessionManager
import boto3
import pymssql
import json
import os
from datetime import datetime, timedelta

app = BedrockAgentCoreApp()

model = BedrockModel(
    model_id=os.getenv("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-20250514-v1:0"),
    region_name=os.getenv("AWS_REGION", "us-west-2"),
    temperature=0.3,
)

memory_manager = AgentCoreMemorySessionManager(
    memory_id=os.getenv("AGENTCORE_MEMORY_ID"),
    region=os.getenv("AWS_REGION", "us-west-2"),
)


def get_db_connection():
    client = boto3.client(
        "secretsmanager", region_name=os.getenv("AWS_REGION", "us-west-2")
    )
    secret = client.get_secret_value(SecretId=os.getenv("DB_SECRET_ID"))
    creds = json.loads(secret["SecretString"])
    return pymssql.connect(
        server=creds["host"],
        user=creds["username"],
        password=creds["password"],
        port=creds["port"],
    )


# Import tools from agent.py — keep a single source of truth
from agent import (
    get_deadlock_graphs,
    get_blocking_chains,
    get_session_details,
    get_blocked_process_reports,
    send_diagnostic_report,
)

agent = Agent(
    system_prompt="""You are a SQL Server DBOps Agent for Amazon RDS with persistent memory.

When investigating issues:
1. Check memory for prior findings on the same tables or sessions before running tools
2. Start with the symptoms — don't run every tool on every question
3. Use the deadlock tool when applications report error 1205 or transaction failures
4. Use blocking chain tools when applications report timeouts or lock waits
5. Use blocked process reports for historical blocking analysis when current blocking has resolved
6. Correlate findings across data sources and past investigations before giving your assessment
7. Provide severity (Critical/Warning/Info) and specific, actionable recommendations
8. When sending diagnostic reports via SNS, include: affected session IDs, SQL statements,
   wait types, duration, host/IP, login name, root cause, and specific remediation steps""",
    model=model,
    tools=[
        get_deadlock_graphs,
        get_blocking_chains,
        get_session_details,
        get_blocked_process_reports,
        send_diagnostic_report,
    ],
    session_manager=memory_manager,
)


@app.entrypoint
def handler(payload):
    response = agent(payload.get("prompt", ""))
    return response.message["content"][0]["text"]


if __name__ == "__main__":
    app.run()
