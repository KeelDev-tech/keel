"use strict";
const $ = (selector, root = document) => root.querySelector(selector);
const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const words = value => String(value ?? "").replaceAll("_", " ").toLowerCase();
const mins = value => value == null ? "Unknown" : `${Math.round(value / 6) / 10} min`;
const stamp = value => value ? new Date(value).toLocaleString(undefined, {month:"short",day:"numeric",hour:"2-digit",minute:"2-digit"}) : "Unknown";
const components = ["policy","form","answers","attachments","target","approval","route"];
const titles = {overview:"Overview",pipeline:"Opportunities",evidence:"Evidence",workflows:"Workflows",twin:"Twin lab",integrations:"Integrations"};
const state = {token:new URLSearchParams(location.hash.slice(1)).get("token") || "", page:"overview", view:null,
  catalog:[], history:[], query:"", lane:"All lanes", filter:"All roles", workflow:"source-repair", scope:"", report:null, busy:false};
if (location.hash.includes("token=")) history.replaceState(null, "", location.pathname);
let toastTimer;
function toast(message, error = false) {
  const node = $("#toast"); node.textContent = message; node.classList.toggle("error", error); node.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { node.hidden = true; }, 7000);
}
async function api(path, body) {
  const response = await fetch(path, {method:body === undefined ? "GET" : "POST", cache:"no-store", credentials:"omit",
    headers:{Authorization:`Bearer ${state.token}`, "Content-Type":"application/json"},
    ...(body === undefined ? {} : {body:typeof body === "string" ? body : JSON.stringify(body)})});
  let data;
  try { data = await response.json(); } catch { throw new Error(`Local server returned HTTP ${response.status}.`); }
  if (!response.ok) {
    if (response.status === 401) { state.token = ""; state.view = null; showLogin(); }
    throw new Error(data.error?.message || `HTTP ${response.status}`);
  }
  return data;
}
function download(name, value) {
  const blob = new Blob([JSON.stringify(value, null, 2)+"\n"], {type:"application/json"});
  const url = URL.createObjectURL(blob); const anchor = document.createElement("a");
  anchor.href = url; anchor.download = name; anchor.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
}
function badge(text, color="neutral") { return `<span class="pill ${color}">${esc(text)}</span>`; }
function pageHead(eyebrow, title, subtitle, action="") {
  return `<div class="page-head"><div><p class="eyebrow">${esc(eyebrow)}</p><h1>${esc(title)}</h1><p class="subtitle">${esc(subtitle)}</p></div>${action}</div>`;
}
function metric(label, value, note, highlight=false) {
  return `<div class="metric ${highlight ? "highlight" : ""}"><div class="metric-label">${esc(label)}<span aria-hidden="true">↗</span></div><div class="metric-value">${esc(value ?? "—")}</div><small>${esc(note)}</small></div>`;
}
function panel(title, subtitle, body, action="") {
  return `<section class="panel"><div class="panel-head"><div><h2>${esc(title)}</h2>${subtitle ? `<p class="subtitle">${esc(subtitle)}</p>` : ""}</div>${action}</div><div class="panel-body">${body}</div></section>`;
}
function go(page) {
  if (!titles[page]) return;
  state.page = page;
  $("#breadcrumb").textContent = titles[page];
  document.querySelectorAll("[data-page]").forEach(node => {
    const active = node.dataset.page === page; node.classList.toggle("active",active);
    if(active) node.setAttribute("aria-current","page"); else node.removeAttribute("aria-current");
  });
  render();
}
function showLogin(message="") {
  $("#main").innerHTML = `<div class="connect-card"><p class="eyebrow">YOUR LOCAL SESSION</p><h1>Open your workbench</h1><p>Use the private link printed when you start Keel Workbench, or paste its session token below.</p><form id="login"><label class="field"><span>Session token</span><input id="session-token" type="password" autocomplete="off" required minlength="32"></label><button class="button primary">Connect to workspace →</button></form><p class="error-text" id="login-error">${esc(message)}</p><p class="small">Start a sample workspace with <span class="inline-code">python3 -m keel_workbench serve --demo</span></p></div>`;
  $("#login").onsubmit = async event => {
    event.preventDefault(); state.token = $("#session-token").value.trim(); $("#session-token").value = "";
    try { await load(); } catch(error) { showLogin(error.message); }
  };
}
function freshness() {
  const v=state.view; $("#mode-pill").textContent=v.synthetic ? "Synthetic demo" : "Operational snapshot";
  const notice=$("#notice"); notice.hidden=false;
  notice.textContent=v.synthetic ? "Demo workspace · synthetic records · clock fixed at September 18, 2026, 12:00 UTC." :
    v.current ? `Export observed ${stamp(v.observed_at)} · review checks are refreshed every 30 seconds.` :
      `Export needs refresh · last observed ${stamp(v.observed_at)}. Import a fresh canonical snapshot to evaluate current readiness.`;
}
async function load() {
  if(!state.token) return showLogin();
  const [view,catalog,history] = await Promise.all([api("/api/v1/overview"),api("/api/v1/workflows"),api("/api/v1/history")]);
  state.view=view; state.catalog=catalog.workflows; state.history=history.runs; freshness(); render();
}
function roleTable(roles) {
  if(!roles.length) return '<div class="empty">No opportunities match these filters. Try a different lane or search.</div>';
  return `<div class="table-wrap"><table><thead><tr><th scope="col">Opportunity</th><th scope="col">Lane</th><th scope="col">Fit</th><th scope="col">Queue</th><th scope="col">Review</th></tr></thead><tbody>${roles.map(r=>`<tr><td><button class="opportunity-title" data-role="${esc(r.role_id)}">${esc(r.title)}</button><span class="company">${esc(r.company)}</span></td><td>${esc(r.lane)}</td><td><span class="fit">${esc(r.fit_score)}</span></td><td>${badge(words(r.queue_status))}</td><td>${badge(r.review_checks_passed ? "Checks passed" : "Review required",r.review_checks_passed ? "teal" : "amber")}</td></tr>`).join("")}</tbody></table></div>`;
}
function overview() {
  const v=state.view,c=v.counts,f=v.forecast;
  const questions=v.questions.slice(0,2).map(q=>`<div class="question"><strong>${esc(q.question)}</strong><p>${q.roles_progressed.length} roles affected · ${esc(words(q.classification))}</p>${q.classification==="CONSENT_QUARANTINED" ? '<p>Awaiting the applicant’s explicit reply.</p>' : ''}</div>`).join("");
  return pageHead("PIPELINE PULSE", "A clearer path forward.", "See what’s ready, what needs evidence, and where your next review will help.", '<button class="button primary" data-brief>Build my review brief <span aria-hidden="true">↗</span></button>')+
    `<div class="metric-grid">${metric("Opportunities",c.roles,"Across your current export")}${metric("Base checks passed",c.base_ready,`${c.nominal_ready} marked READY in the queue`)}${metric("All review checks passed",c.review_checks_passed,"Evidence + assurance + flow",true)}${metric("Source repair groups",c.source_groups,`${c.source_gaps} missing or invalid records`)}</div>`+
    `<div class="cols">${panel("Readiness, with context", "Each layer answers a different question", `<div class="flow-strip"><div><strong>${c.nominal_ready}</strong><small>Queue READY</small></div><div><strong>${c.base_ready}</strong><small>Base checks</small></div><div><strong>${c.review_checks_passed}</strong><small>All review checks</small></div></div><div class="callout">${c.review_checks_passed ? "Review checks describe this export. The authorized host still owns every execution decision." : `Start with the evidence supply: ${c.source_groups} repair groups cover ${c.source_gaps} source gaps. Missing records remain visible until an authentic export supplies them.`}</div>`)}
    ${panel("Your next moves", "Small, concrete steps", `<div class="task-row"><span class="task-icon" aria-hidden="true">◇</span><div class="task-copy"><strong>Repair the evidence supply</strong><p>${c.source_groups} groups · owned adapter tasks</p></div><button class="button quiet" data-start="source-repair" aria-label="Open source repair workflow">→</button></div><div class="task-row"><span class="task-icon" aria-hidden="true">⇄</span><div class="task-copy"><strong>Review application materials</strong><p>Trace claims, revisions and packet bindings</p></div><button class="button quiet" data-start="material-review" aria-label="Open material review workflow">→</button></div><div class="task-row"><span class="task-icon" aria-hidden="true">⌁</span><div class="task-copy"><strong>Test your next scenario</strong><p>See the effect of slower supply or a rate hold</p></div><button class="button quiet" data-nav="twin" aria-label="Open twin lab">→</button></div>`)}</div>`+
    `<div class="cols"><section class="panel"><div class="panel-head"><div><h2>Opportunities to review</h2><p class="subtitle">Readiness is evaluated against the current export</p></div><button class="button quiet" data-nav="pipeline">View all →</button></div>${roleTable(v.roles.slice(0,5))}<div class="table-footer"><span>Showing ${Math.min(5,c.roles)} of ${c.roles} opportunities</span><span>Fit floor 75</span></div></section><div>${panel("Supply outlook","Conditional flow estimate",`<div class="line-item"><span>State</span>${badge(words(f.state),f.state==="STARVATION_RISK" ? "amber":"neutral")}</div><div class="line-item"><span>Current runway</span><strong>${mins(f.runway_seconds)}</strong></div><div class="line-item"><span>First refill opportunity</span><strong>${mins(f.first_refill_opportunity_seconds)}</strong></div><p class="small muted mt">Forecast is uncalibrated. Actual conversions and provider outcomes can differ.</p>`)}<section class="panel mt"><div class="panel-head"><h2>Questions for the applicant</h2>${badge(c.human_questions)}</div><div class="panel-body">${questions || '<p class="small muted">No unresolved questions in this export.</p>'}</div></section></div></div>`;
}
function pipeline() {
  const options=(values,current)=>values.map(v=>`<option ${v===current ? "selected":""}>${esc(v)}</option>`).join("");
  return pageHead("OPPORTUNITIES", "Keep the whole pipeline in view.", "Filter your review queue, then inspect the exact blockers for each role.")+
    `<div class="filters"><label class="field search"><span>Search opportunities</span><input id="search" placeholder="Search role, company or ID…" value="${esc(state.query)}" type="search"></label><label class="field"><span>Career lane</span><select id="lane">${options(["All lanes","Operations","Sales","Technology","General"],state.lane)}</select></label><label class="field"><span>Review filter</span><select id="review-filter">${options(["All roles","Needs review","Base checks passed","All review checks passed"],state.filter)}</select></label></div><section class="panel" id="pipeline-table"></section>`;
}
function updatePipeline() {
  const q=state.query.toLocaleLowerCase();
  const roles=state.view.roles.filter(r=>(state.lane==="All lanes" || r.lane===state.lane) &&
    (state.filter==="All roles" || state.filter==="Needs review" && !r.review_checks_passed || state.filter==="Base checks passed" && r.base_checks_passed || state.filter==="All review checks passed" && r.review_checks_passed) &&
    `${r.role_id} ${r.title} ${r.company}`.toLocaleLowerCase().includes(q));
  $("#pipeline-table").innerHTML=roleTable(roles)+`<div class="table-footer"><span>${roles.length} of ${state.view.roles.length} opportunities</span><span>Click a role for its evidence and blockers</span></div>`;
}
function evidence() {
  const v=state.view; const artifacts=v.evidence?.artifacts || [];
  return pageHead("EVIDENCE SUPPLY", "Make every missing record actionable.", "Seven source families, grouped by cause and owner.",'<button class="button primary" data-start="source-repair">Create repair plan →</button>')+
    `<div class="source-grid">${components.map(name=>{const count=v.roles.filter(r=>r.sources[name]?.status==="READY").length; return `<div class="source-card"><h3>${name}</h3><div class="metric-value">${count}<span class="small muted"> / ${v.roles.length}</span></div>${badge(count===v.roles.length && count>0 ? "Records complete" : "Source gaps",count===v.roles.length && count>0?"teal":"amber")}<p>Current structural source checks</p></div>`;}).join("")}</div>`+
    `<section class="panel mb"><div class="panel-head"><div><h2>Repair groups</h2><p class="subtitle">Infrastructure gaps stay with the adapter; authentic human decisions stay with the applicant.</p></div>${badge(v.source_inventory.groups.length+" groups")}</div><div class="table-wrap"><table><thead><tr><th>Source family</th><th>Cause</th><th>Owner</th><th>Affected roles</th></tr></thead><tbody>${v.source_inventory.groups.map(g=>`<tr><td>${esc(g.component)}</td><td>${esc(words(g.reason))}</td><td>${esc(g.owner)} ${badge(g.responsibility,g.responsibility==="human"?"amber":"neutral")}</td><td>${g.role_ids.length}</td></tr>`).join("")}</tbody></table>${v.source_inventory.groups.length?"":'<div class="empty">No source gaps in this export. Origin authentication remains a host responsibility.</div>'}</div></section>`+
    `<div class="cols">${panel("Application material graph","Review state for evidence-dependent artifacts",artifacts.map(a=>`<div class="line-item"><span>${esc(a.artifact_id)} <small>· ${esc(a.revision)}</small></span>${badge(words(a.status),a.status==="VALID_FOR_REVIEW"?"teal":"amber")}</div>`).join("") || '<p class="small muted">Import a trust export to inspect material dependencies.</p>','<button class="button quiet" data-start="material-review">Inspect →</button>')}${panel("What these checks mean","Keep the evidence boundary visible",'<p class="small muted">A structurally complete record has the fields, revision and validity needed for review. The canonical host must authenticate its origin.</p><div class="callout">Imports and workflow reports do not create approvals, applicant answers, or source observations.</div>')}</div>`;
}
function workflowOptions() {
  const item=state.catalog.find(r=>r.id===state.workflow);
  if(!item) return "";
  if(state.workflow==="daily-brief") return '<label class="field"><span>Review time budget (minutes)</span><input id="budget" type="number" min="0" max="240" step="1" value="20" required></label>';
  if(item.scope==="all_or_selected") return `<label class="field"><span>Workflow scope</span><select id="scope"><option value="">All opportunities</option>${state.view.roles.map(r=>`<option value="${esc(r.role_id)}" ${state.scope===r.role_id?"selected":""}>${esc(r.company+" · "+r.title)}</option>`).join("")}</select></label>`;
  return '<div class="callout">Runs three bundled synthetic regression cases. Your live pipeline is not exercised.</div>';
}
function workflows() {
  const catalog=state.catalog.filter(w=>w.id!=="twin-scenario");
  const selected=catalog.find(w=>w.id===state.workflow) || catalog[0]; state.workflow=selected.id;
  return pageHead("SPECIALIZED WORKFLOWS", "Turn a blocker into a review plan.", "Focused workflows share Keel’s existing checks and produce portable results.")+
    `<div class="workflow-grid">${catalog.map(w=>`<button class="workflow-card ${state.workflow===w.id?"active":""}" data-workflow="${esc(w.id)}" aria-pressed="${state.workflow===w.id}"><span class="category">${esc(w.category)}</span><h2>${esc(w.name)}</h2><p>${esc(w.description)}</p></button>`).join("")}</div>`+
    panel(selected.name,"Choose a scope, run the checks, then download the result.",`<form id="workflow-form"><div class="form-grid">${workflowOptions()}</div><button class="button primary" ${state.busy?"disabled":""}>${state.busy?"Running checks…":"Run workflow →"}</button></form>`)+
    `<div id="workflow-result">${state.report && state.report.workflow_id===state.workflow ? resultHTML(state.report):""}</div>`;
}
function twin() {
  return pageHead("DIGITAL TWIN LAB", "Rehearse before you change the pipeline.", "A snapshot-based what-if model. Every scenario leaves canonical state untouched.")+
    `<div class="cols"><section class="panel"><div class="panel-head"><div><h2>Design a scenario</h2><p class="subtitle">Change one assumption, or combine a few.</p></div>${badge("Simulation only","teal")}</div><div class="panel-body"><form id="twin-form"><div class="form-grid"><label class="field"><span>Refill delay (minutes)</span><input id="delay" type="number" min="0" max="1440" step="1" value="10" required><small>Applied to each forecast release</small></label><label class="field"><span>Consumption multiplier</span><input id="capacity" type="number" min="0.25" max="4" step="0.25" value="1.5" required><small>1× is the measured baseline</small></label><label class="field"><span>Add a source rate hold</span><select id="rate-source"><option value="">No additional hold</option>${state.view.scenario_sources.map(s=>`<option>${esc(s)}</option>`).join("")}</select></label><label class="field"><span>Invalidate an answer binding</span><select id="invalidate"><option value="">Keep current packets</option>${state.view.roles.map(r=>`<option value="${esc(r.role_id)}">${esc(r.title)}</option>`).join("")}</select></label></div><button class="button primary" ${state.busy?"disabled":""}>${state.busy?"Comparing…":"Compare with baseline →"}</button></form></div></section>${panel("What the twin can tell you","A bounded model of pipeline behavior",'<div class="line-item"><span>Supply timing</span><strong>Runway &amp; refill</strong></div><div class="line-item"><span>Changed materials</span><strong>Packet invalidation</strong></div><div class="line-item"><span>Source interruption</span><strong>Rate hold effect</strong></div><div class="callout amber mt">Predictions are uncalibrated. This model does not predict hiring outcomes or clear existing consent and execution gates.</div>')}</div><div id="twin-result">${state.report?.workflow_id==="twin-scenario"?resultHTML(state.report):""}</div>`;
}
function integrations() {
  return pageHead("OPEN INTERFACES", "Connect Keel to your own tools.", "Use a local API, a small Python client, or a JSON-line agent bridge.",'<button class="button" data-export>Export snapshot ↓</button>')+
    `<div class="cols"><section class="panel"><div class="panel-head"><div><h2>Import a workspace snapshot</h2><p class="subtitle">Replace this session’s copy with a canonical adapter export.</p></div></div><div class="panel-body"><form id="import-form"><label class="field mb"><span>Choose a snapshot JSON file</span><input id="import-file" type="file" accept=".json,application/json"></label><label class="field"><span>Or paste snapshot JSON</span><textarea id="import-json" spellcheck="false" placeholder='{"schema": "keel.workbench.snapshot.v1", …}' required></textarea></label><p class="small muted mt">Workspace: <strong>${esc(state.view.workspace_id)}</strong> · ${state.view.synthetic?"synthetic":"operational"}. Imports must match this workspace and mode. Session history clears when the server stops.</p><button class="button primary" ${state.busy?"disabled":""}>Validate and import →</button><p id="import-error" class="error-text" role="status"></p></form></div></section>${panel("Integration surfaces","All routes use the same local checks",'<div class="task-row"><div class="task-copy"><strong>HTTP API + OpenAPI 3.1</strong><p>Authenticated loopback requests; versioned endpoints.</p></div><button class="button quiet" data-openapi>Schema ↓</button></div><div class="task-row"><div class="task-copy"><strong>Python SDK</strong><p>keel_workbench.client.Client · standard library only.</p></div></div><div class="task-row"><div class="task-copy"><strong>JSON-line agent bridge</strong><p>One request per line on stdin, one result on stdout.</p></div></div><div class="task-row"><div class="task-copy"><strong>Existing Keel 0.7 state</strong><p>snapshot_from_agent reads an already-open LocalState through its verified snapshot API.</p></div></div>')}</div>`+
    `<div class="cols">${panel("A simple agent request","Send through the HTTP API or the JSON-line bridge",`<pre class="code">${esc(JSON.stringify({id:"inspect-1",method:"GET",path:"/api/v1/overview",body:null},null,2))}</pre><p class="small muted">Start the bridge: <span class="inline-code">python3 -m keel_workbench bridge --demo</span></p><p class="small muted">See WORKBENCH.md for operational imports, SDK examples, endpoint contracts and the host integration boundary.</p>`)}${panel("This session","Last 100 successful workflow requests",state.history.length ? `<ul class="session-list">${state.history.slice(0,8).map(r=>`<li><div>${esc(words(r.workflow_id))}<small>${esc(r.request_id)}</small></div><span class="muted">${esc(stamp(r.evaluated_at))}</span></li>`).join("")}</ul>`:'<p class="small muted">No workflow runs yet. Run a review workflow or twin scenario to begin.</p>')}</div>`;
}
function resultHTML(report) {
  const r=report.result; let body="";
  if(report.workflow_id==="source-repair") body=`<div class="callout mb">${r.system_tasks} system tasks · ${r.human_tasks} human tasks. ${r.tasks.length?"The plan identifies missing records; the canonical adapter must supply them.":"No source gaps in the selected scope."}</div>`+r.tasks.map((t,i)=>`<div class="result-task"><span class="step-number">${i+1}</span><div><h3>${esc(t.component)} · ${t.affected_roles} roles</h3><p>${esc(t.next_step)}</p><p>Owner: ${esc(t.owner)} · ${esc(words(t.reason))}</p></div></div>`).join("");
  if(report.workflow_id==="daily-brief") body=`<div class="callout mb">${r.decisions.minutes || 0} minutes planned · ${r.decisions.selected?.length || 0} decision groups · 0 answers inferred.</div>`+(r.decisions.selected || []).map((q,i)=>`<div class="result-task"><span class="step-number">${i+1}</span><div><h3>${esc(q.question)}</h3><p>${q.estimated_minutes} min · ${q.roles_progressed.length} roles · ${esc(words(q.classification))}</p></div></div>`).join("");
  if(report.workflow_id==="material-review") body=`<div class="callout mb">${r.roles.length} selected roles · ${r.artifacts.length} workspace artifacts. Review the recorded dependencies before reusing materials.</div>`+r.artifacts.map(a=>`<div class="line-item"><span>${esc(a.artifact_id)}</span>${badge(words(a.status),a.status==="VALID_FOR_REVIEW"?"teal":"amber")}</div>`).join("");
  if(report.workflow_id==="incident-replay") body=`<div class="callout mb">${r.cases_run} synthetic cases checked. This is a regression report; live provider behavior is untested.</div>`+r.results.map(c=>`<div class="line-item"><span>${esc(c.case_id)}</span>${badge(c.status,c.status==="PASS"?"teal":"red")}</div>`).join("");
  if(report.workflow_id==="twin-scenario") {
    const box=(m,label,after)=>`<div class="compare-box ${after?"after":""}"><div class="compare-label">${label}</div><div class="line-item"><span>Base-ready roles</span><strong>${m.executable_ready}</strong></div><div class="line-item"><span>Runway</span><strong>${mins(m.runway_seconds)}</strong></div><div class="line-item"><span>First refill</span><strong>${mins(m.first_refill_seconds)}</strong></div><div class="line-item"><span>Discovery budget</span><strong>${m.discovery_minutes} min</strong></div></div>`;
    body=`<div class="compare">${box(r.baseline,"Recorded baseline",false)}${box(r.scenario,"Your scenario",true)}</div><div class="callout">${esc(r.qualification)}</div>`;
  }
  return `<section class="result-block" aria-label="Workflow result"><div class="result-head"><div><p class="eyebrow">REVIEW RESULT</p><h2>${esc(words(r.status || r.mode))}</h2></div><button class="button" data-download-result>Download result ↓</button></div>${!report.current_export?'<div class="callout amber mb">Source export is unverified. Refresh it before relying on these checks.</div>':""}${body}<details><summary>Inspect complete JSON result</summary><pre>${esc(JSON.stringify(report,null,2))}</pre></details><p class="small muted mt">${report.synthetic?"Synthetic fixture":"Imported export"} · ${esc(stamp(report.evaluated_at))} · no execution authorized</p></section>`;
}
function showRole(id) {
  const r=state.view.roles.find(v=>v.role_id===id); if(!r) return;
  $("#detail-content").innerHTML=`<p class="eyebrow">${esc(r.lane)} · FIT ${r.fit_score}</p><h2 id="detail-title">${esc(r.title)}</h2><p class="subtitle">${esc(r.company)} · ${esc(r.role_id)}</p><ul class="reason-list">${r.reasons.map(reason=>`<li>${esc(words(reason))}</li>`).join("")}</ul><h3>Source records</h3>${components.map(c=>`<div class="detail-source"><strong>${c}</strong>${badge(words(r.sources[c].status),r.sources[c].status==="READY"?"teal":"amber")}<span class="muted">${esc(words(r.sources[c].reason || "structural checks passed"))}</span></div>`).join("")}<div class="actions-row mt"><button class="button primary" data-role-workflow="source-repair" data-id="${esc(r.role_id)}">Build source repair plan</button><button class="button" data-role-workflow="material-review" data-id="${esc(r.role_id)}">Review materials</button></div><p class="small muted mt">Review checks do not grant execution authority.</p>`;
  $("#detail").showModal();
}
async function run(workflow, options, roleIds=[]) {
  if(state.busy) return;
  state.busy=true;
  document.querySelectorAll("#workflow-form button, #twin-form button").forEach(b=>{b.disabled=true;b.textContent="Running checks…";});
  try {
    const report=await api("/api/v1/run",{request_id:crypto.randomUUID(),workflow_id:workflow,snapshot_sha256:state.view.snapshot_sha256,role_ids:roleIds,options});
    state.report=report; state.history=(await api("/api/v1/history")).runs;
    const target=$(workflow==="twin-scenario"?"#twin-result":"#workflow-result");
    if(target) target.innerHTML=resultHTML(report);
    toast("Review complete. Your result is ready to download.");
  } catch(error) { toast(error.message,true); }
  finally {state.busy=false;document.querySelectorAll("#workflow-form button, #twin-form button").forEach(b=>{b.disabled=false;b.textContent=b.closest("#twin-form")?"Compare with baseline →":"Run workflow →";});}
}
function render() {
  if(!state.view) return showLogin();
  const views={overview,pipeline,evidence,workflows,twin,integrations};
  $("#main").innerHTML=views[state.page]();
  if(state.page==="pipeline") {
    updatePipeline();
    $("#search").oninput=e=>{state.query=e.target.value;updatePipeline();};
    $("#lane").onchange=e=>{state.lane=e.target.value;updatePipeline();};
    $("#review-filter").onchange=e=>{state.filter=e.target.value;updatePipeline();};
  }
  if(state.page==="workflows") $("#workflow-form").onsubmit=event=>{
    event.preventDefault(); state.scope=$("#scope")?.value || "";
    const options=state.workflow==="daily-brief"?{budget_minutes:Number($("#budget").value)}:{};
    run(state.workflow,options,state.scope?[state.scope]:[]);
  };
  if(state.page==="twin") $("#twin-form").onsubmit=event=>{
    event.preventDefault();run("twin-scenario",{delay_minutes:Number($("#delay").value),capacity_multiplier:Number($("#capacity").value),rate_hold_source:$("#rate-source").value || null,invalidate_role:$("#invalidate").value || null});
  };
  if(state.page==="integrations") {
    $("#import-file").onchange=async event=>{
      const file=event.target.files[0]; if(!file) return;
      if(file.size>8*1024*1024) return toast("Snapshot exceeds the 8 MiB limit.",true);
      const target=$("#import-json");try {target.value=await file.text();}catch(error){toast(error.message,true);}
    };
    $("#import-form").onsubmit=async event=>{
      event.preventDefault();if(state.busy) return;
      const input=$("#import-json").value; const errorNode=$("#import-error");errorNode.textContent="";
      const button=event.target.querySelector("button");button.disabled=true;state.busy=true;
      try {
        if(new TextEncoder().encode(input).length>8*1024*1024) throw new Error("Snapshot exceeds the 8 MiB limit.");
        // Keep the original JSON so the server can reject duplicate keys.
        await api("/api/v1/snapshot",`{"previous_sha256":${JSON.stringify(state.view.snapshot_sha256)},"snapshot":${input}}`);
        state.report=null;state.busy=false;await load();toast("Snapshot validated and imported for this session.");
      } catch(error) {errorNode.textContent=error.message;}
      finally {state.busy=false;button.disabled=false;}
    };
  }
}
document.addEventListener("click", async event=>{
  const button=event.target.closest("button,a"); if(!button) return;
  if(state.busy && (button.dataset.page || button.dataset.nav || button.dataset.workflow || button.dataset.start || button.dataset.roleWorkflow || button.hasAttribute("data-brief") || button.classList.contains("brand"))) {
    event.preventDefault();return toast("The local check is running. Navigation will be available when it finishes.");
  }
  if(button.dataset.page) go(button.dataset.page);
  else if(button.dataset.nav) go(button.dataset.nav);
  else if(button.classList.contains("brand")) {event.preventDefault();go("overview");}
  else if(button.dataset.role) showRole(button.dataset.role);
  else if(button.dataset.workflow) {state.workflow=button.dataset.workflow;state.scope="";render();}
  else if(button.dataset.start || button.hasAttribute("data-brief")) {state.workflow=button.dataset.start || "daily-brief";state.scope="";go("workflows");}
  else if(button.dataset.roleWorkflow) {state.workflow=button.dataset.roleWorkflow;state.scope=button.dataset.id;$("#detail").close();go("workflows");}
  else if(button.hasAttribute("data-download-result") && state.report) download(`keel-${state.report.workflow_id}-${state.report.request_id}.json`,state.report);
  else if(button.hasAttribute("data-export") || button.hasAttribute("data-openapi")) {
    try {const spec=button.hasAttribute("data-openapi");download(spec?"keel-workbench-openapi.json":"keel-workbench-snapshot.json",await api(spec?"/openapi.json":"/api/v1/snapshot"));} catch(error){toast(error.message,true);}
  }
});
$("#close-detail").onclick=()=>$("#detail").close();
$("#refresh").onclick=async()=>{if(state.busy)return;try{await load();toast("Workspace refreshed.");}catch(error){toast(error.message,true);}};
setInterval(async()=>{
  if(!state.token || !state.view || state.busy || document.hidden) return;
  try {
    state.view=await api("/api/v1/overview"); freshness();
    if(["overview","evidence"].includes(state.page) && !$("#detail").open) render();
    if(state.page==="pipeline") updatePipeline();
  } catch(error) {toast(error.message,true);}
},30000);
load().catch(error=>showLogin(error.message));
