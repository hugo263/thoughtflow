/* A local projection of public answers. No model reasoning or remote inference. */
(() => {
  'use strict';
  const DocumentMap=typeof module!=='undefined'&&module.exports?require('./document.js'):window.ThoughtDocument;
  const plain = value => String(value || '').replace(/\[([^\]]+)\]\([^)]*\)/g, '$1').replace(/[*_`]/g, '').trim();
  const normalized = value => plain(value).toLocaleLowerCase().replace(/[^\p{L}\p{N}]/gu, '');
  const optionId = (turn, key) => turn + '-option-' + key;
  const recommendationStart = /^(?:I (?:would )?(?:recommend|suggest)|My recommendation|Recommendation\b|Recommended (?:option|approach)|我的建议|我(?:更|会|个人)?(?:建议|推荐)|建议(?:先|优先|采用|选择)|推荐(?:的)?(?:方案|组合|路线)?(?:是|为|：|:))/i;
  function label(line) {
    const value = plain(line.replace(/^\s*(?:#{1,6}\s+|[-*+]\s+)/, ''));
    const match = value.match(/^(?:选项|方案|路线|Option)?\s*(?:[（(\[]([A-H])[）)\]]\s*[.．、:：-]?\s*|([A-H])\s*[.．、:：)）-]\s*)(.+)$/);
    return match ? {key:match[1]||match[2], title:match[3].trim()} : null;
  }
  const cells = line => line.trim().replace(/^\||\|$/g, '').split(/(?<!\\)\|/).map(part => part.trim());
  function extract(answer) {
    const lines = String(answer || '').replace(/\r\n?/g, '\n').split('\n'), masked = new Set(), found = new Map(), headings = [];
    let fenced = false;
    const visible = lines.map(line => { if (/^\s*(```|~~~)/.test(line)) {fenced = !fenced;return false;} return !fenced; });
    const ensure = item => {if (!found.has(item.key)) found.set(item.key, {...item, fields:'', detail:''});return found.get(item.key);};
    // Explicit option tables. Repeated headings below a table enrich the same card.
    for (let i=0;i<lines.length-1;i++) {
      if (!visible[i] || !lines[i].includes('|') || !/^\s*\|?\s*:?-{3,}/.test(lines[i+1])) continue;
      const headers = cells(lines[i]), entries=[];let end=i+2;
      while (end<lines.length && visible[end] && lines[end].trim() && lines[end].includes('|')) {
        const row=cells(lines[end]), item=label(row[0]||'');
        if (item) entries.push({...item,fields:row.slice(1).map((value,index) => '**'+plain(headers[index+1]||'Notes')+'**：'+value).join('\n\n')});
        end++;
      }
      if (entries.length>=2) {entries.forEach(item=>{ensure(item).fields=item.fields;});for(let n=i;n<end;n++)masked.add(n);i=end-1;}
    }
    for(let i=0;i<lines.length;i++) {
      if(!visible[i] || masked.has(i))continue;
      const item=label(lines[i]);
      // Plain A./B. lists are accepted; letters occurring inside prose/code are not.
      if(item)headings.push({...item,index:i,level:(lines[i].match(/^\s*(#{1,6})\s/)||[])[1]?.length||0});
    }
    for(let n=0;n<headings.length;n++) {
      const heading=headings[n], option=ensure(heading);let end=headings[n+1]?.index??lines.length;
      for(let j=heading.index+1;j<end;j++) {
        if(!visible[j])continue;
        const nextLevel=(lines[j].match(/^\s*(#{1,6})\s/)||[])[1]?.length;
        if((n===headings.length-1&&recommendationStart.test(plain(lines[j]))) || (nextLevel && heading.level && nextLevel<=heading.level)) {end=j;break;}
      }
      option.title=heading.title;
      option.detail=lines.slice(heading.index+1,end).join('\n').trim();
      for(let j=heading.index;j<end;j++)masked.add(j);
    }
    const options=Array.from(found.values()).sort((a,b)=>a.key.localeCompare(b.key));
    // Do not turn an incidental single label or ordinary numbered steps into branches.
    if(options.length<2 || !options.some(option=>option.key==='A'))return null;
    const outside=lines.filter((_,i)=>!masked.has(i)).join('\n').trim(), paragraphs=outside.split(/\n\s*\n/);
    const recommendations=paragraphs.filter(paragraph=>recommendationStart.test(plain(paragraph)));
    const recommendation=recommendations.join('\n\n');
    const summary=paragraphs.filter(paragraph=>!recommendations.includes(paragraph)).join('\n\n').trim();
    const keys=references(recommendation.split(/如果|或者|也可以|其次|\b(?:but|alternatively|otherwise|if)\b/i)[0],options).keys;
    return {options:options.map(option=>({...option,content:[option.fields,option.detail].filter(Boolean).join('\n\n')})),
      summary:summary.length>2400?summary.slice(0,2400)+'\n\n… (Expand source messages for the full text.)':summary,
      recommendation, recommended:keys};
  }
  function references(question, options) {
    const value=plain(question), matches=[], rejected=new Set();
    for(const match of value.matchAll(/\b(?:not|avoid|reject|skip|do not choose|don't choose)\s+(?:option\s+)?([A-H])\b/gi))rejected.add(match[1].toUpperCase());
    // Explicit negatives must not become selected branches ("不要 A，选 B").
    for(const match of value.matchAll(/(?:不要|不选|不推荐|不建议|排除|放弃|不是|不考虑)\s*(?:方案|选项)?\s*([A-H](?:\s*(?:\+|和|与|、|及)\s*[A-H])*)(?![A-Za-z0-9])/g))
      for(const key of match[1].match(/[A-H]/g)||[])rejected.add(key);
    const allLabels=options.map(option=>option.key);
    for(const match of value.matchAll(/(?<![A-Za-z0-9])([A-H])(?![A-Za-z0-9])/g)) {
      if(allLabels.includes(match[1])&&!rejected.has(match[1]))matches.push(match[1]);
    }
    const q=normalized(value);
    for(const option of options) {
      const name=normalized(option.title.split(/[：:]/)[0]);
      if(name.length>=4 && q.includes(name) && !rejected.has(option.key)) matches.push(option.key);
    }
    return {keys:allLabels.filter(key=>matches.includes(key)), rejected};
  }
  function tokens(text) {
    const value=plain(text).toLowerCase(), result=new Set(value.match(/[a-z][a-z0-9]{2,}/g)||[]);
    for(const match of value.matchAll(/[\u3400-\u9fff]{2,}/g))for(let i=0;i<match[0].length-1;i++)result.add(match[0].slice(i,i+2));
    return result;
  }
  function overlap(a,b) {let n=0;for(const token of a)if(b.has(token))n++;return n;}
  function relation(parents,kind,label,confidence='explicit',reason='') {return {parents,kind,label,confidence,reason};}
  function insightExcerpt(answer) {
    const lines=String(answer||'').split('\n'), candidates=[];let fenced=false;
    const visible=lines.map(line=>{if(/^\s*(```|~~~)/.test(line)){fenced=!fenced;return '';}return fenced?'':line;});
    const headingPattern=/^(?:key (?:findings?|takeaways?)|conclusions?|decision|tradeoffs?|correction|关键(?:洞察|结论|发现)|核心(?:判断|结论|发现)|洞察|insights?|最终(?:结论|方案)|(?:我的)?综合(?:评价|判断|结论)|结论|决策依据|取舍|更正|修正|更新结论)(?:\s|[：:]|$)/i;
    for(let i=0;i<visible.length;i++){
      const match=visible[i].match(/^\s*(#{1,6})\s+(.+)/);if(!match||!headingPattern.test(DocumentMap.cleanTitle(match[2])))continue;
      let end=i+1;while(end<visible.length){const next=visible[end].match(/^\s*(#{1,6})\s+/);if(next&&next[1].length<=match[1].length)break;end++;}
      const body=visible.slice(i+1,end).join('\n').trim();if(body)candidates.push(body);i=end-1;
    }
    const paragraphs=visible.join('\n').split(/\n\s*\n/);
    for(const [index,paragraph] of paragraphs.entries()){
      const value=plain(paragraph);if(/^(?:有一个关键区别|关键(?:结论|发现|洞察)|核心(?:判断|结论|发现)|结论|关键是|核心是|这意味着|我的建议是|我建议(?:先|第一版)|我(?:补充)?(?:更正|纠正)|更正|修正|更新结论)[：:，,\s是]?/.test(value)&&!value.endsWith('？')){
        // Keep the emphasized finding atomic; surrounding evidence stays in the
        // source card. This also avoids retaining obsolete explanatory detail.
        const emphasized=paragraph.trim().match(/^([^*\n]{0,30})(\*\*[^*\n]{12,}\*\*)/);
        const core=emphasized?emphasized[0]:paragraph.trim();
        candidates.push(core+(/[：:]$/.test(core)&&core.length<120&&paragraphs[index+1]?'\n\n'+paragraphs[index+1].trim():''));
      }
    }
    const pieces=[...new Set(candidates)].slice(0,2);if(!pieces.length)return null;
    let body=pieces.join('\n\n');if(body.length>1800)body=body.slice(0,1800)+'\n\n… (Expand source messages for the full text.)';
    const emphasis=pieces[0].match(/\*\*([^*\n]{8,})\*\*/),title=plain(emphasis?.[1]||pieces[0]).replace(/^[-*+]\s*/,'').split(/[。！？\n]/)[0].slice(0,65);
    return {title,body,type:/^(?:我(?:补充)?(?:更正|纠正)|更正|修正|更新结论)/.test(plain(pieces[0]))?'correction':'finding'};
  }
  const deictic=question=>/^(?:那|好|可以|就|按|先|继续|这个|它|做吧|行)/.test(plain(question))&&plain(question).length<=90;
  function retrospectiveChoices(turns){
    const revisions=new Map();for(let i=1;i<turns.length;i++){
      const match=plain(turns[i].question).match(/(?:刚才|上一轮|上一个|前面).{0,12}(?:指的|说的|选的|采用的|选择的)(?:其实)?(?:是)?\s*(?:方案|选项)?\s*([A-H](?:\s*(?:\+|和|与|、)\s*[A-H])*)/);
      if(match&&deictic(turns[i-1].question))revisions.set(turns[i-1].id,match[1]);
    }return revisions;
  }
  function resolve(question, contexts, previous) {
    if(!previous)return relation([],'root','Start');
    if(!contexts.length)return relation([previous.id],'continuation','Continue','inferred','No options detected; continuing from the previous node.');
    const value=plain(question), context=contexts[contexts.length-1];
    if(/换个话题|新话题|另一个话题|说个别的|先不(?:聊|管|做)|暂时不(?:聊|管|做)|与.{0,15}无关/.test(value))
      return {...relation([context.id],'new_topic','New direction','explicit','The user explicitly changed direction; branching from this excerpt.'),context,keys:[]};
    // Recent labels win; a complete option name can refer back to an older decision.
    for(let i=contexts.length-1;i>=0;i--) {
      const candidate=contexts[i], refs=references(value,candidate.options);
      let keys=refs.keys;
      if(!keys.length&&i===contexts.length-1&&/按.{0,6}(?:推荐|建议)|照.{0,6}(?:推荐|建议)|就.{0,4}推荐/.test(value))keys=candidate.recommended;
      if(keys.length) {
        const parents=[...new Set(keys.map(key=>candidate.frontier.get(key)||optionId(candidate.id,key)))];
        const compare=/比较|对比|区别|差别|还是|哪个好|\b(?:compare|versus|vs\.?|difference|which is better)\b/i.test(value), kind=keys.length>1?(compare?'comparison':'combination'):'option';
        return {...relation(parents,kind,(compare?'Compare ':keys.length>1?'Combine ':'Follow ')+keys.join(' + '),'explicit','Connected using an option letter or name in the user message.'),context:candidate,keys};
      }
    }
    const query=tokens(question), scores=context.options.map(option=>({key:option.key,score:overlap(query,option.topic)})).sort((a,b)=>b.score-a.score);
    // Limited lexical inference stays visibly provisional and can be corrected locally.
    if(scores[0]?.score>=3 && scores[0].score>=(scores[1]?.score||0)+2) {
      const keys=[scores[0].key],parents=[context.frontier.get(keys[0])||optionId(context.id,keys[0])];
      return {...relation(parents,'option','Possibly follow '+keys[0],'inferred','Inferred from shared words in the option and question. Sources can be edited.'),context,keys};
    }
    const previousTopic=tokens((previous.question||'')+' '+(previous.answer||''));
    const shortContinuation=value.length<=90&&/^(?:那|好|可以|继续|就|按|先|再|怎么|如何|为什么|这个|它|需要|能不能|能否|还|有没有|目前|然后|做吧|行)/.test(value);
    if((context.active.length || previous.id!==context.id) && (shortContinuation || overlap(query,previousTopic)>=2))
      return {...relation([previous.id],'continuation','Follow current branch','inferred','No new options; continuing within the current branch. Sources can be edited.'),context,keys:context.active};
    return {...relation([context.id],'uncertain','Unconfirmed source','unknown','No clear option or topic match. Linked provisionally; select one or more sources to adjust.'),context,keys:[]};
  }
  function waitsForInput(turn){
    if(turn.awaitingInput===true)return true;
    const prompts=Array.isArray(turn.inputRequests)?turn.inputRequests.filter(item=>typeof item==='string').join('\n\n'):'',answer=plain(turn.answer),value=answer||plain(prompts);
    if(!value||value.length>800||/^\s*#{1,6}\s/m.test(turn.answer)||/(?:我的建议|我建议|综合评价|关键结论|结论是)/.test(value))return false;
    if(extract(turn.answer))return false; // An actual option decision remains a flow step.
    const required=/(?:请|需要你|还需你|麻烦你).{0,18}(?:告诉|提供|补充|确认|选择|选一下|登录)|(?:你想|你要|你希望|你指|你说|想研究|想分析).{0,25}(?:哪|什么|哪些).{0,30}[？?]|(?:能否|能不能|方便|可以).{0,12}(?:告诉|提供|补充|确认).{0,30}[？?]|(?:先确认|需要确认|还需要了解).{0,50}[？?]/;
    return required.test(value)||(answer.length<200&&Boolean(prompts)&&!/(?:我会先继续|先继续查|可选|不影响继续)/.test(prompts)&&required.test(prompts));
  }
  function canonicalId(id,aliases={}){if(typeof id!=='string')return id;if(aliases[id])return aliases[id];for(const [source,target] of Object.entries(aliases))if(id.startsWith(source+'-doc-')||id.startsWith(source+'-option-')||id===source+'-insight')return target+id.slice(source.length);return id;}
  function groupTurns(turns){
    const groups=[],aliases={};
    for(const [index,turn] of turns.entries()){
      const last=groups[groups.length-1],reply=Array.isArray(turn.inputReply)?turn.inputReply.filter(item=>typeof item?.question==='string'&&typeof item?.answer==='string'):[],value=plain(turn.question);
      const changedTopic=/换个话题|新话题|另一个话题|说个别的|先不(?:聊|管|做|研究)|(?:取消|放弃)(?:这个|这项|刚才的)?(?:任务|研究)/.test(value);
      const previous=last?.parts[last.parts.length-1],matchesPrompt=previous&&reply.some(item=>normalized(item.question).length>=4&&(previous.inputRequests||[]).some(prompt=>typeof prompt==='string'&&normalized(prompt).includes(normalized(item.question)))),explicitReply=reply.length>0&&last&&(last.waitingForInput||matchesPrompt||(previous.state==='continued'&&reply.some(item=>normalized(item.question).length>=4&&normalized(previous.answer).includes(normalized(item.question)))));
      const newRequest=/(?:帮我|请|我要|我想)(?:另|再)?(?:写|做|查|找|打开|安排|设计|实现|生成|修复|研究|分析|拆解)/.test(value);
      const plainReply=last?.waitingForInput&&value.length<=500&&!changedTopic&&!newRequest&&DocumentMap.requestMode(value)===null;
      if(last&&!changedTopic&&(explicitReply||plainReply)){
        last.clarifications.push(...(reply.length?reply:[{question:previous.inputRequests?.join('\n\n')||previous.answer,answer:turn.question}]).map(item=>({...item,fromTurnId:previous.id,replyTurnId:turn.id})));
        last.parts.push(turn);last.waitingForInput=waitsForInput(turn);aliases[turn.id]=last.id;
      }else groups.push({id:turn.id,startIndex:index,parts:[turn],clarifications:[],waitingForInput:waitsForInput(turn)});
    }
    return {aliases,turns:groups.map(group=>{const first=group.parts[0],last=group.parts[group.parts.length-1],merged=group.parts.length>1;return {...first,id:group.id,startIndex:group.startIndex,endTurnIndex:group.startIndex+group.parts.length,answer:last.answer,answerSourceTurnId:last.id,sourceTurnIds:group.parts.map(part=>part.id),state:group.waitingForInput?'waiting_input':last.state,waitingForInput:group.waitingForInput,pendingQuestion:group.waitingForInput?(last.inputRequests?.join('\n\n')||last.answer):'',clarifications:group.clarifications,tools:merged?group.parts.flatMap(part=>(part.tools||[]).map((tool,index)=>({...tool,id:part.id+'::'+(tool.id||index)}))):first.tools||[]};})};
  }
  function project(turns, overrides={}, options={}) {
    const grouped=groupTurns(turns),canonical=id=>canonicalId(id,grouped.aliases),manual={};
    for(const [id,parents] of Object.entries(overrides))if(Array.isArray(parents))manual[canonical(id)]=parents.map(canonical);
    const nodes=[],byId=new Map(),contexts=[],documents=[],revisions=retrospectiveChoices(grouped.turns),insightBodies=new Map();let previous=null,contentTask=false;
    for(const turn of grouped.turns) {
      const index=turn.startIndex;
      const requestMode=DocumentMap.requestMode(turn.question),question=plain(turn.question);
      // Carry a requested report through clarification or a short continuation,
      // not through every later message in the conversation.
      const continuingContent=contentTask&&previous&&question.length<=250&&(
        previous.state==='continued'||/[？?]\s*$/.test(plain(previous.answer))||
        /^(?:继续(?:研究|分析|整理|完善)?|接着|再(?:补充|展开|完善)|按.{0,12}(?:范围|要求|方向|内容)(?:继续|整理|分析))/.test(question));
      contentTask=requestMode==='content'||(requestMode!=='flow'&&continuingContent);
      let link=resolve(turn.question,contexts,previous);
      if(link.confidence!=='explicit'&&documents.length){const document=documents[documents.length-1],query=normalized(turn.question),topics=document.topics.filter(topic=>{const name=normalized(topic.question.split(/[：:？?]/)[0]);return name.length>=4&&query.includes(name);});if(topics.length)link=relation(topics.map(topic=>topic.id),topics.length>1?'topic_merge':'topic','Follow-up · '+topics.map(topic=>topic.question.split(/[：:？?]/)[0]).join(' + '),'explicit','Connected to the topic explicitly referenced in the follow-up.');}
      // Completed public answers and explicit later clarifications may refine
      // provisional relationships. Original messages and manual edits stay intact.
      if(link.confidence!=='explicit'&&contexts.length){
        const acknowledged=turn.state==='completed'&&deictic(turn.question)?plain(turn.answer).slice(0,500).match(/(?:你(?:已经)?选(?:择|定)(?:了|的)?|按你(?:的)?(?:选择|要求)|我们(?:就|先|最终)?(?:采用|选择|确定)|(?:好的|可以)[，,：:]?\s*(?:那就|先|采用|按|做))\s*(?:方案|选项)?\s*([A-H](?:\s*(?:\+|和|与|、)\s*[A-H])*)/):null;
        const choice=revisions.get(turn.id)||acknowledged?.[1];
        if(choice){const revised=resolve('Choose '+choice,contexts,previous);if(revised.keys?.length)link={...revised,confidence:'inferred',label:'Revisit · '+revised.label,reason:revisions.has(turn.id)?'An explicit later clarification resolves an earlier ambiguous branch.':'Branch confirmed from the completed response. You can adjust it.'};}
      }
      if(Object.prototype.hasOwnProperty.call(manual,turn.id)&&Array.isArray(manual[turn.id])) {
        const parents=[...new Set(manual[turn.id].filter(id=>typeof id==='string'&&byId.has(id)))];
        // Stale, malformed or forward references never disconnect a card silently.
        if(parents.length||manual[turn.id].length===0) {
          const context=contexts.slice().reverse().find(item=>parents.some(id=>id===item.id||byId.get(id)?.decisionId===item.id));
          const keys=context?[...new Set(parents.flatMap(id=>id===context.id?[]:byId.get(id)?.branchKeys||[]))]:[];
          link={...relation(parents,'manual',parents.length>1?'Manual merge':parents.length?'Manual link':'Independent start','manual','You adjusted this connection locally.'),context,keys};
        }
      }
      const sourceId=turn.answerSourceTurnId,split=turn.waitingForInput?null:extract(turn.answer),document=!split&&contentTask&&!turn.waitingForInput?DocumentMap.parse(turn.answer,options.title||turn.question):null,root={...turn,parentIds:link.parents,parentId:link.parents[0]||null,sourceTurnId:sourceId,taskQuestion:turn.question,turnIndex:index+1,
        kind:split?'decision':document?'document':'answer',relation:{kind:link.kind,label:link.label,confidence:link.confidence,reason:link.reason},
        decisionId:link.context?.id||null,branchKeys:link.keys||[]};
      if(turn.waitingForInput)root.answer='Waiting for clarification in the original chat. It will be merged here when received.';
      if(split) {root.answer=split.summary||'This turn offers '+split.options.length+' options. Expand the branches to read them.';root.recommendation=split.recommendation;root.optionIds=split.options.map(option=>optionId(turn.id,option.key));}
      if(document){root.question=document.title;root.answer=document.intro||'Expand topics to explore this turn. Select a topic to read its text and sources.';root.documentId=root.id;root.childIds=document.top.map(key=>turn.id+'-doc-'+key);root.clipped=document.clipped;}
      nodes.push(root);byId.set(root.id,root);
      if(link.context && link.kind!=='comparison') {link.context.active=link.keys||[];for(const key of link.context.active)link.context.frontier.set(key,root.id);}
      if(split) {
        const context={id:turn.id,options:split.options.map(option=>({...option,topic:tokens(option.title+' '+option.content)})),recommended:split.recommended,frontier:new Map(),active:[]};
        contexts.push(context);
        for(const option of split.options) {
          const id=optionId(turn.id,option.key),node={id,parentId:root.id,parentIds:[root.id],sourceTurnId:sourceId,sourceTurnIds:turn.sourceTurnIds,taskQuestion:turn.question,turnIndex:index+1,kind:'option',optionKey:option.key,
            question:option.key+'. '+option.title,answer:option.content||option.title,tools:[],state:turn.state,createdAt:turn.createdAt,
            decisionId:root.id,branchKeys:[option.key],recommended:split.recommended.includes(option.key),relation:{kind:'branch',label:'Option '+option.key,confidence:'explicit',reason:'Extracted from an explicit option in the response.'}};
          nodes.push(node);byId.set(id,node);context.frontier.set(option.key,id);
        }
      }
      if(document){
        const topics=[];for(const part of document.nodes){const id=turn.id+'-doc-'+part.key,parent=part.parentKey?turn.id+'-doc-'+part.parentKey:root.id,item={id,parentId:parent,parentIds:[parent],sourceTurnId:sourceId,sourceTurnIds:turn.sourceTurnIds,taskQuestion:turn.question,turnIndex:index+1,
          kind:part.kind,question:part.title,answer:part.body||'Expand subtopics for details.',tools:[],state:turn.state,createdAt:turn.createdAt,documentId:root.id,childIds:part.childKeys.map(key=>turn.id+'-doc-'+key),sourceLines:part.sourceLines,
          relation:{kind:'structure',label:part.kind==='section'?'Topic':'Detail',confidence:'explicit',reason:'Follows the original headings, lists and tables. No new facts added.'}};nodes.push(item);byId.set(id,item);if(!part.parentKey)topics.push(item);}
        documents.push({id:root.id,topics});
      }
      const insight=turn.state==='completed'?insightExcerpt(split?split.summary:turn.answer):null;
      if(insight){
        const fingerprint=normalized(insight.body),existing=insightBodies.get(fingerprint);
        if(existing)existing.sourceTurnIds.push(...turn.sourceTurnIds.filter(id=>!existing.sourceTurnIds.includes(id)));
        else {const id=turn.id+'-insight',item={id,parentId:root.id,parentIds:[root.id],sourceTurnId:sourceId,sourceTurnIds:[...turn.sourceTurnIds],taskQuestion:turn.question,turnIndex:index+1,kind:'insight',
          question:'Insight · '+insight.title,answer:insight.body,insightType:insight.type,tools:[],state:'completed',createdAt:turn.createdAt,decisionId:split?root.id:root.decisionId,documentId:document?root.id:null,branchKeys:root.branchKeys,
          relation:{kind:'insight',label:'Key conclusion excerpt',confidence:'explicit',reason:'Extracted from an explicit conclusion or recommendation in a completed response.'}};
          nodes.push(item);byId.set(id,item);insightBodies.set(fingerprint,item);}
      }
      previous=root;
    }
    return {nodes,turnCount:turns.length,taskCount:grouped.turns.length,turnIds:grouped.turns.map(turn=>turn.id),idAliases:grouped.aliases,latestId:canonical(turns[turns.length-1]?.id)||null,insightIds:nodes.filter(node=>node.kind==='insight').map(node=>node.id)};
  }
  // Layered DAG layout: options fan out, multi-parent follow-ups sit after all parents.
  function arrange(nodes, sizes, width=380, gapX=100, gapY=36, widths=new Map()) {
    const byId=new Map(),layers=new Map(),positions=new Map();
    for(const node of nodes) {
      const parents=(node.parentIds||[]).filter(id=>byId.has(id)),depth=parents.length?Math.max(...parents.map(id=>byId.get(id).depth))+1:0;
      const item={node,parents,depth,width:widths.get(node.id)||width,height:sizes.get(node.id)||400};byId.set(node.id,item);
      if(!layers.has(depth))layers.set(depth,[]);layers.get(depth).push(item);
    }
    const columns=new Map();let columnX=32;for(const depth of [...layers.keys()].sort((a,b)=>a-b)){columns.set(depth,columnX);columnX+=Math.max(...layers.get(depth).map(item=>item.width))+gapX;}
    for(const [depth,items] of layers) {
      const groups=new Map();
      for(const item of items) {const key=item.parents.join('\u0000');if(!groups.has(key))groups.set(key,[]);groups.get(key).push(item);}
      for(const group of groups.values()) {
        const parents=group[0].parents,center=parents.length?parents.reduce((sum,id)=>sum+positions.get(id).y+positions.get(id).height/2,0)/parents.length:200;
        const total=group.reduce((sum,item)=>sum+item.height,0)+gapY*(group.length-1);let top=center-total/2;
        for(const item of group) {item.desired=top;top+=item.height+gapY;}
      }
      items.sort((a,b)=>a.desired-b.desired);let bottom=-Infinity;
      for(const [lane,item] of items.entries()) {const y=Math.max(item.desired,bottom+gapY);positions.set(item.node.id,{x:columns.get(depth),y,width:item.width,height:item.height,depth,lane});bottom=y+item.height;}
    }
    let min=0;for(const point of positions.values())min=Math.min(min,point.y);let maxWidth=1,maxHeight=1;
    for(const point of positions.values()) {point.y+=32-min;maxWidth=Math.max(maxWidth,point.x+point.width+32);maxHeight=Math.max(maxHeight,point.y+point.height+32);}
    return {positions,width:maxWidth,height:maxHeight};
  }
  const api={extract,project,arrange,insightExcerpt,groupTurns,canonicalId};
  if(typeof module!=='undefined'&&module.exports)module.exports=api;
  else window.ThoughtGraph=api;
})();
