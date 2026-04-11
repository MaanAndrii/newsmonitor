import asyncio

import ingest
import analyzer
import notifier


async def run():
    await ingest.run()
    await analyzer.run()
    await notifier.run()


if __name__ == "__main__":
    asyncio.run(run())
