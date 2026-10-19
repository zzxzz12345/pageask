"""Live three-target check using ten paired source pages; calls the configured LLM."""
import asyncio
import argparse
from dataclasses import replace
from datetime import datetime
import json
from pathlib import Path
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from reader_backend.agent import run_annotation
from reader_backend.config import ROOT, Settings
from reader_backend.documents import Documents
from reader_backend.storage import Store, dumps
from reader_backend.tools import Workspace


async def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--resume',type=Path,help='Reuse prior accepted pages and knowledge; write a new result file')
    parser.add_argument('--effort',choices=('none','low','medium','high'),help='Override configured effort for this run')
    args=parser.parse_args()
    timestamp=datetime.now().strftime('%Y%m%d-%H%M%S')
    directory=args.resume or ROOT/'.reader-data'/('progressive-live-'+timestamp)
    settings=replace(Settings(),data_dir=directory)
    if args.effort:settings=replace(settings,effort=args.effort)
    store=Store(directory);documents=Documents(store)
    book=documents.ingest((ROOT/'打开量化投资的黑箱.pdf').read_bytes(),'打开量化投资的黑箱.pdf')
    store.enqueue_window(book['id'],55,settings.model,settings.effort)
    job=store.next_job()
    if not job:
        print('目标页已缓存，无须调用模型');return
    job['targets']=[page for page in job['targets'] if page in (55,56,57)]
    with store.connection() as db:
        db.execute('UPDATE jobs SET targets=? WHERE id=?',(dumps(job['targets']),job['id']))
        db.execute('DELETE FROM claims WHERE job_id=? AND page>57',(job['id'],))
    ws=Workspace(store,documents,job)
    print(json.dumps({'model':settings.model,'effort':settings.effort,'context':[55,64],
                      'targets':job['targets'],'report_directory':str(directory)},ensure_ascii=False),flush=True)
    started=time.monotonic();cursor=store.event_cursor(book['id']);publications=[]
    def changed(book_id):
        nonlocal cursor
        for event in store.events_since(book_id,cursor):
            cursor=event['id']
            if event['kind']=='page_ready':
                publication={'page':event['payload']['page'],'seconds':round(time.monotonic()-started,3),
                             'remaining_pages':ws.remaining()}
                publications.append(publication)
                print(json.dumps(publication),flush=True)
    store.on_change=changed
    try:
        metrics=await run_annotation(ws,settings)
        store.finish_job(job['id'],metrics=metrics)
        result={'status':'complete','context':[55,64],'targets':job['targets'],
                'publications':publications,'metrics':metrics}
    except Exception as error:
        detail=str(error).replace(settings.api_key,'[REDACTED]') if settings.api_key else str(error)
        store.finish_job(job['id'],error=detail,metrics=getattr(error,'metrics',{}))
        result={'status':'failed','error':detail,'publications':publications,'metrics':getattr(error,'metrics',{})}
    result.update({'model':settings.model,'effort':settings.effort})
    report=directory/(f'resumed-{timestamp}.json' if args.resume else 'result.json')
    report.write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print(json.dumps({'status':result['status'],'report':str(report),
                      'first_target_seconds':result['metrics'].get('first_target_seconds'),
                      'seconds':result['metrics'].get('seconds'),'error':result.get('error')},ensure_ascii=False),flush=True)


if __name__=='__main__':asyncio.run(main())
