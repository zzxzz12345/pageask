"""Adopt a measured model run as local demo cache, after revalidating every quote.

No canned answers are generated here. The source report remains unchanged.
"""
import argparse
import json
from pathlib import Path
import random
import sys
import pymupdf
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from reader_backend.config import ROOT, Settings, POLICY_VERSION
from reader_backend.storage import Store
from reader_backend.documents import Documents, pdf_page_text
from reader_backend.domain import Annotation, PageSubmission, KnowledgeDraft, canonical, validate_page


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('report',type=Path)
    args=parser.parse_args()
    report=json.loads(args.report.read_text())
    if report['status']!='complete':raise ValueError('只能采用已完整完成的实测结果')
    store=Store(Settings().data_dir);documents=Documents(store)
    metadata=documents.ingest((ROOT/'打开量化投资的黑箱.pdf').read_bytes(),'打开量化投资的黑箱.pdf')
    with pymupdf.open(metadata['path']) as pdf:
        store.save_book(metadata,[{'text':pdf_page_text(page)} for page in pdf])
    for item in report['knowledge']:
        for source in item['sources']:
            if source['book_id']!=metadata['id']:raise ValueError('来源书籍不匹配')
            if canonical(source['evidence']) not in canonical(store.page(metadata['id'],source['page'])['text']):
                raise ValueError('知识来源没有通过原文核验')
            result=store.resolve_knowledge(KnowledgeDraft(name=item['name'],aliases=item['aliases'],definition=item['definition'],
                origin=item['origin'],source_page=source['page'],evidence_quote=source['evidence'],why_related=source['why_related']),metadata['id'])
            if result['id']!=item['id']:raise ValueError('概念ID冲突')
    queued=store.enqueue_window(metadata['id'],55,report['model'],report['effort'])
    job=store.next_job()
    if not job:
        print('示例批注已缓存，无须重复采用');return
    for page in report['pages']:
        if page['book_id']!=metadata['id']:raise ValueError('批注书籍不匹配')
        items=[]
        for stored in page['result']['annotations']:
            payload={key:stored[key] for key in Annotation.model_fields}
            # v5 reports already shuffled choices; reconstruct the model's original
            # ordering so reference normalization and deterministic shuffle run once.
            if report.get('choices_shuffled',True):
                order=list(range(3));random.Random(stored['id']).shuffle(order)
                original=[None]*3
                for new,old in enumerate(order):original[old]=payload['choices'][new]
                payload['choices']=original;payload['correct']=order[payload['correct']]
            items.append(Annotation(**payload))
        submission=PageSubmission(page=page['page'],annotations=items,skip_reason=page['result']['skip_reason'])
        validate_page(submission,store.page(metadata['id'],page['page'])['text'])
        store.save_page(metadata['id'],submission,job['id'],report['model'],report['effort'])
    store.finish_job(job['id'],metrics={**report['metrics'],'adopted_from':str(args.report.relative_to(ROOT)),'policy':POLICY_VERSION})
    print(f"已采用实测缓存：10页，{report['annotation_count']}处批注，{report['knowledge_count']}个知识点")


if __name__=='__main__':main()
