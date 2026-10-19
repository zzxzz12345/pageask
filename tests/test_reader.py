import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import time
import httpx
import pytest
from fastapi.testclient import TestClient
from langchain.messages import AIMessage, HumanMessage, ToolMessage
from reader_backend.config import Settings, POLICY_VERSION
from reader_backend.domain import Annotation, PageSubmission, KnowledgeDraft, validate_page
from reader_backend.storage import Store, explicit_choice_references
from reader_backend.documents import Documents, recover_cid
from reader_backend.tools import Workspace
from reader_backend.scheduler import Scheduler
from reader_backend.agent import create_model
from reader_backend.agent import run_annotation
from reader_backend.api import create_app


TEXT = '这是一个有关市场趋势的核心论点。' + '其他补充内容不会改变原文的结论和前提。'*30


@pytest.fixture
def store(tmp_path):
    store=Store(tmp_path)
    store.save_book({'id':'book','title':'测试','kind':'txt','path':'unused','page_count':150},[{'text':TEXT} for _ in range(150)])
    return store


def draft(name='市场趋势',aliases=None):
    return KnowledgeDraft(name=name,aliases=aliases or [],definition='市场价格在一定时间窗口内沿着某个方向发生变化的现象。',
                          source_page=55,evidence_quote=TEXT[:16],why_related='本页用这一概念说明核心论点的适用前提',origin='source')


def annotation(knowledge_id,quote=TEXT[:16]):
    return Annotation(quote=quote,kind='core',rationale='这一论点决定了后续推理成立的条件',question='这一判断成立时最需要核查哪个前提？',
                      choices=['观察窗口与讨论对象是否一致','是否来自不同的观察窗口','是否可以忽略观察窗口'],correct=0,
                      explanation='原文将论点与特定条件相联系，因此必须核查条件而不能脱离上下文使用。',
                      followup='换一个观察窗口时，结论会怎样改变？',hint='先确认作者讨论的观察范围。',knowledge_ids=[knowledge_id],confidence=.9)


def test_highlight_grounding_density_overlap_and_empty():
    a=annotation('k')
    assert validate_page(PageSubmission(page=1,annotations=[a]),TEXT)
    with pytest.raises(ValueError,match='不存在'):
        validate_page(PageSubmission(page=1,annotations=[a.model_copy(update={'quote':'完全不在书里的虚构文字'})]),TEXT)
    with pytest.raises(ValueError,match='14%'):
        validate_page(PageSubmission(page=1,annotations=[a]),a.quote)
    with pytest.raises(ValueError,match='重叠'):
        validate_page(PageSubmission(page=1,annotations=[a,a]),TEXT)
    with pytest.raises(ValueError,match='具体说明'):
        validate_page(PageSubmission(page=1,annotations=[]),TEXT)
    assert validate_page(PageSubmission(page=1,annotations=[],skip_reason='本页只有题辞，没有新论点'),TEXT)==[]


def test_knowledge_aliases_atomic_reuse_no_definition_overwrite(store):
    def resolve(_):return store.resolve_knowledge(draft(aliases=['Trend']), 'book')
    with ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(resolve,range(8)))
    assert len({r['id'] for r in results})==1
    assert sum(not r['reused'] for r in results)==1
    changed=draft(name='ＴＲＥＮＤ').model_copy(update={'definition':'不应该覆盖的一个新定义，会被已有记录忽略。'})
    reused=store.resolve_knowledge(changed,'book')
    assert reused['definition']==draft().definition
    assert store.search_knowledge('趋势')[0]['id']==reused['id']
    assert len(store.list_knowledge('book'))==1
    store.resolve_knowledge(draft('另一个概念'),'book')
    with pytest.raises(ValueError,match='多个不同概念'):
        store.resolve_knowledge(draft(aliases=['另一个概念']),'book')


def test_prefetch_window_overlap_and_concurrent_claims(store):
    def enqueue(p):return store.enqueue_window('book',p,'model','none')
    initial=enqueue(55)
    assert initial['new_pages']==list(range(55,65))
    for page in range(56,60):assert enqueue(page)['new_pages']==[]
    ahead=enqueue(60)
    assert (ahead['context_start'],ahead['context_end'])==(60,69)
    assert ahead['new_pages']==list(range(65,70))
    for page in range(61,65):assert enqueue(page)['new_pages']==[]
    assert enqueue(65)['new_pages']==list(range(70,75))
    assert enqueue(56)['new_pages']==list(range(56,66))
    with ThreadPoolExecutor(max_workers=4) as pool:
        results=list(pool.map(enqueue,[110]*8))
    assert sum(len(r['new_pages']) for r in results)==10


