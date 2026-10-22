"""Task admission and LangGraph lifecycle; business approval stays transactional."""
import asyncio
import logging
import os
import time
from pydantic import ValidationError
from langgraph.types import Command
from langsmith import tracing_context
from app.agent.contracts import dump, event
from langchain.agents import create_agent
from app.agent.chat_model import AgentChatModel
from app.agent.middleware import AgentPolicy
from app.agent.tools import agent_tools
from app.core.errors import AppError
from app.db.agent_checkpoints import AgentCheckpointSaver

class AgentService:
    def __init__(self, repository, model, settings, retrieval=None):
        self.repo, self.model, self.settings, self.retrieval = repository, model, settings, retrieval
        self.jobs = {}

    def launch(self, run_id, identity):
        if run_id not in self.jobs:
            if len(self.jobs) >= self.settings.concurrency:
                return  # Durable queued run; POST resume retries admission without creating a run.
            task = asyncio.create_task(self.execute(run_id, identity))
            self.jobs[run_id] = task
            def finished(task):
                self.jobs.pop(run_id, None)
                if not task.cancelled() and task.exception():
                    logging.getLogger("knowledgeops").error(dump({"event": "agent.worker_failed", "run_id": run_id,
                        "error_type": type(task.exception()).__name__}))
            task.add_done_callback(finished)

    async def shutdown(self):
        tasks = list(self.jobs.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def execute(self, run_id, identity):
        try:
            claimed = await asyncio.to_thread(self.repo.claim, run_id, identity)
        except AppError as exc:
            if exc.code in {"RUN_BUSY", "CONCURRENCY_LIMIT"}:
                return
            raise
        if not claimed:
            return
        state, token = claimed
        started, prior = time.monotonic(), state["elapsed_seconds"]
        execution = AgentPolicy(self, run_id, identity, token, started, prior)
        try:
            if state.get("policy", self.settings.policy) != self.settings.policy:
                raise AppError("POLICY_CHANGED", "Restore the original run model/budget configuration before execution", 409)
            saver = AgentCheckpointSaver(self.repo, run_id, identity, token)
            graph = create_agent(
                model=AgentChatModel(backend=self.model),
                tools=list(agent_tools(self.repo, identity, self.retrieval, run_id).values()),
                middleware=[execution], checkpointer=saver, name="knowledgeops.agent.v2")
            config = {"configurable": {"thread_id": run_id},
                      "recursion_limit": self.settings.max_steps * 6 + 24}
            enabled = os.getenv("KNOWLEDGEOPS_LANGSMITH_TRACING", "false").lower() == "true"
            async with asyncio.timeout(max(.001, self.settings.max_seconds - prior)):
                with tracing_context(enabled=enabled):
                    snapshot = await graph.aget_state(config)
                    execution.resume_interrupt = bool(snapshot.interrupts)
                    if snapshot.values:
                        inputs = Command(resume=True) if snapshot.interrupts else None
                    else:
                        inputs = {"messages": state["messages"]}
                    await graph.ainvoke(inputs, config, durability="sync")
        except asyncio.CancelledError:
            # Completed nodes/receipts survive shutdown; a paid request in flight
            # retains its conservative reservation and may be retried on resume.
            raise
        except Exception as exc:
            code = ("TIME_LIMIT" if isinstance(exc, TimeoutError) else
                    "INVALID_TOOL_ARGUMENTS" if isinstance(exc, ValidationError) else
                    exc.code if isinstance(exc, AppError) else "INVALID_MODEL_OUTPUT")
            if not isinstance(exc, (AppError, ValidationError, TimeoutError)):
                logging.getLogger("knowledgeops").exception(dump({"event": "agent.graph_failed", "run_id": run_id}))
            current = execution.state if execution.state is not None else await execution.load()
            if current["status"] == "running":
                current.update(status="failed", phase="done", outcome=code)
                event(current, "error", code=code)
                try:
                    await execution.save(current)
                except AppError as error:
                    if error.code != "LEASE_LOST":
                        raise
        finally:
            await asyncio.to_thread(self.repo.release_lease, run_id, identity, token,
                                    prior + time.monotonic() - started)
