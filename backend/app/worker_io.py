"""Bound native work even when its asynchronous caller times out."""

import asyncio
from concurrent.futures import ThreadPoolExecutor

from anyio import to_thread

from app.source_storage import read_verified_source


class BlockingWorker:
    def __init__(self, name: str, capacity: int = 1):
        self.executor = ThreadPoolExecutor(max_workers=capacity, thread_name_prefix=name)
        self.slots = asyncio.Semaphore(capacity)
        self.writes: set[str] = set()

    async def run(self, function, *args, write_key: str | None = None):
        await self.slots.acquire()
        try:
            job = asyncio.get_running_loop().run_in_executor(self.executor, function, *args)
        except BaseException:
            self.slots.release()
            raise
        if write_key is not None:
            self.writes.add(write_key)

        def finished(future):
            self.slots.release()
            if write_key is not None:
                self.writes.discard(write_key)
            if not future.cancelled():
                future.exception()

        job.add_done_callback(finished)
        # The actual worker retains the slot until the native call returns.
        return await asyncio.shield(job)

    async def close(self):
        await to_thread.run_sync(self.executor.shutdown)


class SourceReadWorker(BlockingWorker):
    def __init__(self):
        super().__init__("extraction-source")

    async def read(self, storage, key: str, size: int, checksum: str) -> bytes:
        return await self.run(read_verified_source, storage, key, size, checksum)
