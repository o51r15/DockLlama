"""Check now shows its place in line for the AI."""

import asyncio

from dockllama import ai_queue


def test_place_in_line():
    async def main():
        release = asyncio.Event()

        async def hold(kind, label="", cid=None):
            async with ai_queue.turn(kind, label, cid):
                await release.wait()

        async def check(cid):
            async with ai_queue.turn("check", container_id=cid):
                pass

        first = asyncio.create_task(hold("round"))
        await asyncio.sleep(0.01)
        a = asyncio.create_task(check(5))
        await asyncio.sleep(0.01)
        b = asyncio.create_task(check(6))
        await asyncio.sleep(0.01)
        assert ai_queue.position(5, round_left=12) == {"ahead": 12, "what": ["the check round (12 containers left)"]}
        assert ai_queue.position(6, round_left=12) == {"ahead": 13, "what": ["the check round (12 containers left)",
                                                                           "another Check now"]}
        assert ai_queue.position(7, round_left=12) is None  # not waiting
        release.set()
        await asyncio.gather(first, a, b)
        assert ai_queue.position(5, 0) is None and ai_queue.line == []

        release.clear()
        t = asyncio.create_task(hold("test", "gemma4:latest"))
        await asyncio.sleep(0.01)
        c = asyncio.create_task(check(5))
        await asyncio.sleep(0.01)
        assert ai_queue.position(5, 0) == {"ahead": 1, "what": ["a model test (gemma4:latest)"]}
        release.set()
        await asyncio.gather(t, c)

        # a Check now that's cancelled (page closed) leaves the line
        release.clear()
        t = asyncio.create_task(hold("summary"))
        await asyncio.sleep(0.01)
        c = asyncio.create_task(check(9))
        await asyncio.sleep(0.01)
        assert ai_queue.position(9, 0)["what"] == ["the daily summary"]
        c.cancel()
        await asyncio.sleep(0.01)
        assert ai_queue.position(9, 0) is None
        release.set()
        await t
        assert ai_queue.line == []
    asyncio.run(main())
