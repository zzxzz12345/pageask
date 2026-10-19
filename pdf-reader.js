/* Continuous PDF pages: stable paper slots, nearby rendering, bounded canvases. */
'use strict';
const PDFReader = (() => {
  let epoch=0,views=new Map(),options=null,queue=[],running=0,frame=0,current=0;
  let scrollHandler=null;
  function page(number) { return views.get(number); }
  function rendered() { return [...views.values()].filter(view=>view.ready); }
  function position() {
    const view=page(current);
    if(!view||!options)return null;
    return {page:current,offset:(options.scroll.scrollTop-view.element.offsetTop)/view.element.offsetHeight};
  }
  function release(view) {
    view.token++;
    view.canvasTask?.cancel();view.textTask?.cancel();
    view.pdfPage?.cleanup();view.pdfPage=null;
    if(view.canvas){view.canvas.width=0;view.canvas.height=0;}
    view.canvas=null;view.textRoot=null;view.overlay=null;view.ready=false;view.positions.clear();
    view.element.replaceChildren();
    const placeholder=document.createElement('span');placeholder.className='pdf-placeholder';placeholder.textContent=`第 ${view.number} 页`;
    view.element.append(placeholder);
    view.element.setAttribute('aria-busy','true');
  }
  function destroy() {
    epoch++;cancelAnimationFrame(frame);
    if(options&&scrollHandler)options.scroll.removeEventListener('scroll',scrollHandler);
    for(const view of views.values())release(view);
    views.clear();queue=[];current=0;options=null;scrollHandler=null;
  }
  function indexAt(y) {
    let low=1,high=views.size;
    while(low<high){
      const middle=Math.floor((low+high)/2),element=page(middle).element;
      if(element.offsetTop+element.offsetHeight<y)low=middle+1;
      else high=middle;
    }
    return low;
  }
  function activate(number) {
    if(current===number)return;
    current=number;options.onActive(page(number));
  }
  function update() {
    if(!options||!views.size)return;
    const scroll=options.scroll;
    activate(indexAt(scroll.scrollTop+Math.min(scroll.clientHeight*.35,240)));
    const first=Math.max(1,indexAt(scroll.scrollTop)-1);
    const last=Math.min(views.size,indexAt(scroll.scrollTop+scroll.clientHeight)+1);
    const selection=document.getSelection()?.anchorNode?.parentElement?.closest('.pdf-page');
    for(const view of views.values()){
      view.wanted=view.number>=first&&view.number<=last;
      if(!view.wanted&&view.element!==selection&&(view.ready||view.loading))release(view);
    }
    queue=[...views.values()].filter(view=>view.wanted&&!view.ready&&!view.loading&&!view.failed)
      .sort((a,b)=>Math.abs(a.number-current)-Math.abs(b.number-current));
    pump();
  }
  function schedule() {
    cancelAnimationFrame(frame);frame=requestAnimationFrame(update);
  }
  function pump() {
    while(options&&running<2&&queue.length){
      const view=queue.shift();
      if(!view.wanted||view.ready||view.loading)continue;
      const settings=options,generation=epoch,token=++view.token;
      view.loading=true;running++;
      render(view,settings,generation,token).catch(error=>{
        if(generation!==epoch||token!==view.token||['RenderingCancelledException','AbortException'].includes(error.name))return;
        view.element.querySelector('.pdf-placeholder')?.remove();
        const retry=document.createElement('button');retry.className='pdf-page-retry';retry.textContent=`第 ${view.number} 页加载失败，点击重试`;
        retry.onclick=()=>{retry.remove();view.failed=false;schedule();};view.element.append(retry);view.failed=true;
        console.error('PDF page rendering failed:',error);
      }).finally(()=>{
        view.loading=false;running--;pump();
        if(generation===epoch&&token!==view.token&&view.wanted)schedule();
      });
    }
  }
  async function render(view,settings,generation,token) {
    const valid=()=>generation===epoch&&token===view.token;
    const pdfPage=await settings.document.getPage(view.number);
    if(!valid())return;
    view.pdfPage=pdfPage;
    const base=pdfPage.getViewport({scale:1});
    const viewport=pdfPage.getViewport({scale:settings.width/base.width*settings.zoom});
    const oldHeight=view.element.offsetHeight;
    view.element.style.width=`${viewport.width}px`;view.element.style.height=`${viewport.height}px`;
    view.element.style.setProperty('--scale-factor',viewport.scale);
    view.element.style.setProperty('--total-scale-factor',viewport.scale);
    if(view.number<current)settings.scroll.scrollTop+=viewport.height-oldHeight;
    const canvas=document.createElement('canvas'),overlay=document.createElement('div'),textRoot=document.createElement('div');
    overlay.className='pdf-highlights';overlay.setAttribute('aria-label',`第 ${view.number} 页划线`);
    textRoot.className='textLayer';textRoot.setAttribute('aria-label',`第 ${view.number} 页原文`);
    canvas.setAttribute('aria-label',`PDF 第 ${view.number} 页`);
    const dpr=Math.min(devicePixelRatio||1,2);
    canvas.width=Math.ceil(viewport.width*dpr);canvas.height=Math.ceil(viewport.height*dpr);
    canvas.style.width=`${viewport.width}px`;canvas.style.height=`${viewport.height}px`;
    view.canvas=canvas;view.overlay=overlay;view.textRoot=textRoot;
    view.element.replaceChildren(canvas,overlay,textRoot);
    const task=pdfPage.render({canvasContext:canvas.getContext('2d'),viewport,transform:dpr===1?null:[dpr,0,0,dpr,0,0]});
    view.canvasTask=task;task.promise.catch(()=>{});
    const content=await pdfPage.getTextContent();
    if(!valid())return;
    await task.promise;
    if(!valid())return;
    settings.recoverText(content,pdfPage);
    const textTask=new settings.pdfjs.TextLayer({textContentSource:content,container:textRoot,viewport});view.textTask=textTask;
    await Promise.all([task.promise,textTask.render()]);
    if(!valid())return;
    view.ready=true;view.element.setAttribute('aria-busy','false');
    settings.onRender(view);
    if(view.number===current)settings.onActive(view);
  }
  async function mount(settings) {
    destroy();options=settings;
    const generation=epoch;
    const first=await settings.document.getPage(settings.initialPage);
    if(generation!==epoch)return;
    const base=first.getViewport({scale:1}),height=settings.width/base.width*base.height*settings.zoom;
    const fragment=document.createDocumentFragment();
    for(let number=1;number<=settings.document.numPages;number++){
      const element=document.createElement('div');element.className='pdf-page';element.dataset.page=number;
      element.setAttribute('role','region');element.setAttribute('aria-label',`PDF 第 ${number} 页`);
      element.style.width=`${settings.width*settings.zoom}px`;element.style.height=`${height}px`;
      const view={number,element,token:0,ready:false,loading:false,wanted:false,positions:new Map()};
      release(view);views.set(number,view);fragment.append(element);
      element.addEventListener('click',event=>settings.onClick(view,event));
    }
    settings.stage.replaceChildren(fragment);settings.stage.classList.add('continuous-pdf');
    settings.scroll.scrollTop=page(settings.initialPage).element.offsetTop+(settings.offset??-24/height)*height;
    scrollHandler=schedule;settings.scroll.addEventListener('scroll',scrollHandler,{passive:true});
    update();
  }
  function goTo(number) {
    const view=page(number);if(!view||!options)return;
    options.scroll.scrollTop=view.element.offsetTop-24;
    activate(number);update();
  }
  return {mount,destroy,page,rendered,position,goTo,focus:number=>{if(views.has(number))current=number;}};
})();
