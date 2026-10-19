"""Compare exactly the requested two models x none/high on the same ten pages."""
import asyncio
import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from reader_backend.config import Settings, ROOT, MODELS, DELIVERY_VERSION
from reader_backend.storage import Store
from reader_backend.documents import Documents
from reader_backend.tools import Workspace
from reader_backend.agent import run_annotation
from reader_backend.domain import canonical

async def run_case(model,effort,semaphore,label=''):
    async with semaphore:
        slug=model.split('/')[-1]+'-'+effort+('-'+label if label else '')
        directory=ROOT/'.reader-data'/'benchmarks'/DELIVERY_VERSION/slug
        output=directory.parent/(slug+'.json')
        if output.exists():
            result=json.loads(output.read_text())
            print(json.dumps({'case':slug,'cached':True,'status':result['status']}),flush=True)
            return result
        settings=replace(Settings(),data_dir=directory)
        store=Store(directory);documents=Documents(store)
        metadata=documents.ingest((ROOT/'打开量化投资的黑箱.pdf').read_bytes(),'打开量化投资的黑箱.pdf')
        scheduled=store.enqueue_window(metadata['id'],55,model,effort);job=store.next_job()
        if not job:raise RuntimeError('No benchmark job')
        workspace=Workspace(store,documents,job,settings.max_images)
        print(json.dumps({'case':slug,'status':'started','characters':sum(len(p['text']) for p in workspace.context())}),flush=True)
        started=time.monotonic()
        try:
            metrics=await run_annotation(workspace,settings);status='complete';error=None
        except Exception as exc:
            metrics=getattr(exc,'metrics',{});status='failed';error=str(exc).replace(settings.api_key,'[REDACTED]')[:1500]
        store.finish_job(job['id'],error,metrics)
        pages=[store.page_result(metadata['id'],number) for number in range(55,65)]
        fractions=[]
        for number,page in zip(range(55,65),pages):
            if page:
                fractions.append(sum(len(canonical(a['quote'])) for a in page['result']['annotations'])/max(1,len(canonical(store.page(metadata['id'],number)['text']))))
        result={'case':slug,'model':model,'effort':effort,'status':status,'error':error,'seconds':round(time.monotonic()-started,3),'metrics':metrics,
                'ready_pages':sum(p is not None for p in pages),'annotation_count':sum(len(p['result']['annotations']) for p in pages if p),
                'max_coverage':max(fractions,default=0),'knowledge_count':len(store.list_knowledge(metadata['id'])),'knowledge':store.list_knowledge(metadata['id']),'pages':pages}
        output.write_text(json.dumps(result,ensure_ascii=False,indent=2))
        print(json.dumps({k:result[k] for k in ['model','effort','status','seconds','ready_pages','annotation_count','knowledge_count','error']},ensure_ascii=False),flush=True)
        return result

async def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--model',choices=MODELS)
    parser.add_argument('--effort',choices=('none','low','medium','high'))
    parser.add_argument('--label',default='')
    args=parser.parse_args()
    semaphore=asyncio.Semaphore(2)
    results=await asyncio.gather(*(run_case(model,effort,semaphore,args.label) for model in ([args.model] if args.model else MODELS) for effort in ([args.effort] if args.effort else ('none','high'))))
    output=ROOT/'.reader-data'/'benchmarks'/DELIVERY_VERSION/'summary.json';output.parent.mkdir(parents=True,exist_ok=True)
    all_results=[]
    for path in sorted(output.parent.glob('*.json')):
        if path==output:continue
        result=json.loads(path.read_text())
        all_results.append({'case':path.stem,**result})
    output.write_text(json.dumps(all_results,ensure_ascii=False,indent=2))

if __name__=='__main__':asyncio.run(main())
