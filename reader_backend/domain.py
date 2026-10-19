from typing import Literal
import re
import unicodedata
from pydantic import BaseModel, ConfigDict, Field, model_validator


def canonical(text: str) -> str:
    return re.sub(r'\s+', '', text)


def concept_key(text: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFKC', text).casefold() if c.isalnum())


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class AnnotationContent(StrictModel):
    kind: Literal['core', 'connect', 'challenge']
    rationale: str = Field(min_length=8, max_length=180, description='为什么值得停下来思考，不能只说重要')
    question: str = Field(min_length=8, max_length=180)
    choices: list[str] = Field(min_length=3, max_length=3)
    correct: int = Field(ge=0, le=2)
    explanation: str = Field(min_length=15, max_length=450, description='根据本页解释正确与其他判断的区别')
    followup: str = Field(min_length=8, max_length=200)
    hint: str = Field(min_length=5, max_length=180)
    knowledge_ids: list[str] = Field(min_length=1, max_length=3,description='已有概念用工具返回的真实k_开头ID；本次knowledge_drafts中的新概念用new:规范名称，不能编造ID或只写概念名')
    confidence: float = Field(ge=.75, le=1)

    @model_validator(mode='after')
    def unique_choices(self):
        if len({canonical(choice) for choice in self.choices}) != 3:
            raise ValueError('三个选项必须不同')
        return self


class Annotation(AnnotationContent):
    quote: str = Field(min_length=8, max_length=160, description='原文连续片段，按去空白后最多120字；保留必要条件')

    @model_validator(mode='after')
    def quote_length(self):
        if len(canonical(self.quote))>120:raise ValueError('单处原文片段不能超过120字')
        return self


class AnchoredAnnotation(AnnotationContent):
    span_ids: list[str] = Field(min_length=1,max_length=20,description='从本页highlight_candidates选原文编号。通常只选1个；多个编号将包含中间连续原文，仍受120字和14%限制。禁止改写原文。')


class PagePlan(StrictModel):
    page: int = Field(ge=1)
    annotations: list[AnchoredAnnotation] = Field(max_length=3)
    skip_reason: str = Field(default='',max_length=250)


def highlight_candidates(page: int,text: str):
    source=canonical(text);budget=min(70,int(len(source)*.14))
    if budget<8:return []
    ranges=set()
    for sentence in re.finditer(r'[^。！？；]+[。！？；]?',source):
        if 8<=len(sentence[0])<=budget:ranges.add(sentence.span())
        for clause in re.finditer(r'[^，：]+[，：]?',sentence[0]):
            start=sentence.start()+clause.start();end=sentence.start()+clause.end()
            if 8<=end-start<=budget:ranges.add((start,end))
            elif end-start>budget:
                # Fixed source slices remain exact; the model may combine adjacent slices
                # to keep conditions together, subject to the same coverage check.
                for begin in range(start,end,budget):
                    stop=min(end,begin+budget)
                    if stop-begin>=8:ranges.add((begin,stop))
    return [{'id':f'p{page}s{i}','text':source[start:end],'start':start,'end':end}
            for i,(start,end) in enumerate(sorted(ranges))]


class PageSubmission(StrictModel):
    page: int = Field(ge=1)
    annotations: list[Annotation] = Field(max_length=3)
    skip_reason: str = Field(default='', max_length=250, description='零划线必须说明没有必要划线的理由')


class KnowledgeDraft(StrictModel):
    name: str = Field(min_length=2, max_length=80)
    aliases: list[str] = Field(default_factory=list, max_length=8)
    definition: str = Field(min_length=15, max_length=500)
    why_related: str = Field(min_length=8, max_length=200)
    source_page: int = Field(ge=1)
    evidence_quote: str = Field(min_length=8, max_length=160)
    origin: Literal['source', 'extension'] = Field(description='source:本页明确讨论；extension:为理解本页引入的延伸概念')


def validate_page(submission: PageSubmission, text: str) -> list[tuple[int, int]]:
    source = canonical(text)
    if not submission.annotations:
        if len(submission.skip_reason.strip()) < 5:
            raise ValueError('零划线需要具体说明原因，不能只返回空列表')
        return []
    spans = []
    for annotation in submission.annotations:
        quote = canonical(annotation.quote)
        start = source.find(quote)
        if start < 0:
            raise ValueError(f'原文不存在连续片段：{annotation.quote[:40]}')
        if any(start < end and start + len(quote) > begin for begin, end in spans):
            raise ValueError('同页划线不能重叠或重复')
        spans.append((start, start + len(quote)))
    fraction = sum(end-start for start,end in spans) / max(len(source), 1)
    if fraction > .14:
        raise ValueError(f'划线覆盖率 {fraction:.1%} 超过14%；缩短片段或减少划线，不要删除关键条件')
    return spans
