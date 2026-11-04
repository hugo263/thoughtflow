const assert=require('node:assert/strict');
const {extract,project,arrange}=require('../graph.js');
const answer=`可以有四种做法，区别是通过聊天管理，还是在界面里查看和勾选。

| 选项 | 使用方式 | 工作量 |
|---|---|---|
| **A. 聊天式 Skill** | 用自然语言整理 | 较小 |
| **B. 可视化对话管家** | 本机网页搜索和排序 | 中等 |
| **C. 扩展思考图谱** | 旁边增加管理入口 | 中等 |
| **D. 完整插件** | 打包工具和界面 | 较大 |

### A. 聊天式 Skill：最轻便

用自然语言查找旧对话。

### B. 可视化对话管家：直观整理

提供文件清单、空间分析和勾选界面。

### C. 扩展思考图谱：一起回顾

在现有图谱里增加整理入口。

### D. 完整插件：长期产品化

打包后分享给其他人。

**我的建议是先做 A + B 的精简版**：用 Skill 理解指令，用网页展示结果。

如果更喜欢在图谱里操作，也可以考虑 C。`;
const turn=(id,question,body='继续细化这个选择。')=>({id,question,answer:body,tools:[],state:'completed'});
const initial=()=>turn('t1','对话管家可以怎么做？',answer);
const node=(graph,id)=>graph.nodes.find(node=>node.id===id);
const follow=question=>project([initial(),turn('t2',question)]);