def test_empty_results_persist_resume_only_missing_pages(store):
    store.enqueue_window('book',55,'model','none')
    job=store.next_job()
    store.save_page('book',PageSubmission(page=55,annotations=[],skip_reason='这一页没有新增论点需要标注'),job['id'],'model','none')
    store.recover_jobs()
    restored=Store(store.directory);resumed=restored.next_job()
    assert resumed['id']==job['id']
    assert 55 not in Workspace(restored,Documents(restored),resumed).remaining()
    restored.finish_job(job['id'],error='temporary failure')
    assert restored.enqueue_window('book',55,'model','none')['new_pages']==list(range(56,65))


def test_retry_visible_failed_page_behind_prefetch_watermark(store):
    first=store.enqueue_window('book',55,'model','none')
    store.enqueue_window('book',60,'model','none')
    store.finish_job(first['job_id'],error='failure')
    repair=store.enqueue_window('book',55,'model','none')
    assert repair['context_start']==55
    assert repair['new_pages']==list(range(55,65))


def test_policy_upgrade_releases_old_claims(store):
    queued=store.enqueue_window('book',55,'model','none')
    with store.connection() as db:db.execute("UPDATE claims SET policy='old-policy'")
    store.recover_jobs()
    assert store.status('book',55)['jobs'][0]['status']=='failed'
    assert store.enqueue_window('book',55,'model','none')['new_pages']==list(range(55,65))


def test_navigation_supersedes_unstarted_distant_jobs(store):
    old=store.enqueue_window('book',55,'model','none')
    current=store.enqueue_window('book',110,'model','none')
    assert store.next_job()['id']==current['job_id']
    assert any(j['id']==old['job_id'] and j['status']=='superseded' for j in store.status('book',110)['jobs'])
    assert store.enqueue_window('book',55,'model','none')['new_pages']==list(range(55,65))


def test_visible_missing_pages_run_before_ahead_prefetch(store):
    initial=store.enqueue_window('book',55,'model','none')
    for page in range(56,61):store.enqueue_window('book',page,'model','none')
    assert store.next_job()['id']==initial['job_id']


def test_scoped_tools_batch_validation_and_knowledge_lookup(store):
    store.enqueue_window('book',55,'model','none');job=store.next_job()
    workspace=Workspace(store,Documents(store),job)
    tools={t.name:t for t in workspace.toolset()}
    with pytest.raises(ValueError,match='10页'):
        tools['read_page_text'].invoke({'page':54})
    assert tools['knowledge_resolve'].invoke({'drafts':[draft().model_dump()]})[0].get('error')
    tools['knowledge_search'].invoke({'queries':['市场趋势']})
    resolved=tools['knowledge_resolve'].invoke({'drafts':[draft().model_dump()]})[0]
    assert 'id' in resolved
    a=annotation(resolved['id'])
    candidate=next(s for s in workspace.spans[55].values() if s['text']==a.quote)
    planned={**a.model_dump(exclude={'quote'}),'span_ids':[candidate['id']]}
    with pytest.raises(ValueError,match='当前只允许'):
        tools['save_page_annotations'].invoke({'submissions':[
            {'page':55,'annotations':[planned]}, {'page':54,'annotations':[planned]}]})
    result=tools['save_page_annotations'].invoke({'submissions':[{'page':55,'annotations':[planned]}]})
    assert result['pages'][0]['saved']
    assert store.page_result('book',54) is None
    assert any(s['page']==55 for s in store.list_knowledge('book')[0]['sources'])
    saved=store.page_result('book',55)['result']['annotations'][0]
    assert saved['choices'][saved['correct']]==a.choices[a.correct]
    tools['knowledge_link'].invoke({'knowledge_id':resolved['id'],'page':56,'evidence_quote':TEXT[:16],'why_related':'后续页面继续应用同一个概念'})
    assert len(store.list_knowledge('book'))==1
    assert store.list_knowledge('book')[0]['definition']==draft().definition


