"""POST SSE transport with bounded buffering and cancellation of upstream work."""
import asyncio
import json
import logging
import anyio
from fastapi.responses import StreamingResponse
from app.core.errors import AppError


def stream_response(request, operation):
    async def events():
        queue = asyncio.Queue(maxsize=32)
        app = request.app
        # Middleware releases its own counter after headers; hold another until EOF.
        app.state.management_active_requests += 1

        async def emit(kind, data):
            await queue.put((kind, data))

        request.state.chat_events = emit
        request.state.streaming_chat = True

        async def execute():
            try:
                result = await operation()
                await emit("result", result)
            except AppError as exc:
                await emit("error", {"code": exc.code, "message": exc.message})
            except Exception:
                logging.getLogger("knowledgeops").exception("Streaming chat failed")
                await emit("error", {"code": "STREAM_FAILED", "message": "生成未完成，请重试。"})
            finally:
                if not asyncio.current_task().cancelling():
                    await queue.put(None)

        worker = asyncio.create_task(execute())
        try:
            yield 'event: phase\ndata: {"stage":"accepted"}\n\n'
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), 10)
                except TimeoutError:
                    yield ": heartbeat\n\n"
                    continue
                if item is None:
                    break
                kind, data = item
                yield "event: " + kind + "\ndata: " + json.dumps(data, ensure_ascii=False) + "\n\n"
        finally:
            worker.cancel()
            with anyio.CancelScope(shield=True):
                await asyncio.gather(worker, return_exceptions=True)
            request.state.chat_events = None
            app.state.management_active_requests -= 1

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
