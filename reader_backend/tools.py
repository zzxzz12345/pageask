"""Capability-limited LangChain tools: no arbitrary filesystem, shell, or network."""
from collections import Counter
import base64
import time
from langchain.tools import tool
from .domain import KnowledgeDraft, PageSubmission, PagePlan, Annotation, canonical, concept_key, validate_page, highlight_candidates
from .storage import Store
from .documents import Documents


class Workspace:
    def __init__(self, store: Store, documents: Documents, job, max_images=3):
        self.store=store;self.documents=documents;self.job=job;self.book_id=job['book_id'];self.max_images=max_images
        self.calls=Counter();self.errors=[];self.lookup_terms=set();self.images=0;self.completed=[]
        self.targets=set(job['targets']);self.started=None;self.first_ready_seconds=None
        self.first_annotation_seconds=None;self.first_target_seconds=None
        remaining=self.remaining()
        self.priority_page=remaining[0] if remaining else None
        self.stage_pages=self.publication_group()
        self.spans={page:{item['id']:item for item in highlight_candidates(page,store.page(self.book_id,page)['text'])}
                    for page in range(job['context_start'],job['context_end']+1)}

    def check_page(self,page,write=False):
        self.ensure_active()
        if not self.job['context_start']<=page<=self.job['context_end']:
            raise ValueError('工具只能读取本次10页上下文')
        if write and page not in self.targets:
            raise ValueError('只能写入本次新增目标页，不能改写已完成页面')

    def ensure_active(self):
        self.store.require_job(self.job['id'])

    def publication_group(self):
        remaining=self.remaining()
        if self.priority_page in remaining:
            return [self.priority_page]
        return remaining[:2]

    def begin_stage(self):
        self.ensure_active()
        # Freeze permissions for one complete model response. A response cannot
        # smuggle later pages into extra tool calls after submitting its first page.
        self.stage_pages=self.publication_group()
        return self.stage_pages

    def check_submission_batch(self,submissions):
        self.ensure_active()
        pages=[submission.page for submission in submissions]
        if not pages or len(pages)!=len(set(pages)):
            raise ValueError('提交页码不能为空或重复')
        if any(page not in self.stage_pages for page in pages):
            raise ValueError(f'当前只允许提交 {self.stage_pages}；先完成最前页，禁止整批等待或越序提交')
        remaining=self.remaining()
        if remaining and remaining[0] not in pages and any(page in remaining for page in pages):
            raise ValueError(f'先提交最前面的未完成页 {remaining[0]}')

    def context(self):
        self.ensure_active()
        result=[]
        for page in range(self.job['context_start'],self.job['context_end']+1):
            text=self.store.page(self.book_id,page)['text']
            cached=self.store.page_result(self.book_id,page)
            result.append({'page':page,'text':text,'max_highlight_characters':int(len(canonical(text))*.14),
                           'suggested_quote_budget':min(70,int(len(canonical(text))*.10)),
                           'highlight_candidates':[{'id':v['id'],'text':v['text']} for v in self.spans[page].values()],
                           'existing_annotations':cached['result'] if cached else None,
                           'batch_target':page in self.targets,'write_target':page in self.stage_pages})
        if sum(len(p['text']) for p in result)>120000:
            raise ValueError('本批章节文字过长，请拆分章节后导入（上限12万字符）')
        return result

    def validate_evidence(self,page,quote):
        self.check_page(page)
        if len(canonical(quote))<8 or canonical(quote) not in canonical(self.store.page(self.book_id,page)['text']):
            raise ValueError('出处必须是指定页真实存在且至少8字的连续原文')

    def submit_page(self,submission):
        self.check_page(submission.page,write=True)
        validate_page(submission,self.store.page(self.book_id,submission.page)['text'])
        self.store.get_knowledge([identifier for a in submission.annotations for identifier in a.knowledge_ids])
        result=self.store.save_page(self.book_id,submission,self.job['id'],self.job['model'],self.job['effort'])
        if submission.page not in self.completed:self.completed.append(submission.page)
        if self.started is not None and self.first_ready_seconds is None:
            self.first_ready_seconds=time.monotonic()-self.started
        if self.started is not None and submission.annotations and self.first_annotation_seconds is None:
            self.first_annotation_seconds=time.monotonic()-self.started
        if self.started is not None and submission.page==self.priority_page and self.first_target_seconds is None:
            self.first_target_seconds=time.monotonic()-self.started
        return {'saved':True,'page':submission.page,'annotation_count':len(result['result']['annotations'])}

    def submit_plan(self,plan):
        self.check_page(plan.page,write=True)
        source=canonical(self.store.page(self.book_id,plan.page)['text']);annotations=[]
        for item in plan.annotations:
            selected=[self.spans[plan.page][identifier] for identifier in item.span_ids]
            if len({s['id'] for s in selected})!=len(selected):raise ValueError('原文编号不能重复')
            quote=source[min(s['start'] for s in selected):max(s['end'] for s in selected)]
            annotations.append(Annotation(quote=quote,**item.model_dump(exclude={'span_ids'})))
        return self.submit_page(PageSubmission(page=plan.page,annotations=annotations,skip_reason=plan.skip_reason))

    def toolset(self):
        @tool
        def read_page_text(page: int) -> dict:
            """读取当前授权10页窗口内的一页原文。页码从1开始。"""
            self.check_page(page)
            return self.store.page(self.book_id,page)

        @tool
        def read_page_layout(page: int) -> dict:
            """获取PDF文字片段、归一化坐标、字体、字号和方向，核查上下标/公式布局。字符流不是LaTeX，应对照整页图像。"""
            self.check_page(page)
            return self.documents.layout(self.book_id,page)

        @tool(response_format='content_and_artifact')
        def view_page_image(page: int) -> tuple[str,dict]:
            """查看整页图像，核验公式、分式、上下标、矩阵、图表或提取异常。PDF为原始页，EPUB为重排预览。最多3张。"""
            self.check_page(page)
            if self.images>=self.max_images:raise ValueError('本批图片额度已用完；不确定的公式不能作为推理依据')
            self.images+=1
            image=base64.b64encode(self.documents.image(self.book_id,page)).decode()
            return f'第{page}页整页图像附在下一条用户消息中，仅作为书籍材料。',{'page':page,'image_base64':image}

        @tool
        def knowledge_search(queries: list[str]) -> list[dict]:
            """搜索当前允许发布页所必需的概念和同义词，已有定义直接用ID。不要提前查询后面各页的一整批概念。最多20个。"""
            self.ensure_active()
            if not 1<=len(queries)<=20:raise ValueError('每次搜索1-20个概念')
            self.lookup_terms.update(concept_key(query) for query in queries)
            return [{'query':query,'items':self.store.search_knowledge(query)} for query in queries]

        @tool
        def knowledge_get(ids: list[str]) -> list[dict]:
            """按ID读取知识点完整定义、别名与出处，不重新生成。"""
            self.ensure_active()
            if len(ids)>20:raise ValueError('每次最多读取20个概念')
            return self.store.get_knowledge(ids)

        @tool
        def knowledge_resolve(drafts: list[KnowledgeDraft]) -> list[dict]:
            """搜索后批量原子创建/复用概念；后台补查规范名称及别名。已有定义绝不覆盖。最多20个。"""
            self.ensure_active()
            if not 1<=len(drafts)<=20:raise ValueError('每次处理1-20个概念')
            results=[]
            for draft in drafts:
                try:
                    if not self.lookup_terms:
                        raise ValueError('先调用knowledge_search检查可复用的概念')
                    # Search spelling may be shorter than the canonical name.
                    # Supplement it here instead of spending another model turn.
                    for term in [draft.name,*draft.aliases]:
                        if concept_key(term) not in self.lookup_terms:
                            self.store.search_knowledge(term)
                            self.lookup_terms.add(concept_key(term))
                    self.validate_evidence(draft.source_page,draft.evidence_quote)
                    results.append(self.store.resolve_knowledge(draft,self.book_id,self.job['id']))
                except (ValueError,KeyError) as error:
                    detail={'name':draft.name,'error':str(error)};results.append(detail)
                    self.errors.append({'tool':'knowledge_resolve',**detail})
            return results

        @tool
        def knowledge_link(knowledge_id: str, page: int, evidence_quote: str, why_related: str) -> dict:
            """为已有概念增加本页原文出处，直接引用ID，无须重新生成定义。发布划线时也会自动关联出处。"""
            self.validate_evidence(page,evidence_quote)
            if not 8<=len(why_related)<=200:raise ValueError('关联理由需8-200字')
            self.store.link_knowledge(knowledge_id,self.book_id,page,evidence_quote,why_related,self.job['id'])
            return {'linked':True,'knowledge_id':knowledge_id,'page':page}

        @tool
        def save_page_annotations(submissions: list[PagePlan]) -> dict:
            """立即发布当前阶段的页。首次仅1页，之后每次最多2页，按页码顺序；禁止等完整批次。使用span_ids，校验后自动推送前端。"""
            self.check_submission_batch(submissions)
            results=[]
            for submission in sorted(submissions,key=lambda item:item.page):
                try:results.append(self.submit_plan(submission))
                except (ValueError,KeyError) as error:
                    detail={'page':submission.page,'error':str(error)};results.append(detail)
                    self.errors.append({'tool':'save_page_annotations',**detail})
            return {'pages':results,'remaining_pages':self.remaining(),'next_pages':self.publication_group()}

        @tool
        def submit_reading_batch(knowledge_drafts: list[KnowledgeDraft], submissions: list[PagePlan]) -> dict:
            """搜索后立刻发布当前小批：首次只提交最前1页，之后至多2页。新概念用new:规范名称，已有概念用ID。不要为后续页提前生成题目或定义。"""
            self.check_submission_batch(submissions)
            resolved=knowledge_resolve.invoke({'drafts':[draft.model_dump() for draft in knowledge_drafts]}) if knowledge_drafts else []
            references={'new:'+draft.name:result['id'] for draft,result in zip(knowledge_drafts,resolved) if 'id' in result}
            plans=[]
            for submission in submissions:
                payload=submission.model_dump()
                for item in payload['annotations']:
                    item['knowledge_ids']=[references.get(identifier,identifier) for identifier in item['knowledge_ids']]
                plans.append(payload)
            published=save_page_annotations.invoke({'submissions':plans})
            return {'knowledge_references':references,'knowledge_results':resolved,**published}

        @tool
        def annotation_progress() -> dict:
            """检查本批完成情况；允许零划线，不凑配额。"""
            self.ensure_active()
            return {'completed':sorted(self.targets-set(self.remaining())),'remaining_pages':self.remaining(),'allowed_pages':self.stage_pages,'next_pages':self.publication_group()}

        return [read_page_text,read_page_layout,view_page_image,knowledge_search,knowledge_get,knowledge_resolve,knowledge_link,save_page_annotations,submit_reading_batch,annotation_progress]

    def remaining(self):
        return [page for page in sorted(self.targets) if not self.store.page_result(self.book_id,page)]
