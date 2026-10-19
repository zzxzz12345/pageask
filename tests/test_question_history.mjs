import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const source=readFileSync(new URL('../ask-reader.js',import.meta.url),'utf8');

function reader(saved={chats:{}},highlight='first'){
  const listeners={},requests=[];
  const surface={addEventListener(type,callback){listeners[type]=callback;}};
  const context=vm.createContext({
    state:structuredClone(saved),activeHighlight:{id:highlight,quote:'所选原文'},pageNumber:55,
    bubbleTab:'ask',book:{id:'book'},AbortController,
    $:selector=>selector==='#bubble-body'?surface:{hidden:true},
    highlightKey:h=>`book/55/${h.id}`,persist(){},uid:()=>crypto.randomUUID(),
    document:{addEventListener(type,callback){if(type==='DOMContentLoaded')callback();}},
    addEventListener(){},
    AIReader:{async ask(payload,onEvent){requests.push(payload);onEvent({type:'delta',text:'当前回答'});}},
  });
  vm.runInContext(source,context);
  return {
    get state(){return JSON.parse(JSON.stringify(context.state));},requests,
    async send(question){
      context.state.chats[`book/55/${context.activeHighlight.id}`]??={messages:[],draft:''};
      context.state.chats[`book/55/${context.activeHighlight.id}`].draft=question;
      listeners.click({target:{closest:selector=>selector==='#ask-send'?{}:null}});
      await new Promise(setImmediate);
    },
    async key(event){
      context.state.chats[`book/55/${context.activeHighlight.id}`]??={messages:[],draft:'通过键盘发送'};
      let prevented=false;
      listeners.keydown({target:{id:'ask-input'},key:'Enter',shiftKey:false,isComposing:false,
                        preventDefault(){prevented=true;},...event});
      await new Promise(setImmediate);
      return prevented;
    },
  };
}

test('all history survives new questions and reload, isolated to the selected highlight',async()=>{
  const messages=Array.from({length:26},(_,i)=>({id:`message-${i}`,role:i%2?'assistant':'user',
    content:`历史消息${i}`,status:'complete'}));
  const first=reader({chats:{'book/55/first':{messages,draft:''}}});
  await first.send('继续上一轮讨论');
  assert.equal(first.requests[0].history.length,26);
  assert.deepEqual(first.requests[0].history.map(message=>message.content),messages.map(message=>message.content));
  assert.equal(first.state.chats['book/55/first'].messages.length,28);
  const restored=reader(first.state);
  await restored.send('刷新后继续追问');
  assert.equal(restored.requests[0].history.length,28);
  assert.equal(restored.requests[0].history.at(-1).content,'当前回答');
  const other=reader(restored.state,'second');
  await other.send('解释另一处选文');
  assert.equal(other.requests[0].history.length,0);
});

test('visible partial answers are retained with their interruption status',async()=>{
  const messages=[
    {id:'q1',role:'user',content:'第一个问题',status:'complete'},
    {id:'a1',role:'assistant',content:'已经显示的部分回答',status:'stopped'},
    {id:'q2',role:'user',content:'第二个问题',status:'complete'},
    {id:'a2',role:'assistant',content:'连接中断前的回答',status:'error'},
    {id:'empty',role:'assistant',content:'',status:'streaming'},
  ];
  const current=reader({chats:{'book/55/first':{messages,draft:''}}});
  await current.send('基于前面的内容继续');
  assert.equal(current.requests[0].history.length,4);
  assert.equal(current.requests[0].history[1].content,'已经显示的部分回答');
  assert.equal(current.requests[0].history[1].status,'stopped');
  assert.equal(current.requests[0].history[3].status,'error');
});

test('Enter sends, while Shift+Enter and IME confirmation do not send',async()=>{
  const current=reader();
  assert.equal(await current.key({shiftKey:true}),false);
  assert.equal(await current.key({isComposing:true}),false);
  assert.equal(await current.key({keyCode:229}),false);
  assert.equal(current.requests.length,0);
  assert.equal(await current.key({}),true);
  assert.equal(current.requests.length,1);
});
