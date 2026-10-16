"""Request-scoped chat cancellation and bounded local admission."""
import asyncio
import time
from collections import defaultdict, deque
from app.core.errors import AppError

class ChatAdmission:
    def __init__(self, concurrency=4, rate=20):
        self.concurrency, self.rate, self.active = concurrency, rate, 0
        self.history = defaultdict(deque)

    async def run(self, request, identity, operation):
        key = (identity.tenant_id, identity.user_id)
        recent = self.history[key]
        now = time.monotonic()
        while recent and recent[0] < now-60:
            recent.popleft()
        if len(recent) >= self.rate or self.active >= self.concurrency:
            raise AppError('RATE_LIMIT', 'Chat capacity or request rate exceeded', 429)
        recent.append(now)
        self.active += 1
        work = asyncio.create_task(operation())
        if getattr(getattr(request, "state", None), "streaming_chat", False):
            # StreamingResponse owns disconnect receive; do not race it for ASGI messages.
            try:
                return await work
            finally:
                work.cancel()
                await asyncio.gather(work, return_exceptions=True)
                self.active -= 1
        async def disconnected():
            # Body is already parsed by FastAPI. Direct receive avoids is_disconnected's
            # internal CancelScope swallowing cancellation during immediate app errors.
            while True:
                message = await request.receive()
                if message["type"] == "http.disconnect":
                    return
        watcher = asyncio.create_task(disconnected())
        try:
            done, _ = await asyncio.wait({work,watcher}, return_when=asyncio.FIRST_COMPLETED)
            if work in done:
                return await work
            work.cancel()
            raise AppError('CLIENT_DISCONNECTED', 'Chat cancelled after disconnect', 499)
        finally:
            work.cancel()
            watcher.cancel()
            await asyncio.gather(work,watcher,return_exceptions=True)
            self.active -= 1
