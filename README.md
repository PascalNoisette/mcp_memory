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
          "DATABASE_PATH": "/home/opencode/.local/share/opencode/opencode.db:rw",
          "SERVER_NAME":"memory"
        }
      }
  }
}
```

Environment variables:
*    DATABASE_PATH  (required)    Primary opencode DB path.
*    SERVER_NAME    (required)    Unique tag for this MCP instance (e.g. 'prod', 'dev').
*    FTS_DB_PATH    (optional)    Shared FTS5 index path, otherwise DATABASE_PATH is used with a default suffix



## Architecture

MCP read primary opencode database (via
``DATABASE_PATH``) and writes to a **shared** FTS index (via
``FTS_DB_PATH``).  The ``SERVER_NAME`` env var identifies this instance
so that rows in the shared FTS index are tagged and can be filtered.

    ┌────────────────────────────────────┐ 
    │  MCP instance                      │s
    │  SERVER_NAME=memory                │
    │                                    │
    │  DB:             (opencode.db)     │
    │                       │            │ 
    │  ┌────────┐           │            │
    │  │  part  │───────────┼────────────┤─────────────────────────────
    │  └────────┘           │            │                             │
    │  ┌────────┐           │            │                             │
    │  │session │────────── ┼            │                             │
    │  └────────┘                        │                             │
    └────────────────────────────────────┘                             │            
                                                                       │
    FTS Database created (opencode.db_fts.db)                          │
    ┌──────────────────────────────────────────────────────────────┐   │
    │  part_fts  (FTS5)  ← includes a "server" column for tagging  │ ◄─┘
    │  part_fts_meta (per-server watermarks)                       │
    └──────────────────────────────────────────────────────────────┘
