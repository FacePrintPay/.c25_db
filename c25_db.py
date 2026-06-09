#!/usr/bin/env python3
"""C25 SQLite Persistence - Sovereign, Offline-First, Queryable"""

import sqlite3, json, time, hashlib, os
from pathlib import Path
from typing import Optional, List, Dict, Any
from datetime import datetime
from contextlib import contextmanager

DB_PATH = os.path.expanduser("~/.c25_db/c25_sovereign.db")

@contextmanager
def get_db():
    """Context manager for database connections"""
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    _init_db(conn)
    try:
        yield conn
    finally:
        conn.close()

def _init_db(conn: sqlite3.Connection):
    """Initialize database schema"""
    conn.executescript("""
        -- Agent execution history
        CREATE TABLE IF NOT EXISTS agent_runs (
            id TEXT PRIMARY KEY,
            agent_name TEXT NOT NULL,
            task_type TEXT NOT NULL,
            prompt TEXT,
            response TEXT,
            status TEXT DEFAULT 'pending',
            started_at TEXT NOT NULL,
            completed_at TEXT,
            tokens_used INTEGER,
            latency_ms REAL,
            error TEXT,
            metadata TEXT,
            user_id TEXT
        );
        
        -- Agent-to-agent messages
        CREATE TABLE IF NOT EXISTS agent_messages (
            id TEXT PRIMARY KEY,
            from_agent TEXT NOT NULL,
            to_agent TEXT NOT NULL,
            message_type TEXT NOT NULL,
            content TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            created_at TEXT NOT NULL,
            delivered_at TEXT,
            read_at TEXT,
            metadata TEXT
        );
        
        -- User sessions & auth
        CREATE TABLE IF NOT EXISTS user_sessions (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            bio_hash TEXT,
            created_at TEXT NOT NULL,
            last_activity TEXT,
            expires_at TEXT,
            metadata TEXT
        );
        
        -- LLM request cache (for sovereignty audit)
        CREATE TABLE IF NOT EXISTS llm_cache (
            id TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            prompt_hash TEXT NOT NULL,
            response TEXT,
            tokens_used INTEGER,
            latency_ms REAL,
            cached_at TEXT NOT NULL,
            expires_at TEXT
        );
        
        -- Indexes for performance
        CREATE INDEX IF NOT EXISTS idx_agent_runs_user ON agent_runs(user_id, started_at DESC);
        CREATE INDEX IF NOT EXISTS idx_agent_messages_to ON agent_messages(to_agent, status, created_at);
        CREATE INDEX IF NOT EXISTS idx_llm_cache_hash ON llm_cache(prompt_hash, provider);
    """)
    conn.commit()

