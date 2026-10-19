"""Small live multi-turn thinking-toggle probe. Never saves reasoning text."""
import argparse
import asyncio
import json
from pathlib import Path
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from langchain.messages import HumanMessage, SystemMessage, ToolMessage
from reader_backend.agent import create_model
from reader_backend.config import Settings, MODELS

TOOL={'type':'function','function':{'name':'submit_question','description':'提交当前页的一道中文单选题',
    'parameters':{'type':'object','properties':{'page':{'type':'integer'},'question':{'type':'string'},
        'choices':{'type':'array','items':{'type':'string'}},'correct':{'type':'integer'}},
        'required':['page','question','choices','correct']}}}

VARIANTS={
    'legacy':{'reasoning_effort':'off','extra_body':{'thinking':{'type':'disabled'}}},
    'disabled-only':{'reasoning_effort':None,'extra_body':{'thinking':{'type':'disabled'}}},
    'off-only':{'reasoning_effort':'off','extra_body':{}},
    'reasoning-none':{'reasoning_effort':None,'extra_body':{'reasoning':{'effort':'none'}}},
    'native-none':{'reasoning_effort':'none','extra_body':{}},
}


async def probe(variant,turns,model_name=None):
    settings=Settings()
    if variant=='current':
        model=create_model(settings,model_name or settings.model,'none')
    else:
        model=create_model(settings,model_name or settings.model,'low').model_copy(update=VARIANTS[variant])
    model=model.model_copy(update={'max_tokens':2000,'max_retries':0})
    chat=model.bind_tools([TOOL])
    messages=[SystemMessage('你是中文伴读编辑。按用户指定页码调用submit_question提交一道单选题，三个合理选项，仅一个正确答案。禁止输出长篇分析。')]
    results=[]
    for page in range(1,turns+1):
        messages.append(HumanMessage(f'当前第{page}页，原文：阿尔法是扣除市场基准后的回报，但简单相减不能区分策略与好运。仅提交本页，题目重点是归因边界。'))
        started=time.monotonic()
        response=await chat.ainvoke(messages)
        reasoning_tokens=(response.usage_metadata or {}).get('output_token_details',{}).get('reasoning')
        result={'variant':variant,'turn':page,'seconds':round(time.monotonic()-started,3),
                'reasoning_tokens':reasoning_tokens,
                'reasoning_characters':len(response.additional_kwargs.get('reasoning_content') or ''),
                'tool_calls':len(response.tool_calls)}
        results.append(result);print(json.dumps(result),flush=True)
        messages.append(response)
        for call in response.tool_calls:
            messages.append(ToolMessage(content='已保存本页，请等待下一页。',tool_call_id=call['id']))
    return results


async def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--variant',choices=['current',*VARIANTS],default='current')
    parser.add_argument('--model',choices=MODELS)
    parser.add_argument('--turns',type=int,default=3)
    parser.add_argument('--assert-off',action='store_true')
    args=parser.parse_args()
    results=await probe(args.variant,args.turns,args.model)
    if args.assert_off:
        assert all(not r['reasoning_tokens'] and not r['reasoning_characters'] for r in results),'Provider still returned thinking'


if __name__=='__main__':asyncio.run(main())