def test_reasoning_roundtrip_and_provider_effort_mapping():
    settings=replace(Settings(),api_key='test-key')
    model=create_model(settings,'deepseek/deepseek-v4.1-flash','low')
    result=model._create_chat_result({'choices':[{'message':{'role':'assistant','content':'','reasoning_content':'private trace',
        'tool_calls':[{'id':'a','type':'function','function':{'name':'read_page_text','arguments':'{"page":55}'}}]},'finish_reason':'tool_calls'}],
        'model':'deepseek/deepseek-v4.1-flash','usage':{'prompt_tokens':3,'completion_tokens':4,'total_tokens':7}})
    message=result.generations[0].message
    payload=model._get_request_payload([HumanMessage('book data'),message,ToolMessage('text',tool_call_id='a')])
    assert payload['messages'][1]['reasoning_content']=='private trace'
    assert payload['reasoning_effort']=='low'
    assert payload['extra_body']['thinking']['type']=='enabled'
    assert 'reasoning_effort' not in create_model(settings,'xiaomi/mimo-v2.6-flash','none')._get_request_payload([HumanMessage('test')])


async def test_no_thinking_reaches_responses_wire_with_none_and_full_tool_history():
    requests=[]
    async def send(request):
        body=json.loads(request.content);requests.append(body)
        assert request.url.path.endswith('/responses')
        assert body['reasoning']=={'effort':'none'}
        assert 'reasoning_effort' not in body and 'thinking' not in body
        assert body['store'] is False
        assert 'previous_response_id' not in body
        return httpx.Response(200,request=request,json={'id':'resp_test','object':'response','created_at':0,
            'status':'completed','model':'deepseek/deepseek-v4.1-flash',
            'output':[{'type':'function_call','name':'publish','call_id':'call_test','arguments':'{"page":55}','status':'completed','id':'fc_test'}],
            'usage':{'input_tokens':5,'output_tokens':2,'total_tokens':7,'output_tokens_details':{'reasoning_tokens':0}}})
    model=create_model(replace(Settings(),api_key='test-key'),'deepseek/deepseek-v4.1-flash','none')
    transport=httpx.AsyncClient(transport=httpx.MockTransport(send))
    model.root_async_client._client=transport
    chat=model.bind_tools([{'type':'function','function':{'name':'publish','description':'保存页面',
        'parameters':{'type':'object','properties':{'page':{'type':'integer'}},'required':['page']}}}])
    history=[HumanMessage('第55页文字及原图')]
    try:
        first=await chat.ainvoke(history)
        assert first.tool_calls[0]['args']=={'page':55}
        assert first.usage_metadata['output_token_details']['reasoning']==0
        await chat.ainvoke([*history,first,ToolMessage('已保存',tool_call_id='call_test'),HumanMessage('继续第56页')])
    finally:
        await transport.aclose()
    assert any(item.get('type')=='function_call_output' for item in requests[1]['input'])
    assert len(requests[1]['input'])>len(requests[0]['input'])


async def fake_runner(workspace,settings):
    for page in workspace.remaining():
        workspace.submit_page(PageSubmission(page=page,annotations=[],skip_reason='测试材料没有需要重复划线的论点'))
        await asyncio.sleep(.001)
    return {'test':True}


def test_network_hosts_are_detected_and_configurable(monkeypatch):
    from reader_backend.config import reader_allowed_hosts
    monkeypatch.setattr('reader_backend.config.socket.gethostname', lambda: 'reader.local')
    monkeypatch.setattr('reader_backend.config.socket.getaddrinfo', lambda *args: [
        (2, 1, 6, '', ('192.0.2.10', 0)),
        (2, 1, 6, '', ('192.0.2.10', 0)),
    ])
    monkeypatch.setenv('READER_ALLOWED_HOSTS', 'reader.example.com, reader.lan, reader.local,')
    hosts=reader_allowed_hosts()
    assert set(hosts)=={'127.0.0.1','localhost','testserver','reader','reader.local',
                        '192.0.2.10','reader.example.com','reader.lan'}
    assert len(hosts)==len(set(hosts))
    def unavailable(*args):
        raise OSError('Temporary hostname lookup failure')
    monkeypatch.setattr('reader_backend.config.socket.getaddrinfo', unavailable)
    assert {'127.0.0.1','reader.local','reader.example.com'} <= set(reader_allowed_hosts())


