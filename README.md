# MCP Memory — Open code / Kilo Code Session Recall

MCP server providing **read-only access** to Kilo Code and Opencode server conversations  stored in the local SQLite database. Query projects, list sessions, read conversation history, and search across all messages with keyword recall.

## Available Tools

| Tool | Description |
|---|---|
| `list_projects` | List all Kilo Code projects |
| `list_sessions` | List sessions within a project (paginated) |
| `read_messages` | Read conversation messages from a session (paginated) |
| `recall_session` | Full-text keyword search across all session messages |

## Quick start : MCP Client Configuration


```bash
mkdir -p /home/opencode/workspace/
cd /home/opencode/workspace/
git clone github.com/PascalNoisette/netpascal_mcp_memory
pip install -r requirements.txt
```


```json
{
  "mcp": {
    "servers": {
      "memory": {
        "type": "local",
        "enabled": true,
        "command": [
          "python3",
          "/home/opencode/workspace/netpascal_mcp_memory/server.py"
        ],
        "environment": {
          "DATABASE_PATH": "/home/opencode/.local/share/opencode/opencode.db:rw"
        }
      }
  }
}
```
