from strands import Agent, tool
from strands.models import BedrockModel
from bedrock_agentcore.runtime import BedrockAgentCoreApp
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


def get_db_connection():
    """Retrieve credentials from Secrets Manager and return a pymssql connection."""
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


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@tool
def get_deadlock_graphs(hours: int = 24) -> dict:
    """Read deadlock graphs from the system_health extended event session.
    Returns XML deadlock graphs with process lists, SQL statements, and lock
    details for both sides of each deadlock. Use this when applications report
    error 1205, or for periodic deadlock analysis."""
    conn = get_db_connection()
    cursor = conn.cursor(as_dict=True)
    cursor.execute(
        """
        SELECT
            CAST(event_data AS XML).value('(event/@timestamp)[1]', 'DATETIME2') AS event_time,
            CAST(event_data AS XML).value('(event/data[@name="xml_report"]/value)[1]',
                'NVARCHAR(MAX)') AS deadlock_graph
        FROM sys.fn_xe_file_target_read_file(
            'd:\\rdsdbdata\\log\\system_health*.xel', NULL, NULL, NULL)
        WHERE CAST(event_data AS XML).value('(event/@name)[1]', 'VARCHAR(100)')
              = 'xml_deadlock_report'
          AND CAST(event_data AS XML).value('(event/@timestamp)[1]', 'DATETIME2')
              > DATEADD(HOUR, -%s, GETUTCDATE())
        ORDER BY event_time DESC
        """,
        (hours,),
    )
    results = cursor.fetchall()
    conn.close()
    return {"deadlock_count": len(results), "deadlock_graphs": results}


@tool
def get_blocking_chains() -> dict:
    """Walk the current blocking chain hierarchy using sys.dm_exec_requests
    and sys.dm_exec_sql_text. Returns head blockers, their SQL, wait types,
    durations, and all downstream blocked sessions. Use this when applications
    report timeouts or hangs."""
    conn = get_db_connection()
    cursor = conn.cursor(as_dict=True)
    cursor.execute(
        """
        WITH BlockingChain AS (
            SELECT r.session_id, r.blocking_session_id, r.wait_type,
                   r.wait_time / 1000.0 AS wait_seconds, r.status, r.command,
                   t.text AS sql_text, DB_NAME(r.database_id) AS database_name,
                   0 AS chain_level
            FROM sys.dm_exec_requests r
            CROSS APPLY sys.dm_exec_sql_text(r.sql_handle) t
            WHERE r.blocking_session_id = 0
              AND r.session_id IN (
                  SELECT DISTINCT blocking_session_id FROM sys.dm_exec_requests
                  WHERE blocking_session_id != 0)
            UNION ALL
            SELECT r.session_id, r.blocking_session_id, r.wait_type,
                   r.wait_time / 1000.0, r.status, r.command, t.text,
                   DB_NAME(r.database_id), bc.chain_level + 1
            FROM sys.dm_exec_requests r
            CROSS APPLY sys.dm_exec_sql_text(r.sql_handle) t
            JOIN BlockingChain bc ON r.blocking_session_id = bc.session_id
        )
        SELECT session_id, blocking_session_id, wait_type, wait_seconds,
               status, command, sql_text, database_name, chain_level
        FROM BlockingChain ORDER BY chain_level, wait_seconds DESC
        """
    )
    results = cursor.fetchall()
    conn.close()
    head_blockers = [r for r in results if r["chain_level"] == 0]
    return {
        "total_blocked_sessions": len([r for r in results if r["chain_level"] > 0]),
        "head_blockers": len(head_blockers),
        "blocking_chains": results,
    }


@tool
def get_session_details(session_id: int) -> dict:
    """Get login name, host, program, and current SQL for a specific session.
    Use this after identifying a head blocker."""
    conn = get_db_connection()
    cursor = conn.cursor(as_dict=True)
    cursor.execute(
        """
        SELECT s.session_id, s.login_name, s.host_name, s.program_name,
               s.status, s.transaction_isolation_level,
               s.last_request_start_time, s.last_request_end_time,
               t.text AS current_sql
        FROM sys.dm_exec_sessions s
        OUTER APPLY sys.dm_exec_sql_text(s.most_recent_sql_handle) t
        WHERE s.session_id = %s
        """,
        (session_id,),
    )
    result = cursor.fetchone()
    conn.close()
    return {"session_id": session_id, "details": result}


@tool
def get_blocked_process_reports(hours: int = 24) -> dict:
    """Read blocked process reports from extended event file targets.
    Returns both blocker and blocked session details including SQL text,
    wait time, and lock resources. Requires a custom XE session capturing
    blocked_process_report. Use this for historical blocking analysis
    when you need to identify who was holding the lock."""
    conn = get_db_connection()
    cursor = conn.cursor(as_dict=True)
    cursor.execute(
        """
        SELECT
            CAST(event_data AS XML).value('(event/@timestamp)[1]', 'DATETIME2') AS event_time,
            CAST(event_data AS XML).value('(event/data[@name="duration"]/value)[1]', 'BIGINT') AS duration_ms,
            CAST(event_data AS XML).value('(event/data[@name="blocked_process"]/value)[1]',
                'NVARCHAR(MAX)') AS blocked_process_report
        FROM sys.fn_xe_file_target_read_file(
            'd:\\rdsdbdata\\log\\blocked*.xel', NULL, NULL, NULL)
        WHERE CAST(event_data AS XML).value('(event/@timestamp)[1]', 'DATETIME2')
              > DATEADD(HOUR, -%s, GETUTCDATE())
        ORDER BY event_time DESC
        """,
        (hours,),
    )
    results = cursor.fetchall()
    conn.close()
    return {"blocked_process_count": len(results), "reports": results}


@tool
def send_diagnostic_report(subject: str, report: str) -> dict:
    """Send a diagnostic report via SNS email to the DBA team. Use this after
    completing an investigation to deliver findings and recommendations."""
    sns = boto3.client("sns", region_name=os.getenv("AWS_REGION", "us-west-2"))
    topics = sns.list_topics()["Topics"]
    topic_arn = next(
        (
            t["TopicArn"]
            for t in topics
            if os.getenv("SNS_TOPIC_NAME") in t["TopicArn"]
        ),
        None,
    )
    if topic_arn:
        sns.publish(TopicArn=topic_arn, Subject=subject[:100], Message=report)
        return {"status": "sent", "topic": topic_arn}
    return {"status": "no_topic_found"}


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

agent = Agent(
    system_prompt="""You are a SQL Server DBOps Agent for Amazon RDS.

When investigating issues:
1. Start with the symptoms — don't run every tool on every question
2. Use the deadlock tool when applications report error 1205 or transaction failures
3. Use blocking chain tools when applications report timeouts or lock waits
4. Use blocked process reports for historical blocking analysis when current blocking has resolved
5. Correlate findings across data sources before giving your assessment
6. Provide severity (Critical/Warning/Info) and specific, actionable recommendations
7. When sending diagnostic reports via SNS, include: affected session IDs, SQL statements,
   wait types, duration, host/IP, login name, root cause, and specific remediation steps""",
    model=model,
    tools=[
        get_deadlock_graphs,
        get_blocking_chains,
        get_session_details,
        get_blocked_process_reports,
        send_diagnostic_report,
    ],
)


@app.entrypoint
def handler(payload):
    response = agent(payload.get("prompt", ""))
    return response.message["content"][0]["text"]


if __name__ == "__main__":
    app.run()