@pytest.mark.parametrize('host', ['reader.local','192.0.2.10','reader.example.com'])
def test_network_host_serves_reader_and_same_origin_api(tmp_path,host):
    allowed=('127.0.0.1','localhost','testserver','reader.local','192.0.2.10','reader.example.com')
    settings=replace(Settings(),data_dir=tmp_path,api_key='',allowed_hosts=allowed)
    app=create_app(settings,fake_runner)
    origin=f'http://{host}:8765'
    with TestClient(app,base_url=origin) as client:
        assert client.get('/').status_code==200
        assert client.get('/api/health').status_code==200
        response=client.post('/api/books',files={'file':('sample.txt',TEXT.encode())},
                             headers={'Origin':origin})
        assert response.status_code==200
        assert response.json()['page_count']==1
        assert client.post('/api/books',files={'file':('sample.txt',TEXT.encode())},
                           headers={'Origin':'https://elsewhere.example'}).status_code==403
        assert client.get('/api/health',headers={'Host':'elsewhere.example'}).status_code==400


def test_sample_works_without_private_book_and_missing_exports_are_404(tmp_path,monkeypatch):
    root=tmp_path/'checkout'
    for folder in ('examples','vendor','assets'):
        (root/folder).mkdir(parents=True,exist_ok=True)
    (root/'examples/reading-practice.txt').write_text('原创中文阅读样例\n\n'+TEXT)
    monkeypatch.setattr('reader_backend.api.ROOT',root)
    settings=replace(Settings(),data_dir=tmp_path/'data',api_key='')
    with TestClient(create_app(settings,fake_runner)) as client:
        sample=client.post('/api/books/sample')
        assert sample.status_code==200
        assert sample.json()['kind']=='txt'
        assert sample.json()['page_count']==1
        assert client.get('/examples/reading-practice.txt').text.startswith('原创中文')
        assert client.get('/打开量化投资的黑箱.pdf').status_code==404
        assert client.get('/demo.html').status_code==404


def test_api_queue_cache_security_and_input_validation(tmp_path):
    settings=replace(Settings(),data_dir=tmp_path,api_key='test-secret')
    app=create_app(settings,fake_runner)
    with TestClient(app) as client:
        for path in ['/.env.local','/.reader-data/reader.sqlite3','/reader_backend/config.py']:
            assert client.get(path).status_code==404
        assert client.get('/app.js?filename=.env.local').text.startswith('/* 页问')
        health=client.get('/api/health').json()
        assert 'test-secret' not in json.dumps(health)
        assert client.post('/api/books',files={'file':('sample.txt',TEXT.encode())},headers={'Origin':'https://elsewhere.example'}).status_code==403
        book=client.post('/api/books',files={'file':('sample.txt',TEXT.encode())}).json()
        assert 'path' not in book
        url='/api/books/'+book['id']
        assert client.post(url+'/progress',json={'page':2}).status_code==400
        assert client.post(url+'/progress',json={'page':1}).status_code==200
        deadline=time.monotonic()+2
        while time.monotonic()<deadline:
            status=client.get(url+'/status?page=1').json()
            if status['page']:break
            time.sleep(.01)
        assert status['page']['result']['annotations']==[]
        assert client.post(url+'/progress',json={'page':1}).json()['new_pages']==[]
        assert client.get('/api/books/unknown/status').status_code==404


def test_pdf_layout_math_positions_and_original_image(tmp_path):
    import pymupdf
    with pymupdf.open() as pdf:
        page=pdf.new_page();page.insert_text((50,80),'E = mc');page.insert_text((90,74),'2',fontsize=7)
        data=pdf.tobytes()
    documents=Documents(Store(tmp_path));book=documents.ingest(data,'formula.pdf')
    layout=documents.layout(book['id'],1)
    assert layout['fixed_layout']
    assert any(s['size']==7 for s in layout['spans'])
    assert all(0<=value<=1 for s in layout['spans'] for value in s['bbox'])
    assert documents.image(book['id'],1).startswith(b'\x89PNG')


