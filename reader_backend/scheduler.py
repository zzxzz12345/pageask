"""Persistent queue with immediate preemption when the reader jumps."""
import asyncio
from .agent import run_annotation
from .tools import Workspace


class Scheduler:
    def __init__(self, store, documents, settings, runner=run_annotation):
        self.store = store
        self.documents = documents
        self.settings = settings
        self.runner = runner
        self.event = asyncio.Event()
        self.task = None
        self.active = None
        self.active_job = None
        self.retired = set()

    def start(self):
        self.store.recover_jobs()
        self.task = asyncio.create_task(self.work())
        self.event.set()

    def wake(self, superseded=()):
        if self.active and self.active_job in superseded and not self.active.cancelling():
            self.active.cancel()
        self.event.set()

    async def close(self):
        tasks={task for task in (self.task,self.active,*self.retired) if task}
        for task in tasks:
            task.cancel()
        if tasks:
            done,_=await asyncio.wait(tasks,timeout=1)
            for task in done:self.discard(task)

    def discard(self, task):
        self.retired.discard(task)
        if not task.cancelled():
            task.exception()

    def retire_active(self):
        # A stubborn worker/thread cannot hold up the new location. Database
        # guards reject its late writes even if remote cancellation is delayed.
        if not self.active.done() and not self.active.cancelling():
            self.active.cancel()
        self.retired.add(self.active)
        self.active.add_done_callback(self.discard)
        self.active=None
        self.active_job=None

    async def work(self):
        waiter=None
        try:
            while True:
                self.event.clear()
                if self.active and (self.active.done() or not self.store.job_active(self.active_job)):
                    self.retire_active()
                if not self.active and (job := self.store.next_job()):
                    self.active_job=job['id']
                    self.active=asyncio.create_task(self.run(job))
                waiter=asyncio.create_task(self.event.wait())
                pending={waiter}
                if self.active:pending.add(self.active)
                await asyncio.wait(pending,return_when=asyncio.FIRST_COMPLETED)
                waiter.cancel()
                await asyncio.gather(waiter,return_exceptions=True)
        finally:
            if waiter:
                waiter.cancel()

    async def run(self, job):
        try:
            workspace=Workspace(self.store,self.documents,job,self.settings.max_images)
            metrics=await self.runner(workspace,self.settings)
        except asyncio.CancelledError:
            # Shutdown resumes this job; a page jump already superseded it.
            raise
        except Exception as error:
            message=str(error)
            if self.settings.api_key:message=message.replace(self.settings.api_key,'[REDACTED]')
            self.store.finish_job(job['id'],message[:1500],getattr(error,'metrics',{}))
        else:
            self.store.finish_job(job['id'],metrics=metrics)
