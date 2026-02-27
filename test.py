import asyncio
from tools.tavily_search import tavily_search

async def main():
    results = await tavily_search(
        query="Latest advances in large language models 2024",
        max_results=3,
    )

    print("Results:", len(results))
    for r in results:
        print("\n---")
        print("Title:", r["title"])
        print("URL:", r["url"])
        print("Score:", r["score"])
        print("Content length:", len(r["content"]))

asyncio.run(main())