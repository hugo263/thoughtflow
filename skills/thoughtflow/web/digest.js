/* Source snapshots, complete Markdown export, and AI-organized graph projection. */
(() => {
  'use strict';
  const Doc=typeof module!=='undefined'&&module.exports?require('./document.js'):window.ThoughtDocument;
  const rows=turns=>turns.map(t=>[t.id,t.question||'',t.answer||'',t.state||'',t.inputReply||[],t.inputRequests||[]]);
  function key(turns){const value=JSON.stringify(rows(turns));let a=2166136261,b=3339675911;for(let i=0;i<value.length;i++){a=Math.imul(a^value.charCodeAt(i),16777619);b=Math.imul(b^value.charCodeAt(i),2246822519);}return (a>>>0).toString(16).padStart(8,'0')+(b>>>0).toString(16).padStart(8,'0');}
  const line=value=>String(value||'').replace(/[\r\n#]/g,' ').trim();
  function fullMarkdown(conversation,turns,includeTools=false){
    const parts=['# '+(line(conversation?.title)||'Full conversation'),'Full conversation snapshot · '+turns.length+' turns · Original messages preserved'];
    for(const [index,turn] of turns.entries()){
      parts.push('## Turn '+(index+1),'### User\n\n'+(turn.question||'(No text input)'),'### AI\n\n'+(turn.answer||'(No readable response yet)'));
      if(turn.state!=='completed')parts.push('> Status: '+(turn.state==='continued'?'Continued in later turns':turn.state==='waiting_input'?'Awaiting more information':'This turn is unfinished; refer to later updates.'));
      if(includeTools&&turn.tools?.length){parts.push('### Tool activity');for(const tool of turn.tools){const detail=String(tool.detail||''),runs=detail.match(/`+/g)||[],fence='`'.repeat(Math.max(3,...runs.map(run=>run.length+1)));parts.push('#### '+line(tool.label||'Tool call')+'\n\nStatus: '+line(tool.status||'completed')+'\n\n'+fence+'text\n'+detail+'\n'+fence);}}
    }
    return parts.join('\n\n')+'\n';
  }
  function organizedMarkdown(job){return job.markdown.trim()+'\n\n---\n\n## Summary scope\n\n'+(job.engine==='local-outline'?'Local outline':'AI summary')+' · Based on '+job.turnCount+' source turns · References preserved\n\n'+(job.createdAt?'Created: '+job.createdAt+'\n':'');}
  function toGraph(job,turns){
    // Use structured sections directly so embedded A/B lists cannot override sources.
    const id='organized-'+job.id,valid=ids=>(ids||[]).filter(key=>turns.some(t=>t.id===key)),sources=valid(job.sourceTurnIds||turns.map(t=>t.id)),local=job.engine==='local-outline';
    const common={curated:true,engine:job.engine,tools:[],state:'completed',createdAt:job.createdAt,documentId:id,clarifications:[],waitingForInput:false,pendingQuestion:'',taskQuestion:job.title};
    const root={...common,id,kind:'document',question:job.title,answer:job.overview,sourceTurnIds:sources,sourceTurnId:sources[sources.length-1],turnIndex:1,parentIds:[],parentId:null,childIds:[]};
    const nodes=[root],sections=[...(job.sections||[])];
    if(job.openQuestions?.length)sections.push({heading:'Open questions and next steps',body:job.openQuestions.map(q=>'- '+q).join('\n'),sourceTurnIds:sources});
    for(const [index,section] of sections.entries()){
      const refs=valid(section.sourceTurnIds||sources),nodeId=id+'-section-'+index;
      const node={...common,id:nodeId,kind:'section',question:Doc.cleanTitle(section.heading),answer:section.body||'',sourceTurnIds:refs,sourceTurnId:refs[refs.length-1],turnIndex:Math.max(1,turns.findIndex(t=>t.id===refs[0])+1),parentIds:[id],parentId:id,childIds:[],relation:{kind:'organized',label:local?'Local excerpt':'AI summary',confidence:local?'explicit':'synthesized',reason:local?'Original-order excerpts, without AI rewriting. Open sources for full context.':'Organized from existing messages. Expand sources to verify.'}};
      root.childIds.push(nodeId);nodes.push(node);
    }
    return {nodes,latestId:id,insightIds:[],idAliases:{},turnCount:turns.length,taskCount:1};
  }

  const api={key,rows,fullMarkdown,organizedMarkdown,toGraph};if(typeof module!=='undefined'&&module.exports)module.exports=api;else window.ThoughtDigest=api;
})();
