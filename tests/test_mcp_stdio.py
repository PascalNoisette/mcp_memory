#!/usr/bin/env python3
"""
MCP stdio transport reproduction test.

Verifies that after the parameter-passing fix, the MCP server correctly
accepts individual tool arguments sent directly in the ``arguments`` object
(as MCP clients do) rather than wrapped in a ``params`` key.

Usage:
    python3 tests/test_mcp_stdio.py
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import time

# Ensure DATABASE_PATH points to the real database (with :rw mode for FTS)
os.environ.setdefault(
    "DATABASE_PATH",
    os.path.expanduser("~/.local/share/opencode/opencode.db") + ":rw",
)

# Import clean_db_path helper for sqlite3.connect() calls
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from base import _clean_db_path

SERVER_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "server.py",
)

PASS = 0
FAIL = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        msg = f"  [FAIL] {name}"
        if detail:
            msg += f" — {detail}"
        print(msg)


class MCPClient:
    """Simple MCP stdio client for testing."""

    def __init__(self, proc: subprocess.Popen):
        self.proc = proc
        self._next_id = 0

    def request(self, method: str, params: dict | None = None) -> dict | None:
        self._next_id += 1
        request = json.dumps({
            "jsonrpc": "2.0",
            "id": self._next_id,
            "method": method,
            "params": params or {},
        })
        self.proc.stdin.write((request + "\n").encode())
        self.proc.stdin.flush()
        time.sleep(0.5)
        raw = self.proc.stdout.readline()
        if not raw:
            return None
        return json.loads(raw.decode())

    def notification(self, method: str, params: dict | None = None) -> None:
        notification = json.dumps({
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
        })
        self.proc.stdin.write((notification + "\n").encode())
        self.proc.stdin.flush()
        time.sleep(0.3)

    def call_tool(self, name: str, arguments: dict) -> str:
        """Call a tool and return the text content as a string."""
        resp = self.request("tools/call", {"name": name, "arguments": arguments})
        if resp is None or "result" not in resp:
            return ""
        content = resp["result"].get("content", [])
        if isinstance(content, list) and content:
            return content[0].get("text", "")
        return ""


def count_messages_in_json(text: str) -> int | None:
    """Count actual message entries in JSON output."""
    try:
        data = json.loads(text)
        return len(data.get("messages", []))
    except (json.JSONDecodeError, TypeError):
        return None


def main() -> int:
    global PASS, FAIL

    env = os.environ.copy()
    proc = subprocess.Popen(
        [sys.executable, SERVER_PATH],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )

    # Give server time to start
    time.sleep(1)

    # Check if server started
    if proc.poll() is not None:
        stderr = proc.stderr.read().decode("utf-8", errors="replace")
        print(f"[ERROR] Server failed to start:\n{stderr}")
        return 1

    client = MCPClient(proc)

    try:
        # ── Step 1: Initialize ──────────────────────────────────────────
        print("\n=== Step 1: Initialize ===")
        resp = client.request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "stdio-test", "version": "1.0"},
        })
        check("Initialize succeeded", resp is not None and "result" in resp)

        client.notification("notifications/initialized", {})

        # ── Step 2: List tools ──────────────────────────────────────────
        print("\n=== Step 2: List tools ===")
        resp = client.request("tools/list")
        tools = resp.get("result", {}).get("tools", []) if resp else []
        tool_names = [t["name"] for t in tools]
        expected_tools = {"list_projects", "list_sessions", "read_messages", "recall_session"}
        check(
            "All 4 tools registered",
            expected_tools.issubset(set(tool_names)),
            f"Found: {tool_names}",
        )

        # ── Step 3: Find a session with many parts ──────────────────────
        print("\n=== Step 3: Discover session ===")
        conn = sqlite3.connect(_clean_db_path())
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("""
            SELECT s.id, s.title, COUNT(p.id) as part_count
            FROM session s
            JOIN part p ON p.session_id = s.id
            GROUP BY s.id
            ORDER BY part_count DESC
            LIMIT 1
        """)
        row = cur.fetchone()

        if not row:
            print("  [SKIP] No sessions with parts found!")
            conn.close()
            proc.stdin.close()
            proc.wait(timeout=5)
            return 0

        session_id = row["id"]
        total_parts = row["part_count"]
        print(f"  Session ID: {session_id}")
        print(f"  Total parts: {total_parts}")

        # ── Step 4: read_messages with default page_size=2 ──────────────
        print("\n=== Step 4: read_messages with page_size=2 (default) ===")
        text = client.call_tool("read_messages", {
            "session_id": session_id,
            "page_size": 2,
            "offset": 0,
            "response_format": "json",
        })
        count = count_messages_in_json(text)
        check(
            "Returns 2 messages with page_size=2",
            count == 2,
            f"Got {count} messages",
        )

        # ── Step 5: read_messages with page_size=10 ─────────────────────
        print("\n=== Step 5: read_messages with page_size=10 ===")
        text = client.call_tool("read_messages", {
            "session_id": session_id,
            "page_size": 10,
            "offset": 0,
            "response_format": "json",
        })
        count = count_messages_in_json(text)
        check(
            "Returns 10 messages with page_size=10",
            count == 10,
            f"Got {count} messages (expected 10)",
        )

        # ── Step 6: read_messages with page_size=50 ─────────────────────
        print("\n=== Step 6: read_messages with page_size=50 ===")
        text = client.call_tool("read_messages", {
            "session_id": session_id,
            "page_size": 50,
            "offset": 0,
            "response_format": "json",
        })
        count = count_messages_in_json(text)
        expected = min(50, total_parts)
        check(
            f"Returns {expected} messages with page_size=50 (total={total_parts})",
            count == expected,
            f"Got {count} messages",
        )

        # ── Step 7: recall_session with direct fields ───────────────────
        print("\n=== Step 7: recall_session with direct fields ===")
        text = client.call_tool("recall_session", {
            "keywords": "database",
            "limit": 5,
            "offset": 0,
            "response_format": "json",
        })
        try:
            data = json.loads(text)
            check(
                "recall_session returns matches list",
                isinstance(data.get("matches"), list),
                f"Got type: {type(data.get('matches'))}",
            )
            check(
                "recall_session has query info",
                "query" in data,
            )
        except json.JSONDecodeError:
            check("recall_session returns valid JSON", False, text[:200])

        # ── Step 8: list_sessions with direct fields ────────────────────
        print("\n=== Step 8: list_sessions with direct fields ===")
        cur.execute(
            "SELECT directory FROM session ORDER BY time_created DESC LIMIT 1"
        )
        top_dir = cur.fetchone()["directory"]
        cur.close()

        text = client.call_tool("list_sessions", {
            "directory": top_dir,
            "limit": 5,
            "offset": 0,
            "response_format": "json",
        })
        try:
            data = json.loads(text)
            check(
                "list_sessions returns session list",
                isinstance(data.get("sessions"), list),
            )
            check(
                "list_sessions returns correct count with limit=5",
                len(data.get("sessions", [])) == 5,
                f"Got {len(data.get('sessions', []))}",
            )
        except json.JSONDecodeError:
            check("list_sessions returns valid JSON", False, text[:200])

        # ── Step 9: list_projects with direct fields ────────────────────
        print("\n=== Step 9: list_projects with direct fields ===")
        text = client.call_tool("list_projects", {
            "response_format": "json",
        })
        try:
            data = json.loads(text)
            check(
                "list_projects returns project list",
                isinstance(data.get("projects"), list),
            )
        except json.JSONDecodeError:
            check("list_projects returns valid JSON", False, text[:200])

    finally:
        proc.stdin.close()
        proc.wait(timeout=5)

    print(f"\n{'='*60}")
    print(f"Results: {PASS} passed, {FAIL} failed out of {PASS+FAIL} checks")
    print(f"{'='*60}\n")

    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
