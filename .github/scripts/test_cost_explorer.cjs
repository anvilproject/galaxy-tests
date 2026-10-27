const {test}=require('node:test');
const assert=require('node:assert/strict');
const x=require('../../docs/assets/cost-explorer.js');
test('aggregate costs, true median, unknown phases and unpriced coverage',()=>{
 const jobs=[{tool_id:'s/repos/iuc/foo/foo/1',cost:2,boot_cost:1,run_cost:1,phases_separated:true},
 {tool_id:'s/repos/iuc/foo/foo/2',cost:4,phases_separated:false},
 {tool_id:'s/repos/iuc/foo/foo/2',cost:null}];
 const r=x.rows(jobs,'tools')[0];assert.equal(r.label,'iuc/foo');assert.equal(r.count,3);assert.equal(r.priced,2);
 assert.equal(r.cost,6);assert.equal(r.median,3);assert.equal(r.overhead,0.5);assert.equal(r.knownCost,2);
 assert.equal(Object.values(r.phases).reduce((a,b)=>a+b,0),r.cost);
 assert.equal(r.phases.unknown,4);
});
test('different actions within a repository remain distinct',()=>{
 const rows=x.rows([{tool_id:'s/repos/iuc/bedtools/intersect/1',cost:1},{tool_id:'s/repos/iuc/bedtools/coverage/1',cost:2}],'tools');
 assert.equal(rows.length,2);assert.equal(rows[0].label,'iuc/bedtools · intersect');
});
test('missing prices remain unknown and sort last in both directions',()=>{
 const rows=x.rows([{tool_name:'free',cost:0},{tool_name:'unknown',cost:null},{tool_name:'paid',cost:2}],'tools');
 assert.equal(x.sort(rows,'cost',true).at(-1).label,'unknown');assert.equal(x.sort(rows,'cost',false)[0].label,'paid');
 assert.equal(rows.find(r=>r.label==='unknown').cost,null);
 assert.equal(rows.find(r=>r.label==='free').cost,0);
});
test('overhead includes idle and teardown; CSV quotes delimiters',()=>{
 const r=x.summarize([{cost:10,boot_cost:1,run_cost:5,idle_cost:2,teardown_cost:2,phases_separated:true}], 'a','a,"b');
 assert.equal(r.overhead,.5);assert.match(x.csv([r]),/"a,""b"/);
});
test('actual run aggregation conserves spending and job count',()=>{
 const fs=require('node:fs');const run=JSON.parse(fs.readFileSync('docs/cost-data/runs/anvil-test-ci-261027-110700.json'));
 const rows=x.rows(run.jobs,'tools');assert.equal(rows.reduce((a,r)=>a+r.count,0),run.jobs.length);
 const sum=rows.reduce((a,r)=>a+(r.cost||0),0);assert.ok(Math.abs(sum-run.gcp_batch_total_cost)<.0001);
 assert.equal(x.sort(rows,'cost',false)[0].label,'iuc/shovill');
});