# Agent Runs
def save_agent_run(agent_name: str, task_type: str, prompt: str, response: str,
                  status: str = 'completed', tokens_used: int = None,
                  latency_ms: float = None, error: str = None,
                  metadata: Dict = None, user_id: str = None) -> str:
    """Save an agent execution to database"""
    run_id = hashlib.sha256(f"{agent_name}:{task_type}:{time.time()}".encode()).hexdigest()[:16]
    now = datetime.now().isoformat()
    
    with get_db() as conn:
        conn.execute("""
            INSERT INTO agent_runs 
            (id, agent_name, task_type, prompt, response, status, started_at, completed_at, 
             tokens_used, latency_ms, error, metadata, user_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            run_id, agent_name, task_type, prompt, response, status, now, 
            now if status != 'pending' else None,
            tokens_used, latency_ms, error,
            json.dumps(metadata) if metadata else None,
            user_id
        ))
        conn.commit()
    
    return run_id

def get_agent_history(user_id: Optional[str] = None, agent_name: Optional[str] = None,
                     limit: int = 50, offset: int = 0) -> List[Dict]:
    """Query agent execution history"""
    with get_db() as conn:
        query = "SELECT * FROM agent_runs WHERE 1=1"
        params = []
        if user_id:
            query += " AND user_id = ?"
            params.append(user_id)
        if agent_name:
            query += " AND agent_name = ?"
            params.append(agent_name)
        query += " ORDER BY started_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        
        rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

# Agent Messages (for agent-to-agent communication)
def send_agent_message(from_agent: str, to_agent: str, message_type: str,
                      content: str, metadata: Dict = None) -> str:
    """Send a message from one agent to another"""
    msg_id = hashlib.sha256(f"{from_agent}:{to_agent}:{time.time()}".encode()).hexdigest()[:16]
    now = datetime.now().isoformat()
    
    with get_db() as conn:
        conn.execute("""
            INSERT INTO agent_messages
            (id, from_agent, to_agent, message_type, content, status, created_at, metadata)
            VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
        """, (
            msg_id, from_agent, to_agent, message_type, content, now,
            json.dumps(metadata) if metadata else None
        ))
        conn.commit()
    
    return msg_id

def get_pending_messages(to_agent: str, limit: int = 20) -> List[Dict]:
    """Get pending messages for an agent"""
    with get_db() as conn:
        rows = conn.execute("""
            SELECT * FROM agent_messages 
            WHERE to_agent = ? AND status = 'pending'
            ORDER BY created_at ASC LIMIT ?
        """, (to_agent, limit)).fetchall()
        return [dict(row) for row in rows]

def mark_message_delivered(msg_id: str):
    """Mark a message as delivered"""
    with get_db() as conn:
        conn.execute("""
            UPDATE agent_messages 
            SET status = 'delivered', delivered_at = ?
            WHERE id = ?
        """, (datetime.now().isoformat(), msg_id))
        conn.commit()

def mark_message_read(msg_id: str):
    """Mark a message as read"""
    with get_db() as conn:
        conn.execute("""
            UPDATE agent_messages 
            SET status = 'read', read_at = ?
            WHERE id = ? AND status = 'delivered'
        """, (datetime.now().isoformat(), msg_id))
        conn.commit()

# User Sessions
def create_user_session(user_id: str, bio_hash: str, expires_hours: int = 24) -> str:
    """Create a new user session"""
    session_id = hashlib.sha256(f"{user_id}:{time.time()}".encode()).hexdigest()[:16]
    now = datetime.now().isoformat()
    expires = datetime.fromtimestamp(time.time() + expires_hours * 3600).isoformat()
    
    with get_db() as conn:
        conn.execute("""
            INSERT INTO user_sessions
            (id, user_id, bio_hash, created_at, last_activity, expires_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (session_id, user_id, bio_hash, now, now, expires))
        conn.commit()
    
    return session_id

def validate_session(session_id: str) -> Optional[Dict]:
    """Validate and refresh a user session"""
    with get_db() as conn:
        row = conn.execute("""
            SELECT * FROM user_sessions 
            WHERE id = ? AND expires_at > ?
        """, (session_id, datetime.now().isoformat())).fetchone()
        
        if row:
            # Refresh last_activity
            conn.execute("""
                UPDATE user_sessions SET last_activity = ? WHERE id = ?
            """, (datetime.now().isoformat(), session_id))
            conn.commit()
            return dict(row)
        return None

# LLM Cache
def cache_llm_response(provider: str, model: str, prompt_hash: str,
                      response: str, tokens_used: int, latency_ms: float,
                      cache_hours: int = 24) -> str:
    """Cache an LLM response"""
    cache_id = hashlib.sha256(f"{provider}:{model}:{prompt_hash}".encode()).hexdigest()[:16]
    now = datetime.now().isoformat()
    expires = datetime.fromtimestamp(time.time() + cache_hours * 3600).isoformat()
    
    with get_db() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO llm_cache
            (id, provider, model, prompt_hash, response, tokens_used, latency_ms, cached_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (cache_id, provider, model, prompt_hash, response, tokens_used, latency_ms, now, expires))
        conn.commit()
    
    return cache_id

def get_cached_llm_response(provider: str, model: str, prompt_hash: str) -> Optional[Dict]:
    """Retrieve a cached LLM response"""
    with get_db() as conn:
        row = conn.execute("""
            SELECT * FROM llm_cache 
            WHERE provider = ? AND model = ? AND prompt_hash = ? AND expires_at > ?
        """, (provider, model, prompt_hash, datetime.now().isoformat())).fetchone()
        return dict(row) if row else None

# Utility: Export history for TotalRecall
def export_history_to_json(user_id: Optional[str] = None, output_path: Optional[str] = None) -> str:
    """Export agent history to JSON for forensic audit"""
    history = get_agent_history(user_id=user_id, limit=10000)  # Large limit for export
    
    export_data = {
        "exported_at": datetime.now().isoformat(),
        "user_id": user_id,
        "total_runs": len(history),
        "runs": history
    }
    
    if output_path:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_text(json.dumps(export_data, indent=2))
        return output_path
    else:
        return json.dumps(export_data, indent=2)
