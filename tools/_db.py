"""数据库初始化"""

import sqlite3
from ._constants import DB_PATH


def init_db():
    with sqlite3.connect(DB_PATH) as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY, ts TEXT, op TEXT, detail TEXT,
                who TEXT DEFAULT 'AI'
            );
            CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL DEFAULT '新对话',
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                reasoning INTEGER NOT NULL DEFAULT 0,
                reasoning_effort TEXT NOT NULL DEFAULT 'medium'
            );
            CREATE TABLE IF NOT EXISTS conversation_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT,
                reasoning_content TEXT,
                tool_calls TEXT,
                tool_call_id TEXT,
                display_name TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_conv_msg_conv_id
                ON conversation_messages(conversation_id);
        """)