def test_txt_frontend_utf16_boundaries_and_epub_spine_alignment(tmp_path):
    from reader_backend.config import ROOT
    documents=Documents(Store(tmp_path))
    book=documents.ingest(('甲'*1500+'\n\n'+'😀'*1000+'\n\n'+'乙'*1000).encode(),'emoji.txt')
    assert book['page_count']==2
    assert documents.store.page(book['id'],1)['text']=='甲'*1500
    epub=ROOT/'tmp/fixtures/中文阅读测试.epub'
    if epub.exists():
        imported=documents.ingest(epub.read_bytes(),epub.name)
        assert imported['page_count']>=1
        assert '<script' not in documents.store.page(imported['id'],1)['text']


def test_sample_cid_recovery_preserves_real_punctuation():
    assert recover_cid('䱯ቄ⌅')=='阿尔法'
    assert recover_cid('——“”‘’…')=='——“”‘’…'
    assert explicit_choice_references('选项二混淆了概念，第三项把条件删除。',['甲','乙','丙'])=='「乙」混淆了概念，「丙」把条件删除。'
    assert explicit_choice_references('选项1、3分别是误区',['甲','乙','丙'])=='「甲」与「丙」分别是误区'


async def test_initial_context_pairs_all_ten_texts_with_images(store, monkeypatch):
    store.enqueue_window('book',55,'deepseek/deepseek-v4.1-flash','none')
    job=store.next_job()
    class Images:
        def __init__(self):self.pages=[]
        def image(self,book_id,page):
            self.pages.append(page)
            return b'test-image'
    pictures=Images()
    workspace=Workspace(store,pictures,job)
    stages=[]
    class Model:
        def bind_tools(self,tools):
            names={tool.name for tool in tools}
            assert 'submit_reading_batch' in names
            assert not names & {'knowledge_resolve','save_page_annotations'}
            return self
        async def ainvoke(self,messages):
            content=messages[1].content
            assert sum(item['type']=='image_url' for item in content)==10
            assert all(f'"page": {page}' in content[0]['text'] for page in range(55,65))
            stages.append(workspace.stage_pages.copy())
            return AIMessage(content='',tool_calls=[{'id':'publish','name':'submit_reading_batch','args':{'knowledge_drafts':[],'submissions':[
                {'page':page,'annotations':[],'skip_reason':'本页只有重复论述，不需要增加划线'} for page in workspace.stage_pages]}}])
    monkeypatch.setattr('reader_backend.agent.create_model',lambda *args:Model())
    metrics=await run_annotation(workspace,replace(Settings(),api_key='test-key'))
    assert pictures.pages==list(range(55,65))
    assert metrics['supplied_page_images']==10
    assert metrics['completed_pages']==list(range(55,65))
    assert stages==[[55],[56,57],[58,59],[60,61],[62,63],[64]]


def test_search_then_combined_publish_resolves_temporary_references(store):
    store.enqueue_window('book',55,'model','none');job=store.next_job()
    workspace=Workspace(store,Documents(store),job)
    tools={tool.name:tool for tool in workspace.toolset()}
    tools['knowledge_search'].invoke({'queries':['市场趋势']})
    candidate=next(s for s in workspace.spans[55].values() if s['text']==TEXT[:16])
    a=annotation('new:市场趋势').model_dump(exclude={'quote'})
    result=tools['submit_reading_batch'].invoke({'knowledge_drafts':[draft().model_dump()],
        'submissions':[{'page':55,'annotations':[{**a,'span_ids':[candidate['id']]}]}]})
    assert result['pages'][0]['saved']
    assert result['knowledge_references']['new:市场趋势'].startswith('k_')
    assert store.page_result('book',55)['result']['annotations'][0]['quote']==TEXT[:16]


async def test_worker_recovers_running_job_without_rewriting_saved_page(store):
    store.enqueue_window('book',55,'model','none');old=store.next_job()
    store.save_page('book',PageSubmission(page=55,annotations=[],skip_reason='已经保存的页面不应重新调用模型'),old['id'],'model','none')
    scheduler=Scheduler(store,Documents(store),replace(Settings(),api_key='test-key'),runner=fake_runner)
    scheduler.start()
    deadline=time.monotonic()+2
    while time.monotonic()<deadline:
        if store.page_result('book',64):break
        await asyncio.sleep(.01)
    await scheduler.close()
    assert store.page_result('book',64)
    assert store.page_result('book',55)['result']['skip_reason']=='已经保存的页面不应重新调用模型'
