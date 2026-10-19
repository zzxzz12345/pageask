import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import json
import socket
import time

import httpx
import pytest
import uvicorn

from reader_backend.api import create_app
from reader_backend.config import Settings
from reader_backend.documents import Documents
from reader_backend.domain import PagePlan, PageSubmission
from reader_backend.storage import Store
from reader_backend.tools import Workspace
from test_reader import annotation, draft, TEXT


@asynccontextmanager
async def serve(app):
    sock=socket.socket()
    sock.bind(('127.0.0.1',0));sock.setblocking(False)
    port=sock.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(app,log_level='error',timeout_graceful_shutdown=1))
    task=asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(3):
            while not server.started:
                if task.done():await task
                await asyncio.sleep(.01)
        yield f'http://127.0.0.1:{port}'
    finally:
        server.should_exit=True
        await asyncio.wait_for(task,3)
        sock.close()


async def next_event(lines,kind=None):
    fields={}
    async with asyncio.timeout(3):
        async for line in lines:
            if not line:
                if 'data' in fields:
                    event={**fields,'data':json.loads(fields['data'])}
                    fields={}
                    if kind is None or event.get('event')==kind:return event
                continue
            if line.startswith(':'):continue
            name,_,value=line.partition(':')
            fields[name]=value.lstrip()
    raise AssertionError('Event stream ended before expected event')


def workspace(store):
    store.enqueue_window('book',55,'model','low')
    return Workspace(store,Documents(store),store.next_job())


def empty(page):
    return {'page':page,'annotations':[],'skip_reason':'本页重复此前论点，没有额外标注的必要'}


@pytest.fixture
def store(tmp_path):
    store=Store(tmp_path)
    store.save_book({'id':'book','title':'测试','kind':'txt','path':'unused','page_count':80},
                    [{'text':TEXT} for _ in range(80)])
    return store


def test_first_page_is_a_hard_gate_before_knowledge_creation(store):
    ws=workspace(store)
    tools={t.name:t for t in ws.toolset()}
    tools['knowledge_search'].invoke({'queries':['市场趋势']})
    with pytest.raises(ValueError,match='当前只允许'):
        tools['submit_reading_batch'].invoke({'knowledge_drafts':[draft().model_dump()],
                                             'submissions':[empty(55),empty(56)]})
    assert not store.list_knowledge('book')
    assert not store.page_result('book',55)
    tools['save_page_annotations'].invoke({'submissions':[empty(55)]})
    with pytest.raises(ValueError,match='当前只允许'):
        tools['save_page_annotations'].invoke({'submissions':[empty(56)]})
    assert ws.begin_stage()==[56,57]
    with pytest.raises(ValueError,match='最前面'):
        tools['save_page_annotations'].invoke({'submissions':[empty(57)]})
    cursor=store.event_cursor('book')
    tools['save_page_annotations'].invoke({'submissions':[empty(57),empty(56)]})
    assert [e['payload']['page'] for e in store.events_since('book',cursor) if e['kind']=='page_ready']==[56,57]
    assert ws.begin_stage()==[58,59]


def test_resume_publishes_first_missing_page_alone(store):
    ws=workspace(store)
    store.save_page('book',PageSubmission(**empty(55)),ws.job['id'],'model','low')
    recovered=Workspace(store,Documents(store),ws.job)
    assert recovered.stage_pages==[56]
    recovered.started=time.monotonic()
    recovered.submit_page(PageSubmission(**empty(56)))
    assert recovered.first_target_seconds is not None


def test_later_page_failure_keeps_earlier_page_and_event(store):
    ws=workspace(store);tools={t.name:t for t in ws.toolset()}
    tools['save_page_annotations'].invoke({'submissions':[empty(55)]})
    ws.begin_stage()
    invalid={**annotation('not-created').model_dump(exclude={'quote'}),'span_ids':['unknown-span']}
    cursor=store.event_cursor('book')
    result=tools['save_page_annotations'].invoke({'submissions':[empty(56),{'page':57,'annotations':[invalid]}]})
    assert result['pages'][0]['saved']
    assert 'error' in result['pages'][1]
    assert store.page_result('book',56) and store.page_result('book',57) is None
    assert [e['payload']['page'] for e in store.events_since('book',cursor) if e['kind']=='page_ready']==[56]


def test_page_event_is_committed_before_callback_and_idempotent(store):
    ws=workspace(store);observed=[]
    def notify(book_id):
        events=store.events_since(book_id,0)
        page_events=[e for e in events if e['kind']=='page_ready']
        if page_events:
            observed.append((store.page_result(book_id,55),page_events[-1]))
    store.on_change=notify
    ws.submit_page(PageSubmission(**empty(55)))
    assert len(observed)==1
    assert observed[0][0]['result']['page']==55
    assert observed[0][1]['payload']['page']==55
    cursor=store.event_cursor('book')
    ws.submit_page(PageSubmission(**empty(55)))
    assert store.event_cursor('book')==cursor
    assert store.events_since('other-book',0)==[]


async def test_sse_publishes_first_page_while_rest_waits_and_replays(store):
    release=asyncio.Event()
    async def runner(ws,settings):
        tools={t.name:t for t in ws.toolset()}
        tools['knowledge_search'].invoke({'queries':['市场趋势']})
        candidate=next(s for s in ws.spans[55].values() if s['text']==TEXT[:16])
        planned={**annotation('new:市场趋势').model_dump(exclude={'quote'}),'span_ids':[candidate['id']]}
        # Run the actual LangChain tool in its worker thread, as production does.
        await tools['submit_reading_batch'].ainvoke({'knowledge_drafts':[draft().model_dump()],
            'submissions':[{'page':55,'annotations':[planned]}]})
        await release.wait()
        while ws.remaining():
            group=ws.begin_stage()
            await tools['save_page_annotations'].ainvoke({'submissions':[empty(page) for page in group]})
        return {'test':True}

    app=create_app(replace(Settings(),data_dir=store.directory,api_key='test-key'),runner)
    try:
        async with serve(app) as url, httpx.AsyncClient(base_url=url,timeout=4) as client:
            async with client.stream('GET','/api/books/book/events') as response:
                assert response.headers['content-type'].startswith('text/event-stream')
                lines=response.aiter_lines()
                assert (await next_event(lines))['event']=='snapshot'
                await client.post('/api/books/book/progress',json={'page':55})
                first=await next_event(lines,'page_ready')
                assert first['data']['page']==55
                assert first['data']['page_result']['result']['annotations']
                current=(await client.get('/api/books/book/status?page=55')).json()
                assert current['jobs'][0]['status']=='running'
                assert store.page_result('book',56) is None
                first_cursor=first['id']
                release.set()
                async with asyncio.timeout(3):
                    while store.page_result('book',64) is None:await asyncio.sleep(.01)
            async with client.stream('GET','/api/books/book/events',headers={'Last-Event-ID':first_cursor}) as response:
                lines=response.aiter_lines()
                await next_event(lines,'snapshot')
                replay=await next_event(lines,'page_ready')
                assert replay['data']['page']==56
                assert int(replay['id'])>int(first_cursor)
            assert (await client.get('/api/books/book/events',headers={'Last-Event-ID':'invalid'})).status_code==400
    finally:
        release.set()
