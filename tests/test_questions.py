import asyncio
from dataclasses import replace
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from langchain.messages import AIMessageChunk, HumanMessage, SystemMessage

from reader_backend.api import create_app
from reader_backend.config import Settings
from reader_backend.questions import PassageQuestion, question_context, answer_question
from reader_backend.documents import Documents
from reader_backend.storage import Store
from reader_backend.agent import create_model
from test_progressive import serve


TEXT='观察先后发生的两件事，还不能单独证明因果关系。\n\n需要检查其他可能影响结果的因素。'


@pytest.fixture
def material(tmp_path):
    store=Store(tmp_path)
    documents=Documents(store)
    book=documents.ingest(TEXT.encode(),'阅读样例.txt')
    question=PassageQuestion(page=1,quote='观察先后发生的两件事，还不能单独证明因果关系。',question='为什么不能证明因果？')
    return store,documents,book,question


def decode_events(body):
    return [json.loads(line[6:]) for line in body.splitlines() if line.startswith('data: ')]


def test_question_validates_selection_and_hides_book_path(material):
    store,_,book,question=material
    _,context=question_context(store,book['id'],question)
    assert context['selected_passage']==question.quote
    assert 'path' not in context
    question_context(store,book['id'],question.model_copy(update={'quote':'观察先后\n发生的两件事'}))
    with pytest.raises(ValueError,match='不在当前页'):
        question_context(store,book['id'],question.model_copy(update={'quote':'不存在的文字'}))
    with pytest.raises(KeyError):
        question_context(store,book['id'],question.model_copy(update={'page':2}))


async def test_question_pairs_pdf_image_history_and_only_streams_visible_text(tmp_path,monkeypatch):
    import pymupdf
    store=Store(tmp_path);documents=Documents(store)
    with pymupdf.open() as pdf:
        page=pdf.new_page();page.insert_text((50,80),'E = mc2')
        book=documents.ingest(pdf.tobytes(),'formula.pdf')
    question=PassageQuestion(page=1,quote='E = mc2',question='这里的 c 是什么？',
                            history=[{'role':'user','content':'先解释公式'},{'role':'assistant','content':'这是质能关系。'}])
    _,context=question_context(store,book['id'],question)
    requests=[]
    class Model:
        async def astream(self,messages):
            requests.extend(messages)
            yield AIMessageChunk(content=[{'type':'reasoning','text':'不得显示的思考'},
                                           {'type':'text','text':'c 表示光速。'}])
            yield AIMessageChunk(content='公式含有平方项。')
    monkeypatch.setattr('reader_backend.questions.create_model',lambda *args,**kwargs:Model())
    events=[event async for event in answer_question(documents,replace(Settings(),api_key='test-key'),book,context,question)]
    body=''.join(events)
    assert '不得显示' not in body
    assert decode_events(body)[-1]['type']=='done'
    assert isinstance(requests[0],SystemMessage)
    assert requests[1].content[1]['image_url']['url'].startswith('data:image/png;base64,')
    assert requests[-1].content==question.question
    assert requests[-3].content=='先解释公式'


def test_ask_api_validation_and_sanitized_failure(material,monkeypatch):
    store,_,book,question=material
    class Model:
        async def astream(self,messages):
            raise RuntimeError('Do not leak test-secret or provider request body')
            yield
    monkeypatch.setattr('reader_backend.questions.create_model',lambda *args,**kwargs:Model())
    settings=replace(Settings(),data_dir=store.directory,api_key='test-secret')
    with TestClient(create_app(settings)) as client:
        url=f'/api/books/{book["id"]}/ask'
        payload=question.model_dump()
        response=client.post(url,json=payload)
        assert response.status_code==200
        assert 'text/event-stream' in response.headers['content-type']
        assert decode_events(response.text)[-1]['type']=='error'
        assert 'test-secret' not in response.text
        assert client.post(url,json={**payload,'quote':'根本不存在的文字'}).status_code==400
        assert client.post(url,json={**payload,'history':[{'role':'system','content':'bad'}]}).status_code==422
        assert client.post(url,json={**payload,'question':'   '}).status_code==422
        assert client.post(url,json=payload,headers={'Origin':'https://elsewhere.example'}).status_code==403
        assert client.post('/api/books/unknown/ask',json=payload).status_code==404
    with TestClient(create_app(replace(settings,api_key=''))) as client:
        assert client.post(url,json=payload).status_code==503


