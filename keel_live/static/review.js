"use strict";
const $ = selector => document.querySelector(selector);
const reviewState = {token: new URLSearchParams(location.hash.slice(1)).get("token") || "",
  overview: null, detail: null, index: 0, busy: false};
if (location.hash.includes("token=")) history.replaceState(null, "", location.pathname);
const components = ["policy", "form", "answers", "attachments", "target", "route"];
function message(text, error = false) {
  $("#message").textContent = text;
  $("#message").classList.toggle("error", error);
}
function jsonNode(value) {
  const node = document.createElement("pre");
  node.textContent = JSON.stringify(value, null, 2);
  return node;
}
function detailsNode(title, value, open = false) {
  const node = document.createElement("details");
  const summary = document.createElement("summary");
  summary.textContent = title;
  node.append(summary, jsonNode(value));
  node.open = open;
  return node;
}
function showMaterials(target, sources) {
  target.replaceChildren(...components.map(name => detailsNode(name, sources?.[name] ?? {status: "MISSING"}, true)));
}
async function api(path, data) {
  const response = await fetch(path, {method: data === undefined ? "GET" : "POST", cache: "no-store", credentials: "omit",
    headers: {Authorization: "Bearer " + reviewState.token, "Content-Type": "application/json"},
    ...(data === undefined ? {} : {body: JSON.stringify(data)})});
  const result = await response.json();
  if (!response.ok) {
    if (response.status === 401) {
      reviewState.token = ""; reviewState.overview = null; reviewState.detail = null;
      $("#workspace").hidden = true; $("#login").hidden = false;
    }
    throw new Error(result.error?.message || "Local request failed.");
  }
  return result;
}
function context() {
  if (!reviewState.overview?.scopes[reviewState.index]) throw new Error("Select an opportunity first.");
  return {scope: reviewState.overview.scopes[reviewState.index], flow_sha256: reviewState.overview.flow_sha256};
}
function controls() {
  const request = reviewState.detail?.current_request;
  const current = reviewState.overview?.current === true;
  const allowed = new Set(reviewState.overview?.allowed_actions || []);
  const prereqs = reviewState.detail?.semantic_validation?.ready_for_approval === true;
  $("#request").disabled = reviewState.busy || !current || !prereqs || !allowed.has("review:request") || !$("#request-confirm").checked;
  const canDecide = !reviewState.busy && current && request?.request_actionable === true && allowed.has("review:decide") && $("#decision-confirm").checked;
  $("#approve").disabled = !canDecide;
  $("#reject").disabled = !canDecide;
  $("#revoke").disabled = reviewState.busy || !current || !request || request.revocation != null || !allowed.has("review:revoke") || !$("#reason").value.trim() || !$("#revoke-confirm").checked;
  $("#refresh").disabled = reviewState.busy;
  $("#scope").disabled = reviewState.busy;
  $("#proof").disabled = reviewState.busy;
}
function resetConfirmations() {
  for (const selector of ["#request-confirm", "#decision-confirm", "#revoke-confirm"]) $(selector).checked = false;
}
function decisionExpiry(request) {
  const expiry = Math.min(Date.parse(request.expires_at), ...components.map(name => Date.parse(reviewState.detail.sources[name].expires_at)));
  if (!Number.isFinite(expiry) || expiry <= Date.parse(reviewState.detail.validated_at)) throw new Error("The review evidence has expired. Refresh it before deciding.");
  return new Date(expiry).toISOString();
}
function renderDetail() {
  const detail = reviewState.detail;
  const request = detail?.current_request;
  $("#decision-card").hidden = !request;
  const checks = detail?.revision_diagnostics?.components || {};
  $("#diagnostics").replaceChildren(...[...components, "approval"].map(name => {
    const row = checks[name] || {status: "MISSING", reason: "source_not_exported"};
    const box = document.createElement("div"); box.className = "diagnostic";
    const title = document.createElement("strong"); title.textContent = name;
    const state = document.createElement("span"); state.textContent = row.status + (row.reason ? " · " + row.reason : "");
    state.className = row.status === "READY" ? "ready" : "blocked";
    box.append(title, state);
    if (row.revision) {const revision = document.createElement("span"); revision.className = "mono"; revision.textContent = row.revision; box.append(revision);}
    return box;
  }));
  $("#readiness").textContent = detail?.semantic_validation?.ready_for_approval ? "SIX SOURCES REVIEWABLE" : "SOURCE REPAIR REQUIRED";
  const issues = detail?.semantic_validation?.issues || [];
  $("#semantic").textContent = issues.length ? issues.map(value => value.component + ": " + value.code).join(" · ") :
    detail?.semantic_validation?.ready_for_approval === true ? "Source semantics checked. Factual support and original source authenticity remain host responsibilities." : "No current source validation available.";
  showMaterials($("#current-materials"), detail?.sources);
  if (request) {
    $("#request-state").textContent = request.state;
    $("#request-meta").textContent = request.request_id + " · expires " + request.expires_at;
    $("#review-digest").value = request.review_sha256;
    try {$("#decision-expiry").textContent = "Decision valid until at most " + decisionExpiry(request) + ", bounded by source evidence expiry.";}
    catch {$("#decision-expiry").textContent = "The decision interval has expired.";}
    $("#request-warning").textContent = request.reason || (request.state === "PENDING" ? "Review all six records below. Your decision is bound to this exact packet and hash." : "Recorded state: " + request.recorded_state + ". Current approval valid: " + String(request.approval_currently_valid) + ".");
    showMaterials($("#materials"), request.review_payload.sources);
    $("#materials").append(detailsNode("Verified attachment bytes and hashes", request.review_payload.verified_attachments, true));
    $("#packet-json").textContent = JSON.stringify(request, null, 2);
  }
  resetConfirmations(); controls();
}
async function loadDetail() {
  $("#proof-json").hidden = true; $("#proof-json").textContent = "";
  reviewState.detail = null;
  controls();
  if (!reviewState.overview.scopes.length) {
    $("#identity").textContent = "The trusted host export contains no opportunities.";
    renderDetail(); return;
  }
  const scope = context().scope;
  $("#identity").textContent = scope.role_id + " · " + scope.application_id + " · " + scope.action;
  reviewState.detail = await api("/api/live/v1/review/show", context());
  renderDetail();
}
async function load() {
  $("#proof-json").hidden = true; $("#proof-json").textContent = "";
  if (!reviewState.token) {$("#login").hidden = false; $("#workspace").hidden = true; return;}
  const previous = reviewState.overview?.scopes[reviewState.index];
  reviewState.overview = await api("/api/live/v1/review");
  const view = reviewState.overview;
  const index = previous ? view.scopes.findIndex(scope => JSON.stringify(scope) === JSON.stringify(previous)) : 0;
  reviewState.index = Math.max(index, 0);
  $("#scope").replaceChildren(...view.scopes.map((scope, position) => {
    const option = document.createElement("option"); option.value = String(position);
    const role = view.roles.find(role => role.role_id === scope.role_id);
    option.textContent = role ? role.company + " · " + role.title : scope.role_id;
    return option;
  }));
  $("#scope").value = String(reviewState.index);
  $("#mode").textContent = (view.synthetic ? "Synthetic rehearsal. " : "Host-supplied operational records. ") +
    (view.current ? "Canonical export current. " : "Canonical export stale or incomplete; review decisions are blocked. ") + "Observed: " + view.observed_at;
  $("#actor").textContent = view.operator.actor_id;
  $("#login").hidden = true; $("#workspace").hidden = false;
  await loadDetail();
}
async function guarded(operation) {
  if (reviewState.busy) return;
  reviewState.busy = true; controls(); message("");
  try {await operation();}
  catch (error) {
    // No automatic retry of a decision. A transport failure may occur after
    // commit; refresh reveals the immutable decision and current packet state.
    reviewState.detail = null;
    if (reviewState.overview) reviewState.overview.current = false;
    $("#mode").textContent = "UNVERIFIED. The last check failed. Refresh the trusted host evidence before relying on this view.";
    $("#proof-json").hidden = true; $("#proof-json").textContent = "";
    renderDetail(); resetConfirmations();
    message(error.message + " Refresh evidence before another decision.", true);
  } finally {reviewState.busy = false; controls();}
}
function requestExpiry() {
  const minutes = Number($("#minutes").value);
  if (!Number.isInteger(minutes) || minutes < 1 || minutes > 1440) throw new Error("Choose 1–1440 whole minutes.");
  return new Date(Date.parse(reviewState.overview.evaluated_at) + minutes * 60000).toISOString();
}
$("#login-form").addEventListener("submit", event => {event.preventDefault(); reviewState.token = $("#token").value.trim(); $("#token").value = ""; guarded(load);});
$("#refresh").addEventListener("click", () => guarded(load));
$("#scope").addEventListener("change", () => {reviewState.index = Number($("#scope").value); guarded(loadDetail);});
for (const id of ["request-confirm", "decision-confirm", "revoke-confirm", "reason"]) $("#" + id).addEventListener("input", controls);
$("#request").addEventListener("click", () => guarded(async () => {
  await api("/api/live/v1/review/request", {...context(), confirmed: $("#request-confirm").checked, expires_at: requestExpiry()});
  await load(); message("Review request recorded. Inspect the packet before deciding.");
}));
for (const [id, decision] of [["approve", "APPROVE"], ["reject", "REJECT"]]) $("#" + id).addEventListener("click", () => guarded(async () => {
  const request = reviewState.detail.current_request;
  const result = await api("/api/live/v1/review/decide", {...context(), confirmed: $("#decision-confirm").checked,
    request_id: request.request_id, decision, reviewed_sha256: request.review_sha256, expires_at: decisionExpiry(request)});
  await load(); message("Decision recorded: " + result.state + ". No application action was authorized.");
}));
$("#revoke").addEventListener("click", () => guarded(async () => {
  await api("/api/live/v1/review/revoke", {...context(), request_id: reviewState.detail.current_request.request_id,
    reason: $("#reason").value.trim(), confirmed: $("#revoke-confirm").checked});
  $("#reason").value = ""; await load(); message("Approval revoked.");
}));
$("#proof").addEventListener("click", () => guarded(async () => {
  const report = await api("/api/live/v1/proof");
  $("#proof-json").hidden = false; $("#proof-json").textContent = JSON.stringify(report, null, 2);
  message("Local capture-to-review checks complete: " + report.state + ".");
}));
$("#workbench").addEventListener("click", event => {
  if (reviewState.busy) {event.preventDefault(); return;}
  event.preventDefault(); location.assign("/" + (reviewState.token ? "#token=" + encodeURIComponent(reviewState.token) : ""));
});
controls(); guarded(load);
