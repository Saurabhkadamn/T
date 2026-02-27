import asyncio
from app.llm_factory import get_llm

async def main():
    llm = get_llm("query_analysis", temperature=0.2)
    response = await llm.ainvoke("Respond ONLY with valid JSON: {\"ok\": true}")
    print(response.content)

asyncio.run(main())