const split=extract(answer);assert.equal(split.options.length,4);assert.deepEqual(split.recommended,['A','B']);
assert.match(split.summary,/四种做法/);assert.match(split.recommendation,/先做 A \+ B/);
assert.match(split.options[0].content,/自然语言/);assert.match(split.options[1].content,/空间分析/);
assert.ok(!split.options[3].content.includes('我的建议'),'recommendation must not leak into D');
assert.ok(!split.summary.includes('### A'),'option details belong to branch cards');
assert.equal(extract('1. 搜索\n2. 归档\n3. 清理'),null,'ordinary steps are not alternatives');
assert.equal(extract('```\nA. code\nB. code\n```'),null,'code is never split');
assert.equal(extract('A. only one alternative'),null);
assert.equal(extract('### A. Alpha\nDetails\n### B. Beta\nMore details').options.length,2);
assert.equal(extract('- **A. Alpha**\nDetail\n- **B. Beta**\nDetail').options.length,2);
assert.equal(extract('### [A] Alpha\nDetail\n### （B）Beta\nDetail').options.length,2);
assert.match(extract('### A. Alpha\n我建议先检查文件。\n### B. Beta\n另一个方向。').options[0].content,/我建议/,'an option-specific suggestion stays in its option');
assert.equal(extract('| 选项 | 说明 |\n|---|---|\n| A. Alpha | 一 |\n| B. Beta | 二 |').options.length,2);
let graph=project([initial()]);assert.equal(graph.nodes.length,5);assert.equal(graph.latestId,'t1');assert.equal(graph.turnCount,1);
assert.equal(node(graph,'t1').kind,'decision');assert.equal(node(graph,'t1-option-A').recommended,true);assert.equal(node(graph,'t1-option-C').recommended,false);
assert.deepEqual(node(follow('我选 A，先做起来'),'t2').parentIds,['t1-option-A']);
assert.deepEqual(node(follow('A+B 一起做'),'t2').parentIds,['t1-option-A','t1-option-B']);
assert.deepEqual(node(follow('A 和 B 组合起来'),'t2').parentIds,['t1-option-A','t1-option-B']);
assert.deepEqual(node(follow('不要 A，我选择 B'),'t2').parentIds,['t1-option-B']);
assert.deepEqual(node(follow('不要 A 和 B，我选择 C'),'t2').parentIds,['t1-option-C']);
assert.deepEqual(node(follow('我更喜欢可视化对话管家'),'t2').parentIds,['t1-option-B']);
assert.deepEqual(node(follow('按你的推荐先做'),'t2').parentIds,['t1-option-A','t1-option-B']);
assert.equal(node(follow('比较 A 和 B 的区别'),'t2').relation.kind,'comparison');
assert.deepEqual(node(project([initial(),turn('t2','比较 A 和 B'),turn('t3','我选 A')]),'t3').parentIds,['t1-option-A'],'comparison does not count as adopting both options');
assert.deepEqual(node(follow('换个话题，聊一下出差安排'),'t2').parentIds,['t1']);
assert.equal(node(follow('换个话题，聊一下出差安排'),'t2').relation.kind,'new_topic');
assert.equal(node(follow('周末天气晴吗'),'t2').relation.confidence,'unknown');
graph=project([initial(),turn('t2','选 A'),turn('t3','继续细化'),turn('t4','那 B 呢？'),turn('t5','A+B 组合')]);
assert.deepEqual(node(graph,'t3').parentIds,['t2'],'follow-up continues along selected branch');
assert.deepEqual(node(graph,'t4').parentIds,['t1-option-B'],'switching branch does not continue from A');
assert.deepEqual(node(graph,'t5').parentIds,['t3','t4'],'merges use the current frontier of both branches');
graph=project([initial(),turn('t2','换个话题，聊旅行'),turn('t3','那需要准备什么？')]);assert.deepEqual(node(graph,'t3').parentIds,['t2']);
graph=project([initial(),turn('t2','周末天气晴吗')],{t2:['t1-option-A','t1-option-C']});
assert.deepEqual(node(graph,'t2').parentIds,['t1-option-A','t1-option-C']);assert.equal(node(graph,'t2').relation.confidence,'manual');
graph=project([initial(),turn('t2','选 A')],{t2:['future-id']});assert.deepEqual(node(graph,'t2').parentIds,['t1-option-A'],'stale overrides fall back to automatic routing');
graph=project([initial(),turn('t2','选 A')],{t2:[]});assert.deepEqual(node(graph,'t2').parentIds,[]);
const original=initial(),before=JSON.stringify(original);project([original]);assert.equal(JSON.stringify(original),before,'source turns stay read-only');
graph=project([initial(),turn('t2','A+B'),turn('t3','选 C')]);
const arranged=arrange(graph.nodes,new Map(graph.nodes.map((n,i)=>[n.id,240+i*20]))),seen=new Set();
for(const n of graph.nodes){for(const p of n.parentIds){assert.ok(seen.has(p),'all edges go forward');assert.ok(arranged.positions.get(p).depth<arranged.positions.get(n.id).depth);}seen.add(n.id);}
const positions=[...arranged.positions.values()];for(let i=0;i<positions.length;i++)for(let j=i+1;j<positions.length;j++){const a=positions[i],b=positions[j];if(a.depth===b.depth)assert.ok(a.y+a.height<=b.y||b.y+b.height<=a.y,'cards in a column do not overlap');}
assert.equal(new Set(['t1-option-A','t1-option-B','t1-option-C','t1-option-D'].map(id=>arranged.positions.get(id).x)).size,1,'four options share a branching column');
graph=project([initial(),turn('t2','那就这个吧','好的，采用 A+B 的精简版。')]);assert.deepEqual(node(graph,'t2').parentIds,['t1-option-A','t1-option-B']);assert.equal(node(graph,'t2').relation.confidence,'inferred','completed public answer can clarify a provisional relationship');
const corrected=[initial(),turn('t2','那就这个吧','准备细化。'),turn('t3','刚才说的其实是 B','继续 B。')];
graph=project(corrected);assert.deepEqual(node(graph,'t2').parentIds,['t1-option-B'],'later explicit clarification revises an earlier ambiguous link');
graph=project(corrected,{t2:['t1-option-A']});assert.deepEqual(node(graph,'t2').parentIds,['t1-option-A'],'manual correction wins over retrospective inference');
const fact='## 关键结论\n\n归档对话不会释放磁盘空间；清理输出文件需要检查归属。';
graph=project([turn('i1','怎么清理？',fact),turn('i2','再说一遍',fact)]);assert.equal(graph.insightIds.length,1);assert.deepEqual(node(graph,'i1-insight').sourceTurnIds,['i1','i2']);assert.match(node(graph,'i1-insight').answer,/归档对话不会释放/);
graph=project([{...turn('i1','怎么清理？',fact),state:'running'}]);assert.equal(graph.insightIds.length,0,'streaming fragments do not become final insights');
assert.equal(project([turn('i1','代码示例','```\n## 关键结论\n这里是代码\n```')]).insightIds.length,0,'code is never treated as an insight');
graph=project([turn('i1','怎么清理？','有一个关键区别：**归档整理列表，但不释放磁盘空间。** 这里是后面可能被修正的旧解释。'),turn('i2','进一步核对','我补充更正一下前面的说法：当前存在相关接口，实际可用性需要验证。')]);assert.ok(!node(graph,'i1-insight').answer.includes('旧解释'),'insights retain the atomic emphasized finding');assert.equal(node(graph,'i2-insight').insightType,'correction','later corrections remain visible with their own provenance');
console.log('PASS: public-answer extraction, A/B/C/D branches, selection, combination, recommendation, topic changes, manual correction and DAG layout.');
module.exports={answer,turn,initial};