async def test_answer_arrives_before_completion_and_disconnect_cancels(material,monkeypatch):
    store,_,book,question=material
    released=asyncio.Event();cancelled=asyncio.Event()
    class Model:
        async def astream(self,messages):
            try:
                yield AIMessageChunk(content='先后发生不等于因果关系。')
                await released.wait()
                yield AIMessageChunk(content='还需要检查其他解释。')
            finally:cancelled.set()
    monkeypatch.setattr('reader_backend.questions.create_model',lambda *args,**kwargs:Model())
    app=create_app(replace(Settings(),data_dir=store.directory,api_key='test-key'))
    async with serve(app) as base, httpx.AsyncClient(base_url=base) as client:
        async with client.stream('POST',f'/api/books/{book["id"]}/ask',json=question.model_dump()) as response:
            assert response.status_code==200
            async with asyncio.timeout(3):
                async for line in response.aiter_lines():
                    if line.startswith('data: ') and json.loads(line[6:])['type']=='delta':
                        assert not released.is_set()
                        break
        await asyncio.wait_for(cancelled.wait(),3)
    assert not store.status(book['id'],1)['jobs']


async def test_full_highlight_history_reaches_responses_stream_in_order(material,monkeypatch):
    store,_,book,question=material
    settings=replace(Settings(),data_dir=store.directory,api_key='test-key',
                     model='deepseek/deepseek-v4.1-flash',effort='none')
    history=[{'role':'assistant' if i%2 else 'user','content':f'第{i}条历史内容',
              'status':'stopped' if i==1 else 'complete'} for i in range(26)]
    requests=[]
    def send(request):
        requests.append(json.loads(request.content))
        response={'id':'resp_history','object':'response','created_at':0,'status':'completed',
                  'model':settings.model,'output':[{'id':'msg_history','type':'message',
                    'role':'assistant','status':'completed','content':[{
                        'type':'output_text','text':'我会沿用第一轮的例子。','annotations':[]}]}],
                  'usage':{'input_tokens':100,'output_tokens':10,'total_tokens':110}}
        events=[{'type':'response.created','sequence_number':0,
                 'response':{**response,'status':'in_progress','output':[]}},
                {'type':'response.output_text.delta','sequence_number':1,'item_id':'msg_history',
                 'output_index':0,'content_index':0,'delta':'我会沿用第一轮的例子。'},
                {'type':'response.completed','sequence_number':2,'response':response}]
        body=''.join('event: '+event['type']+'\ndata: '+json.dumps(event,ensure_ascii=False)+'\n\n' for event in events)
        return httpx.Response(200,request=request,headers={'content-type':'text/event-stream'},content=body.encode())
    model=create_model(settings,settings.model,settings.effort,max_tokens=1800)
    transport=httpx.AsyncClient(transport=httpx.MockTransport(send))
    model.root_async_client._client=transport
    monkeypatch.setattr('reader_backend.questions.create_model',lambda *args,**kwargs:model)
    app=create_app(settings)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://testserver') as client:
            response=await client.post(f'/api/books/{book["id"]}/ask',json={**question.model_dump(),'history':history})
        assert response.status_code==200
        assert decode_events(response.text)[-1]['type']=='done'
        assert '我会沿用第一轮的例子。' in response.text
    finally:
        await transport.aclose()
    assert len(requests)==1
    wire=requests[0]
    assert wire['stream'] and wire['store'] is False
    assert 'previous_response_id' not in wire
    messages=[message for message in wire['input'] if message.get('role') in ('user','assistant')]
    assert len(messages)==len(history)+2  # Page material, the full thread, current question.
    def content(message):
        value=message['content']
        return value if isinstance(value,str) else ''.join(item.get('text','') for item in value)
    for original,forwarded in zip(history,messages[1:-1]):
        assert original['content'] in content(forwarded)
        assert original['role']==forwarded['role']
    assert '此前回答未完成' in content(messages[2])
    assert content(messages[-1])==question.question


async def test_context_limit_error_preserves_history_and_does_not_leak_provider_error(material,monkeypatch):
    store,documents,book,question=material
    class Model:
        async def astream(self,messages):
            raise RuntimeError('context_length_exceeded: test-secret and raw provider body')
            yield
    monkeypatch.setattr('reader_backend.questions.create_model',lambda *args,**kwargs:Model())
    _,context=question_context(store,book['id'],question)
    response=''.join([item async for item in answer_question(documents,Settings(),book,context,question)])
    assert '上下文上限' in response and '历史仍保留' in response
    assert 'test-secret' not in response
