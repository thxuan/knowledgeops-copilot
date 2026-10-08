"""DB polling worker; leases recover crashes and claim tokens fence stale workers."""

import asyncio
import json
import logging

from app.db.ingestion import IngestionRepository
from app.ingestion.parser import ParseError, clean_document, parse_document

logger = logging.getLogger("knowledgeops")


class IngestionWorker:
    def __init__(self, repository: IngestionRepository, lease_seconds: float = 30,
                 max_attempts: int = 3, poll_seconds: float = 0.2):
        self.repository = repository
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts
        self.poll_seconds = poll_seconds

    def process_next(self) -> bool:
        task = self.repository.claim(self.lease_seconds, self.max_attempts)
        if task is None:
            return False
        try:
            parsed = parse_document(task["filename"], task["content"])
            if not self.repository.advance(task, "cleaning"):
                return True
            parsed = clean_document(parsed)
            self.repository.complete(task, parsed)
        except ParseError as exc:
            self.repository.fail(task, exc.code, str(exc))
        except Exception:
            # Exception text could contain file content; publish only a safe error.
            logger.error(json.dumps({"event": "ingestion.failed", "task_id": task["task_id"]}))
            self.repository.fail(task, "PROCESSING_ERROR", "Unexpected processing error")
        return True

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                processed = await asyncio.to_thread(self.process_next)
            except Exception:
                logger.error(json.dumps({"event": "ingestion.worker_error"}))
                processed = False
            if not processed:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self.poll_seconds)
                except TimeoutError:
                    pass

