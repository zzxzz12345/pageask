/* 页问: original-document rendering, persisted annotations, and anchored thinking bubbles. */
'use strict';

const STORAGE_KEY = 'yejian-reading-demo-v3';
let restored = {};
try { restored = JSON.parse(localStorage.getItem(STORAGE_KEY) || 'null') || {}; } catch { /* browser storage unavailable */ }
if (!Object.keys(restored).length) {
  try {
    const previous = JSON.parse(localStorage.getItem('yejian-reading-demo-v2') || '{}');
    restored.notes = previous.notes || [];
    const legacy = previous.annotations?.[`${SAMPLE_BOOK.id}/pdf-page-55`] || [];
    restored.annotations = {[`${SAMPLE_BOOK.id}/55`]: legacy.map((a,i) => ({...a,id:`legacy-${i}`,start:undefined,end:undefined,rects:undefined}))};
  } catch { /* fresh reader */ }
}
const state = {notes:[],annotations:{},answers:{},drafts:{},chats:{},reading:{},bookmarks:{},generated:{},settings:{font:19,zoom:1,highlights:true},...restored};
state.settings = {font:19,zoom:1,highlights:true,...restored.settings};
let book = {...SAMPLE_BOOK,kind:'pdf',pageCount:SAMPLE_BOOK.totalPages};
let pageNumber = 55;
let pdfDoc = null;
let pdfjsPromise;
let loadVersion = 0;
let renderVersion = 0;
let renderTask = null;
let textLayer = null;
let readingRoot = null;
let contentFrame = null;
let contentResizeObserver = null;
let pageHighlights = [];
let activeHighlight = null;
let bubbleTab = 'question';
let drawerTab = 'highlights';
let navigationTab = 'contents';
let selectedColor = 'yellow';
let selectionSnapshot = null;
let bubbleAnchorIndex = 0;
let toastTimer;
let importBusy = false;
let ready = false;
let renderedContainerWidth = 0;
let activePDFView=null,pdfProgressTimer;
const markPositions = new Map();
const COLORS = {yellow:{fill:'rgba(255,211,77,.19)',line:'#bd941d'},blue:{fill:'rgba(91,157,237,.15)',line:'#4886c6'},purple:{fill:'rgba(166,126,209,.14)',line:'#9773b7'}};

function uid() { return crypto.randomUUID?.() || `mark-${Date.now()}-${Math.random().toString(36).slice(2,7)}`; }
function persist() {
  state.reading[book.id] = pageNumber;
  state.currentBook = book.isDemo ? null : {id:book.id};
  try { localStorage.setItem(STORAGE_KEY,JSON.stringify(state)); }
  catch { toast('浏览器存储空间不足，请先导出阅读记录。'); }
}
function toast(message) {
  $('#toast').textContent = message; $('#toast').hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(()=>{$('#toast').hidden=true;},3000);
}
function key(number=pageNumber) { return `${book.id}/${number}`; }
function annotations(number=pageNumber) { return state.annotations[key(number)] || []; }
function highlights(number=pageNumber) {
  const generated=book.kind==='pdf'?pdfPageHighlights(number):pageHighlights;
  const manual=annotations(number),quotes=new Set(manual.map(h=>canonical(h.quote)));
  return [...generated.filter(h=>!quotes.has(canonical(h.quote))),...manual];
}
function pdfPageHighlights(number) {
  if(AIReader.enabled())return AIReader.pageHighlights(number);
  if(book.isDemo&&number===55)return SAMPLE_BOOK.pages[0].highlights.map((h,i)=>({...h,id:`preset-${i}`,color:['yellow','blue','purple'][i],preset:true}));
  return [];
}
function activatePDFPage(view,{notify=true}={}) {
  if(!view)return;
  const changed=pageNumber!==view.number;
  if(changed){AskReader.stop();closeBubble();hideSelection();}
  if(activePDFView!==view){
    for(const id of ['pdf-page','pdf-canvas','pdf-text-layer','pdf-highlights'])document.getElementById(id)?.removeAttribute('id');
    activePDFView=view;
  }
  view.element.id='pdf-page';
  if(view.canvas)view.canvas.id='pdf-canvas';
  if(view.textRoot)view.textRoot.id='pdf-text-layer';
  if(view.overlay)view.overlay.id='pdf-highlights';
  pageNumber=view.number;readingRoot=view.textRoot;contentFrame=null;ready=view.ready;
  PDFReader.focus(view.number);
  pageHighlights=pdfPageHighlights(pageNumber);
  markPositions.clear();for(const [id,rects] of view.positions)markPositions.set(id,rects);
  updatePageControls();renderNavigation();renderCounts();
  $('#open-highlights').disabled=!ready;
  $('#render-status').innerHTML=`<i class="status-dot"></i>${ready?'PDF 连续阅读 · '+(readingRoot?.textContent.trim()?'可选择文字':'本页无可选文字'):'正在渲染当前页……'}`;
  if(changed){
    persist();
    if(!$('#notes-drawer').hidden)renderDrawer();
    if(notify){clearTimeout(pdfProgressTimer);pdfProgressTimer=setTimeout(()=>void AIReader.pageChanged(),200);}
  }
}
function pdfPageRendered(view) {
  paintPDFHighlights(view);
  void AIReader.pageRendered(view.number);
  if(view.number===pageNumber){activatePDFPage(view);clearTimeout(pdfProgressTimer);pdfProgressTimer=setTimeout(()=>void AIReader.pageChanged(),200);}
}
function highlightKey(h) { return `${key()}/${h.id}`; }
function bookNotes() { return state.notes.filter(note=>note.bookId===book.id); }
function chapterTitle(number = pageNumber) {
  if(book.kind!=='pdf')return book.chapters[number-1]?.title || `第 ${number} 节`;
  const chapter = book.outline?.filter(ch=>ch.page<=number).at(-1);
  return chapter ? `第 ${chapter.number} 章 · ${chapter.title}` : `第 ${number} 页`;
}
function canonical(text) { return text.replace(/\s/g,''); }
function contentRect(rect) {
  const frameRect = contentFrame?.getBoundingClientRect();
  return {left:rect.left+(frameRect?.left||0),top:rect.top+(frameRect?.top||0),width:rect.width,height:rect.height,right:rect.right+(frameRect?.left||0),bottom:rect.bottom+(frameRect?.top||0)};
}

