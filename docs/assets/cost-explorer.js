/* Cost aggregation is independent of timing: unpriced and untimed jobs stay visible. */
(function (root) {
  "use strict";
  function identity(job) {
    var id = job.tool_id || job._toolName || job.tool_name || "Unknown tool";
    var m = id.match(/\/repos\/([^/]+)\/([^/]+)\/([^/]+)/);
    return m ? {key: m.slice(1).join("/"), label: m[1] + "/" + m[2] + (m[2] !== m[3] ? " · " + m[3] : "")} : {key: id, label: job._toolName || job.tool_name || id};
  }
  function summarize(jobs, key, label) {
    var priced = jobs.filter(function (j) { return Number.isFinite(j.cost); });
    var values = priced.map(function (j) { return j.cost; }).sort(function (a,b) { return a-b; });
    var cost = priced.length ? values.reduce(function(a,b){return a+b;},0) : null;
    var phases = {boot:0, run:0, idle:0, teardown:0, unknown:0};
    var knownCost = 0, overhead = 0;
    priced.forEach(function (j) {
      var keys = ["boot", "run", "idle", "teardown"];
      var phaseTotal = keys.reduce(function(a,k){return a + (j[k + "_cost"] || 0);},0);
      // Legacy phase estimates can be displayed, but missing phase data is never zero overhead.
      if (j.phases_separated !== false && phaseTotal > 0 && Math.abs(phaseTotal-j.cost) < 0.0001) {
        keys.forEach(function(k){phases[k] += j[k + "_cost"] || 0;});
        knownCost += j.cost; overhead += (j.boot_cost || 0) + (j.idle_cost || 0) + (j.teardown_cost || 0);
      } else phases.unknown += j.cost;
    });
    var matched = jobs.filter(function(j){return j._recon && Number.isFinite(j._recon.billing);});
    return {key:key, label:label, jobs:jobs, count:jobs.length, priced:priced.length,
      cost:cost, median:values.length ? (values[Math.floor((values.length-1)/2)]+values[Math.floor(values.length/2)])/2 : null,
      phases:phases, overhead:knownCost ? overhead/knownCost : null, knownCost:knownCost,
      billing:matched.length ? matched.reduce(function(a,j){return a+j._recon.billing;},0) : null, matched:matched.length};
  }
  function rows(jobs, mode) {
    if (mode === "jobs") return jobs.map(function(j,i){return summarize([j], String(i), identity(j).label + " · job " + (j.source_id || j.name || j.id || i));});
    var groups = new Map();
    jobs.forEach(function(j){var id=identity(j); if(!groups.has(id.key)) groups.set(id.key,{id:id,jobs:[]}); groups.get(id.key).jobs.push(j);});
    return Array.from(groups.values()).map(function(g){return summarize(g.jobs,g.id.key,g.id.label);});
  }
  function sort(rows, key, ascending) {
    return rows.slice().sort(function(a,b){
      var x=a[key],y=b[key];
      if(x==null && y!=null)return 1;if(y==null && x!=null)return -1;
      var diff=typeof x === "string" ? x.localeCompare(y) : (x||0)-(y||0);
      return (ascending ? diff : -diff) || a.label.localeCompare(b.label);
    });
  }
  function csv(rows) {
    function quote(x){return '"'+String(x==null ? "" : x).replace(/"/g,'""')+'"';}
    return [["Tool / job","Jobs","Priced jobs","Total USD","Median USD/job","Overhead fraction (known phases)"]].concat(rows.map(function(r){return [r.label,r.count,r.priced,r.cost,r.median,r.overhead];})).map(function(r){return r.map(quote).join(",");}).join("\r\n");
  }
  var api={identity:identity,summarize:summarize,rows:rows,sort:sort,csv:csv};
  if(typeof module!=="undefined" && module.exports)module.exports=api;
  root.GxCostExplorer=api;
})(typeof window!=="undefined" ? window : globalThis);
