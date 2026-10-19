from contextlib import asynccontextmanager
import asyncio
from pathlib import Path
from urllib.parse import urlsplit
from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, Field, ConfigDict, model_validator
from .config import ROOT, Settings, MODELS, POLICY_VERSION
from .storage import Store
from .documents import Documents
from .scheduler import Scheduler
from .events import EventHub, stream_events
from .questions import PassageQuestion, question_context, answer_question


class ReadingProgress(BaseModel):
    model_config = ConfigDict(extra='forbid')
    page: int = Field(ge=1)
    restart: bool = False
    session_id: str | None = Field(default=None,min_length=1,max_length=64)
    sequence: int | None = Field(default=None,ge=1)

    @model_validator(mode='after')
    def ordered_request(self):
        if (self.session_id is None)!=(self.sequence is None):
            raise ValueError('阅读会话与请求序号必须同时提供')
        return self


def create_app(settings=None, runner=None):
    settings = settings or Settings()
    store = Store(settings.data_dir)
    documents = Documents(store)
    hub = EventHub()
    store.on_change = hub.notify
    scheduler = Scheduler(store, documents, settings, **({'runner': runner} if runner else {}))

    @asynccontextmanager
    async def lifespan(app):
        hub.start()
        scheduler.start()
        try:
            yield
        finally:
            await scheduler.close()

    app = FastAPI(title='页问 · PageAsk', lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store = store
    app.state.scheduler = scheduler
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)

    @app.middleware('http')
    async def same_origin(request: Request, call_next):
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            origin = request.headers.get('origin')
            if origin and urlsplit(origin).netloc != request.headers.get('host'):
                return JSONResponse({'detail': '只接受本阅读器的请求'}, status_code=403)
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        if request.url.path.startswith('/api/'):
            response.headers['Cache-Control'] = 'no-store'
        return response

    @app.exception_handler(KeyError)
    async def missing(request, error):
        return JSONResponse({'detail': '书籍或页码不存在'}, status_code=404)

    @app.exception_handler(ValueError)
    async def invalid(request, error):
        return JSONResponse({'detail': str(error)[:300]}, status_code=400)

    def metadata(book):
        return {key: book[key] for key in ('id', 'title', 'kind', 'page_count')}

    @app.get('/api/health')
    def health():
        return {'configured': bool(settings.api_key), 'model': settings.model, 'effort': settings.effort,
                'policy': POLICY_VERSION, 'batch_size': 10, 'prefetch_step': 5,
                'delivery':'first-page-then-two','updates':'sse','navigation':'preempt-latest',
                'model_api':'responses' if settings.model.startswith('deepseek/') and settings.effort=='none' else 'chat/completions'}

    @app.post('/api/books/sample')
    async def sample():
        source = ROOT/'打开量化投资的黑箱.pdf'
        if not source.is_file():
            source = ROOT/'examples/reading-practice.txt'
        book = await asyncio.to_thread(documents.ingest, source.read_bytes(), source.name)
        return metadata(book)

    @app.post('/api/books')
    async def upload(file: UploadFile):
        data = await file.read(20*1024*1024+1)
        await file.close()
        try:
            book = await asyncio.to_thread(documents.ingest, data, Path(file.filename or 'book.pdf').name)
        except (ValueError, KeyError):
            raise
        except Exception:
            raise HTTPException(400, '书籍解析失败，请检查文件是否完整、未加密') from None
        return metadata(book)

    def progress(book_id: str, progress: ReadingProgress):
        if not settings.api_key:
            raise HTTPException(503, '尚未配置模型密钥')
        if settings.model not in MODELS or settings.effort not in ('none','low','medium','high'):
            raise HTTPException(503, '模型配置无效')
        book = store.book(book_id)
        if progress.page > book['page_count']:
            raise HTTPException(400, '页码超出书籍范围')
        queued = store.enqueue_window(book_id, progress.page, settings.model, settings.effort,
                                      restart=progress.restart,session_id=progress.session_id,sequence=progress.sequence)
        return queued

    @app.post('/api/books/{book_id}/progress')
    async def progress_async(book_id: str, body: ReadingProgress):
        queued = progress(book_id, body)
        scheduler.wake(queued['superseded_jobs'])
        return queued

    @app.post('/api/books/{book_id}/ask')
    async def ask(book_id: str, body: PassageQuestion):
        if not settings.api_key:
            raise HTTPException(503, '尚未配置模型密钥')
        if settings.model not in MODELS or settings.effort not in ('none','low','medium','high'):
            raise HTTPException(503, '模型配置无效')
        book, material = question_context(store, book_id, body)
        return StreamingResponse(answer_question(documents, settings, book, material, body),
                                 media_type='text/event-stream',
                                 headers={'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no'})

    @app.get('/api/books/{book_id}/status')
    def status(book_id: str, page: int = 1):
        book = store.book(book_id)
        if not 1 <= page <= book['page_count']:
            raise HTTPException(400, '页码超出书籍范围')
        return store.status(book_id, page)

    @app.get('/api/books/{book_id}/knowledge')
    def knowledge(book_id: str):
        store.book(book_id)
        return {'items': store.list_knowledge(book_id)}

    @app.get('/api/books/{book_id}/events')
    async def events(book_id: str, request: Request):
        store.book(book_id)
        header=request.headers.get('last-event-id')
        try:
            cursor=int(header) if header is not None else None
            if cursor is not None and cursor<0:raise ValueError()
        except ValueError:
            raise HTTPException(400,'无效的事件游标') from None
        return StreamingResponse(stream_events(request,store,hub,book_id,cursor),
                                 media_type='text/event-stream',
                                 headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})

    @app.get('/api/knowledge/{knowledge_id}')
    def knowledge_detail(knowledge_id: str):
        return store.get_knowledge([knowledge_id])[0]

    @app.get('/')
    def index():
        return FileResponse(ROOT/'index.html', headers={'Cache-Control': 'no-cache'})

    # Explicit allowlist: never serve .env.local, source code, uploaded books or DB.
    def serve_asset(filename):
        def asset():
            if not (ROOT/filename).is_file():
                raise HTTPException(404, '本地文件未提供')
            return FileResponse(ROOT/filename, headers={'Cache-Control': 'no-cache'})
        return asset

    for filename in ('app.js', 'pdf-reader.js', 'ai-reader.js', 'ask-reader.js', 'content.js', 'styles.css', 'demo.html', '打开量化投资的黑箱.pdf', 'examples/reading-practice.txt'):
        app.add_api_route('/'+filename, serve_asset(filename), methods=['GET'])
    app.mount('/vendor', StaticFiles(directory=ROOT/'vendor'), name='vendor')
    app.mount('/assets', StaticFiles(directory=ROOT/'assets'), name='assets')
    return app


app = create_app()