// Large original files live in IndexedDB rather than localStorage.
let databasePromise;
function database() {
  if(!databasePromise)databasePromise=new Promise((resolve,reject)=>{
    const request=indexedDB.open('yejian-original-books',1);
    request.onupgradeneeded=()=>request.result.createObjectStore('books',{keyPath:'id'});
    request.onsuccess=()=>resolve(request.result);request.onerror=()=>reject(request.error);
  });
  return databasePromise;
}
async function storedBook(id) {
  const db=await database();
  return new Promise((resolve,reject)=>{const request=db.transaction('books','readonly').objectStore('books').get(id);request.onsuccess=()=>resolve(request.result);request.onerror=()=>reject(request.error);});
}
async function storeBook(record) {
  const db=await database();
  return new Promise((resolve,reject)=>{const transaction=db.transaction('books','readwrite');transaction.objectStore('books').put(record);transaction.oncomplete=resolve;transaction.onerror=()=>reject(transaction.error);});
}
async function pdfLibrary() {
  if(!pdfjsPromise)pdfjsPromise=import('./vendor/pdf.min.mjs').then(module=>{
    module.GlobalWorkerOptions.workerSrc=new URL('./vendor/pdf.worker.min.mjs',document.baseURI).href;
    return module;
  });
  return pdfjsPromise;
}
async function openPDF(bytes) {
  const pdfjs=await pdfLibrary();
  const task=pdfjs.getDocument({data:new Uint8Array(bytes.slice(0)),cMapUrl:'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/4.10.38/cmaps/',cMapPacked:true,standardFontDataUrl:'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/4.10.38/standard_fonts/'});
  task.onPassword=()=>{void task.destroy();};
  try{return await task.promise;}catch(error){if(/password|destroyed/i.test(error.message))throw new Error('这份 PDF 需要密码，请先解锁后重新打开。');throw error;}
}
async function showBook(metadata, documentProxy = null, initialPage = 1, sourceRecord = null) {
  AskReader.stop();
  closeBubble();hideSelection();ready=false;
  clearTimeout(pdfProgressTimer);PDFReader.destroy();activePDFView=null;
  $('#notes-drawer').hidden=true;
  renderTask?.cancel();textLayer?.cancel();
  const oldDoc=pdfDoc;pdfDoc=documentProxy;book=metadata;
  if(oldDoc&&oldDoc!==documentProxy)void oldDoc.destroy();
  pageNumber=Math.min(Math.max(1,initialPage),book.pageCount);
  navigationTab='contents';state.settings.zoom=1;
  $('#book-title').textContent=book.title;$('#breadcrumb-title').textContent=book.title;
  $('#book-author').textContent=book.author;$('#book-format').textContent=book.kind==='pdf'?'PDF 原版':book.kind.toUpperCase();
  $('.mini-cover').classList.toggle('quant-cover',!!book.isDemo);
  document.title=`${book.title} · 页问 AI 伴读`;
  void AIReader.connect(book,sourceRecord);
  applySettings();renderNavigation();renderCounts();persist();await renderCurrentPage();
}
async function loadDemo(reset = false) {
  const version=++loadVersion;
  ++renderVersion;ready=false;renderTask?.cancel();textLayer?.cancel();
  clearTimeout(pdfProgressTimer);PDFReader.destroy();activePDFView=null;
  $('#import-dialog').close();
  $('#document-stage').innerHTML='<div class="document-loading"><span class="loading-ring"></span>正在渲染原始 PDF……</div>';
  try{
    if(globalThis.PAGEASK_TEXT_SAMPLE){await loadReadingPractice(version,reset);return;}
    let bytes;
    if(globalThis.YEJIAN_PDF_BASE64)bytes=Uint8Array.from(atob(globalThis.YEJIAN_PDF_BASE64),c=>c.charCodeAt(0)).buffer;
    else {
      const response=await fetch(encodeURI(SAMPLE_BOOK.sourceFile));
      if(!response.ok){
        if(response.status!==404)throw new Error('无法打开示例 PDF');
        await loadReadingPractice(version,reset);return;
      }
      bytes=await response.arrayBuffer();
    }
    const documentProxy=await openPDF(bytes);
    if(version!==loadVersion){void documentProxy.destroy();return;}
    await showBook({...SAMPLE_BOOK,kind:'pdf',pageCount:documentProxy.numPages},documentProxy,reset?55:(state.reading[SAMPLE_BOOK.id]||55));
  }catch(error){if(version===loadVersion)showLoadError(error);}
}
async function loadReadingPractice(version,reset) {
  let bytes;
  if(globalThis.PAGEASK_TEXT_SAMPLE)bytes=new TextEncoder().encode(globalThis.PAGEASK_TEXT_SAMPLE).buffer;
  else{
    const response=await fetch('examples/reading-practice.txt');
    if(!response.ok)throw new Error('无法打开阅读样例，请导入自己的书籍。');
    bytes=await response.arrayBuffer();
  }
  if(version!==loadVersion)return;
  const record={id:'pageask-reading-practice-v1',name:'从一句话，读到一个问题.txt',kind:'txt',bytes};
  $('#load-demo strong').textContent='从一句话，读到一个问题';
  $('#load-demo small').textContent='原创中文阅读样例 · 可划线、思考与记笔记';
  await importRecord(record,reset?1:(state.reading[record.id]||1));
  try{await storeBook(record);}catch{/* Sample remains available from the server. */}
}
function showLoadError(error) {
  $('#document-stage').innerHTML=`<div class="document-error">${icon('book')}<h2>原书还没有打开</h2><p>${escapeHTML(error.message)}</p><p>可以使用单文件 demo.html，或导入自己的书籍。</p><button class="primary-button" id="retry-import">选择书籍</button></div>`;
}

