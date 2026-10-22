"""LangGraph persistence scoped and fenced by the application run lease.

Framework messages, node outputs and interrupts are checkpointed here.
ko_agent_runs remains the transactional business ledger and API projection.
"""
import asyncio
import time
from langgraph.checkpoint.base import BaseCheckpointSaver, CheckpointTuple, WRITES_IDX_MAP
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from psycopg.types.json import Jsonb
from app.core.errors import AppError


def initialize_checkpoints(db):
    db.execute("""CREATE TABLE IF NOT EXISTS ko_agent_graph_checkpoints (
        run_id TEXT NOT NULL REFERENCES ko_agent_runs ON DELETE CASCADE,
        namespace TEXT NOT NULL, checkpoint_id TEXT NOT NULL, parent_id TEXT,
        encoding TEXT NOT NULL, checkpoint BYTEA NOT NULL, metadata JSONB NOT NULL,
        PRIMARY KEY(run_id,namespace,checkpoint_id));
        CREATE TABLE IF NOT EXISTS ko_agent_graph_writes (
        run_id TEXT NOT NULL REFERENCES ko_agent_runs ON DELETE CASCADE,
        namespace TEXT NOT NULL, checkpoint_id TEXT NOT NULL, task_id TEXT NOT NULL,
        idx INTEGER NOT NULL, channel TEXT NOT NULL, task_path TEXT NOT NULL,
        encoding TEXT NOT NULL, value BYTEA NOT NULL,
        PRIMARY KEY(run_id,namespace,checkpoint_id,task_id,idx));""")


class AgentCheckpointSaver(BaseCheckpointSaver):
    def __init__(self, repository, run_id, identity, token, *, namespace_prefix="create-agent-v1:"):
        allowed = [("langgraph.types", "Interrupt"),
                   *[("langchain_core.messages." + module, name) for module, name in
                     [("ai", "AIMessage"), ("human", "HumanMessage"), ("system", "SystemMessage"),
                      ("tool", "ToolMessage"), ("modifier", "RemoveMessage")]]]
        super().__init__(serde=JsonPlusSerializer(
            pickle_fallback=False, allowed_msgpack_modules=allowed,
            allowed_json_modules=[tuple(module.split(".")) + (name,) for module, name in allowed]))
        self.namespace_prefix = namespace_prefix
        self.repo, self.run_id, self.identity, self.token = repository, run_id, identity, token

    def _scope(self, db, config, *, write=False):
        scope = config["configurable"]
        if scope["thread_id"] != self.run_id:
            raise AppError("NOT_FOUND", "Run not found", 404)
        row = self.repo._row(db, self.run_id, self.identity, lock=write)
        if write and (row["lease_token"] != self.token or row["lease_until"] <= time.time()):
            raise AppError("LEASE_LOST", "Graph executor lease expired or was cancelled", 409)
        return self.namespace_prefix + scope.get("checkpoint_ns", "")

    def _config(self, namespace, checkpoint_id):
        return {"configurable": {"thread_id": self.run_id, "checkpoint_ns": namespace.removeprefix(self.namespace_prefix),
                                 "checkpoint_id": checkpoint_id}}

    def _tuple(self, db, row):
        writes = db.execute("""SELECT * FROM ko_agent_graph_writes
            WHERE run_id=%s AND namespace=%s AND checkpoint_id=%s
            ORDER BY task_path,task_id,idx""",
            (self.run_id, row["namespace"], row["checkpoint_id"])).fetchall()
        return CheckpointTuple(
            config=self._config(row["namespace"], row["checkpoint_id"]),
            checkpoint=self.serde.loads_typed((row["encoding"], bytes(row["checkpoint"]))),
            metadata=row["metadata"],
            parent_config=self._config(row["namespace"], row["parent_id"]) if row["parent_id"] else None,
            pending_writes=[(w["task_id"], w["channel"],
                             self.serde.loads_typed((w["encoding"], bytes(w["value"])))) for w in writes],
        )

    def get_tuple(self, config):
        with self.repo.connection() as db:
            namespace = self._scope(db, config)
            checkpoint_id = config["configurable"].get("checkpoint_id")
            row = db.execute("""SELECT * FROM ko_agent_graph_checkpoints
                WHERE run_id=%s AND namespace=%s""" +
                (" AND checkpoint_id=%s" if checkpoint_id else "") +
                " ORDER BY checkpoint_id DESC LIMIT 1",
                (self.run_id, namespace) + ((checkpoint_id,) if checkpoint_id else ())).fetchone()
            return self._tuple(db, row) if row else None

    def list(self, config, *, filter=None, before=None, limit=None):
        config = config or {"configurable": {"thread_id": self.run_id}}
        with self.repo.connection() as db:
            namespace = self._scope(db, config)
            query = "SELECT * FROM ko_agent_graph_checkpoints WHERE run_id=%s AND namespace=%s"
            args = [self.run_id, namespace]
            if filter:
                query += " AND metadata @> %s"
                args.append(Jsonb(filter))
            if before:
                query += " AND checkpoint_id<%s"
                args.append(before["configurable"]["checkpoint_id"])
            query += " ORDER BY checkpoint_id DESC"
            if limit is not None:
                query += " LIMIT %s"
                args.append(limit)
            return iter([self._tuple(db, row) for row in db.execute(query, args).fetchall()])

    def put(self, config, checkpoint, metadata, new_versions):
        encoding, value = self.serde.dumps_typed(checkpoint)
        with self.repo.connection() as db:
            namespace = self._scope(db, config, write=True)
            db.execute("""INSERT INTO ko_agent_graph_checkpoints VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (run_id,namespace,checkpoint_id) DO UPDATE
                SET encoding=EXCLUDED.encoding,checkpoint=EXCLUDED.checkpoint,metadata=EXCLUDED.metadata""",
                (self.run_id, namespace, checkpoint["id"], config["configurable"].get("checkpoint_id"),
                 encoding, value, Jsonb(metadata)))
        return self._config(namespace, checkpoint["id"])

    def put_writes(self, config, writes, task_id, task_path=""):
        with self.repo.connection() as db:
            namespace = self._scope(db, config, write=True)
            for index, (channel, value) in enumerate(writes):
                index = WRITES_IDX_MAP.get(channel, index)
                encoding, payload = self.serde.dumps_typed(value)
                conflict = ("DO UPDATE SET channel=EXCLUDED.channel,encoding=EXCLUDED.encoding,"
                            "value=EXCLUDED.value,task_path=EXCLUDED.task_path" if index < 0 else "DO NOTHING")
                db.execute("""INSERT INTO ko_agent_graph_writes VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (run_id,namespace,checkpoint_id,task_id,idx) """ + conflict,
                    (self.run_id, namespace, config["configurable"]["checkpoint_id"], task_id,
                     index, channel, task_path, encoding, payload))

    async def aget_tuple(self, config):
        return await asyncio.to_thread(self.get_tuple, config)

    async def alist(self, config, *, filter=None, before=None, limit=None):
        rows = await asyncio.to_thread(self.list, config, filter=filter, before=before, limit=limit)
        for row in rows:
            yield row

    async def aput(self, config, checkpoint, metadata, new_versions):
        return await asyncio.to_thread(self.put, config, checkpoint, metadata, new_versions)

    async def aput_writes(self, config, writes, task_id, task_path=""):
        await asyncio.to_thread(self.put_writes, config, writes, task_id, task_path)
