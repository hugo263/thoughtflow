const assert=require('node:assert/strict');
const {project}=require('../graph.js');
const {report}=require('./document-map.test.cjs');
const turn=(id,question,answer=report,state='completed')=>({id,question,answer,state,tools:[]});
const kind=(graph,id)=>graph.nodes.find(node=>node.id===id).kind;

// Multi-heading prose is common in ordinary advice and problem-solving.
for(const question of ['我最近总是拖延，帮我梳理一下问题','这件事该怎么想？','分析一下我为什么这么纠结','我们简单聊聊这个问题','研究报告和普通聊天的边界是什么？']){
  const graph=project([turn('chat',question)],{},{title:'产品研究'});
  assert.equal(kind(graph,'chat'),'answer',question);
  assert.ok(!graph.nodes.some(node=>node.kind==='section'),'a title or answer format does not select content mode');
  assert.ok(graph.nodes.some(node=>node.kind==='insight'),'normal dialogue keeps insight extraction');
}
for(const question of ['帮我研究下捞捞这个 App，分析一下它的方方面面','研究这个产品','整理这份文档','把这篇文章转成思维导图','做一份竞品分析报告','拆解这个网站的盈利模式','分析这家公司的盈利情况','调研这个行业','拆解这个小程序'])
  assert.equal(kind(project([turn('report',question)]),'report'),'document',question);

let graph=project([
  turn('request','帮我研究下捞捞这个 app','你想研究的是哪一款？','continued'),
  turn('clarification','饭圈数据与应援工具「捞捞APP」'),
  turn('continue','继续完善'),
  turn('ordinary','我最近为什么总是拖延？'),
  turn('ordinary2','继续聊聊我的问题'),
]);
assert.equal(kind(graph,'request'),'document','research and clarification form one task');
assert.equal(graph.idAliases.clarification,'request');
assert.equal(kind(graph,'continue'),'document','explicit continuation stays with the requested report');
assert.equal(kind(graph,'ordinary'),'answer','later ordinary dialogue resets content mode');
assert.equal(kind(graph,'ordinary2'),'answer');

graph=project([turn('report','研究这个产品'),turn('follow','商业模式详细说说')]);
const topic=graph.nodes.find(node=>node.question.startsWith('商业模式：'));
assert.equal(kind(graph,'follow'),'answer','a topic discussion is still part of the dialogue flow');
assert.deepEqual(graph.nodes.find(node=>node.id==='follow').parentIds,[topic.id],'discussion connects to the report topic');

for(const question of ['先只聊聊，不要研究报告','换个话题，帮我梳理一下最近的困惑','回到之前的思考流程']){
  graph=project([turn('request','研究这个产品','你想研究哪一款？','continued'),turn('chat',question)]);
  assert.equal(kind(graph,'chat'),'answer',question);
}
graph=project([turn('options','帮我梳理该怎么选','## A. 先试小规模\n验证成本\n## B. 直接推广\n投入较大\n我的建议是 A。'),turn('choose','我选 A')]);
assert.equal(kind(graph,'options'),'decision');
assert.deepEqual(graph.nodes.find(node=>node.id==='choose').parentIds,['options-option-A'],'ordinary dialogue retains option routing');
console.log('PASS: ordinary dialogue stays a flow; explicit content tasks, clarification, continuation, topic follow-ups and switches keep their boundaries.');