function renderNavigation() {
  const bookmarks=state.bookmarks[book.id] || [];
  $('#bookmark-count').textContent=bookmarks.length;
  $('#contents-tab').classList.toggle('active',navigationTab==='contents');$('#bookmarks-tab').classList.toggle('active',navigationTab==='bookmarks');
  $('#contents-tab').setAttribute('aria-selected',navigationTab==='contents');$('#bookmarks-tab').setAttribute('aria-selected',navigationTab==='bookmarks');
  let entries;
  if(navigationTab==='bookmarks')entries=bookmarks.slice().sort((a,b)=>a-b).map(number=>({number,title:chapterTitle(number),page:number,detail:`第 ${number} ${book.kind==='pdf'?'页':'节'}`}));
  else if(book.outline)entries=book.outline;
  else if(book.kind==='pdf'){
    const start=Math.max(1,Math.floor((pageNumber-1)/30)*30+1);
    entries=Array.from({length:Math.min(30,book.pageCount-start+1)},(_,i)=>({number:start+i,page:start+i,title:`第 ${start+i} 页`}));
  }else entries=book.chapters.map((chapter,i)=>({number:i+1,title:chapter.title,page:i+1}));
  const currentChapter=book.outline?.filter(ch=>ch.page<=pageNumber).at(-1)?.page;
  $('#chapter-list').innerHTML=`<div class="part-label">${navigationTab==='bookmarks'?'留在这里的阅读坐标':book.outline?'主要章节':'阅读目录'}</div>`+(entries.length?entries.map(entry=>{
    const active=book.outline&&navigationTab!=='bookmarks'?entry.page===currentChapter:entry.page===pageNumber;
    return `<button class="chapter-item ${active?'active':''}" data-page="${entry.page}" ${active?'aria-current="page"':''}><span class="chapter-number">${pad(entry.number)}</span><span class="chapter-name">${escapeHTML(entry.title)}${entry.detail?`<small>${entry.detail}</small>`:''}</span>${active?'<span class="chapter-dot"></span>':''}</button>`;
  }).join(''):'<p class="chapter-empty">点击书页上方的书签图标，<br>为想回来的地方留一个坐标。</p>');
  const progress=Math.round(pageNumber/book.pageCount*100);
  $('#progress-label').textContent=`${progress}%`;$('#progress-fill').style.width=`${progress}%`;
  $('#progress-detail').textContent=`${book.kind==='pdf'?'PDF 第':'第'} ${pageNumber} / ${book.pageCount} ${book.kind==='pdf'?'页':'节'}`;
  const marked=bookmarks.includes(pageNumber);$('#bookmark-button').classList.toggle('active',marked);$('#bookmark-button').setAttribute('aria-label',marked?'移除书签':'添加书签');
}
function updatePageControls() {
  $('#view-badge').textContent=book.kind==='pdf'?'PDF 连续':book.kind==='epub'?'EPUB 正文':'文本阅读';
  document.body.classList.toggle('pdf-continuous',book.kind==='pdf');
  $('#current-chapter-label').textContent=chapterTitle();
  $('#page-input').value=pageNumber;$('#page-input').max=book.pageCount;$('#total-pages').textContent=`/ ${book.pageCount}`;
  $('#prev-page').disabled=pageNumber===1;$('#next-page').disabled=pageNumber===book.pageCount;
  $('#zoom-fit').textContent=`${Math.round(state.settings.zoom*100)}%`;
  $('#reader-footnote').textContent=book.kind==='pdf'?'已到本书末页 · 保留原书排版，让思考停在文字发生的地方。':'文字来自书内章节，选中后即可在原文上划线。';
}
async function renderCurrentPage({keepPosition=false}={}) {
  const version=++renderVersion;
  const pdfPosition=keepPosition&&book.kind==='pdf'?PDFReader.position():null;
  const scroll=$('#reader-scroll');const previousRatio=keepPosition?scroll.scrollTop/Math.max(1,scroll.scrollHeight):0;
  closeBubble();hideSelection();ready=false;readingRoot=null;contentFrame=null;markPositions.clear();
  $('#document-stage').setAttribute('aria-busy','true');$('#open-highlights').disabled=true;
  contentResizeObserver?.disconnect();contentResizeObserver=null;
  renderTask?.cancel();textLayer?.cancel();
  updatePageControls();
  try{
    if(book.kind==='pdf'){
      const pdfjs=await pdfLibrary();if(version!==renderVersion)return;
      const containerWidth=scroll.clientWidth;
      renderedContainerWidth=containerWidth;activePDFView=null;
      await PDFReader.mount({document:pdfDoc,pdfjs,stage:$('#document-stage'),scroll,initialPage:pageNumber,
        width:Math.max(260,Math.min(900,containerWidth-(innerWidth<=720?24:80))),zoom:state.settings.zoom,
        offset:pdfPosition?.offset,recoverText:recoverTextContent,onActive:activatePDFPage,
        onRender:pdfPageRendered,onClick:(view,event)=>{if(view.ready){activatePDFPage(view);handlePDFClick(event);}}});
      if(version!==renderVersion)return;
      bindSelectionDocument(document,$('#document-stage'));
      $('#document-stage').setAttribute('aria-busy','false');
      return;
    }
    PDFReader.destroy();activePDFView=null;$('#document-stage').classList.remove('continuous-pdf');
    await renderEPUBPage(version);
    if(version!==renderVersion)return;
    pageHighlights=AIReader.enabled()?AIReader.pageHighlights():[];
    ready=true;paintHighlights();renderCounts();
    $('#document-stage').setAttribute('aria-busy','false');$('#open-highlights').disabled=false;
    const hasText=!!readingRoot?.textContent.trim();
    $('#render-status').innerHTML=`<i class="status-dot"></i>${book.kind==='pdf'?(hasText?'原版 PDF · 可选择文字':'原版 PDF · 本页无可选文字，扫描件需要 OCR'):book.kind==='epub'?'EPUB 章节 · 可选择文字':'文本阅读 · 可选择文字'}`;
    scroll.scrollTop=keepPosition?previousRatio*scroll.scrollHeight:0;
    void AIReader.pageChanged();
  }catch(error){
    if(version!==renderVersion||error.name==='RenderingCancelledException'||error.name==='AbortException')return;
    showLoadError(error);console.error('Rendering failed:',error);
  }
}
function recoverTextContent(content,pdfPage) {
  const fonts=new Map();
  content.items.forEach(item=>{if(item.str)fonts.set(item.fontName,(fonts.get(item.fontName)||'')+item.str);});
  // Target only the supplied edition's verified missing-ToUnicode font signatures.
  const brokenFonts=new Set([...fonts].filter(([,text])=>text.includes('䱯ቄ⌅')||(text.includes('䟿ॆ')&&text.includes('Ⲵ'))).map(([font])=>font));
  for(const item of content.items)if(brokenFonts.has(item.fontName)||pdfPage.commonObjs.get(item.fontName)?.name?.includes('font000000002156afc2'))item.str=recoverBookCID(item.str);
}
function recoverBookCID(text) {
  const punctuation={'\u01c2':'　','\u01c3':'、','\u01c4':'。','\u01c9':'《','\u01ca':'》'};
  return [...text].map(character=>{if(punctuation[character])return punctuation[character];if('—–“”‘’…'.includes(character))return character;const code=character.codePointAt(0);if(code>=0x430&&code<=0x562f)return String.fromCodePoint(code+0x49d0);if(code>=0x2bd&&code<=0x31a)return String.fromCodePoint(code+0xfc44);return character;}).join('');
}
async function renderEPUBPage(version) {
  const chapter=book.chapters[pageNumber-1];
  const stage=$('#document-stage');
  stage.innerHTML='<div class="epub-sheet" id="epub-sheet"><iframe id="epub-frame" title="书籍原文" sandbox="allow-same-origin"></iframe></div>';
  const frame=$('#epub-frame');
  const baseCSS=`html{background:#fff;color:#15191f;overflow:hidden}body{max-width:680px;margin:0 auto;padding:54px 55px 68px;font-family:"Songti SC","Noto Serif CJK SC",serif;font-size:${state.settings.font}px;line-height:1.95;overflow-wrap:anywhere}img,svg{max-width:100%;height:auto}h1,h2,h3{line-height:1.6;margin:1.1em 0 .7em}p{margin:0 0 1.35em}a{color:inherit}::selection{background:#bdd8ff;color:inherit}mark[data-h-id]{color:inherit;cursor:pointer;box-decoration-break:clone;-webkit-box-decoration-break:clone;border-radius:1px;padding:1px 0}body.hide-highlights mark[data-h-id]{background:transparent!important;border-color:transparent!important}body.selecting mark[data-h-id]{cursor:text}@media(max-width:600px){body{padding:30px 25px 45px}}`;
  const source=`<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data: blob:; style-src 'unsafe-inline'; font-src data: blob:;"><style>${chapter.css||''}\n${baseCSS}</style></head><body>${chapter.html}</body></html>`;
  await new Promise(resolve=>{frame.onload=resolve;frame.srcdoc=source;});
  if(version!==renderVersion)return;
  contentFrame=frame;readingRoot=frame.contentDocument.body;
  const resize=()=>{if(version!==renderVersion)return;frame.style.height=`${Math.max(500,Math.ceil(readingRoot.getBoundingClientRect().height))}px`;if(ready)positionBubble();};
  resize();
  contentResizeObserver=new ResizeObserver(resize);contentResizeObserver.observe(readingRoot);
  frame.contentDocument.querySelectorAll('img').forEach(img=>img.addEventListener('load',resize));
  bindSelectionDocument(frame.contentDocument,readingRoot);
  readingRoot.addEventListener('click',event=>{
    const mark=event.target.closest('mark[data-h-id]');
    if(mark&&!frame.contentWindow.getSelection()?.toString().trim()){
      const h=highlights().find(item=>item.id===mark.dataset.hId);if(h)openBubble(h,0);
    }
    if(!event.target.closest('mark'))closeBubble();
    if(event.target.closest('a'))event.preventDefault();
  });
}

