/* Server-side LangChain companion; credentials never enter the browser. */
'use strict';
const AIReader = (() => {
  let version=0, remote=location.protocol.startsWith('http'), documentId=null, health=null;
  let cache=new Map(), knowledge=[], timer, busy=false, latest=null;
  let stream=null,streamConnected=false,pollAgain=false;
  const sessionId=globalThis.crypto?.randomUUID?.()||`${Date.now()}-${Math.random()}`;
  let sequence=0,lastSentPage=null,progressController=null;
  let checkedPages=new Set();
  let connection=null;
  const esc=value=>escapeHTML(String(value??''));
  async function request(path,options={}) {
    const timeout=AbortSignal.timeout(30000);
    const signal=options.signal?AbortSignal.any([options.signal,timeout]):timeout;
    const response=await fetch(path,{...options,signal});
    const body=await response.json().catch(()=>({}));
    if(!response.ok)throw new Error(body.detail||`服务暂时不可用 (${response.status})`);
    return body;
  }
  function label(text,detail='') {
    $('#ai-state').textContent=text;$('#ai-detail').textContent=detail;
    $('#analysis-info').innerHTML=esc(text)+icon('info');
  }
  function failure(error) {
    label('批注暂未完成',error.message||'请检查后端服务');
    $('#ai-retry').hidden=false;
  }
  function refreshPage() {
    if(typeof book!=='undefined'&&book.kind==='pdf'){
      paintHighlights();renderCounts();
      if(activeHighlight&&!$('#thought-bubble').hidden)renderBubble();
      if(!$('#notes-drawer').hidden)renderDrawer();
      return;
    }
    if(typeof ready==='undefined'||!ready)return;
    if(remote) {
      const result=cache.get(pageNumber);
      pageHighlights=(result?.result.annotations||[]).map(a=>({...a,concepts:a.knowledge_ids}));
      paintHighlights();renderCounts();
      if(activeHighlight&&!$('#thought-bubble').hidden)renderBubble();
      if(!$('#notes-drawer').hidden)renderDrawer();
    }
  }
  function acceptPage(result) {
    if(!result||result.book_id!==documentId)return;
    const previous=cache.get(result.page);
    cache.set(result.page,result);
    if(typeof book!=='undefined'&&book.kind==='pdf'&&(!previous||previous.policy!==result.policy)){
      paintPDFHighlights(PDFReader.page(result.page));
    }
    if(result.page===pageNumber&&(!previous||previous.policy!==result.policy)){
      refreshPage();$('#ai-retry').hidden=true;
      const count=result.result.annotations.length;
      label(count?`AI 批注 · 本页 ${count} 处`:'本页无需额外划线',count?'已收到批注 · 后续页面继续准备':result.result.skip_reason);
    }
  }
  async function pageRendered(number) {
    if(!remote||!documentId||cache.has(number)||checkedPages.has(number))return;
    const current=version,id=documentId;checkedPages.add(number);
    try{
      const status=await request(`/api/books/${id}/status?page=${number}`);
      if(current===version)acceptPage(status.page);
    }catch{if(current===version)checkedPages.delete(number);}
  }
  function loadRenderedPages() {
    if(typeof book!=='undefined'&&book.kind==='pdf')for(const view of PDFReader.rendered())void pageRendered(view.number);
  }
  function closeEvents() {
    stream?.close();stream=null;streamConnected=false;
  }
  function startEvents(current,id) {
    closeEvents();
    if(!globalThis.EventSource)return; // Older browsers keep the polling fallback.
    const source=new EventSource(`/api/books/${id}/events`);stream=source;
    const valid=()=>current===version&&documentId===id&&stream===source;
    source.onopen=()=>{if(valid()){streamConnected=true;clearTimeout(timer);}};
    source.addEventListener('snapshot',()=>{if(valid()){checkedPages.clear();loadRenderedPages();void poll();}});
    source.addEventListener('page_ready',event=>{
      if(!valid())return;
      try {
        const data=JSON.parse(event.data);
        if(data.book_id!==id)return;
        acceptPage(data.page_result); // Paint immediately; status fetching is secondary.
      }catch{/* Reconcile through the status endpoint below. */}
      void poll();
    });
    source.addEventListener('job_changed',()=>{if(valid())void poll();});
    source.onerror=()=>{
      if(!valid())return;
      streamConnected=false;void poll(); // EventSource reconnects and replays missed IDs.
    };
  }
  function connect(metadata,record) {
    connection=connectBook(metadata,record);
    return connection;
  }
  async function connectBook(metadata,record) {
    const current=++version;
    progressController?.abort();lastSentPage=null;
    closeEvents();clearTimeout(timer);documentId=null;cache=new Map();knowledge=[];latest=null;busy=false;pollAgain=false;
    checkedPages=new Set();
    remote=location.protocol.startsWith('http');
    $('#ai-retry').hidden=true;
    if(!remote){label('离线示例','连接本地服务后启用真实 AI 批注');return;}
    label('正在连接伴读','原文可继续阅读，批注在后台准备');
    try {
      health=await request('/api/health');
      if(current!==version)return;
      if(!health.configured){remote=false;label('尚未配置模型','可手动划线；后端配置密钥后刷新');refreshOffline();return;}
      let registered;
      if(metadata.isDemo)registered=await request('/api/books/sample',{method:'POST'});
      else {
        if(!record)record=await storedBook(metadata.id);
        if(!record)throw new Error('请重新导入原件以启用 AI 批注');
        const form=new FormData();form.append('file',new Blob([record.bytes]),record.name);
        registered=await request('/api/books',{method:'POST',body:form});
      }
      if(current!==version)return;
      if(registered.page_count!==metadata.pageCount)throw new Error('书籍章节与后端解析不一致，请重新导入');
      documentId=registered.id;
      startEvents(current,documentId);
      loadRenderedPages();
      await pageChanged({restart:true});
    } catch(error) {if(current===version)failure(error);}
  }
  function refreshOffline() {
    if(typeof ready!=='undefined'&&ready) {
      pageHighlights=[];
      paintHighlights();renderCounts();
    }
  }
  async function ask(payload,onEvent,signal) {
    const current=version;
    if(connection)await connection;
    signal.throwIfAborted();
    if(current!==version)throw new Error('阅读位置已改变，请重新提问');
    if(!remote||!health?.configured)throw new Error('请通过本地服务打开阅读器，并在后端配置模型密钥');
    if(!documentId)throw new Error('原书尚未连接，请稍后重试');
    const response=await fetch(`/api/books/${documentId}/ask`,{
      method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload),
      signal:AbortSignal.any([signal,AbortSignal.timeout(110000)])});
    if(!response.ok){
      const error=await response.json().catch(()=>({}));
      throw new Error(error.detail||`暂时无法回答 (${response.status})`);
    }
    const reader=response.body.getReader(),decoder=new TextDecoder();let buffer='',done=false;
    try{
      while(!done){
        const chunk=await reader.read();
        buffer+=decoder.decode(chunk.value||new Uint8Array(),{stream:!chunk.done});
        buffer=buffer.replace(/\r\n/g,'\n');
        let boundary;
        while((boundary=buffer.indexOf('\n\n'))>=0){
          const block=buffer.slice(0,boundary);buffer=buffer.slice(boundary+2);
          const data=block.split('\n').filter(line=>line.startsWith('data:')).map(line=>line.slice(5).trimStart()).join('\n');
          if(!data)continue;
          const event=JSON.parse(data);
          if(event.type==='error')throw new Error(event.message||'回答未完成，请重试');
          if(event.type==='done'){done=true;break;}
          onEvent(event);
        }
        if(chunk.done&&!done)throw new Error('连接已中断，已收到的回答已保留，可重新提问');
      }
    }finally{await reader.cancel().catch(()=>{});reader.releaseLock();}
  }
  async function pageChanged({restart=false,force=false}={}) {
    refreshPage();
    if(!documentId||!remote)return;
    if(lastSentPage===pageNumber&&!restart&&!force)return;
    const current=version,id=documentId,number=pageNumber;
    const revision=++sequence;
    lastSentPage=number;progressController?.abort();
    const controller=new AbortController();progressController=controller;
    clearTimeout(timer);$('#ai-retry').hidden=true;
    if(!cache.has(number))label(restart?'正在切换批注位置':'正在准备当前页',`优先第 ${number} ${book.kind==='pdf'?'页':'节'} · 原文可继续阅读`);
    try {
      await request(`/api/books/${id}/progress`,{method:'POST',signal:controller.signal,
        headers:{'Content-Type':'application/json'},body:JSON.stringify({page:number,restart,session_id:sessionId,sequence:revision})});
      if(current===version&&revision===sequence)await poll();
    }catch(error){
      if(current===version&&revision===sequence&&!controller.signal.aborted){lastSentPage=null;failure(error);}
    }
  }
  async function poll() {
    if(!documentId)return;
    if(busy){pollAgain=true;return;}
    clearTimeout(timer);
    busy=true;const current=version,id=documentId,number=pageNumber;
    try {
      const status=await request(`/api/books/${id}/status?page=${number}`);
      if(current!==version)return;
      acceptPage(status.page);
      if(number!==pageNumber){pollAgain=true;return;}
      latest=status;
      const active=status.jobs.filter(j=>['queued','running'].includes(j.status));
      const failed=status.jobs.find(j=>j.status==='failed'&&j.targets.includes(number));
      const total=active.reduce((n,j)=>n+j.targets.length,0),done=active.reduce((n,j)=>n+j.completed_pages.length,0);
      if(active.length) {
        const first=active.at(-1),last=active[0];
        const running=active.find(j=>j.status==='running');
        const phase={rendering:'准备图文',reading:'理解图文',drafting:'生成重点与题目',checking:'核对原文与题目'}[running?.metrics.phase]||'后台准备';
        const allowed=running?.metrics.allowed_pages;
        const preparing=allowed?.length?`准备第 ${allowed.join('、')} ${book.kind==='pdf'?'页':'节'}`:`第 ${first.context_start}–${last.context_end} ${book.kind==='pdf'?'页':'节'}`;
        label(status.page?`本页已就绪 · 后续 ${done}/${total}`:`批注准备中 ${done}/${total}`,`${preparing} · ${phase} · 可以继续阅读`);
      } else if(!status.page&&failed) {
        failure(new Error('部分页面未完成；点击重试会保留已有批注'));
        $('#ai-detail').title=failed.error||'';
      } else if(status.page) {
        const count=status.page.result.annotations.length;
        label(count?`AI 批注 · 本页 ${count} 处`:'本页无需额外划线',count?'重点与选择题已就绪 · 点击划线思考':status.page.result.skip_reason);
      } else label('等待批注','翻到新位置后会准备前方 10 页');
      $('#knowledge-count').textContent=status.knowledge_count;
      if(status.knowledge_count!==knowledge.length) {
        const result=await request(`/api/books/${id}/knowledge`);
        if(current!==version)return;
        knowledge=result.items;
        if(!$('#notes-drawer').hidden&&drawerTab==='knowledge')renderDrawer();
      }
      if(!streamConnected&&(active.length||number!==pageNumber))timer=setTimeout(()=>void poll(),2200);
    }catch(error){
      if(current===version&&number===pageNumber){
        if(streamConnected&&cache.has(pageNumber))label('本页批注已就绪','进度暂未同步，已收到的批注可继续阅读');
        else failure(error);
      }
    }
    finally {
      if(current===version){
        busy=false;
        if(pollAgain){pollAgain=false;queueMicrotask(()=>void poll());}
      }
    }
  }
  function renderKnowledge() {
    return '<p class="drawer-section-label">整本书复用同一份知识定义 · '+knowledge.length+' 个概念</p>'+(knowledge.length?knowledge.map(item=>{
      const sources=item.sources.filter(source=>source.book_id===documentId);
      return `<details class="knowledge-item"><summary>${esc(item.name)}<small>${item.origin==='source'?'原文概念':'延伸理解'}</small></summary><p>${esc(item.definition)}</p><small class="knowledge-id">${esc(item.id)}</small><div class="knowledge-sources">${sources.map(s=>`<button data-knowledge-page="${s.page}">第 ${s.page} ${book.kind==='pdf'?'页':'节'}</button><blockquote>${esc(s.evidence)}</blockquote>`).join('')}</div></details>`;
    }).join(''):'<div class="empty-state">'+icon('network')+'<h3>知识点会在这里沉淀</h3><p>模型先查已有概念，再创建新条目。<br>相同概念跨页复用，点击即可查看。</p></div>');
  }
  function concept(h,id) {
    const item=(h.knowledge||[]).find(k=>k.id===id)||knowledge.find(k=>k.id===id);
    if(!item)return CONCEPTS[id]||CONCEPTS.boundary;
    const source=item.sources.find(s=>s.book_id===documentId&&s.page===pageNumber)||item.sources[0];
    return {name:esc(item.name),icon:'network',subtitle:item.origin==='source'?'原文概念 · 已存入知识库':'延伸理解 · 已存入知识库',description:esc(item.definition)+(source?`<span class="knowledge-evidence">出处 · 第 ${source.page} 页<br>「${esc(source.evidence)}」</span>`:'')};
  }
  document.addEventListener('DOMContentLoaded',()=>{
    $('#ai-retry').addEventListener('click',()=>documentId?void pageChanged({force:true}):void connect(book,null));
    $('#knowledge-button').addEventListener('click',()=>openDrawer('knowledge'));
    $('#drawer-knowledge-tab').addEventListener('click',()=>openDrawer('knowledge'));
    $('#drawer-content').addEventListener('click',event=>{
      const button=event.target.closest('[data-knowledge-page]');
      if(button){$('#notes-drawer').hidden=true;void navigateTo(Number(button.dataset.knowledgePage),{jump:true});}
    });
  });
  addEventListener('pagehide',closeEvents);
  addEventListener('pageshow',event=>{
    if(event.persisted&&remote&&documentId)startEvents(version,documentId);
  });
  return {connect,ask,pageChanged,pageRendered,enabled:()=>remote,hasPage:(number=pageNumber)=>cache.has(number),pageHighlights:(number=pageNumber)=>cache.get(number)?.result.annotations.map(a=>({...a,concepts:a.knowledge_ids}))||[],renderKnowledge,concept};
})();
