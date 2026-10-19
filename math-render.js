/* Render only companion UI; original PDF text and selection offsets stay intact. */
'use strict';
const MathRender=(()=>{
  function render(element){
    if(!element||!globalThis.renderMathInElement)return;
    renderMathInElement(element,{
      delimiters:[{left:'$$',right:'$$',display:true},{left:'\\[',right:'\\]',display:true},
                  {left:'\\(',right:'\\)',display:false},{left:'$',right:'$',display:false}],
      ignoredTags:['script','noscript','style','textarea','pre','code','option'],
      ignoredClasses:['katex','katex-display'],
      throwOnError:false,trust:false,strict:'ignore',maxExpand:200,maxSize:12,
      errorCallback:()=>{},macros:{},
    });
  }
  return {render};
})();