// Text offsets plus quote fallback keep EPUB annotations stable across reflow.
function textNodes(root=readingRoot) {
  const nodes=[];if(!root)return nodes;
  const walker=root.ownerDocument.createTreeWalker(root,NodeFilter.SHOW_TEXT);let node;
  while((node=walker.nextNode()))if(!node.parentElement.closest('script,style'))nodes.push(node);
  return nodes;
}
function rangeAtOffsets(start,end,root=readingRoot) {
  let offset=0,startNode,endNode,startOffset,endOffset;
  for(const node of textNodes(root)){
    const next=offset+node.length;
    if(!startNode&&start>=offset&&start<next){startNode=node;startOffset=start-offset;}
    if(end>offset&&end<=next){endNode=node;endOffset=end-offset;break;}
    offset=next;
  }
  if(!startNode||!endNode)return null;
  const range=root.ownerDocument.createRange();range.setStart(startNode,startOffset);range.setEnd(endNode,endOffset);return range;
}
function findQuoteRange(quote,root=readingRoot) {
  const characters=[],mapping=[];
  for(const node of textNodes(root))for(let i=0;i<node.length;i++)if(!/\s/.test(node.data[i])){characters.push(node.data[i]);mapping.push({node,offset:i});}
  const target=canonical(quote),index=characters.join('').indexOf(target);
  if(index<0||!target)return null;
  const first=mapping[index],last=mapping[index+target.length-1];
  const range=root.ownerDocument.createRange();range.setStart(first.node,first.offset);range.setEnd(last.node,last.offset+1);return range;
}
function highlightRange(h,root=readingRoot) {
  if(Number.isInteger(h.start)&&Number.isInteger(h.end)){
    const range=rangeAtOffsets(h.start,h.end,root);
    if(range&&canonical(range.toString())===canonical(h.quote))return range;
  }
  return findQuoteRange(h.quote,root);
}
function mergedRects(range) {
  const source=[...range.getClientRects()].filter(rect=>rect.width>.25&&rect.height>2).map(rect=>({left:rect.left,top:rect.top,right:rect.right,bottom:rect.bottom,width:rect.width,height:rect.height})).sort((a,b)=>Math.abs(a.top-b.top)<3?a.left-b.left:a.top-b.top);
  const result=[];
  for(const rect of source){
    const last=result.at(-1);
    if(last&&Math.abs((last.top+last.bottom)/2-(rect.top+rect.bottom)/2)<Math.max(3,Math.min(last.height,rect.height)*.25)&&rect.left<=last.right+3){last.right=Math.max(last.right,rect.right);last.left=Math.min(last.left,rect.left);last.top=Math.min(last.top,rect.top);last.bottom=Math.max(last.bottom,rect.bottom);last.width=last.right-last.left;last.height=last.bottom-last.top;}
    else result.push({...rect});
  }
  return result;
}
function paintPDFHighlights(view) {
    if(!view?.ready)return;
    const bounds=view.element.getBoundingClientRect(),overlay=view.overlay;overlay.innerHTML='';view.positions.clear();
    for(const h of highlights(view.number)){
      const range=highlightRange(h,view.textRoot);
      const rectangles=range?mergedRects(range).map(rect=>({x:(rect.left-bounds.left)/bounds.width,y:(rect.top-bounds.top)/bounds.height,w:rect.width/bounds.width,h:rect.height/bounds.height})):(h.rects||[]);
      view.positions.set(h.id,rectangles);
      rectangles.forEach((rect,i)=>{
        const mark=document.createElement('button');mark.className='pdf-mark';mark.dataset.highlightId=h.id;mark.dataset.rectIndex=i;mark.tabIndex=i===0?0:-1;
        mark.setAttribute('aria-label',`打开划线思考：${h.quote}`);mark.style.cssText=`left:${rect.x*100}%;top:${rect.y*100}%;width:${rect.w*100}%;height:${rect.h*100}%;--mark-fill:${COLORS[h.color||'yellow'].fill};--mark-line:${COLORS[h.color||'yellow'].line}`;
        mark.addEventListener('click',()=>{activatePDFPage(view);openBubble(h,i);});
        mark.addEventListener('keydown',event=>{if(['Enter',' '].includes(event.key)){event.preventDefault();activatePDFPage(view);openBubble(h,i);}});
        mark.classList.toggle('is-active',activeHighlight?.id===h.id&&view.number===pageNumber);overlay.append(mark);
      });
    }
    if(view.number===pageNumber){markPositions.clear();for(const [id,rects] of view.positions)markPositions.set(id,rects);}
}
function paintHighlights() {
  if(book.kind==='pdf'){
    for(const view of PDFReader.rendered())paintPDFHighlights(view);
  }else if(readingRoot){
    markPositions.clear();
    // Unwrap old marks first: offsets are measured in original text, never in annotation markup.
    readingRoot.querySelectorAll('mark[data-h-id]').forEach(mark=>mark.replaceWith(...mark.childNodes));readingRoot.normalize();
    for(const h of highlights()){
      const range=highlightRange(h);if(!range)continue;
      const before=readingRoot.ownerDocument.createRange();before.selectNodeContents(readingRoot);before.setEnd(range.startContainer,range.startOffset);
      const start=before.toString().length,end=start+range.toString().length;let offset=0;const parts=[];
      for(const node of textNodes()){
        const next=offset+node.length;
        if(next>start&&offset<end)parts.push({node,start:Math.max(0,start-offset),end:Math.min(node.length,end-offset)});offset=next;
      }
      for(const part of parts.reverse()){
        const slice=readingRoot.ownerDocument.createRange();slice.setStart(part.node,part.start);slice.setEnd(part.node,part.end);
        const mark=readingRoot.ownerDocument.createElement('mark');mark.dataset.hId=h.id;mark.tabIndex=0;mark.setAttribute('role','button');mark.setAttribute('aria-label',`打开划线思考：${h.quote}`);
        const color=COLORS[h.color||'yellow'];mark.style.setProperty('background',color.fill,'important');mark.style.setProperty('border-bottom',`1.5px solid ${color.line}`,'important');slice.surroundContents(mark);
        mark.addEventListener('keydown',event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();openBubble(h);}});
      }
    }
    readingRoot.classList.toggle('hide-highlights',!state.settings.highlights);
  }
  document.body.classList.toggle('hide-highlights',!state.settings.highlights);
  if(!$('#notes-drawer').hidden&&drawerTab==='highlights')renderDrawer();
}
function handlePDFClick(event) {
  if(!state.settings.highlights||window.getSelection()?.toString().trim())return;
  const bounds=$('#pdf-page').getBoundingClientRect();
  const x=(event.clientX-bounds.left)/bounds.width,y=(event.clientY-bounds.top)/bounds.height;
  for(const h of highlights().slice().reverse()){
    const rectangles=markPositions.get(h.id)||[];const index=rectangles.findIndex(r=>x>=r.x&&x<=r.x+r.w&&y>=r.y&&y<=r.y+r.h);
    if(index>=0){openBubble(h,index);return;}
  }
  closeBubble();
}
const boundDocuments=new WeakSet();
const boundSelectionSurfaces=new WeakSet();
function bindSelectionDocument(doc,selectionSurface) {
  if(!boundSelectionSurfaces.has(selectionSurface)){
    boundSelectionSurfaces.add(selectionSurface);
    selectionSurface.addEventListener('mouseup',()=>setTimeout(captureSelection,0));
    selectionSurface.addEventListener('touchend',()=>setTimeout(captureSelection,160));
  }
  if(boundDocuments.has(doc))return;boundDocuments.add(doc);
  doc.addEventListener('selectionchange',()=>{if(doc.getSelection()?.isCollapsed)$('#selection-popover').hidden=true;});
  if(doc!==document)doc.addEventListener('keydown',handleKeyboard);
}
function captureSelection() {
  if(book.kind==='pdf'){
    const selection=document.getSelection();
    if(!selection||selection.isCollapsed||!selection.rangeCount)return;
    const range=selection.getRangeAt(0),element=range.startContainer.parentElement?.closest('.pdf-page');
    const end=range.endContainer.parentElement?.closest('.pdf-page');
    if(element!==end){toast('请在同一书页内选择要划线的文字。');return;}
    const view=PDFReader.page(Number(element?.dataset.page));if(view?.ready)activatePDFPage(view);
  }
  if(!ready||!readingRoot)return;
  const doc=readingRoot.ownerDocument,selection=doc.getSelection();
  if(!selection||selection.isCollapsed||!selection.rangeCount)return;
  const range=selection.getRangeAt(0);
  if(!readingRoot.contains(range.startContainer)||!readingRoot.contains(range.endContainer))return;
  const raw=range.toString(),quote=raw.trim();
  if(quote.length<2)return;
  if(quote.length>1500){toast('一次选取 1500 字以内，更适合深入思考。');return;}
  const prefix=doc.createRange();prefix.selectNodeContents(readingRoot);prefix.setEnd(range.startContainer,range.startOffset);
  const start=prefix.toString().length+raw.indexOf(quote);
  const rects=mergedRects(range);if(!rects.length)return;
  const pageBounds=book.kind==='pdf'?$('#pdf-page').getBoundingClientRect():null;
  selectionSnapshot={quote,start,end:start+quote.length,pageKey:key(),rects:pageBounds?rects.map(r=>({x:(r.left-pageBounds.left)/pageBounds.width,y:(r.top-pageBounds.top)/pageBounds.height,w:r.width/pageBounds.width,h:r.height/pageBounds.height})):undefined};
  closeBubble();const anchor=contentRect(rects.at(-1));const popover=$('#selection-popover');popover.hidden=false;
  const width=Math.min(330,innerWidth-24);popover.style.width=`${width}px`;popover.style.left=`${Math.min(innerWidth-width-12,Math.max(12,anchor.left+anchor.width/2-width/2))}px`;
  popover.style.top=`${anchor.top>130?anchor.top-50:anchor.bottom+10}px`;
}
function hideSelection() { $('#selection-popover').hidden=true;selectionSnapshot=null; }
function createHighlight(withBubble,tab='question') {
  if(!selectionSnapshot||selectionSnapshot.pageKey!==key())return;
  const snapshot=selectionSnapshot;
  let h=highlights().find(item=>canonical(item.quote)===canonical(snapshot.quote));
  if(!h){h={quote:snapshot.quote,kind:'manual',concepts:[],id:uid(),start:snapshot.start,end:snapshot.end,rects:snapshot.rects,color:selectedColor,custom:true,createdAt:Date.now()};(state.annotations[key()] ||= []).push(h);}
  readingRoot.ownerDocument.getSelection()?.removeAllRanges();hideSelection();paintHighlights();renderCounts();persist();
  if(withBubble)openBubble(h,0,tab);else toast('已划线。点一下划线，就能打开思考气泡。');
}

