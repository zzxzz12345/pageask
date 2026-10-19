from contextlib import contextmanager
from pathlib import Path
import hashlib
import json
import random
import re
import sqlite3
import time
import uuid
from .domain import concept_key
from .config import POLICY_VERSION


class JobSuperseded(RuntimeError):
    """The reader moved away; this job no longer has write permission."""


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def explicit_choice_references(text,choices):
    """Keep explanations correct when display positions are shuffled."""
    ordinals={'一':0,'二':1,'三':2,'1':0,'2':1,'3':2,'A':0,'B':1,'C':2}
    text=re.sub(r'选项([一二三123ABC](?:、[一二三123ABC])+)',
                lambda m:'与'.join('「'+choices[ordinals[label]]+'」' for label in m[1].split('、')),text)
    text=re.sub(r'第([一二三123](?:、[一二三123])+)(?:个选项|项)',
                lambda m:'与'.join('「'+choices[ordinals[label]]+'」' for label in m[1].split('、')),text)
    def replace(match):
        label=next(group for group in match.groups() if group is not None)
        return '「'+choices[ordinals[label]]+'」'
    return re.sub(r'选项\s*([一二三123ABC])|第([一二三123])(?:个选项|项)|([ABC])\s*(?:选项|项)',replace,text)


class Store:
    def __init__(self, directory: Path):
        self.on_change = None
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / 'reader.sqlite3'
        with self.connection() as db:
            db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS books(id TEXT PRIMARY KEY, title TEXT, kind TEXT, path TEXT, page_count INTEGER);
            CREATE TABLE IF NOT EXISTS pages(book_id TEXT, page INTEGER, text TEXT, html TEXT, PRIMARY KEY(book_id,page));
            CREATE TABLE IF NOT EXISTS knowledge(id TEXT PRIMARY KEY, name TEXT, definition TEXT, origin TEXT, created REAL);
            CREATE TABLE IF NOT EXISTS knowledge_aliases(alias TEXT PRIMARY KEY, knowledge_id TEXT REFERENCES knowledge(id));
            CREATE TABLE IF NOT EXISTS knowledge_sources(knowledge_id TEXT, book_id TEXT, page INTEGER, evidence TEXT, why_related TEXT,
              UNIQUE(knowledge_id,book_id,page,evidence));
            CREATE TABLE IF NOT EXISTS page_results(book_id TEXT, page INTEGER, policy TEXT, result TEXT, model TEXT, effort TEXT, job_id TEXT,
              PRIMARY KEY(book_id,page,policy));
            CREATE TABLE IF NOT EXISTS reading(book_id TEXT PRIMARY KEY, anchor INTEGER, furthest INTEGER);
            CREATE TABLE IF NOT EXISTS reader_requests(session_id TEXT PRIMARY KEY,sequence INTEGER);
            CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,book_id TEXT,context_start INTEGER,context_end INTEGER,targets TEXT,
              status TEXT,model TEXT,effort TEXT,created REAL,started REAL,finished REAL,error TEXT,metrics TEXT);
            CREATE TABLE IF NOT EXISTS claims(book_id TEXT,page INTEGER,policy TEXT,job_id TEXT,PRIMARY KEY(book_id,page,policy));
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,book_id TEXT,kind TEXT,payload TEXT,created REAL);
            CREATE INDEX IF NOT EXISTS events_book_cursor ON events(book_id,id);
            CREATE TABLE IF NOT EXISTS question_prefixes(id TEXT PRIMARY KEY,content TEXT,created REAL);
            ''')
            if 'current_page' not in {row[1] for row in db.execute('PRAGMA table_info(reading)')}:
                db.execute('ALTER TABLE reading ADD COLUMN current_page INTEGER')

    @staticmethod
    def _event(db,book_id,kind,payload):
        db.execute('INSERT INTO events(book_id,kind,payload,created) VALUES(?,?,?,?)',
                   (book_id,kind,dumps(payload),time.time()))

    def _notify(self,book_id):
        if self.on_change:self.on_change(book_id)

    def event_cursor(self,book_id):
        with self.connection() as db:
            return db.execute('SELECT COALESCE(MAX(id),0) FROM events WHERE book_id=?',(book_id,)).fetchone()[0]

    def events_since(self,book_id,cursor,limit=100):
        with self.connection() as db:
            rows=db.execute('SELECT * FROM events WHERE book_id=? AND id>? ORDER BY id LIMIT ?',
                            (book_id,cursor,limit)).fetchall()
            return [{**dict(row),'payload':json.loads(row['payload'])} for row in rows]

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def book(self, book_id):
        with self.connection() as db:
            row = db.execute('SELECT * FROM books WHERE id=?', (book_id,)).fetchone()
            if not row:
                raise KeyError('书籍不存在')
            return dict(row)

    def save_book(self, metadata, pages):
        with self.connection() as db:
            db.execute('INSERT OR IGNORE INTO books VALUES(:id,:title,:kind,:path,:page_count)', metadata)
            db.executemany('INSERT OR REPLACE INTO pages VALUES(?,?,?,?)', [(metadata['id'],i+1,p['text'],p.get('html','')) for i,p in enumerate(pages)])

    def page(self, book_id, page):
        with self.connection() as db:
            row = db.execute('SELECT * FROM pages WHERE book_id=? AND page=?', (book_id,page)).fetchone()
            if not row:
                raise KeyError('页码不存在')
            return dict(row)

    def question_prefix(self, identifier):
        with self.connection() as db:
            row=db.execute('SELECT content FROM question_prefixes WHERE id=?',(identifier,)).fetchone()
            return json.loads(row[0]) if row else None

    def freeze_question_prefix(self, identifier, content):
        with self.connection() as db:
            db.execute('INSERT OR IGNORE INTO question_prefixes VALUES(?,?,?)',(identifier,dumps(content),time.time()))
            return json.loads(db.execute('SELECT content FROM question_prefixes WHERE id=?',(identifier,)).fetchone()[0])

    def search_knowledge(self, query, limit=8):
        normalized = concept_key(query)
        terms = {normalized, *(concept_key(term) for term in query.split())}
        with self.connection() as db:
            aliases = db.execute('SELECT alias,knowledge_id FROM knowledge_aliases').fetchall()
            scores = {}
            for alias, identifier in aliases:
                score = max((1 if term==alias else .8 if term and (term in alias or alias in term) else self._similarity(term,alias)) for term in terms)
                if score >= .3:
                    scores[identifier] = max(scores.get(identifier,0),score)
            ids = sorted(scores,key=scores.get,reverse=True)[:limit]
            return [self._knowledge(db,identifier) for identifier in ids]

    @staticmethod
    def _similarity(a,b):
        aa={a[i:i+2] for i in range(len(a)-1)};bb={b[i:i+2] for i in range(len(b)-1)}
        return len(aa&bb)/max(1,len(aa|bb))

    def _knowledge(self, db, identifier):
        row = db.execute('SELECT * FROM knowledge WHERE id=?',(identifier,)).fetchone()
        if not row:
            raise KeyError(f'知识点不存在：{identifier}')
        result = dict(row)
        result['aliases'] = [r[0] for r in db.execute('SELECT alias FROM knowledge_aliases WHERE knowledge_id=?',(identifier,))]
        result['sources'] = [dict(r) for r in db.execute('SELECT * FROM knowledge_sources WHERE knowledge_id=?',(identifier,))]
        return result

    def get_knowledge(self, identifiers):
        with self.connection() as db:
            return [self._knowledge(db, identifier) for identifier in identifiers]

    @staticmethod
    def _require_job(db, job_id, book_id=None):
        row=db.execute('SELECT status,book_id FROM jobs WHERE id=?',(job_id,)).fetchone()
        if not row or row['status']!='running' or (book_id and row['book_id']!=book_id):
            raise JobSuperseded('阅读位置已改变，旧批注任务已取消')

    def job_active(self, job_id):
        with self.connection() as db:
            row=db.execute('SELECT status FROM jobs WHERE id=?',(job_id,)).fetchone()
            return bool(row and row['status']=='running')

    def require_job(self, job_id):
        with self.connection() as db:self._require_job(db,job_id)

    def resolve_knowledge(self, draft, book_id, job_id=None):
        aliases = {concept_key(draft.name), *(concept_key(alias) for alias in draft.aliases)} - {''}
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            if job_id:self._require_job(db,job_id,book_id)
            existing = db.execute(f'SELECT DISTINCT knowledge_id FROM knowledge_aliases WHERE alias IN ({",".join("?" for _ in aliases)})',tuple(aliases)).fetchall()
            if len(existing)>1:
                raise ValueError('这些别名属于多个不同概念；不能强行合并，分别读取并选择正确ID')
            reused = bool(existing)
            identifier = existing[0][0] if reused else 'k_'+hashlib.sha256(concept_key(draft.name).encode()).hexdigest()[:20]
            if not reused:
                db.execute('INSERT INTO knowledge VALUES(?,?,?,?,?)',(identifier,draft.name,draft.definition,draft.origin,time.time()))
            for alias in aliases:
                db.execute('INSERT OR IGNORE INTO knowledge_aliases VALUES(?,?)',(alias,identifier))
            db.execute('INSERT OR IGNORE INTO knowledge_sources VALUES(?,?,?,?,?)',(identifier,book_id,draft.source_page,draft.evidence_quote,draft.why_related))
            return {'reused':reused,**self._knowledge(db,identifier)}

    def list_knowledge(self, book_id):
        with self.connection() as db:
            ids=[row[0] for row in db.execute('SELECT DISTINCT knowledge_id FROM knowledge_sources WHERE book_id=?',(book_id,))]
            return [self._knowledge(db, identifier) for identifier in ids]

    def link_knowledge(self, identifier, book_id, page, evidence, why_related, job_id=None):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            if job_id:self._require_job(db,job_id,book_id)
            self._knowledge(db, identifier)
            db.execute('INSERT OR IGNORE INTO knowledge_sources VALUES(?,?,?,?,?)',
                       (identifier,book_id,page,evidence,why_related))

    def save_page(self, book_id, submission, job_id, model, effort):
        payload = submission.model_dump()
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            self._require_job(db,job_id,book_id)
            existing=self._result(db,book_id,submission.page)
            if existing:return existing
            for index,annotation in enumerate(payload['annotations']):
                for identifier in annotation['knowledge_ids']:
                    self._knowledge(db,identifier)
                    db.execute('INSERT OR IGNORE INTO knowledge_sources VALUES(?,?,?,?,?)',
                               (identifier,book_id,submission.page,annotation['quote'],annotation['rationale']))
                annotation['id']='llm_'+hashlib.sha256(f'{book_id}/{submission.page}/{POLICY_VERSION}/{annotation["quote"]}'.encode()).hexdigest()[:20]
                for field in ('explanation','hint','followup'):
                    annotation[field]=explicit_choice_references(annotation[field],annotation['choices'])
                order=list(range(3))
                random.Random(annotation['id']).shuffle(order)
                annotation['choices']=[annotation['choices'][i] for i in order]
                annotation['correct']=order.index(annotation['correct'])
                annotation['color']={'core':'yellow','connect':'blue','challenge':'purple'}[annotation['kind']]
                annotation['llm']=True
                annotation['source_page']=submission.page
                annotation['knowledge']=[self._knowledge(db,identifier) for identifier in annotation['knowledge_ids']]
            db.execute('INSERT OR IGNORE INTO page_results VALUES(?,?,?,?,?,?,?)',(book_id,submission.page,POLICY_VERSION,dumps(payload),model,effort,job_id))
            result=self._result(db,book_id,submission.page)
            self._event(db,book_id,'page_ready',{'page':submission.page,'job_id':job_id})
        # The event and page commit together; notify only after readers can see it.
        self._notify(book_id)
        return result

    def _result(self, db, book_id, page):
        row=db.execute('SELECT * FROM page_results WHERE book_id=? AND page=? AND policy=?',(book_id,page,POLICY_VERSION)).fetchone()
        if not row:return None
        result=dict(row);result['result']=json.loads(result['result']);return result

    def page_result(self, book_id, page):
        with self.connection() as db:return self._result(db,book_id,page)

    def enqueue_window(self, book_id, page, model, effort, *, restart=False, session_id=None, sequence=None):
        metadata=self.book(book_id)
        if not 1<=page<=metadata['page_count']:raise ValueError('页码超出书籍范围')
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            # A slow request from an earlier click must not steal the reader back.
            if session_id is not None:
                previous=db.execute('SELECT sequence FROM reader_requests WHERE session_id=?',(session_id,)).fetchone()
                if previous and sequence<=previous['sequence']:
                    return {'stale':True,'job_id':None,'new_pages':[],'superseded_jobs':[]}
                db.execute('INSERT OR REPLACE INTO reader_requests VALUES(?,?)',(session_id,sequence))
            reading=db.execute('SELECT * FROM reading WHERE book_id=?',(book_id,)).fetchone()
            last_page=(reading['current_page'] or reading['furthest']) if reading else page
            jumped=restart or abs(page-last_page)>1
            if not reading or jumped:
                anchor=page;furthest=page
            else:
                anchor=reading['anchor'];furthest=max(page,reading['furthest'])
                if page<anchor:
                    anchor=page;furthest=page
            start=anchor+max(0,(furthest-anchor)//5)*5
            # A failed/uncached page behind the forward watermark still needs repair.
            # Merely retrying the forward window would leave the visible page empty.
            current_claim=db.execute('SELECT 1 FROM claims WHERE book_id=? AND page=? AND policy=?',(book_id,page,POLICY_VERSION)).fetchone()
            if page<start and not self._result(db,book_id,page) and not current_claim:
                start=page
            end=min(metadata['page_count'],start+9)
            db.execute('INSERT OR REPLACE INTO reading(book_id,anchor,furthest,current_page) VALUES(?,?,?,?)',(book_id,anchor,furthest,page))
            obsolete=db.execute("""SELECT id,book_id FROM jobs WHERE status IN ('queued','running')
                AND (book_id<>? OR ? OR (status='queued' AND (context_end<? OR context_start>?)))""",
                (book_id,jumped,start,end)).fetchall()
            for identifier,old_book_id in obsolete:
                db.execute("UPDATE jobs SET status='superseded',finished=? WHERE id=?",(time.time(),identifier))
                db.execute('DELETE FROM claims WHERE job_id=?',(identifier,))
                self._event(db,old_book_id,'job_changed',{'job_id':identifier,'status':'superseded'})
            targets=[]
            for number in range(start,end+1):
                if self._result(db,book_id,number) or db.execute('SELECT 1 FROM claims WHERE book_id=? AND page=? AND policy=?',(book_id,number,POLICY_VERSION)).fetchone():continue
                targets.append(number)
            job_id=None
            if targets:
                job_id=str(uuid.uuid4())
                db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',(job_id,book_id,start,end,dumps(targets),'queued',model,effort,time.time(),None,None,None,'{}'))
                db.executemany('INSERT INTO claims VALUES(?,?,?,?)',[(book_id,n,POLICY_VERSION,job_id) for n in targets])
            if job_id:
                self._event(db,book_id,'job_changed',{'job_id':job_id,'status':'queued'})
            result={'job_id':job_id,'context_start':start,'context_end':end,'new_pages':targets,
                    'restarted':jumped,'stale':False,'superseded_jobs':[row['id'] for row in obsolete]}
        for changed_book in {row['book_id'] for row in obsolete}|({book_id} if job_id else set()):
            self._notify(changed_book)
        return result

    def next_job(self):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute("""SELECT j.* FROM jobs j LEFT JOIN reading r ON r.book_id=j.book_id
                WHERE j.status='queued' ORDER BY CASE WHEN EXISTS
                (SELECT 1 FROM json_each(j.targets) WHERE CAST(value AS INTEGER)=r.furthest)
                THEN 0 ELSE 1 END,j.created DESC LIMIT 1""").fetchone()
            if not row:return None
            db.execute("UPDATE jobs SET status='running',started=? WHERE id=?",(time.time(),row['id']))
            self._event(db,row['book_id'],'job_changed',{'job_id':row['id'],'status':'running'})
            result=dict(row);result['targets']=json.loads(result['targets'])
        self._notify(result['book_id'])
        return result

    def finish_job(self, job_id, error=None, metrics=None):
        with self.connection() as db:
            book_id=db.execute('SELECT book_id FROM jobs WHERE id=?',(job_id,)).fetchone()[0]
            changed=db.execute("UPDATE jobs SET status=?,finished=?,error=?,metrics=? WHERE id=? AND status IN ('queued','running')",('failed' if error else 'complete',time.time(),error,dumps(metrics or {}),job_id))
            if not changed.rowcount:return
            db.execute('DELETE FROM claims WHERE job_id=?',(job_id,))
            self._event(db,book_id,'job_changed',{'job_id':job_id,'status':'failed' if error else 'complete'})
        self._notify(book_id)

    def update_job_metrics(self,job_id,metrics):
        with self.connection() as db:
            book_id=db.execute('SELECT book_id FROM jobs WHERE id=?',(job_id,)).fetchone()[0]
            changed=db.execute("UPDATE jobs SET metrics=? WHERE id=? AND status='running'",(dumps(metrics),job_id))
            if not changed.rowcount:return
            self._event(db,book_id,'job_changed',{'job_id':job_id,'phase':metrics.get('phase')})
        self._notify(book_id)

    def recover_jobs(self):
        with self.connection() as db:
            db.execute("UPDATE jobs SET status='failed',error='批注规则已更新，请重新准备缺失页面' WHERE status IN ('queued','running') AND id IN (SELECT job_id FROM claims WHERE policy<>?)",(POLICY_VERSION,))
            db.execute('DELETE FROM claims WHERE policy<>?',(POLICY_VERSION,))
            db.execute("UPDATE jobs SET status='queued' WHERE status='running'")

    def status(self, book_id, page):
        with self.connection() as db:
            jobs=[]
            for row in db.execute('SELECT * FROM jobs WHERE book_id=? ORDER BY created DESC LIMIT 6',(book_id,)):
                job=dict(row);job['targets']=json.loads(job['targets']);job['metrics']=json.loads(job['metrics']);
                job['completed_pages']=[n for n in job['targets'] if self._result(db,book_id,n)]
                jobs.append(job)
            return {'page':self._result(db,book_id,page),'jobs':jobs,'knowledge_count':len(self.list_knowledge(book_id))}
