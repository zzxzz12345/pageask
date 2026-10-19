/* Passage questions stay attached to the selected highlight in browser storage. */
'use strict';
const AskReader=(()=>{
  let pending=null;
  const prompts=['解释这段话','举一个具体例子','这段话有哪些成立前提？'];
  const answerHTML=text=>escapeHTML(text).replace(/\*\*([^*\n]+)\*\*/g,'<strong>$1</strong>')
    .replace(/`([^`\n]+)`/g,'<code>$1</code>');
  function thread(h){
    state.chats??={};
    return state.chats[highlightKey(h)]??={messages:[],draft:''};
  }
  function visible(key){return activeHighlight&&highlightKey(activeHighlight)===key&&bubbleTab==='ask'&&!$('#thought-bubble').hidden;}
  function render(h){
    const chat=thread(h),key=highlightKey(h),busy=pending?.key===key;
    const messages=chat.messages.map(message=>{
      const status=message.status==='stopped'?'已停止':message.status==='error'?'未完成':message.status==='streaming'&&!busy?'上次回答已中断':'';
      const text=message.content||(busy?'正在阅读选文……':'回答未完成，可以重新提问。');
      return `<article class="ask-message ${message.role}" data-message-id="${message.id}"><strong>${message.role==='user'?'你':'AI 伴读'}</strong><div class="ask-message-text">${message.role==='assistant'?answerHTML(text):escapeHTML(text)}</div>${status?`<small>${status}</small>`:''}</article>`;
    }).join('');
    return `<section class="ask-panel" aria-label="围绕划线问 AI"><p class="ask-intro">结合所选原文和本页内容回答，也可以继续追问。</p><div class="ask-messages" role="log" aria-live="off">${messages||'<p class="ask-empty">这句话，哪里让你想多问一步？</p>'}</div><div class="ask-suggestions">${prompts.map((prompt,i)=>`<button data-ask-prompt="${i}" ${busy?'disabled':''}>${escapeHTML(prompt)}</button>`).join('')}</div>${chat.error?`<p class="ask-error" role="alert">${escapeHTML(chat.error)}</p>`:''}<label class="sr-only" for="ask-input">关于这段原文的问题</label><textarea id="ask-input" maxlength="1000" rows="2" placeholder="输入你的问题，或点选上方的提问……" ${busy?'disabled':''}>${escapeHTML(chat.draft)}</textarea><div class="ask-actions"><span role="status">${busy?'正在回答 · 可以停止':'对话自动保存在这处划线'}</span>${busy?'<button id="ask-stop">停止回答</button>':'<button id="ask-send">发送'+icon('arrow')+'</button>'}</div>${chat.messages.some(message=>message.role==='assistant'&&message.content)?'<button id="ask-save-note" class="ask-save-note">'+icon('pen')+'将这段对话存入笔记</button>':''}</section>`;
  }
  function refresh(key,streaming=false){
    if(!visible(key))return;
    if(!streaming){const scroll=$('#bubble-scroll').scrollTop;renderBubble();$('#bubble-scroll').scrollTop=scroll;return;}
    const message=state.chats[key].messages.at(-1);
    const scroll=$('#bubble-scroll'),follow=scroll.scrollHeight-scroll.scrollTop-scroll.clientHeight<90;
    const element=$(`[data-message-id="${message.id}"] .ask-message-text`);
    if(element)element.innerHTML=answerHTML(message.content);
    positionBubble();
    if(follow)scroll.scrollTop=scroll.scrollHeight;
  }
  function stop(){
    if(pending){
      const request=pending;pending=null;
      request.answer.status='stopped';request.chat.draft=request.question;
      request.controller.abort();persist();refresh(request.key);
    }
  }
  async function send(question){
    const h=activeHighlight;if(!h)return;
    const chat=thread(h),key=highlightKey(h),text=(question??chat.draft).trim();
    if(!text){$('#ask-input')?.focus();return;}
    if(pending?.key===key)return;
    stop();
    const history=chat.messages.filter(message=>message.content?.trim())
      .map(({role,content,status})=>({role,content,status:status||'complete'}));
    const user={id:uid(),role:'user',content:text,status:'complete'};
    const answer={id:uid(),role:'assistant',content:'',status:'streaming'};
    // Keep the entire highlight-specific thread, including visible partial answers.
    chat.messages.push(user,answer);chat.draft='';chat.error='';
    const request={key,controller:new AbortController(),answer,chat,question:text};pending=request;
    persist();refresh(key);
    if(visible(key))$('#bubble-scroll').scrollTop=$('#bubble-scroll').scrollHeight;
    try{
      await AIReader.ask({page:pageNumber,quote:h.quote,question:text,history},event=>{
        if(event.type==='delta'&&typeof event.text==='string'){
          answer.content+=event.text;refresh(key,true);
        }
      },request.controller.signal);
      answer.status='complete';
    }catch(error){
      if(request.controller.signal.aborted)answer.status='stopped';
      else{answer.status='error';chat.error=error.message||'回答未完成，请重试';chat.draft=text;}
    }finally{
      if(pending===request)pending=null;
      persist();refresh(key);
    }
  }
  function saveNote(){
    const h=activeHighlight;if(!h)return;
    const chat=thread(h);
    const answer=chat.messages.filter(message=>message.content).map(message=>`${message.role==='user'?'我问':'AI 伴读'}：${message.content}${message.status==='error'||message.status==='stopped'?'（回答未完成）':''}`).join('\n\n');
    if(!answer)return;
    state.notes.push({id:uid(),bookId:book.id,bookTitle:book.title,page:pageNumber,chapter:chapterTitle(),quote:h.quote,question:'划线问 AI · 对话记录',answer,createdAt:Date.now()});
    persist();renderCounts();toast('对话已存入阅读笔记');
  }
  document.addEventListener('DOMContentLoaded',()=>{
    $('#bubble-body').addEventListener('input',event=>{
      if(event.target.id==='ask-input'&&activeHighlight){thread(activeHighlight).draft=event.target.value;persist();}
    });
    $('#bubble-body').addEventListener('click',event=>{
      if(event.target.closest('#ask-send'))void send();
      if(event.target.closest('#ask-stop'))stop();
      if(event.target.closest('#ask-save-note'))saveNote();
      const prompt=event.target.closest('[data-ask-prompt]');
      if(prompt)void send(prompts[Number(prompt.dataset.askPrompt)]);
    });
    $('#bubble-body').addEventListener('keydown',event=>{
      if(event.target.id==='ask-input'&&event.key==='Enter'&&(event.ctrlKey||event.metaKey)){
        event.preventDefault();void send();
      }
    });
  });
  addEventListener('pagehide',stop);
  return {render,stop};
})();