function openBubble(h,rectIndex=0,tab='question') {
  if(!h||!ready)return;
  activeHighlight=h;bubbleAnchorIndex=rectIndex;bubbleTab=tab;
  hideSelection();$('#thought-bubble').hidden=false;renderBubble();positionBubble();
  $$('.pdf-mark').forEach(mark=>mark.classList.toggle('is-active',mark.dataset.highlightId===h.id));
  if(tab==='ask')$('#ask-input')?.focus({preventScroll:true});
}
function closeBubble() {
  $('#thought-bubble').hidden=true;activeHighlight=null;
  $$('.pdf-mark.is-active').forEach(mark=>mark.classList.remove('is-active'));
}
function anchorBounds() {
  if(!activeHighlight||!readingRoot)return null;
  if(book.kind==='pdf'){
    const rect=(markPositions.get(activeHighlight.id)||[])[bubbleAnchorIndex];const page=$('#pdf-page')?.getBoundingClientRect();if(!rect||!page)return null;
    return {left:page.left+rect.x*page.width,top:page.top+rect.y*page.height,width:rect.w*page.width,height:rect.h*page.height};
  }
  const marks=[...readingRoot.querySelectorAll('mark[data-h-id]')].filter(mark=>mark.dataset.hId===activeHighlight.id);
  if(!marks.length)return null;
  const rect=marks[Math.min(bubbleAnchorIndex,marks.length-1)].getBoundingClientRect();return contentRect(rect);
}
function positionBubble() {
  const bubble=$('#thought-bubble');if(bubble.hidden)return;
  const anchor=anchorBounds();if(!anchor){closeBubble();return;}
  const readerBounds=$('#reader-scroll').getBoundingClientRect();
  if(anchor.top+anchor.height<readerBounds.top||anchor.top>readerBounds.bottom){closeBubble();return;}
  const width=Math.min(380,innerWidth-24);bubble.style.width=`${width}px`;
  const maxHeight=Math.min(670,innerHeight-100);bubble.style.maxHeight=`${maxHeight}px`;
  const height=Math.min(bubble.offsetHeight,maxHeight),margin=12;
  const x=anchor.left+Math.min(anchor.width*.6,210),y=anchor.top+anchor.height/2;
  let left,top,side;
  if(innerWidth>720&&x+width+32<innerWidth){left=x+22;top=y-100;side='left';}
  else if(innerWidth>720&&anchor.left>width+32){left=anchor.left-width-22;top=y-100;side='right';}
  else {left=Math.max(margin,Math.min(innerWidth-width-margin,x-width/2));if(anchor.top+anchor.height+height+20<innerHeight){top=anchor.top+anchor.height+14;side='top';}else{top=anchor.top-height-14;side='bottom';}}
  top=Math.min(innerHeight-height-margin,Math.max(84,top));left=Math.min(innerWidth-width-margin,Math.max(margin,left));
  bubble.style.left=`${left}px`;bubble.style.top=`${top}px`;bubble.dataset.arrow=side;
  bubble.style.setProperty('--arrow-x',`${Math.max(20,Math.min(width-20,x-left))}px`);bubble.style.setProperty('--arrow-y',`${Math.max(20,Math.min(height-20,y-top))}px`);
}
function questionForHighlight(h) {
  if((h.llm||h.preset)&&h.question&&h.choices?.length===3)return h;
  if(!h.custom)return null;
  const quote=canonical(h.quote);
  if(quote.length<8)return null;
  return AIReader.pageHighlights().filter(item=>{
    const source=canonical(item.quote);
    return item.llm&&(quote.includes(source)||(source.includes(quote)&&quote.length/source.length>=.6));
  }).sort((a,b)=>b.quote.length-a.quote.length)[0]||null;
}
function answerKey(h) {
  const question=questionForHighlight(h);
  return h.custom&&question?`${highlightKey(h)}/question/${question.id}`:highlightKey(h);
}
function renderQuestion(h,selected) {
  const answered=Number.isInteger(selected),correct=selected===h.correct;
  const choices=h.choices.map((choice,i)=>{
    const classes=['choice-button'];
    if(selected===i)classes.push('selected');
    if(answered&&i===h.correct)classes.push('is-correct');
    if(selected===i&&!correct)classes.push('reconsider');
    return `<button class="${classes.join(' ')}" data-choice="${i}" aria-pressed="${selected===i}"><span class="choice-letter">${String.fromCharCode(65+i)}</span><span>${escapeHTML(choice)}</span>${answered&&i===h.correct?icon('check'):''}</button>`;
  }).join('');
  let feedback=`<button id="hint-button" class="hint-button">${icon('bulb')}给我一点线索</button><div class="hint-content" id="hint-content" hidden>${escapeHTML(h.hint)}</div>`;
  if(answered)feedback=`<div class="choice-feedback ${correct?'affirm':'rethink'}"><strong>${icon(correct?'check':'bulb')}${correct?'抓住了这个区分':`再看一眼原文 · ${String.fromCharCode(65+h.correct)} 更贴近作者的意思`}</strong><p>${escapeHTML(h.explanation)}</p><div class="followup"><span>再往深处想</span>${escapeHTML(h.followup)}</div></div>`;
  return `<div class="question-label">先做一个判断 <span>单选</span></div><h3>${escapeHTML(h.question)}</h3><div class="choice-list" role="group" aria-label="选择你的判断">${choices}</div>${feedback}`;
}
function renderMissingQuestion() {
  let message='这处划线没有关联的 AI 思考题。可以查看本页已有的 AI 划线，或先记下自己的问题。';
  if(!AIReader.enabled())message='当前未连接真实 AI。通过本地服务打开阅读器并配置模型后，才能获取针对原文的题目；划线和笔记可以继续使用。';
  else if(!AIReader.hasPage())message='本页 AI 批注尚未完成。批注到达后，重合的划线会关联实际题目；你也可以先记下自己的问题。';
  return `<div class="missing-question"><span>${icon('info')}暂无关联的 AI 思考题</span><p>${message}</p></div>`;
}
function renderNoteEditor(draft,hasQuestion,editing) {
  return `<button class="reflection-button" id="start-answer" ${editing?'hidden':''}>${icon('pen')}${hasQuestion?'记下我的理解':'记录自己的问题或理解'}${icon('chevron')}</button><div class="answer-area" id="answer-area" ${editing?'':'hidden'}><textarea id="answer-input" rows="3" maxlength="5000" placeholder="写下理由、疑问，或一个自己的例子……" aria-label="写下你的理解">${escapeHTML(draft)}</textarea><div class="answer-tools"><span>${hasQuestion?'可只保存当前选择':'笔记保存在当前页'}</span><button id="save-answer">存入笔记${icon('check')}</button></div></div><div class="saved-response" id="saved-response" hidden>${icon('check')}已存入笔记，回头也能找到。</div>`;
}
function renderBubble() {
  const original=activeHighlight;if(!original)return;
  const askInput=document.activeElement?.id==='ask-input'?document.activeElement:null;
  const selection=askInput?[askInput.selectionStart,askInput.selectionEnd]:null;
  const question=questionForHighlight(original);
  const h=question?{...original,...question,id:original.id,custom:original.custom,quote:original.quote}:original;
  const selected=question?state.answers[answerKey(original)]:undefined;
  const draft=state.drafts[highlightKey(original)]||'',concepts=question?.concepts||[];
  const editing=Object.prototype.hasOwnProperty.call(state.drafts,highlightKey(original));
  const names={core:'核心观点',connect:'知识关联',challenge:'值得追问'};
  $('#bubble-title').textContent=`第 ${pageNumber} ${book.kind==='pdf'?'页':'节'} · ${names[h.kind]||'我的划线'}`;
  let sourceLabel='我的划线 · 暂无 AI 思考题';
  if(question?.llm)sourceLabel=original.custom?'关联本页 AI 批注 · 原文核验通过':'AI 生成 · 原文核验通过';
  else if(question?.preset)sourceLabel='预设示例题 · 离线模式';
  $('#bubble-source-label').textContent=bubbleTab==='ask'?'AI 回答 · 结合所选原文与本页内容':sourceLabel;
  $('#thought-bubble .bubble-footer>span:last-child').textContent=bubbleTab==='ask'?'理解原文，继续追问':'先判断，再理解';
  $('#bubble-delete').hidden=!original.custom;$('#bubble-delete').innerHTML=icon('trash');$('#bubble-delete').title='删除这条划线';
  let body;
  if(bubbleTab==='question')body=`<div class="bubble-question">${question?renderQuestion(h,selected):renderMissingQuestion()}${renderNoteEditor(draft,Boolean(question),editing)}</div>`;
  else if(bubbleTab==='ask')body=AskReader.render(original);
  else body=`<div class="connections"><p class="connection-intro">${concepts.length?'知识定义跨页复用；原文概念与延伸理解分别标明。':'本处暂无已验证的关联知识。'}</p>${concepts.map(concept=>{const c=AIReader.concept(h,concept);return `<button class="concept-card" data-concept="${concept}" aria-expanded="false"><span class="concept-icon">${icon(c.icon)}</span><span><strong>${c.name}</strong><small>${c.subtitle}</small></span>${icon('chevron')}</button><p class="concept-detail" data-detail="${concept}" hidden>${c.description}</p>`;}).join('')}</div>`;
  $('#bubble-body').innerHTML=`<blockquote class="bubble-quote">${escapeHTML(h.quote)}</blockquote>${h.rationale?`<p class="annotation-rationale">${escapeHTML(h.rationale)}</p>`:''}<div class="bubble-tabs" role="tablist"><button data-bubble-tab="question" class="${bubbleTab==='question'?'active':''}" role="tab" aria-selected="${bubbleTab==='question'}">${icon('spark')}思考题</button><button data-bubble-tab="ask" class="${bubbleTab==='ask'?'active':''}" role="tab" aria-selected="${bubbleTab==='ask'}">${icon('bulb')}问 AI</button><button data-bubble-tab="connections" class="${bubbleTab==='connections'?'active':''}" role="tab" aria-selected="${bubbleTab==='connections'}">${icon('network')}关联知识 <span>${concepts.length}</span></button></div>${body}`;
  MathRender.render($('#bubble-body'));
  requestAnimationFrame(positionBubble);
  if(selection&&$('#ask-input')&&!$('#ask-input').disabled){$('#ask-input').focus({preventScroll:true});$('#ask-input').setSelectionRange(...selection);}
}
function saveAnswer() {
  const h=activeHighlight;if(!h)return;
  const question=questionForHighlight(h),thought=$('#answer-input').value.trim();
  const selected=question?state.answers[answerKey(h)]:undefined;
  const answer=(Number.isInteger(selected)&&question.choices[selected]?`我的选择：${String.fromCharCode(65+selected)}. ${question.choices[selected]}${thought?'\n\n我的想法：\n':''}`:'')+thought;
  if(!answer){toast(question?'先选择一个判断，或写下一点自己的想法。':'先写下一点自己的问题或想法。');return;}
  state.notes.push({id:uid(),bookId:book.id,bookTitle:book.title,page:pageNumber,chapter:chapterTitle(),quote:h.quote,question:question?.question||'我的问题与理解',answer,createdAt:Date.now()});
  delete state.drafts[highlightKey(h)];persist();renderCounts();$('#answer-area').hidden=true;$('#start-answer').hidden=true;$('#saved-response').hidden=false;
  if(!$('#notes-drawer').hidden&&drawerTab==='notes')renderDrawer();positionBubble();
}
function renderCounts() {
  const count=bookNotes().length;$('#note-count').textContent=count;$('#drawer-note-count').textContent=count;$('#highlight-count').textContent=highlights().length;$('#drawer-highlight-count').textContent=highlights().length;
}
function openDrawer(tab='notes') { drawerTab=tab;$('#notes-drawer').hidden=false;renderDrawer(); }
function renderDrawer() {
  $('#drawer-highlights-tab').classList.toggle('active',drawerTab==='highlights');$('#drawer-notes-tab').classList.toggle('active',drawerTab==='notes');
  $('#drawer-highlights-tab').setAttribute('aria-selected',drawerTab==='highlights');$('#drawer-notes-tab').setAttribute('aria-selected',drawerTab==='notes');
  $('#drawer-knowledge-tab').classList.toggle('active',drawerTab==='knowledge');$('#drawer-knowledge-tab').setAttribute('aria-selected',drawerTab==='knowledge');
  if(drawerTab==='knowledge'){$('#drawer-content').innerHTML=AIReader.renderKnowledge();MathRender.render($('#drawer-content'));return;}
  if(drawerTab==='highlights'){
    $('#drawer-content').innerHTML=`<p class="drawer-section-label">第 ${pageNumber} ${book.kind==='pdf'?'页':'节'} · ${highlights().length} 处划线</p>`+(highlights().length?highlights().map(h=>`<button class="highlight-list-item" data-open-highlight="${h.id}"><span class="highlight-stripe ${h.color||'yellow'}"></span><span><strong>${h.custom?'我的划线':'阅读线索'}</strong><q>${escapeHTML(h.quote)}</q><small>点击回到原文，并打开思考气泡 ${icon('arrow')}</small></span></button>`).join(''):`<div class="empty-state">${icon('pen')}<h3>从你在意的一句话开始</h3><p>在原文中拖选文字，点击「划线」。</p></div>`);
  }else{
    const notes=bookNotes().slice().reverse();
    $('#drawer-content').innerHTML=notes.length?`<div class="notes-heading"><span>${notes.length} 条阅读笔记</span><button id="export-notes">${icon('download')}导出阅读记录</button></div>${notes.map(note=>`<article class="note-card"><blockquote>${escapeHTML(note.quote)}</blockquote><div class="note-question">${escapeHTML(note.question)}</div><p>${escapeHTML(note.answer)}</p><footer><span>${note.page?`第 ${note.page} ${book.kind==='pdf'?'页':'节'} · `:''}${new Date(note.createdAt).toLocaleDateString('zh-CN')}</span><button data-delete-note="${note.id}">删除</button></footer></article>`).join('')}`:`<div class="empty-state">${icon('pen')}<h3>把理解，写成自己的话</h3><p>点击划线，在思考气泡里做一个判断，<br>或记下此刻的想法。</p></div>`;
  }
  MathRender.render($('#drawer-content'));
}
function scrollToHighlight(id) {
  const h=highlights().find(item=>item.id===id);if(!h)return;
  $('#notes-drawer').hidden=true;
  if(book.kind==='pdf'){
    const rect=(markPositions.get(id)||[])[0],page=$('#pdf-page');if(rect&&page)$('#reader-scroll').scrollTop=page.offsetTop+rect.y*page.offsetHeight-$('#reader-scroll').clientHeight*.42;
  }else{
    const mark=[...readingRoot.querySelectorAll('mark[data-h-id]')].find(item=>item.dataset.hId===id);
    if(mark){const frameTop=contentFrame.offsetTop;$('#reader-scroll').scrollTop=frameTop+mark.getBoundingClientRect().top-150;}
  }
  openBubble(h);
}
function exportNotes() {
  const notes=bookNotes();
  const allMarks=Object.entries(state.annotations).filter(([pageKey])=>pageKey.startsWith(`${book.id}/`)).flatMap(([pageKey,list])=>list.map(h=>({page:pageKey.split('/').at(-1),quote:h.quote})));
  const markdown=`# ${book.title} · 阅读记录\n\n导出自「页问」AI 伴读 Demo\n\n## 我的划线\n\n${allMarks.length?allMarks.map(h=>`- 第 ${h.page} 页：${h.quote}\n`).join(''):'暂无自主划线。\n'}\n## 我的笔记\n\n`+notes.map(note=>`### ${note.chapter}${note.page?` · 第 ${note.page} 页`:''}\n\n> ${note.quote.replace(/\n/g,'\n> ')}\n\n**思考问题：** ${note.question}\n\n${note.answer}\n\n---\n`).join('\n');
  const url=URL.createObjectURL(new Blob([markdown],{type:'text/markdown;charset=utf-8'}));const link=document.createElement('a');link.href=url;link.download=`${book.title}-阅读记录.md`;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
}

