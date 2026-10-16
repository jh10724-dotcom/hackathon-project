"use strict";

// UI state is derived from server receipts. There are no canned agent results.
const STAGES = ["watcher", "matcher", "closer", "scribe"];
const ROLES = {
  watcher: { name: "Watcher", title: "What changed?", description: "Finds material changes, dates and missing facts in the original sources.", waiting: "Ready for the first handoff", detail: "Watcher will read the selected case and pass its cited findings to Matcher." },
  matcher: { name: "Matcher", title: "What does the evidence say?", description: "Compares Watcher’s findings with the applicable policy and endorsement.", waiting: "Waiting for Watcher’s findings", detail: "Matcher will compare each change with the original policy evidence, keeping dates and uncertainty intact." },
  closer: { name: "Closer", title: "What should we ask next?", description: "Turns unresolved questions into follow-up drafts for your review.", waiting: "Waiting for the comparison", detail: "Closer will use Watcher and Matcher’s handoffs to draft the right questions. Nothing is sent." },
  scribe: { name: "Scribe", title: "What belongs in the record?", description: "Prepares cited record proposals while preserving the case’s history.", waiting: "Waiting for the team’s handoffs", detail: "Scribe will bring the three earlier handoffs together into a review packet. Record changes remain proposals." }
};
const $ = (id) => document.getElementById(id);
const state = { case: null, preflight: null, status: null, run: null, selectedStage: "watcher", autoFollow: true, starting: false, approving: false, statusBusy: false, pollTimer: null, statusTimer: null, error: "", stageRenderKey: "", reviewRenderKey: "", dialogTrigger: null };

