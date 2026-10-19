"""Committed SQLite events + thread-safe wakeups for SSE subscribers."""
import asyncio
import json
from contextlib import contextmanager


class EventHub:
    def __init__(self):
        self.loop = None
        self.listeners = {}

    def start(self):
        self.loop = asyncio.get_running_loop()

    def notify(self, book_id):
        # LangChain executes synchronous tools in worker threads.
        if self.loop and not self.loop.is_closed():
            try:
                self.loop.call_soon_threadsafe(self._wake, book_id)
            except RuntimeError:
                # Shutdown may close the loop after the check. The committed event
                # remains replayable; a notification failure must not undo a save.
                pass

    def _wake(self, book_id):
        for event in self.listeners.get(book_id, ()):
            event.set()

    @contextmanager
    def subscribe(self, book_id):
        event = asyncio.Event()
        self.listeners.setdefault(book_id, set()).add(event)
        try:
            yield event
        finally:
            self.listeners[book_id].discard(event)
            if not self.listeners[book_id]:
                del self.listeners[book_id]


def encode_event(kind, cursor, payload):
    return f'id: {cursor}\nevent: {kind}\ndata: '+json.dumps(payload,ensure_ascii=False,separators=(',', ':'))+'\n\n'


async def stream_events(request, store, hub, book_id, cursor):
    with hub.subscribe(book_id) as wake:
        latest = store.event_cursor(book_id)
        if cursor is None or cursor > latest:
            cursor = latest
        yield 'retry: 1500\n\n'+encode_event('snapshot',cursor,{'book_id':book_id})
        while not await request.is_disconnected():
            # Clear before querying: any commit between query and wait leaves wake set.
            wake.clear()
            events = store.events_since(book_id,cursor)
            if events:
                for event in events:
                    cursor = event['id']
                    payload = {'book_id':book_id,**event['payload']}
                    if event['kind']=='page_ready':
                        payload['page_result'] = store.page_result(book_id,payload['page'])
                    yield encode_event(event['kind'],cursor,payload)
                continue
            try:
                await asyncio.wait_for(wake.wait(),timeout=15)
            except TimeoutError:
                yield ': keep-alive\n\n'