function applySettings() {
  state.settings.font=Math.min(28,Math.max(15,state.settings.font));$('#font-value').textContent=state.settings.font;$('#highlight-toggle').checked=state.settings.highlights;
  document.body.classList.toggle('hide-highlights',!state.settings.highlights);
  if(readingRoot&&book.kind!=='pdf'){readingRoot.style.fontSize=`${state.settings.font}px`;readingRoot.classList.toggle('hide-highlights',!state.settings.highlights);}
}
async function navigateTo(number,{jump=Math.abs(number-pageNumber)>1}={}) {
  if(!Number.isInteger(number)||number<1||number>book.pageCount)return;
  if(number===pageNumber)return;
  AskReader.stop();
  if(book.kind==='pdf'&&PDFReader.page(number)){
    clearTimeout(pdfProgressTimer);closeBubble();hideSelection();
    activatePDFPage(PDFReader.page(number),{notify:false});PDFReader.goTo(number);
    void AIReader.pageChanged({restart:jump});
    $('#library-sidebar').classList.remove('mobile-open');persist();return;
  }
  pageNumber=number;ready=false;
  void AIReader.pageChanged({restart:jump});
  $('#library-sidebar').classList.remove('mobile-open');renderNavigation();persist();await renderCurrentPage();
}
function openImport() { $('#import-status').hidden=true;$('#file-input').value='';$('#import-dialog').showModal(); }
function importStatus(message,error=false) { $('#import-status').hidden=false;$('#import-status').textContent=message;$('#import-status').classList.toggle('error',error); }
function dataURI(bytes,mime) {
  let binary='';for(let i=0;i<bytes.length;i+=8192)binary+=String.fromCharCode(...bytes.subarray(i,i+8192));return `data:${mime};base64,${btoa(binary)}`;
}
function epubPath(href,base) { return decodeURIComponent(new URL(href,`https://epub.local/${base}`).pathname.slice(1)); }
async function parseEPUB(bytes) {
  const {unzipSync,strFromU8}=await import('./vendor/fflate.mjs');let extracted=0;
  const files=unzipSync(new Uint8Array(bytes),{filter(entry){
    if(!/\.(xml|opf|xhtml|html|htm|css|png|jpe?g|gif|webp|woff2?|ttf|otf)$/i.test(entry.name))return false;
    extracted+=entry.originalSize;if(extracted>60_000_000||entry.originalSize>12_000_000)throw new Error('书籍解压体积较大，请拆分后导入。');return true;
  }});
  const parseXML=data=>new DOMParser().parseFromString(strFromU8(data),'application/xml');
  const elements=(doc,name)=>[...doc.getElementsByTagNameNS('*',name)];
  if(!files['META-INF/container.xml'])throw new Error('未找到 EPUB 目录，请确认格式有效。');
  const packagePath=elements(parseXML(files['META-INF/container.xml']),'rootfile')[0]?.getAttribute('full-path');
  if(!files[packagePath])throw new Error('未找到 EPUB 书籍信息，文件可能受 DRM 保护。');
  const opf=parseXML(files[packagePath]);
  const title=elements(opf,'title')[0]?.textContent?.trim(),author=elements(opf,'creator')[0]?.textContent?.trim();
  const manifest=new Map(elements(opf,'item').map(item=>[item.getAttribute('id'),{href:item.getAttribute('href'),type:item.getAttribute('media-type'),properties:item.getAttribute('properties')||''}]));
  const spine=elements(opf,'itemref').map(ref=>manifest.get(ref.getAttribute('idref'))).filter(item=>item&&/html/.test(item.type)&&!item.properties.split(/\s/).includes('nav'));
  const mimeFor=path=>/\.png$/i.test(path)?'image/png':/\.jpe?g$/i.test(path)?'image/jpeg':/\.gif$/i.test(path)?'image/gif':/\.webp$/i.test(path)?'image/webp':/\.woff2?$/i.test(path)?'font/woff2':'font/ttf';
  const asset=path=>files[path]?dataURI(files[path],mimeFor(path)):'';
  const cssResources=(css,path)=>css.replace(/url\(\s*(['"]?)(.*?)\1\s*\)/gi,(_,q,href)=>`url("${asset(epubPath(href,path))}")`).replace(/@import[^;]+;/gi,'');
  const chapters=[];
  for(const item of spine){
    const path=epubPath(item.href,packagePath);if(!files[path])continue;
    const doc=new DOMParser().parseFromString(strFromU8(files[path]),'text/html');
    const heading=doc.querySelector('h1,h2,h3')?.textContent?.trim()||doc.title||`第 ${chapters.length+1} 节`;
    const styles=[...doc.querySelectorAll('style')].map(style=>cssResources(style.textContent,path));
    doc.querySelectorAll('link[rel="stylesheet"]').forEach(link=>{const stylePath=epubPath(link.getAttribute('href'),path);if(files[stylePath])styles.push(cssResources(strFromU8(files[stylePath]),stylePath));});
    doc.querySelectorAll('script,iframe,object,embed,form,input,button,textarea,meta,link,style,base,video,audio,source,svg').forEach(element=>element.remove());
    doc.querySelectorAll('*').forEach(element=>{
      for(const attr of [...element.attributes])if(/^on/i.test(attr.name)||['srcdoc','srcset','formaction','target'].includes(attr.name))element.removeAttribute(attr.name);
      if(element.hasAttribute('style'))element.setAttribute('style',cssResources(element.getAttribute('style'),path));
      if(element.tagName==='IMG'){const src=element.getAttribute('src');const resolved=src?asset(epubPath(src,path)):'';if(resolved)element.setAttribute('src',resolved);else element.removeAttribute('src');}
      else element.removeAttribute('src');
      if(element.hasAttribute('href')&&!element.getAttribute('href').startsWith('#'))element.removeAttribute('href');
    });
    if(doc.body.textContent.trim()||doc.querySelector('img'))chapters.push({title:heading,html:doc.body.innerHTML,css:styles.join('\n')});
  }
  if(!chapters.length)throw new Error('未读取到 EPUB 章节，暂不支持受 DRM 保护的书籍。');
  return {title,author,chapters};
}
function parseTXT(bytes,name) {
  const text=new TextDecoder().decode(bytes);const paragraphs=text.replace(/\r\n?/g,'\n').split(/\n\s*\n/).filter(p=>p.trim());
  if(!paragraphs.length)throw new Error('文件中没有可阅读文字。');
  const groups=[];let group=[],length=0;
  for(const paragraph of paragraphs){if(length+paragraph.length>3000&&group.length){groups.push(group);group=[];length=0;}group.push(paragraph);length+=paragraph.length;}
  if(group.length)groups.push(group);
  return {chapters:groups.map((parts,i)=>({title:i?`继续阅读 · ${i+1}`:name,html:`<h1>${escapeHTML(i?`继续阅读 · ${i+1}`:name)}</h1>`+parts.map(p=>`<p>${escapeHTML(p).replace(/\n/g,'<br>')}</p>`).join(''),css:''}))};
}
async function importRecord(record,initialPage=1) {
  const version=++loadVersion;const {id,name,bytes,kind}=record;
  const title=name.replace(/\.[^.]+$/,'');
  if(kind==='pdf'){
    const documentProxy=await openPDF(bytes);if(version!==loadVersion){void documentProxy.destroy();return;}
    await showBook({id,title,author:'本地 PDF · 原始版式',kind,pageCount:documentProxy.numPages},documentProxy,initialPage,record);
  }else{
    const parsed=kind==='epub'?await parseEPUB(bytes):parseTXT(bytes,title);if(version!==loadVersion)return;
    await showBook({id,title:parsed.title||title,author:parsed.author||'本地书籍 · 原文阅读',kind,chapters:parsed.chapters,pageCount:parsed.chapters.length},null,initialPage,record);
  }
}
async function importFile(file) {
  if(!file||importBusy)return;
  const kind=file.name.split('.').at(-1).toLowerCase();
  if(!['pdf','epub','txt'].includes(kind)){importStatus('请选择 PDF、EPUB 或 TXT 文件。',true);return;}
  if(file.size>20*1024*1024){importStatus('文件超过 20 MB，请拆分后再导入。',true);return;}
  importBusy=true;$('#drop-zone').classList.add('import-loading');importStatus('正在本地打开原件……');
  try{
    const record={id:`file-${file.name}-${file.size}-${file.lastModified}`,name:file.name,kind,bytes:await file.arrayBuffer()};
    await importRecord(record,state.reading[record.id]||1);
    try{await storeBook(record);}catch{toast('当前浏览器未能保存原件；刷新后需重新选择书籍。');}
    $('#import-dialog').close();
  }catch(error){console.error('Import failed:',error);importStatus(`打开失败：${error.message}`,true);}
  finally{importBusy=false;$('#drop-zone').classList.remove('import-loading');$('#file-input').value='';}
}

$('#home-link').addEventListener('click',event=>{event.preventDefault();void loadDemo(true);});
$('#load-demo').addEventListener('click',()=>void loadDemo(true));
$$('.import-trigger').forEach(button=>button.addEventListener('click',openImport));
$('#file-input').addEventListener('change',event=>void importFile(event.target.files[0]));
$('#drop-zone').addEventListener('dragover',event=>{event.preventDefault();event.currentTarget.classList.add('dragover');});
$('#drop-zone').addEventListener('dragleave',event=>event.currentTarget.classList.remove('dragover'));
$('#drop-zone').addEventListener('drop',event=>{event.preventDefault();event.currentTarget.classList.remove('dragover');void importFile(event.dataTransfer.files[0]);});
$('#document-stage').addEventListener('click',event=>{if(event.target.closest('#retry-import'))openImport();});
$('#chapter-list').addEventListener('click',event=>{const button=event.target.closest('[data-page]');if(button)void navigateTo(Number(button.dataset.page),{jump:true});});
$('#contents-tab').addEventListener('click',()=>{navigationTab='contents';renderNavigation();});
$('#bookmarks-tab').addEventListener('click',()=>{navigationTab='bookmarks';renderNavigation();});
$('#prev-page').addEventListener('click',()=>void navigateTo(pageNumber-1));$('#next-page').addEventListener('click',()=>void navigateTo(pageNumber+1));
$('#page-input').addEventListener('change',event=>{const number=Number(event.target.value);if(Number.isInteger(number)&&number>=1&&number<=book.pageCount)void navigateTo(number,{jump:true});else{$('#page-input').value=pageNumber;toast(`请输入 1 到 ${book.pageCount} 之间的页码。`);}});
$('#bookmark-button').addEventListener('click',()=>{const list=state.bookmarks[book.id] ||= [],index=list.indexOf(pageNumber);if(index>=0)list.splice(index,1);else list.push(pageNumber);persist();renderNavigation();toast(index>=0?'已移除本页书签。':'已为这一页留下书签。');});
for(const [id,change] of [['zoom-in',.15],['zoom-out',-.15],['zoom-fit',0]])$('#'+id).addEventListener('click',()=>{state.settings.zoom=change?Math.max(.6,Math.min(2,state.settings.zoom+change)):1;persist();void renderCurrentPage({keepPosition:true});});
$('#focus-button').addEventListener('click',()=>{const enabled=document.body.classList.toggle('focus-mode');$('#focus-button').classList.toggle('active',enabled);$('#focus-button').setAttribute('aria-label',enabled?'退出专注阅读':'进入专注阅读');});
$('#mobile-contents').addEventListener('click',()=>{if(innerWidth<=900)$('#library-sidebar').classList.toggle('mobile-open');else document.body.classList.toggle('collapsed-contents');});
$('#settings-button').addEventListener('click',()=>{$('#settings-popover').hidden=!$('#settings-popover').hidden;});$('#close-settings').addEventListener('click',()=>{$('#settings-popover').hidden=true;});
$('#highlight-toggle').addEventListener('change',event=>{state.settings.highlights=event.target.checked;applySettings();persist();if(!state.settings.highlights)closeBubble();});
for(const [id,change] of [['font-smaller',-1],['font-larger',1]])$('#'+id).addEventListener('click',()=>{state.settings.font+=change;applySettings();persist();});
$('#open-highlights').addEventListener('click',()=>openDrawer('highlights'));$('#notes-button').addEventListener('click',()=>openDrawer('notes'));
$('#close-drawer').addEventListener('click',()=>{$('#notes-drawer').hidden=true;});
$('#drawer-highlights-tab').addEventListener('click',()=>{drawerTab='highlights';renderDrawer();});$('#drawer-notes-tab').addEventListener('click',()=>{drawerTab='notes';renderDrawer();});
$('#drawer-content').addEventListener('click',event=>{
  const highlight=event.target.closest('[data-open-highlight]');if(highlight)scrollToHighlight(highlight.dataset.openHighlight);
  if(event.target.closest('#export-notes'))exportNotes();
  const button=event.target.closest('[data-delete-note]');if(button){if(button.dataset.confirm!=='true'){button.dataset.confirm='true';button.textContent='确认删除';return;}state.notes=state.notes.filter(note=>note.id!==button.dataset.deleteNote);persist();renderCounts();renderDrawer();}
});
$('#close-bubble').addEventListener('click',closeBubble);
$('#bubble-delete').addEventListener('click',()=>{if(!activeHighlight?.custom)return;AskReader.stop();state.annotations[key()]=annotations().filter(h=>h.id!==activeHighlight.id);closeBubble();paintHighlights();renderCounts();persist();toast('已删除这条划线。');});
$('#bubble-body').addEventListener('click',event=>{
  const tab=event.target.closest('[data-bubble-tab]');if(tab){bubbleTab=tab.dataset.bubbleTab;renderBubble();return;}
  const choice=event.target.closest('[data-choice]');if(choice){state.answers[answerKey(activeHighlight)]=Number(choice.dataset.choice);persist();const scroll=$('#bubble-scroll').scrollTop;renderBubble();$('#bubble-scroll').scrollTop=scroll;return;}
  if(event.target.closest('#hint-button')){$('#hint-content').hidden=!$('#hint-content').hidden;positionBubble();}
  if(event.target.closest('#start-answer')){state.drafts[highlightKey(activeHighlight)]??='';$('#answer-area').hidden=false;$('#start-answer').hidden=true;positionBubble();$('#answer-input').focus({preventScroll:true});}
  if(event.target.closest('#save-answer'))saveAnswer();
  const concept=event.target.closest('[data-concept]');if(concept){const detail=$(`[data-detail="${concept.dataset.concept}"]`);detail.hidden=!detail.hidden;concept.setAttribute('aria-expanded',!detail.hidden);positionBubble();}
});
$('#bubble-body').addEventListener('input',event=>{if(event.target.id==='answer-input'&&activeHighlight){state.drafts[highlightKey(activeHighlight)]=event.target.value;persist();}});
$('#selection-popover').addEventListener('pointerdown',event=>event.preventDefault());
$$('[data-color]').forEach(button=>button.addEventListener('click',()=>{selectedColor=button.dataset.color;$$('[data-color]').forEach(b=>{b.classList.toggle('active',b===button);b.setAttribute('aria-pressed',b===button);});}));
$('#highlight-selection').addEventListener('click',()=>createHighlight(false));$('#explore-selection').addEventListener('click',()=>createHighlight(true));
$('#ask-selection').addEventListener('click',()=>createHighlight(true,'ask'));
for(const id of ['analysis-info','about-button'])$('#'+id).addEventListener('click',()=>$('#info-dialog').showModal());$('#understood-button').addEventListener('click',()=>$('#info-dialog').close());
$('#reader-scroll').addEventListener('scroll',()=>{positionBubble();$('#selection-popover').hidden=true;},{passive:true});
document.addEventListener('pointerdown',event=>{
  if(!event.target.closest('#thought-bubble,#selection-popover,.pdf-page,.notes-drawer'))closeBubble();
  if(!event.target.closest('#settings-popover,#settings-button'))$('#settings-popover').hidden=true;
  if(!event.target.closest('#library-sidebar,#mobile-contents'))$('#library-sidebar').classList.remove('mobile-open');
  if(!event.target.closest('#selection-popover,.pdf-page'))$('#selection-popover').hidden=true;
});
function handleKeyboard(event) {
  if(event.key==='Escape'){closeBubble();hideSelection();$('#notes-drawer').hidden=true;$('#settings-popover').hidden=true;$('#library-sidebar').classList.remove('mobile-open');document.body.classList.remove('focus-mode');$('#focus-button').classList.remove('active');$('#focus-button').setAttribute('aria-label','进入专注阅读');}
  if(['INPUT','TEXTAREA'].includes(event.target.tagName)||document.querySelector('dialog[open]')||!$('#thought-bubble').hidden||event.ctrlKey||event.metaKey||event.altKey)return;
  if(readingRoot?.ownerDocument.getSelection()?.toString().trim())return;
  if(event.key==='ArrowRight')void navigateTo(pageNumber+1);if(event.key==='ArrowLeft')void navigateTo(pageNumber-1);
}
document.addEventListener('keydown',handleKeyboard);
let resizeTimer,lastWidth=0;
new ResizeObserver(entries=>{
  const width=entries[0].contentRect.width;
  if(Math.abs(lastWidth-width)<2)return;lastWidth=width;
  clearTimeout(resizeTimer);resizeTimer=setTimeout(()=>{
    if(ready&&book.kind==='pdf'&&Math.abs(renderedContainerWidth-$('#reader-scroll').clientWidth)>2)void renderCurrentPage({keepPosition:true});
    else positionBubble();
  },180);
}).observe($('#reader-scroll'));
window.addEventListener('resize',positionBubble);
(async()=>{
  applySettings();
  try{if(state.currentBook?.id){const record=await storedBook(state.currentBook.id);if(record){await importRecord(record,state.reading[record.id]||1);return;}}}catch{/* fall back to bundled source */}
  await loadDemo();
})();
