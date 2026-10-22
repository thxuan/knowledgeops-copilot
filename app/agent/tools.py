"""Server-bound LangChain tools; ticket submission remains transactional."""
from decimal import Decimal
from langchain_core.tools import StructuredTool
from langchain.tools import ToolRuntime
from app.agent.calculator import CalculatorArguments, calculate
from app.agent.contracts import TOOLS
from app.core.errors import AppError


def agent_tools(repository, identity, retrieval, run_id):
    async def calculator(left, operator, right):
        return {"value": calculate(CalculatorArguments(
            left=Decimal(str(left)), right=Decimal(str(right)), operator=operator))}

    async def query_business_data(query, ticket_id=None):
        import asyncio
        return await asyncio.to_thread(repository.business, identity, query,
                                       **({"ticket_id": ticket_id} if ticket_id else {}))

    async def search_knowledge_base(query, top_k=3):
        if retrieval is None:
            raise AppError("RETRIEVAL_NOT_CONFIGURED", "Retrieval unavailable", 503)
        found = await retrieval.retrieve(query, identity, top_k=top_k)
        return {"untrusted_evidence": found["hits"], "duration_ms": found["duration_ms"], "top_k": top_k}

    async def create_ticket(title, description, runtime: ToolRuntime):
        # The confirmation endpoint already committed the transaction. This tool
        # returns that receipt; a model/tool invocation cannot create a ticket.
        import asyncio
        state = await asyncio.to_thread(repository.get, run_id, identity)
        receipt = next((t for t in state["trace"] if t["name"] == "create_ticket"
                        and t["call_id"] == runtime.tool_call_id and t["status"] == "committed"), None)
        if not receipt or receipt["arguments"] != {"title": title, "description": description}:
            raise AppError("APPROVAL_MISMATCH", "Tool requires the exact server-approved receipt", 409)
        return receipt["result"]

    return {fn.__name__: StructuredTool.from_function(
        coroutine=fn, name=fn.__name__, args_schema=TOOLS[fn.__name__][0],
        description=TOOLS[fn.__name__][1])
        for fn in (calculator, query_business_data, search_knowledge_base, create_ticket)}