function element(tag, className, value) {
  const item = document.createElement(tag);
  if (className) item.className = className;
  if (value !== undefined && value !== null) item.textContent = String(value);
  return item;
}
function list(value) { return Array.isArray(value) ? value : []; }
function human(value) { return String(value || "").replace(/_/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase()); }
function dateLabel(value, includeTime = false) {
  if (!value || value === "UNKNOWN") return "Date not supplied";
  const dateOnly = /^\d{4}-\d{2}-\d{2}$/.test(value);
  const parsed = new Date(dateOnly ? value + "T12:00:00Z" : value);
  if (Number.isNaN(parsed.getTime())) return String(value);
  const options = { month: "short", day: "numeric", year: "numeric", timeZone: dateOnly ? "UTC" : (state.case?.review_timezone || "America/New_York") };
  if (includeTime && !dateOnly) Object.assign(options, { hour: "numeric", minute: "2-digit" });
  return new Intl.DateTimeFormat("en-US", options).format(parsed);
}
function announce(message) { if ($("announcer").textContent !== message) $("announcer").textContent = message; }
function setError(message) {
  state.error = message || "";
  $("error-banner").hidden = !state.error;
  $("error-message").textContent = state.error;
}
async function request(path, body) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 15000);
  try {
    const response = await fetch(path, { method: body === undefined ? "GET" : "POST", cache: "no-store", credentials: "same-origin", signal: controller.signal, headers: body === undefined ? { Accept: "application/json" } : { Accept: "application/json", "Content-Type": "application/json", "X-Broker-Request": "1" }, ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
    let data;
    try { data = await response.json(); } catch { throw new Error("The workspace returned an unreadable response. Please try again."); }
    if (!response.ok) {
      const detail = typeof data.error === "string" ? data.error : (data.error?.message || data.message);
      throw new Error(detail || `The request could not finish (${response.status}). Please try again.`);
    }
    return data;
  } catch (error) {
    if (error.name === "AbortError") throw new Error("The workspace took too long to respond. Your last received result is still here.");
    if (error instanceof TypeError) throw new Error("Could not reach the workspace. Check the connection and try again.");
    throw error;
  } finally { clearTimeout(timeout); }
}

function entries() {
  const existing = list(state.run?.stages);
  if (existing.length) return existing.map((entry) => entry.packet ? entry : { stage: entry.stage, status: "completed", packet: entry });
  return list(state.run?.result?.stages).map((packet) => ({ stage: packet.stage, status: "completed", packet }));
}
function stageEntry(stage) { return entries().find((entry) => entry.stage === stage); }
function packetFor(stage) { return stageEntry(stage)?.packet || null; }
function running() { return state.starting || state.run?.status === "running" || Boolean(state.status?.active_run_id); }
function isBlocked() { return state.run?.result?.status === "BLOCKED" || entries().some((entry) => entry.packet?.status === "BLOCKED"); }
function readyForApproval() { return state.run?.status === "completed" && !isBlocked() && Boolean(packetFor("scribe")) && entries().length === 4; }

function updateControls() {
  const ready = state.status?.model_ready === true;
  $("run-button").disabled = !state.case || !ready || running();
  $("run-button").replaceChildren(document.createTextNode(state.starting ? "Starting the team…" : running() ? "Agents are working…" : state.run ? "Run four agents again" : "Run four agents"));
  if (!running()) $("run-button").append(element("span", "", "↗"));
  $("run-button").lastElementChild?.setAttribute("aria-hidden", "true");
  $("run-button").setAttribute("aria-busy", String(running()));
  let hint = "Checking agent availability…";
  if (state.status) hint = ready ? (running() ? "Live handoffs appear as each agent finishes." : "Runs Watcher, Matcher, Closer, then Scribe.") : "Model unavailable. Finish sign-in, then refresh.";
  $("run-hint").textContent = hint;
  const runtime = typeof state.status?.runtime === "string" ? state.status.runtime : (state.status?.runtime?.environment || state.status?.runtime?.name);
  $("connection-label").textContent = state.status ? `${runtime === "daytona" ? "Daytona workspace" : runtime === "local" ? "Local workspace" : "Workspace"} · ${ready ? "model ready" : "model unavailable"}` : "Connecting to workspace";
  $("connection-dot").className = "status-dot " + (state.status ? ready ? "ready" : "unavailable" : "");
  const canReview = readyForApproval() && !state.run?.approval && !state.approving;
  $("approve-button").disabled = !canReview;
  $("reject-button").disabled = !canReview;
  $("approval-note").disabled = !canReview;
  $("approve-button").textContent = state.approving ? "Saving your decision…" : "Approve review packet";
  $("approval-form").setAttribute("aria-busy", String(state.approving));
}

function renderStageTabs() {
  const completed = entries().filter((entry) => entry.packet && entry.packet.status !== "BLOCKED").length;
  $("workflow-progress").textContent = `${completed} of 4 handoffs${state.run?.status === "running" ? " · In progress" : ""}`;
  for (const stage of STAGES) {
    const tab = $("tab-" + stage);
    const entry = stageEntry(stage);
    const failed = entry?.packet?.status === "BLOCKED" || entry?.status === "failed" || (state.run?.status === "failed" && state.run?.current_stage === stage);
    const active = !entry?.packet && state.run?.status === "running" && state.run?.current_stage === stage;
    tab.setAttribute("aria-selected", String(state.selectedStage === stage));
    tab.tabIndex = state.selectedStage === stage ? 0 : -1;
    tab.classList.toggle("is-running", active);
    tab.classList.toggle("is-complete", Boolean(entry?.packet) && !failed);
    tab.classList.toggle("is-failed", failed);
    tab.querySelector(".stage-state").textContent = failed ? "Needs attention" : entry?.packet ? "Handoff received" : active ? "Working" : stage === "watcher" && !state.run ? "Ready to start" : "Waiting";
  }
}
function selectStage(stage, userSelected = false, focus = false) {
  if (!STAGES.includes(stage)) return;
  state.selectedStage = stage;
  if (userSelected) state.autoFollow = false;
  renderStageTabs(); renderStage();
  if (focus) $("tab-" + stage).focus();
}

function sourceTitle(source) {
  const names = { "AP-PROFILE-001": "Client profile", "AP-POL-001": "Base policy · R1", "AP-MSG-001": "Four new drivers", "AP-MSG-002": "Newsletter update", "AP-MSG-003": "Start-date clarification", "COR-AP-001": "Reviewed correction", "AP-END-001": "Issued endorsement · R2" };
  return source.title || names[source.source_id] || human(source.source_type) || source.source_id;
}
function renderCase() {
  if (!state.case) return;
  $("case-title").textContent = state.case.client_name;
  $("case-subtitle").textContent = `Commercial auto · ${state.case.policy_id} · Review date ${dateLabel(state.case.review_at)}`;
  $("sidebar-client").textContent = state.case.client_name;
  $("sidebar-case").textContent = state.case.client_id + " · Fictional sample";
  const initials = state.case.client_name.split(/\s+/).filter(Boolean).slice(0, 2).map((word) => word[0]).join("");
  document.querySelector(".case-monogram").textContent = initials;
  renderSources(); renderPreflight();
}
function renderSources() {
  const container = $("source-list");
  container.replaceChildren();
  const query = $("source-search").value.trim().toLowerCase();
  const all = list(state.case?.sources);
  const filtered = all.filter((source) => [sourceTitle(source), source.source_id, source.source_type, ...Object.values(source.sections || {})].join(" ").toLowerCase().includes(query));
  $("source-count").textContent = query ? `${filtered.length} of ${all.length}` : String(all.length);
  if (!filtered.length) { container.append(element("p", "loading-copy muted", query ? "No matching sources. Try another word." : "No source documents were supplied.")); return; }
  for (const source of filtered) {
    const button = element("button", "source-item"); button.type = "button";
    button.append(element("strong", "", sourceTitle(source)), element("span", "", dateLabel(source.recorded_at)), element("span", "source-type", human(source.source_type).replace(/ Fixture$/, "")));
    button.setAttribute("aria-label", `Open ${sourceTitle(source)}, ${source.source_id}`);
    button.addEventListener("click", () => openSource(source.source_id, [], button));
    container.append(button);
  }
}

function normalizedRef(ref) { return { source_id: ref.source_id, locator: ref.section_or_locator || ref.locator, quote: ref.excerpt || ref.quote }; }
function sourceButton(sourceId, refs = []) {
  const button = element("button", "evidence-button", sourceId); button.type = "button";
  button.setAttribute("aria-label", `Read original evidence from ${sourceId}`);
  button.addEventListener("click", () => openSource(sourceId, refs.map(normalizedRef), button));
  return button;
}
function addSources(container, sourceIds, packet) {
  const ids = [...new Set(list(sourceIds))];
  if (!ids.length) return;
  const group = element("div", "evidence-buttons");
  for (const id of ids) group.append(sourceButton(id, list(packet?.source_refs).filter((ref) => ref.source_id === id)));
  container.append(group);
}
function openSource(sourceId, refs = [], trigger = document.activeElement) {
  const source = list(state.case?.sources).find((item) => item.source_id === sourceId);
  if (!source) { setError("That source is not in the selected case. Refresh the workspace to check its evidence."); return; }
  state.dialogTrigger = trigger;
  $("source-dialog-title").textContent = sourceTitle(source);
  $("source-dialog-meta").replaceChildren(element("span", "", source.source_id), element("span", "", "Recorded " + dateLabel(source.recorded_at, true)), element("span", "", human(source.source_type)));
  if (source.effective_from) $("source-dialog-meta").append(element("span", "", "Effective " + dateLabel(source.effective_from, true)));
  const body = $("source-dialog-content"); body.replaceChildren();
  const uniqueRefs = [...new Map(refs.filter((ref) => ref.quote).map((ref) => [ref.locator + "|" + ref.quote, ref])).values()];
  for (const ref of uniqueRefs) {
    const cited = element("section", "source-quote");
    cited.append(element("p", "eyebrow", "Cited passage · " + ref.locator), element("p", "", ref.quote));
    body.append(cited);
  }
  for (const [locator, text] of Object.entries(source.sections || {})) {
    const section = element("section", "source-section" + (refs.some((ref) => ref.locator === locator) ? " highlighted" : ""));
    section.append(element("h3", "", locator), element("p", "", text)); body.append(section);
  }
  const dialog = $("source-dialog"); if (!dialog.open) dialog.showModal();
  dialog.scrollTop = 0; $("close-source").focus();
}

function renderPreflight() {
  const data = state.preflight;
  $("preflight-state").textContent = data ? "Checked" : "Unavailable";
  const root = $("preflight-content"); root.replaceChildren();
  root.append(element("p", "preflight-note", "A deterministic evidence check, separate from the four-agent run. It verifies dates and source scope; it does not make a coverage decision."));
  if (!data) return;
  const facts = element("div", "fact-grid");
  for (const [value, label] of [[data.current_schedule?.revision || "Unknown", "Current supplied schedule"], [list(data.current_schedule?.drivers).length, "Drivers listed in that schedule"], [list(data.reported_assignments).length, "Client-reported start dates"], [list(data.review_questions).length, "Questions flagged for review"]]) {
    const fact = element("div", "fact"); fact.append(element("strong", "", value), element("span", "", label)); facts.append(fact);
  }
  root.append(facts);
  if (data.current_schedule?.source_ref) { const refs = element("div", "evidence-buttons"); refs.append(sourceButton(data.current_schedule.source_ref.source_id, [data.current_schedule.source_ref])); root.append(refs); }
  root.append(element("p", "preflight-note", data.time_basis || "Review the original source passages before relying on a conclusion."));
}

function section(title) { const root = element("section", "content-section"); root.append(element("h4", "", title)); return root; }
function record(title, label) {
  const root = element("article", "record");
  const heading = element("div", "record-head"); heading.append(element("h4", "", title));
  if (label) heading.append(element("span", "small-label", label));
  root.append(heading); return root;
}
function pair(root, before, after, labels = ["Previous", "Proposed"]) {
  const row = element("div", "field-pair");
  for (const [index, value] of [before, after].entries()) {
    const col = element("div"); col.append(element("span", "field-label", labels[index]), element("p", "field-value" + (index ? " after" : ""), value == null ? "Not supplied" : value)); row.append(col);
  }
  root.append(row);
}
function plainList(items) {
  const ul = element("ul", "question-list");
  for (const item of list(items)) ul.append(element("li", "", typeof item === "string" ? item : (item.question || item.decision || item.description || "")));
  return ul;
}
function renderWatcher(root, packet) {
  const payload = packet.payload;
  const events = section(`Material changes · ${list(payload.events).length}`);
  if (!list(payload.events).length) events.append(element("p", "muted", "No material changes identified in this handoff."));
  for (const event of list(payload.events)) {
    const item = record(human(event.field), human(event.evidence_state));
    item.append(element("p", "", event.fact));
    if (event.prior_value != null || event.reported_value != null) pair(item, event.prior_value, event.reported_value, ["Prior evidence", "New evidence"]);
    const meta = element("div", "record-meta"); meta.append(element("span", "", event.entity), element("span", "", dateLabel(event.date))); item.append(meta);
    addSources(item, event.source_ids, packet); events.append(item);
  }
  root.append(events);
  renderQuestions(root, payload.open_questions, packet, "Questions passed to Matcher");
}
function renderQuestions(root, questions, packet, title) {
  if (!list(questions).length) return;
  const group = section(title);
  for (const question of questions) {
    const item = record(question.question || question.request || question.suggestion, question.classification === "OPTIONAL_BROKER_SUGGESTION" ? "Optional" : null);
    if (question.reason) item.append(element("p", "record-reason", question.reason));
    addSources(item, question.source_ids, packet); group.append(item);
  }
  root.append(group);
}
function renderMatcher(root, packet) {
  const payload = packet.payload;
  const group = section(`Policy comparisons · ${list(payload.comparisons).length}`);
  for (const comparison of list(payload.comparisons)) {
    const item = record(comparison.potential_impact, comparison.conflict_state === "UNRESOLVED" ? "Unresolved" : null);
    pair(item, comparison.historical_state, comparison.current_state, ["Historical evidence", "Current evidence"]);
    item.append(element("blockquote", "quote", comparison.relevant_policy_language), element("p", "record-reason", comparison.remaining_decision));
    addSources(item, comparison.source_ids, packet); group.append(item);
  }
  root.append(group);
  renderQuestions(root, payload.required_verification, packet, "Verification supported by the policy");
  renderQuestions(root, payload.optional_suggestions, packet, "Optional broker suggestions");
  renderConflicts(root, payload.conflicts, packet);
}
function renderCloser(root, packet) {
  const payload = packet.payload;
  const group = section("Questions behind the drafts");
  if (!list(payload.drafts).length) group.append(element("p", "muted", payload.no_draft_reason || "No follow-up draft was produced."));
  for (const draft of list(payload.drafts)) {
    const item = record(draft.subject, "Unsent");
    item.append(element("p", "record-reason", draft.recipient_placeholder));
    for (const question of list(draft.question_to_source_map)) {
      const questionBlock = element("div", "record");
      questionBlock.append(element("p", "", question.question), element("p", "record-reason", question.reason));
      addSources(questionBlock, question.source_ids, packet); item.append(questionBlock);
    }
    group.append(item);
  }
  root.append(group);
  if (list(payload.broker_review_checklist).length) { const checklist = section("Before approving"); checklist.append(plainList(payload.broker_review_checklist)); root.append(checklist); }
}
function renderScribe(root, packet) {
  const payload = packet.payload;
  const group = section(`Record proposals · ${list(payload.proposed_changes).length}`);
  if (!list(payload.proposed_changes).length) group.append(element("p", "muted", "No case-record change was proposed."));
  for (const change of list(payload.proposed_changes)) {
    const item = record(human(change.field), "Unapplied");
    pair(item, change.prior_value, change.proposed_value);
    item.append(element("p", "record-reason", change.reason));
    const meta = element("div", "record-meta"); meta.append(element("span", "", human(change.evidence_state)), element("span", "", "Effective " + dateLabel(change.effective_date))); item.append(meta);
    addSources(item, change.source_ids, packet); group.append(item);
  }
  root.append(group);
  if (list(payload.learning_proposals).length) {
    const learning = section("Proposed lessons for review");
    for (const proposal of payload.learning_proposals) {
      const item = record(proposal.proposed_rule, "Unapplied");
      item.append(element("p", "record-reason", proposal.corrected_evidence), element("p", "record-reason", "Scope: " + proposal.scope)); addSources(item, proposal.source_ids, packet); learning.append(item);
    }
    root.append(learning);
  }
  renderConflicts(root, payload.conflicts, packet);
  if (list(payload.approval_needed).length) { const decisions = section("Decisions for the broker"); decisions.append(plainList(payload.approval_needed)); root.append(decisions); }
}
function renderConflicts(root, conflicts, packet) {
  if (!list(conflicts).length) return;
  const group = section("Unresolved evidence");
  for (const conflict of conflicts) { const item = record(conflict.description, "Needs review"); item.append(element("p", "record-reason", conflict.resolution_required)); addSources(item, conflict.source_ids, packet); group.append(item); }
  root.append(group);
}
function renderUnknowns(root, packet) {
  if (!list(packet.unknowns).length) return;
  const group = section("Still unknown");
  const content = element("div", "unknown-list");
  for (const unknown of packet.unknowns) { const item = element("div", "unknown-item"); item.append(element("strong", "", human(unknown.field) + ": "), document.createTextNode(unknown.description || "Not supplied in the records.")); addSources(item, unknown.source_ids, packet); content.append(item); }
  group.append(content); root.append(group);
}

function renderStage() {
  const role = ROLES[state.selectedStage];
  const packet = packetFor(state.selectedStage);
  const active = state.run?.status === "running" && state.run?.current_stage === state.selectedStage;
  const key = JSON.stringify([state.selectedStage, state.run?.id, packet ? null : state.run?.status, packet ? null : state.run?.current_stage, packet, packet ? null : state.run?.error]);
  if (key === state.stageRenderKey) return;
  state.stageRenderKey = key;
  const root = $("stage-content"); root.replaceChildren();
  $("stage-panel").setAttribute("aria-labelledby", "tab-" + state.selectedStage);
  const heading = element("div", "panel-heading");
  heading.append(element("h3", "", role.title));
  const label = packet ? packet.status === "BLOCKED" ? "Blocked" : packet.status === "NEEDS_REVIEW" ? "Needs review" : "Handoff ready" : active ? "Working" : "Not run";
  heading.append(element("span", "small-label" + (packet ? " success" : ""), label));
  root.append(heading, element("p", "stage-subtitle", role.description));
  if (!packet) {
    const empty = element("div", "empty-state");
    empty.append(element("p", "empty-kicker", role.name + (active ? " is working" : " · Agent " + (STAGES.indexOf(state.selectedStage) + 1))));
    const hasFailed = state.run?.status === "failed";
    empty.append(element("h4", "", active ? "Reading, reasoning, then handing off." : hasFailed ? "This handoff is not available." : role.waiting));
    empty.append(element("p", "", active ? "The result will appear when this agent returns a source-checked packet. Earlier handoffs remain available." : hasFailed ? (state.run.error || "The run stopped before this stage produced a validated result. You can review earlier handoffs and try the run again.") : role.detail));
    root.append(empty); return;
  }
  if (packet.status === "BLOCKED") {
    const notice = element("div", "empty-state"); notice.append(element("h4", "", "The team stopped for review."), element("p", "", human(packet.reason_code || "Source evidence needs attention")));
    if (list(packet.missing_inputs).length) notice.append(plainList(packet.missing_inputs)); root.append(notice);
  } else ({ watcher: renderWatcher, matcher: renderMatcher, closer: renderCloser, scribe: renderScribe })[state.selectedStage](root, packet);
  renderUnknowns(root, packet);
  const details = element("details", "packet-details"); details.append(element("summary", "", "Inspect the full handoff packet"), element("pre", "", JSON.stringify(packet, null, 2))); root.append(details);
}

function renderReview() {
  const closer = packetFor("closer");
  const scribe = packetFor("scribe");
  const approval = state.run?.approval;
  const key = JSON.stringify([state.run?.id, state.run?.status, closer, scribe, approval]);
  if (key !== state.reviewRenderKey) {
    state.reviewRenderKey = key;
    const root = $("draft-content"); root.replaceChildren();
    const drafts = list(closer?.payload?.drafts);
    for (const draft of drafts) {
      const item = element("article", "draft-card");
      const label = element("div", "draft-label"); label.append(element("span", "", "Follow-up draft"), element("span", "", "Unsent"));
      item.append(label, element("h4", "", draft.subject), element("p", "draft-recipient", "To: " + draft.recipient_placeholder), element("p", "draft-body", draft.draft_message));
      const ids = list(draft.question_to_source_map).flatMap((question) => list(question.source_ids)); addSources(item, ids, closer); root.append(item);
    }
    if (!drafts.length) {
      const empty = element("div", "empty-state compact"); empty.append(element("h4", "", closer ? "No follow-up needed in this packet." : "Your next step, prepared."), element("p", "", closer ? closer.payload.no_draft_reason || "The agent supplied no draft." : "Closer’s draft will appear here, followed by Scribe’s record proposals.")); root.append(empty);
    }
    const summary = $("proposal-summary"); summary.replaceChildren();
    if (scribe) {
      const count = list(scribe.payload.proposed_changes).length;
      const item = element("div", "review-proposals"); item.append(element("strong", "", `${count} record proposal${count === 1 ? "" : "s"}`), element("p", "", "Prepared by Scribe. Nothing has been applied."));
      const button = element("button", "text-button", "Review Scribe’s proposals"); button.type = "button"; button.addEventListener("click", () => { selectStage("scribe", true, true); $("stage-panel").scrollIntoView({ block: "nearest", behavior: "auto" }); }); item.append(button); summary.append(item);
    }
    $("review-status").textContent = approval ? approval.decision === "approve" ? "Review approved" : "Review rejected" : readyForApproval() ? "Ready for you" : closer ? "Draft ready" : "Waiting";
    $("review-status").className = "small-label" + (approval?.decision === "approve" || readyForApproval() ? " success" : "");
    if (approval) {
      $("approval-feedback").className = "approval-feedback";
      $("approval-feedback").replaceChildren(document.createTextNode(`${approval.decision === "approve" ? "Review approved" : "Review rejected"}${approval.decided_at ? " · " + dateLabel(approval.decided_at, true) : ""}.`));
      if (approval.note) $("approval-feedback").append(element("p", "decision-note", approval.note));
    }
  }
  updateControls();
}

function acceptRun(run) {
  if (!run || !run.id) return;
  // Slow polling responses must not overwrite a newer accepted handoff/result.
  if (state.run?.id === run.id) {
    if (state.run.status !== "running" && run.status === "running") return;
    if (list(run.stages).length < list(state.run.stages).length) return;
    if (state.run.approval && !run.approval) return;
  }
  const newRun = state.run?.id !== run.id;
  const oldStage = state.run?.current_stage;
  const oldStatus = state.run?.status;
  state.run = run;
  if (newRun) { $("approval-note").value = ""; $("approval-feedback").replaceChildren(); state.autoFollow = true; }
  if (state.status) state.status.active_run_id = run.status === "running" ? run.id : null;
  if (state.autoFollow) state.selectedStage = STAGES.includes(run.current_stage) ? run.current_stage : run.status === "completed" && packetFor("scribe") ? "scribe" : state.selectedStage;
  if (run.status !== oldStatus || run.current_stage !== oldStage) announce(run.status === "running" ? `${ROLES[run.current_stage]?.name || "The agent team"} is working.` : run.status === "completed" ? "The four-agent run is complete. Review the drafts and proposals." : "The agent run stopped. Review the available handoffs and error.");
  $("run-reference").textContent = `Run ${run.id.slice(0, 8)} · ${run.status === "completed" ? isBlocked() ? "Blocked" : "Awaiting broker review" : human(run.status)}`;
  if (run.approval) $("run-reference").textContent = `Run ${run.id.slice(0, 8)} · Review ${run.approval.decision === "approve" ? "approved" : "rejected"}`;
  renderStageTabs(); renderStage(); renderReview();
  if (run.status === "failed" && run.error) setError(run.error);
  if (run.status === "running") schedulePoll(run.id);
  else { clearTimeout(state.pollTimer); state.pollTimer = null; }
}
function schedulePoll(id) {
  clearTimeout(state.pollTimer);
  state.pollTimer = setTimeout(async () => {
    try { const run = await request("/api/runs/" + encodeURIComponent(id)); if (state.run?.id === id) { setError(""); acceptRun(run); } }
    catch (error) { if (state.run?.id === id) { setError(error.message); schedulePoll(id); } }
  }, 1500);
}
async function refreshStatus() {
  if (state.statusBusy) return;
  state.statusBusy = true;
  try {
    const status = await request("/api/status"); state.status = status;
    if (status.latest_run && (!state.run || status.latest_run.id === state.run.id || status.active_run_id)) acceptRun(status.latest_run);
    else if (status.active_run_id && state.run?.id !== status.active_run_id) acceptRun(await request("/api/runs/" + encodeURIComponent(status.active_run_id)));
    updateControls();
  } catch (error) { state.status = null; updateControls(); setError(error.message); }
  finally { state.statusBusy = false; }
}
async function loadWorkspace() {
  $("retry-button").disabled = true; $("refresh-button").disabled = true;
  const results = await Promise.allSettled([request("/api/case"), request("/api/status")]);
  const errors = [];
  if (results[0].status === "fulfilled") { state.case = results[0].value.case; state.preflight = results[0].value.preflight; renderCase(); }
  else errors.push(results[0].reason.message);
  if (results[1].status === "fulfilled") {
    state.status = results[1].value;
    if (state.status.latest_run) acceptRun(state.status.latest_run);
    else if (state.status.active_run_id) { try { acceptRun(await request("/api/runs/" + encodeURIComponent(state.status.active_run_id))); } catch (error) { errors.push(error.message); } }
  } else { state.status = null; errors.push(results[1].reason.message); }
  setError(errors.length ? errors.join(" ") : state.run?.status === "failed" ? state.run.error || "The agent run stopped before all handoffs were complete." : "");
  renderStageTabs(); renderStage(); renderReview();
  $("retry-button").disabled = false; $("refresh-button").disabled = false;
}
async function startRun() {
  if (running() || !state.case || state.status?.model_ready !== true) return;
  state.starting = true; setError(""); updateControls();
  try {
    const response = await request("/api/runs", {});
    state.starting = false;
    if (!response.job_id) throw new Error("The workspace did not return a run receipt. Refresh before starting again.");
    state.autoFollow = true;
    acceptRun({ id: response.job_id, status: "running", current_stage: "watcher", stages: [], result: null, error: null, approval: null });
    try { acceptRun(await request("/api/runs/" + encodeURIComponent(response.job_id))); } catch (error) { setError(error.message); }
  } catch (error) { state.starting = false; setError(error.message); await refreshStatus(); }
  finally { updateControls(); }
}
async function saveApproval(decision) {
  if (!readyForApproval() || state.approving || state.run.approval) return;
  state.approving = true; updateControls();
  $("approval-feedback").className = "approval-feedback";
  $("approval-feedback").textContent = "Saving your review decision…";
  const id = state.run.id;
  try {
    const updated = await request("/api/runs/" + encodeURIComponent(id) + "/approval", { decision, note: $("approval-note").value.trim() });
    if (state.run?.id === id) { acceptRun(updated); announce(decision === "approve" ? "Review approved. Drafts remain unsent and changes remain unapplied." : "Review rejected. No customer action was taken."); }
  } catch (error) { $("approval-feedback").className = "approval-feedback error"; $("approval-feedback").textContent = error.message; }
  finally { state.approving = false; updateControls(); }
}

$("stage-tabs").addEventListener("click", (event) => { const tab = event.target.closest("[data-stage]"); if (tab) selectStage(tab.dataset.stage, true); });
$("stage-tabs").addEventListener("keydown", (event) => {
  const index = STAGES.indexOf(event.target.closest("[data-stage]")?.dataset.stage); if (index < 0) return;
  const next = event.key === "ArrowRight" ? (index + 1) % 4 : event.key === "ArrowLeft" ? (index + 3) % 4 : event.key === "Home" ? 0 : event.key === "End" ? 3 : null;
  if (next !== null) { event.preventDefault(); selectStage(STAGES[next], true, true); }
});
$("source-search").addEventListener("input", renderSources);
$("run-button").addEventListener("click", startRun);
$("retry-button").addEventListener("click", loadWorkspace);
$("refresh-button").addEventListener("click", loadWorkspace);
$("approval-form").addEventListener("submit", (event) => { event.preventDefault(); saveApproval("approve"); });
$("reject-button").addEventListener("click", () => saveApproval("reject"));
$("close-source").addEventListener("click", () => $("source-dialog").close());
$("source-dialog").addEventListener("click", (event) => { if (event.target === $("source-dialog")) { const r = event.target.getBoundingClientRect(); if (event.clientX < r.left || event.clientX > r.right || event.clientY < r.top || event.clientY > r.bottom) event.target.close(); } });
$("source-dialog").addEventListener("close", () => { const target = state.dialogTrigger?.isConnected ? state.dialogTrigger : $("source-search"); target.focus({ preventScroll: true }); });
document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshStatus(); });
window.addEventListener("online", refreshStatus);
window.addEventListener("pagehide", () => { clearTimeout(state.pollTimer); clearInterval(state.statusTimer); });
renderStageTabs(); renderStage(); renderReview();
loadWorkspace();
state.statusTimer = setInterval(() => { if (!document.hidden && !state.starting && !state.approving) refreshStatus(); }, 5000);
