import asyncio
from dataclasses import replace

import httpx
import pytest
from langchain.messages import AIMessage

from reader_backend.agent import run_annotation
from reader_backend.api import create_app
from reader_backend.config import Settings
from reader_backend.documents import Documents
from reader_backend.domain import PageSubmission
from reader_backend.scheduler import Scheduler
from reader_backend.storage import JobSuperseded
from reader_backend.tools import Workspace
from test_progressive import empty, serve
from test_reader import store, draft, TEXT


def test_jump_inside_window_revokes_running_and_queued_claims_but_keeps_cache(store):
    store.enqueue_window('book',55,'model','low')
    old=store.next_job()
    store.save_page('book',PageSubmission(**empty(55)),old['id'],'model','low')
    for page in range(56,61):store.enqueue_window('book',page,'model','low')
    assert len([j for j in store.status('book',60)['jobs'] if j['status'] in ('queued','running')])==2
    jumped=store.enqueue_window('book',62,'model','low')
    assert jumped['restarted']
    assert jumped['new_pages']==list(range(62,72))
    assert len(jumped['superseded_jobs'])==2
    assert store.page_result('book',55)
    current=store.next_job()
    assert current['id']==jumped['job_id']
    assert Workspace(store,Documents(store),current).stage_pages==[62]
    with store.connection() as db:
        assert not db.execute('SELECT 1 FROM claims WHERE job_id=?',(old['id'],)).fetchone()
    store.recover_jobs()
    assert next(j for j in store.status('book',62)['jobs'] if j['id']==old['id'])['status']=='superseded'


def test_explicit_adjacent_jump_and_out_of_order_clicks(store):
    first=store.enqueue_window('book',55,'model','low',session_id='tab',sequence=1)
    store.next_job()
    latest=store.enqueue_window('book',56,'model','low',restart=True,session_id='tab',sequence=3)
    assert first['job_id'] in latest['superseded_jobs']
    assert latest['context_start']==56
    stale=store.enqueue_window('book',110,'model','low',restart=True,session_id='tab',sequence=2)
    assert stale['stale']
    assert not stale['superseded_jobs']
    assert store.next_job()['id']==latest['job_id']
    with store.connection() as db:
        assert db.execute('SELECT current_page FROM reading WHERE book_id=?',('book',)).fetchone()[0]==56


def test_cancelled_tools_and_late_completion_cannot_mutate_any_results(store):
    store.enqueue_window('book',55,'model','low')
    old=store.next_job();ws=Workspace(store,Documents(store),old)
    tools={t.name:t for t in ws.toolset()}
    tools['knowledge_search'].invoke({'queries':['市场趋势']})
    store.enqueue_window('book',58,'model','low')
    cursor=store.event_cursor('book')
    for write in [
        lambda:tools['submit_reading_batch'].invoke({'knowledge_drafts':[draft().model_dump()],'submissions':[empty(55)]}),
        lambda:store.save_page('book',PageSubmission(**empty(55)),old['id'],'model','low'),
        lambda:store.resolve_knowledge(draft(),'book',old['id']),
        lambda:store.link_knowledge('missing','book',55,TEXT[:16],'旧关联不应保存',old['id']),
    ]:
        with pytest.raises(JobSuperseded):write()
    store.update_job_metrics(old['id'],{'phase':'drafting'})
    store.finish_job(old['id'],metrics={'late':True})
    assert store.event_cursor('book')==cursor
    assert not store.page_result('book',55)
    assert not store.list_knowledge('book')
    assert next(j for j in store.status('book',58)['jobs'] if j['id']==old['id'])['status']=='superseded'


async def test_http_jump_cancels_pending_model_request_and_starts_latest_page(store,monkeypatch):
    entered=asyncio.Event();cancelled=asyncio.Event();latest_started=asyncio.Event()
    class Model:
        def bind_tools(self,tools):return self
        async def ainvoke(self,messages):
            entered.set()
            try:await asyncio.Event().wait()
            finally:cancelled.set()
            return AIMessage(content='')
    monkeypatch.setattr('reader_backend.agent.create_model',lambda *args:Model())
    monkeypatch.setattr(Documents,'image',lambda *args:b'page-image')
    starts=[]
    async def runner(ws,settings):
        starts.append(min(ws.targets))
        if min(ws.targets)==55:return await run_annotation(ws,settings)
        latest_started.set()
        ws.submit_page(PageSubmission(**empty(min(ws.targets))))
        await asyncio.Event().wait()
    app=create_app(replace(Settings(),data_dir=store.directory,api_key='test-key'),runner)
    async with serve(app) as url,httpx.AsyncClient(base_url=url,timeout=3) as client:
        first=(await client.post('/api/books/book/progress',json={'page':55})).json()
        await asyncio.wait_for(entered.wait(),2)
        jumped=(await client.post('/api/books/book/progress',json={'page':58})).json()
        await asyncio.wait_for(cancelled.wait(),1)
        await asyncio.wait_for(latest_started.wait(),1)
        assert starts==[55,58]
        assert first['job_id'] in jumped['superseded_jobs']
        assert store.page_result('book',58)
        assert not store.page_result('book',55)
        invalid=await client.post('/api/books/book/progress',json={'page':60,'session_id':'no-sequence'})
        assert invalid.status_code==422


async def test_stubborn_cancelled_runner_cannot_block_new_location(store):
    old_started=asyncio.Event();release_old=asyncio.Event();new_started=asyncio.Event();old_done=asyncio.Event()
    async def runner(ws,settings):
        if 55 in ws.targets:
            old_started.set()
            try:await asyncio.Event().wait()
            except asyncio.CancelledError:await release_old.wait()
            with pytest.raises(JobSuperseded):ws.submit_page(PageSubmission(**empty(55)))
            old_done.set()
        else:
            ws.submit_page(PageSubmission(**empty(min(ws.targets))))
            new_started.set()
        return {}
    scheduler=Scheduler(store,Documents(store),replace(Settings(),api_key='test-key'),runner)
    scheduler.start()
    try:
        store.enqueue_window('book',55,'model','low');scheduler.wake()
        await asyncio.wait_for(old_started.wait(),1)
        jump=store.enqueue_window('book',58,'model','low');scheduler.wake(jump['superseded_jobs'])
        await asyncio.wait_for(new_started.wait(),1)
        release_old.set()
        await asyncio.wait_for(old_done.wait(),1)
        assert store.page_result('book',58)
        assert not store.page_result('book',55)
    finally:
        release_old.set()
        await scheduler.close()


def test_switch_book_cancels_previous_book_and_preserves_existing_pages(store):
    store.enqueue_window('book',55,'model','low');old=store.next_job()
    store.save_book({'id':'second','title':'第二本','kind':'txt','path':'unused','page_count':2},[{'text':TEXT}]*2)
    switched=store.enqueue_window('second',1,'model','low')
    assert old['id'] in switched['superseded_jobs']
    assert store.next_job()['book_id']=='second'


def test_canonical_knowledge_search_is_supplemented_without_another_model_round(store):
    store.enqueue_window('book',55,'model','low')
    tools={t.name:t for t in Workspace(store,Documents(store),store.next_job()).toolset()}
    tools['knowledge_search'].invoke({'queries':['趋势']})
    result=tools['knowledge_resolve'].invoke({'drafts':[draft().model_dump()]})[0]
    assert result['id'].startswith('k_')
    same=tools['knowledge_resolve'].invoke({'drafts':[draft().model_dump()]})[0]
    assert same['id']==result['id'] and same['reused']
