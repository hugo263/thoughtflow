/* Local Markdown structure extraction. Source text is retained; no remote model. */
(() => {
  'use strict';
  const plain=value=>String(value||'').replace(/\[([^\]]+)\]\([^)]*\)/g,'$1').replace(/[*_`]/g,'').trim();
  const cleanTitle=value=>plain(value).replace(/^(?:[一二三四五六七八九十百]+[、.．]|\d+[.、．)]\s*)\s*/,'').trim();
  const short=(value,n=90)=>Array.from(value).slice(0,n).join('');
  const key=value=>{let a=2166136261,b=3339675911;for(let i=0;i<value.length;i++){a=Math.imul(a^value.charCodeAt(i),16777619);b=Math.imul(b^value.charCodeAt(i),2246822519);}return (a>>>0).toString(16).padStart(8,'0')+(b>>>0).toString(16).padStart(8,'0');};
  const tableCells=line=>line.trim().replace(/^\||\|$/g,'').split(/(?<!\\)\|/).map(value=>value.trim());
  function pointTitle(value){const strong=value.match(/^\*\*([^*]+)\*\*/);return short(cleanTitle(strong?strong[1]:plain(value).split(/[。！？\n]/)[0]),85);}
  function requestMode(question){
    // The user's task selects a content map. Markdown formatting alone never does.
    const value=plain(question).replace(/\s+/g,' '),request='(?:帮我|请|麻烦|替我|给我|能不能(?:帮我)?|能否(?:帮我)?)?\\s*';
    if(/\b(?:change (?:the )?topic|new topic|just (?:chat|discuss)|back to (?:the )?(?:conversation|flow))\b/i.test(value)||/\b(?:do not|don't|no need to)\b.{0,35}\b(?:report|document|mind map)\b/i.test(value))return 'flow';
    if(/\b(?:write|create|draft|organize|organise|summarize|summarise|read|analy[sz]e|research)\b.{0,70}\b(?:report|document|article|paper|pdf|market|industry|company|product|app|website|mind map)\b/i.test(value))return 'content';
    if(/换个话题|说个别的|先不(?:聊|管|做|研究)|(?:先|只|只是|还是|继续|就)(?:简单)?(?:聊(?:聊|一下|事情)?|讨论|梳理(?:一下|下)?(?:问题|思路))|(?:不(?:要|用|需要)|别).{0,12}(?:研究报告|内容图谱|文档导图|报告|导图)|(?:回到|切回|按)(?:之前的)?(?:思考流程|讨论流程|对话流程)/.test(value))return 'flow';
    const artifact='(?:研究报告|调研报告|分析报告|报告|研究文档|文档|资料|文章|论文|PDF|Word|竞品分析|产品研究|市场调研|技术调研|内容图谱|思维导图|脑图)';
    if(new RegExp(request+'(?:写|做|生成|输出|整理|总结|梳理|解读|阅读|转换|提炼|转成|做成).{0,35}'+artifact,'i').test(value)||
       new RegExp('(?:分析|研究)(?:一下|下)?(?:这|那|此|给你的|上传的|附件|一份|一篇).{0,25}'+artifact,'i').test(value)||
       new RegExp('(?:把|将).{0,50}(?:整理|转换|转|做|生成).{0,8}(?:内容图谱|思维导图|脑图)').test(value)||
       new RegExp(request+'研究(?:下|一下|一下子)?[^。！？?]{1,60}(?:app|应用|产品|项目|公司|行业|市场|论文|技术方案|方方面面)','i').test(value)||
       /(?:拆解|调研|分析)(?:一下|下)?[^。！？?]{1,45}(?:网站|app|小程序|产品|行业|公司)|(?:拆解|调研|分析)(?:一下|下)?(?:网站|app|小程序|产品|行业|公司)[^。！？?]{1,45}/i.test(value)||
       /(?:全面|系统|深入|详细)(?:地)?(?:分析|研究|调研)[^。！？?]{1,60}(?:app|应用|产品|项目|公司|行业|市场)|(?:分析|研究|调研)[^。！？?]{1,60}(?:方方面面|各个方面)/i.test(value))return 'content';
    return null;
  }
  function parse(markdown,title='Content map'){
    const lines=String(markdown||'').replace(/\r\n?/g,'\n').split('\n'),headings=[],visible=[];let fence=null;
    for(let i=0;i<lines.length;i++){
      const marker=lines[i].match(/^\s*(`{3,}|~{3,})/);if(marker){if(!fence)fence=marker[1][0];else if(fence===marker[1][0])fence=null;visible[i]=false;continue;}visible[i]=!fence;
      const match=visible[i]&&lines[i].match(/^\s*(#{1,6})\s+(.+?)\s*#*\s*$/);if(match)headings.push({line:i,level:match[1].length,title:cleanTitle(match[2])});
    }
    // A single conclusion heading remains a normal answer/insight card.
    if(headings.length<2)return null;
    const minLevel=Math.min(...headings.map(item=>item.level)),first=headings[0];
    const documentHeading=first.level===1&&headings.filter(item=>item.level===1).length===1?first:null;
    const contentHeadings=headings.filter(item=>item!==documentHeading);
    if(contentHeadings.length<2)return null;
    const nodes=[],counts=new Map(),stack=[],byKey=new Map();let clipped=0;
    function add(parent,kind,name,body,start,end){
      if(nodes.length>=128){clipped++;return null;}
      const signature=(parent||'root')+'\0'+kind+'\0'+name,occurrence=(counts.get(signature)||0)+1;counts.set(signature,occurrence);
      const item={key:key(signature+'\0'+occurrence),parentKey:parent,kind,title:short(name||'Untitled topic',120),body:body.trim(),sourceLines:[start+1,end],childKeys:[]};
      nodes.push(item);byKey.set(item.key,item);if(parent&&byKey.has(parent))byKey.get(parent).childKeys.push(item.key);return item;
    }
    for(let i=0;i<contentHeadings.length;i++){
      const heading=contentHeadings[i];while(stack.length&&stack[stack.length-1].level>=heading.level)stack.pop();
      let end=lines.length;for(let j=i+1;j<contentHeadings.length;j++)if(contentHeadings[j].level<=heading.level){end=contentHeadings[j].line;break;}
      const item=add(stack[stack.length-1]?.key||null,'section',heading.title,lines.slice(heading.line+1,end).join('\n'),heading.line,end);
      heading.key=item?.key;heading.directEnd=contentHeadings[i+1]?.line??lines.length;
      if(item)stack.push({key:item.key,level:heading.level});
    }
    for(const heading of contentHeadings){
      if(!heading.key)continue;let listStack=[];
      for(let i=heading.line+1;i<heading.directEnd;i++){
        if(!visible[i])continue;
        if(lines[i].includes('|')&&i+1<heading.directEnd&&/^\s*\|?\s*:?-{3,}/.test(lines[i+1])){
          const headers=tableCells(lines[i]);i+=2;let rows=0;
          for(;i<heading.directEnd&&visible[i]&&lines[i].includes('|')&&lines[i].trim();i++){
            const values=tableCells(lines[i]);if(values.length<2||!plain(values[0]))continue;
            if(rows++>=24){clipped++;continue;}
            add(heading.key,'detail',short(plain(values[0]),80),values.slice(1).map((value,index)=>'**'+plain(headers[index+1]||'Notes')+'**：'+value).join('\n\n'),i,i+1);
          }i--;listStack=[];continue;
        }
        const bullet=lines[i].match(/^(\s*)(?:[-*+]|\d+[.)、])\s+(.+)$/);
        const bold=lines[i].match(/^\s*\*\*([^*]{2,90})\*\*\s*(.*)$/);
        if(!bullet&&!bold)continue;
        // Standalone emphasized conclusions are kept in the chapter body.
        if(bold&&!bullet&&!/[：:。.]$/.test(bold[1])&&!/^\d+[.、]/.test(bold[1]))continue;
        const value=bullet?bullet[2]:lines[i].trim(),indent=bullet?bullet[1].replace(/\t/g,'    ').length:0;
        let end=i+1;while(end<heading.directEnd&&lines[end].trim()&&!/^\s*(?:[-*+]|\d+[.)、])\s+|^\s*\*\*/.test(lines[end])&&!lines[end].includes('|'))end++;
        while(listStack.length&&listStack[listStack.length-1].indent>=indent)listStack.pop();
        const parent=bullet&&listStack.length?listStack[listStack.length-1].key:heading.key;
        const item=add(parent,'detail',pointTitle(value),[value,...lines.slice(i+1,end)].join('\n'),i,end);
        if(item&&bullet)listStack.push({key:item.key,indent});else if(!bullet)listStack=[];
        i=end-1;
      }
    }
    const top=nodes.filter(node=>!node.parentKey).map(node=>node.key);
    const introStart=documentHeading?documentHeading.line+1:0;
    const intro=lines.slice(introStart,contentHeadings[0].line).join('\n').split(/\n\s*\n/).filter(part=>part.trim()&&!/^思考图谱.*(?:已经|已)(?:打开|启动)/.test(plain(part))).join('\n\n').trim();
    return {title:documentHeading?.title||title,intro:intro.length>1400?intro.slice(0,1400)+'\n\n… (Expand the source for the full text.)':intro,nodes,top,clipped,minLevel};
  }
  function collapsed(node,expanded=new Set(),folded=new Set()){return Boolean(node?.childIds?.length)&&(folded.has(node.id)||(!expanded.has(node.id)&&node.kind!=='document'));}
  function visibleGraph(nodes,expanded=new Set(),folded=new Set()){
    const byId=new Map(nodes.map(node=>[node.id,node])),hidden=new Set();
    for(const node of nodes){if(!node.documentId)continue;let current=node,seen=new Set();while(current?.parentIds?.length===1){const parent=byId.get(current.parentIds[0]);if(!parent||seen.has(parent.id)||parent.documentId!==node.documentId)break;seen.add(parent.id);if(collapsed(parent,expanded,folded)){hidden.add(node.id);break;}current=parent;}}
    const shown=nodes.filter(node=>!hidden.has(node.id));
    return shown.map(node=>{const parents=[];for(const ident of node.parentIds||[]){let parent=byId.get(ident),seen=new Set();while(parent&&hidden.has(parent.id)&&!seen.has(parent.id)){seen.add(parent.id);parent=byId.get(parent.parentIds?.[0]);}if(parent&&parent.id!==node.id&&!parents.includes(parent.id))parents.push(parent.id);}return {...node,parentIds:parents,parentId:parents[0]||null};});
  }
  const api={parse,requestMode,cleanTitle,collapsed,visibleGraph};if(typeof module!=='undefined'&&module.exports)module.exports=api;else window.ThoughtDocument=api;
})();
