const assert=require('node:assert/strict');
const {parse,visibleGraph}=require('../document.js');
const {project}=require('../graph.js');
const report=`这是原回答的概览。产品定位明确，但目前价格待验证。

## 一、产品定位

为特定人群减少重复劳动。

- **重复任务**：每天需要处理。
  - **定时任务**：按指定时间执行。
- **组织成本**：多人协作需要协调。

## 二、功能体验

| 功能 | 价值 | 待核实 |
|---|---|---|
| 账号管理 | 减少切换 | 授权范围 |
| 任务配置 | 批量配置 | 当前限制 |

## 三、商业模式：会员与消耗品

**第一层：会员。** 为持续能力付费。

**第二层：用量。** 具体价格待核实。[产品说明](https://example.com/pricing)

### 优势

- 有持续使用需求。

### 限制

- 付费规模未知。

## 四、我的综合评价

**需求清楚，但缺少经过验证的留存和付费数据。**

原始证据仍需核实。`;
const turn={id:'research',question:'研究这个产品',answer:report,state:'completed',tools:[]};
const outline=parse(report,'产品研究');assert.equal(outline.top.length,4);assert.ok(outline.nodes.some(node=>node.title==='账号管理'));assert.ok(outline.nodes.some(node=>node.title==='授权范围')===false,'cells remain facts inside the row');assert.ok(outline.nodes.find(node=>node.title==='账号管理').body.includes('**待核实**：授权范围'));
const nested=outline.nodes.find(node=>node.title==='定时任务');assert.equal(outline.nodes.find(node=>node.key===nested.parentKey).title,'重复任务');
assert.equal(parse('普通段落\n\n1. 一个步骤\n2. 另一个步骤'),null);assert.equal(parse('## 结论\n\n一个结论'),null);
assert.equal(parse('```\n## Fake A\n## Fake B\n```'),null);
const before=outline.nodes.find(node=>node.title==='账号管理').key,updated=parse(report.replace('减少切换','减少账号切换'),'产品研究');assert.equal(updated.nodes.find(node=>node.title==='账号管理').key,before,'body edits preserve node identity');
const h1=parse('# 报告标题\n\n## 章节甲\n内容\n## 章节乙\n内容','fallback');assert.equal(h1.title,'报告标题');assert.equal(h1.top.length,2);
const large=parse('## 章节甲\n'+Array.from({length:200},(_,i)=>'- 要点 '+i).join('\n')+'\n## 章节乙\n结尾');assert.ok(large.nodes.length<=128);assert.ok(large.clipped>0);assert.equal(large.top.length,2,'all chapters are prioritized before details');
let graph=project([turn],{}, {title:'产品研究'}),root=graph.nodes.find(node=>node.kind==='document');assert.equal(root.question,'产品研究');assert.equal(root.childIds.length,4);assert.equal(graph.insightIds.length,1);assert.ok(graph.nodes.filter(node=>node.kind==='detail').length>=6);
let visible=visibleGraph(graph.nodes);assert.ok(visible.length<graph.nodes.length);assert.equal(visible.filter(node=>node.kind==='section').length,4,'initial overview shows the primary chapters');
const feature=graph.nodes.find(node=>node.question==='功能体验');visible=visibleGraph(graph.nodes,new Set([feature.id]));assert.ok(visible.some(node=>node.question==='账号管理'));
const closed=visibleGraph(graph.nodes,new Set(),new Set([root.id]));assert.equal(closed.length,1,'folding the report hides its descendants');
graph=project([turn,{id:'follow',question:'商业模式详细说说',answer:'继续分析收入来源',tools:[],state:'completed'}],{}, {title:'产品研究'});const revenue=graph.nodes.find(node=>node.question.startsWith('商业模式：'));assert.deepEqual(graph.nodes.find(node=>node.id==='follow').parentIds,[revenue.id],'follow-up continues from a referenced chapter');
console.log('PASS: report headings, nested lists, table row facts, stable IDs, bounded extraction, folding, source provenance and topic follow-ups.');
module.exports={report,turn};
