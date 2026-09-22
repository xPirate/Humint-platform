"use strict";

/* ============================================================================
 * State + small helpers
 * ========================================================================== */

const state = { user: null, currentEntityId: null, currentReportId: null, sessionExpiredHandled: false, assistantLoaded: false, reportEditorOpen: false, debriefOpen: false };

// Kept in one place because both the entity form (dynamic label swap — see
// wireCommunicationMediumDetail) and the read-only detail view (see
// renderDetailFields) need to know what medium_detail means for whichever
// medium is selected. Must match api/entities.py's COMMUNICATION_MEDIUM_VALUES
// exactly, or a value would show up on the server but never be selectable
// here.
const COMMUNICATION_MEDIUM_OPTIONS = [
  "", "Cellphone", "Landline", "Text/SMS", "Satellite Phone", "Email",
  "HF Radio", "VHF Radio", "UHF Radio", "FM Radio", "GMRS", "FRS", "CB Radio",
  "In-Person", "Mail/Courier", "Other",
];

// Same contract as the media above: must match VEHICLE_STYLE_VALUES in
// api/entities.py exactly, or a value the server accepts is not selectable
// here. Blank first, because "not recorded" is a real answer.
const VEHICLE_STYLE_OPTIONS = [
  "", "Sedan", "Coupe", "Hatchback", "SUV", "Pickup truck", "Van", "Panel van",
  "Box truck", "Semi-tractor", "Bus", "Motorcycle", "ATV/UTV", "Trailer",
  "Boat", "Aircraft", "Other",
];
const COMMUNICATION_MEDIUM_DETAIL_LABELS = {
  "Cellphone": "Phone number", "Landline": "Phone number", "Text/SMS": "Phone number",
  "Satellite Phone": "Phone number", "Email": "Email address",
  "HF Radio": "Frequency (MHz)", "VHF Radio": "Frequency (MHz)",
  "UHF Radio": "Frequency (MHz)", "FM Radio": "Frequency (MHz)",
  "GMRS": "Channel", "FRS": "Channel", "CB Radio": "Channel",
  "In-Person": "Location", "Mail/Courier": "Address", "Other": "Details",
};

// Whose side is this subject on — an analyst's own read, never something the
// extraction pass proposes. Must match ALIGNMENT_VALUES in api/entities.py.
//
// "Family" is not here on purpose: it was in the old Faction list, kinship is
// what the family_of relationship is for, and a person could not be recorded
// as both a relative and hostile while the two shared one dropdown. Records
// that already carry it keep it — alignmentField() puts it back in the list
// for exactly those records, so editing one does not silently change it.
const ALIGNMENT_OPTIONS = ["", "Friendly", "Neutral", "Unknown", "Hostile"];
const LEGACY_ALIGNMENT_OPTIONS = ["Family"];

// Ground does not take a side; it is more or less workable. Must match
// ENVIRONMENT_VALUES in api/entities.py.
const ENVIRONMENT_OPTIONS = ["", "Permissive", "Semi-permissive", "Non-permissive",
                             "Denied", "Unknown"];

const ALIGNMENT_FIELD = {
  key: "alignment", label: "Alignment", type: "select", options: ALIGNMENT_OPTIONS,
  hint: "Your assessment. For a named group, create it as an Organization and link people to it.",
};

const DETAIL_FIELDS = {
  person: [
    { key: "aliases", label: "Aliases (comma-separated)", type: "text-list" },
    { key: "date_of_birth", label: "Date of birth", type: "date" },
    ALIGNMENT_FIELD,
    { key: "occupation", label: "Occupation", type: "text" },
    { key: "physical_description", label: "Physical description", type: "textarea" },
    // Two separate questions, deliberately not one dropdown: a person can be
    // Deceased and their disposition simply no longer relevant. Blank is not
    // "Unknown" — blank means nobody assessed it, Unknown means someone tried
    // and could not resolve it. Must match LIFE_STATUS_VALUES and
    // DISPOSITION_VALUES in api/entities.py.
    { key: "life_status", label: "Status", type: "select", options: ["", "Living", "Deceased", "Unknown"] },
    { key: "disposition", label: "Disposition", type: "select",
      options: ["", "At liberty", "Captured", "Detained", "Evading", "Missing"] },
  ],
  organization: [
    { key: "org_type", label: "Org type", type: "text" },
    { key: "founded_date", label: "Founded date", type: "date" },
    { key: "website", label: "Website", type: "text" },
    ALIGNMENT_FIELD,
  ],
  location: [
    { key: "address", label: "Address", type: "text" },
    { key: "environment", label: "Environment", type: "select", options: ENVIRONMENT_OPTIONS,
      hint: "How freely you could operate here. Denied means no access at all." },
    // clearable: blanking these actually sends null (clearing them
    // server-side) instead of collectDetails' usual "omit = leave
    // unchanged" — see detailFieldInputHtml. Without this, there'd be no
    // way to trigger the "blank lat/lng to force a re-geocode" behavior
    // documented in the form hint (see entityFormDetailsHtml) through the
    // edit form at all.
    { key: "lat", label: "Latitude", type: "number", clearable: true },
    { key: "lng", label: "Longitude", type: "number", clearable: true },
  ],
  event: [
    { key: "event_type", label: "Event type", type: "text" },
    { key: "started_at", label: "Started at", type: "datetime-local" },
    { key: "ended_at", label: "Ended at", type: "datetime-local" },
    // Not the same as "Ended at", and the hint says so because the two get
    // confused constantly: a raid that ended in ten minutes can stay relevant
    // for months, and a strike still under way can already be old news.
    { key: "expires_at", label: "Relevant until", type: "date", clearable: true,
      hint: "When this stops being current, not when it ended. Blank for no expiry." },
  ],
  source: [
    { key: "source_type", label: "Source type", type: "text" },
    { key: "reliability_rating", label: "Reliability (Admiralty A–F)", type: "select", options: ["", "A", "B", "C", "D", "E", "F"] },
    { key: "handling_notes", label: "Handling notes", type: "textarea" },
    // Separate from the reliability grade above, and deliberately so: a source
    // can be Hostile and grade A. Whose side they are on and how well their
    // reporting has held up are different questions.
    ALIGNMENT_FIELD,
  ],
  communication: [
    { key: "medium", label: "Medium", type: "select", options: COMMUNICATION_MEDIUM_OPTIONS },
    { key: "medium_detail", label: "Detail", type: "text" },
    { key: "occurred_at", label: "Occurred at", type: "datetime-local" },
    { key: "participants_note", label: "Participants note", type: "textarea" },
  ],
  // Make, model and colour are free text on purpose: constraining them would
  // mean shipping a list of every manufacturer on earth and keeping it up to
  // date. Style is the one that has to be a list, because filtering "show me
  // the panel vans" only works if everyone spells it the same way. Must match
  // VEHICLE_STYLE_VALUES in api/entities.py.
  vehicle: [
    { key: "make", label: "Make", type: "text" },
    { key: "model", label: "Model", type: "text" },
    { key: "color", label: "Colour", type: "text" },
    { key: "license_plate", label: "Licence plate", type: "text",
      hint: "As your source wrote it." },
    { key: "plate_region", label: "Plate issued by", type: "text",
      hint: "State, province or country." },
    ALIGNMENT_FIELD,
    { key: "style", label: "Style", type: "select", options: VEHICLE_STYLE_OPTIONS },
    { key: "notes", label: "Notes", type: "textarea",
      hint: "Damage, markings, equipment, who drives it." },
  ],
  record: [
    { key: "record_kind", label: "Kind", type: "text", placeholder: "Letter, bank statement, registration…" },
    { key: "record_date", label: "Date on the document", type: "date", clearable: true },
    { key: "issued_by", label: "Issued by", type: "text" },
    { key: "body", label: "Full text", type: "textarea", rows: 14 },
  ],
};

function escapeHtml(str) {
  if (str === null || str === undefined) return "";
  return String(str).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function truncate(s, n) {
  s = String(s || "");
  return s.length > n ? s.slice(0, n) + "…" : s;
}

/* ---------------------------------------------------------------------------
 * Timestamps
 *
 * IMPORTANT distinction, and the reason these are two separate helpers:
 * relative time ("3 days ago") is right for ACTIVITY metadata — when a record
 * was created, edited, uploaded, last polled. It is wrong for CASE data — a
 * date of birth rendered as "45 years ago", or an event that "happened 8
 * months ago" instead of on a specific date, actively destroys the precision
 * an analyst needs. So relativeTime/timeAgoHtml are only ever applied to the
 * former; case dates keep using formatDate/absolute rendering.
 * ------------------------------------------------------------------------- */

const RELATIVE_UNITS = [
  ["year", 365 * 24 * 3600],
  ["month", 30 * 24 * 3600],
  ["week", 7 * 24 * 3600],
  ["day", 24 * 3600],
  ["hour", 3600],
  ["minute", 60],
];

function relativeTime(value) {
  if (!value) return "";
  const then = new Date(value);
  if (isNaN(then.getTime())) return "";
  const seconds = (Date.now() - then.getTime()) / 1000;
  const abs = Math.abs(seconds);
  if (abs < 45) return seconds >= 0 ? "just now" : "in a moment";
  for (const [unit, unitSeconds] of RELATIVE_UNITS) {
    if (abs >= unitSeconds) {
      const n = Math.round(abs / unitSeconds);
      const label = `${n} ${unit}${n === 1 ? "" : "s"}`;
      return seconds >= 0 ? `${label} ago` : `in ${label}`;
    }
  }
  return seconds >= 0 ? "just now" : "in a moment";
}

// The exact value is never actually lost — it's one hover away, which is what
// makes relative time safe to use as the default rendering on a case tool.
function timeAgoHtml(value) {
  if (!value) return "";
  const exact = new Date(value);
  if (isNaN(exact.getTime())) return escapeHtml(String(value));
  return `<time class="ts" datetime="${escapeHtml(exact.toISOString())}" title="${escapeHtml(exact.toLocaleString())}">${escapeHtml(relativeTime(value))}</time>`;
}

function debounce(fn, ms) {
  let t;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
}

let toastTimer = null;
function showToast(msg, isError) {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.classList.toggle("error", !!isError);
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 4000);
}

/* ============================================================================
 * API client
 * ========================================================================== */

// A 401 on any call made *while already logged in* means the session that
// used to be valid no longer is (it timed out, was revoked, or the account
// was deactivated) — not "never logged in," which is what a 401 on
// /api/auth/me or a failed /api/auth/login attempt also looks like before
// state.user is ever set. Gating on state.user is what keeps this from
// firing during the normal pre-login flow. Without this, the app had no way
// to notice its session died mid-use: whatever view was already rendered
// (e.g. the Entities list, fetched before expiry) just kept showing as-is,
// and only a *new* action that needed a fresh API call — opening an entity,
// say — would fail, landing an error message inline instead of sending the
// person back to log in again.
function handleSessionExpired() {
  if (state.sessionExpiredHandled) return;
  state.sessionExpiredHandled = true;
  state.user = null;
  closeModal();
  toggleAssistantPanel(false);
  document.getElementById("assistant-tab-btn").hidden = true;
  showAuthScreen("login");
  document.getElementById("login-error").textContent = "Your session has expired — please log in again.";
}

async function api(path, opts = {}) {
  const hasBody = opts.body !== undefined;
  const res = await fetch(path, {
    method: opts.method || "GET",
    headers: hasBody ? { "Content-Type": "application/json" } : undefined,
    body: hasBody ? JSON.stringify(opts.body) : undefined,
  });
  if (res.status === 401 && state.user) handleSessionExpired();
  const text = await res.text();
  let data = null;
  if (text) {
    try { data = JSON.parse(text); } catch (e) { /* non-JSON body, leave data null */ }
  }
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    if (data && data.detail) {
      if (Array.isArray(data.detail)) detail = data.detail.map((d) => d.msg || JSON.stringify(d)).join("; ");
      else if (typeof data.detail === "object") detail = data.detail.message || JSON.stringify(data.detail);
      else detail = data.detail;
    }
    const err = new Error(detail);
    err.status = res.status;
    err.data = data;
    throw err;
  }
  return data;
}

async function apiUpload(path, formData) {
  const res = await fetch(path, { method: "POST", body: formData });
  if (res.status === 401 && state.user) handleSessionExpired();
  const text = await res.text();
  let data = null;
  try { data = JSON.parse(text); } catch (e) { /* ignore */ }
  if (!res.ok) {
    throw new Error((data && data.detail) || `HTTP ${res.status}`);
  }
  return data;
}


/* ============================================================================
 * Right-click actions on a record
 *
 * The same menu wherever a record's name appears: the tree, the cards, the
 * network, a map popup, a report's linked list. Two of the items need a model,
 * and those are disabled with the reason on them when Ollama is off rather
 * than hidden, so nobody has to wonder where they went.
 * ========================================================================== */

const FLAG_STATE_LABELS = {
  pending: "in the review queue", confirmed: "confirmed a match", dismissed: "dismissed before",
};
let entityMenuEl = null;
let entityMenuCtx = null;

function closeEntityMenu() {
  if (entityMenuEl) { entityMenuEl.remove(); entityMenuEl = null; entityMenuCtx = null; }
}
document.addEventListener("click", (e) => {
  if (entityMenuEl && !entityMenuEl.contains(e.target)) closeEntityMenu();
});
document.addEventListener("keydown", (e) => {
  if (!entityMenuEl) return;
  const items = [...entityMenuEl.querySelectorAll("button.ctx-item:not([disabled])")];
  const at = items.indexOf(document.activeElement);
  if (e.key === "Escape") { e.preventDefault(); closeEntityMenu(); }
  else if (e.key === "ArrowDown") { e.preventDefault(); (items[at + 1] || items[0]).focus(); }
  else if (e.key === "ArrowUp") { e.preventDefault(); (items[at - 1] || items[items.length - 1]).focus(); }
});
window.addEventListener("resize", closeEntityMenu);
window.addEventListener("scroll", closeEntityMenu, true);

// Copying works on a plain-http LAN deployment too, where the clipboard API
// is not available at all.
async function copyText(text, what) {
  try {
    if (navigator.clipboard && window.isSecureContext) await navigator.clipboard.writeText(text);
    else {
      const ta = document.createElement("textarea");
      ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
      document.body.appendChild(ta); ta.select(); document.execCommand("copy"); ta.remove();
    }
    showToast(`${what} copied`);
  } catch (err) { showToast("Couldn't copy: " + err.message, true); }
}

function askAssistantAbout(question) {
  toggleAssistantPanel(true);
  const input = document.getElementById("assistant-input");
  input.value = question;
  document.getElementById("assistant-form").requestSubmit();
}

async function proposeLinksFor(ctx) {
  showToast("Asking the model for links…");
  try {
    const res = await api("/api/suggestions/propose", { method: "POST", body: {
      instruction: `What is ${ctx.name} connected to? Propose relationships between `
                 + `${ctx.name} and the other records listed.`,
      focus_entity_id: ctx.id } });
    showToast(res.created
      ? `${res.created} proposal(s) waiting in Review`
      : (res.message || "Nothing proposed"));
  } catch (err) { showToast(err.message, true); }
}

async function openSimilarPanel(ctx) {
  openModal(`<h3>Records like ${escapeHtml(ctx.name)}</h3>
    <p class="field-hint">Comparing the text of every record. Nothing is changed by looking.</p>
    <div id="similar-body"><p class="empty-state">Comparing…</p></div>
    <div class="modal-actions"><button type="button" class="btn-secondary" id="similar-close">Close</button></div>`);
  document.getElementById("modal-box").style.maxWidth = "48em";
  document.getElementById("similar-close").addEventListener("click", closeModal);
  let data;
  try {
    data = await api(`/api/entities/${encodeURIComponent(ctx.id)}/similar?limit=10`);
  } catch (err) {
    document.getElementById("similar-body").innerHTML =
      `<p class="form-error">${escapeHtml(err.message)}</p>`;
    return;
  }
  const body = document.getElementById("similar-body");
  if (!data.items.length) {
    body.innerHTML = '<p class="empty-state">Nothing else reads like this record.</p>';
    return;
  }
  body.innerHTML = `<ul class="similar-list">${data.items.map((it) => `
    <li class="similar-row">
      <div class="similar-main">
        <span class="type-pill type-${escapeHtml(it.entity_type)}">${escapeHtml(ENTITY_TYPE_LABELS[it.entity_type] || it.entity_type)}</span>
        <button type="button" class="btn-link" data-similar-open="${escapeHtml(it.id)}">${escapeHtml(it.name)}</button>
        ${it.is_active ? "" : '<span class="card-meta">archived</span>'}
        ${it.already_flagged ? `<span class="card-meta">${escapeHtml(FLAG_STATE_LABELS[it.already_flagged] || it.already_flagged)}</span>` : ""}
      </div>
      <div class="similar-score" title="${(it.score * 100).toFixed(1)}% similar">
        <span class="similar-bar" style="width:${Math.round(it.score * 100)}%"></span>
        <span class="similar-num">${Math.round(it.score * 100)}</span>
      </div>
      <div class="similar-actions">
        ${it.already_flagged ? "" :
          `<button type="button" class="btn-secondary btn-sm" data-similar-flag="${escapeHtml(it.id)}"
                   data-score="${it.score}">Flag as duplicate</button>`}
        <button type="button" class="btn-secondary btn-sm" data-similar-merge="${escapeHtml(it.id)}">Merge…</button>
      </div>
    </li>`).join("")}</ul>`;
  body.querySelectorAll("[data-similar-open]").forEach((b) => b.addEventListener("click", () => {
    closeModal(); openEntityDetail(b.dataset.similarOpen);
  }));
  body.querySelectorAll("[data-similar-flag]").forEach((b) => b.addEventListener("click", async () => {
    try {
      const res = await api(`/api/entities/${encodeURIComponent(ctx.id)}/flag-duplicate`,
        { method: "POST", body: { other_entity_id: b.dataset.similarFlag, score: Number(b.dataset.score) } });
      showToast(res.created ? "Flagged — it's in the Review queue" : res.message);
      b.disabled = true;
    } catch (err) { showToast(err.message, true); }
  }));
  body.querySelectorAll("[data-similar-merge]").forEach((b) => b.addEventListener("click", () => {
    closeModal();
    openMergeDialog([ctx.id, b.dataset.similarMerge], () => openEntityDetail(ctx.id));
  }));
}

function entityMenuItems(ctx) {
  const ai = ctx.ollama_enabled;
  const aiOff = "Ollama is switched off";
  const noEmbed = "No embed model is configured";
  const items = [
    { label: "Open record", act: () => openEntityDetail(ctx.id),
      hide: state.currentEntityId === ctx.id },
    { sep: true },
    { label: "Look for links", ai: true, disabled: !ai && aiOff,
      note: "proposes into Review", act: () => proposeLinksFor(ctx) },
    { label: "Ask the assistant about this", ai: true, disabled: !ai && aiOff,
      act: () => askAssistantAbout(`What do we know about ${ctx.name}?`) },
    { label: "What is missing on this record?", ai: true, disabled: !ai && aiOff,
      act: () => askAssistantAbout(
        `Looking only at what is in the case file about ${ctx.name}, what is missing or `
        + `unanswered, and what would you check next?`) },
    { label: "Find similar records", ai: true,
      disabled: (!ai && aiOff) || (ai && !ctx.embed_model_configured && noEmbed),
      note: "duplicates", act: () => openSimilarPanel(ctx) },
    { sep: true },
    { label: "Add a relationship…", act: async () => {
        await openEntityDetail(ctx.id);
        const btn = document.getElementById("add-rel-btn");
        if (btn) btn.click();
      } },
    { label: "Write a report about this", act: () => startReportAbout(ctx) },
    { label: "Select to merge", act: () => startMergeFrom(ctx) },
    { sep: true },
    { label: "Export a dossier (PDF)",
      act: () => window.open(`/api/exports/entities/${encodeURIComponent(ctx.id)}/dossier.pdf?shape=plain`, "_blank") },
    { label: "Show on the map", hide: !ctx.has_coordinates,
      act: () => { switchView("map"); focusMapOnEntity(ctx.id); } },
    { sep: true },
    { label: "Copy name", act: () => copyText(ctx.name, "Name") },
    { label: "Copy record ID", act: () => copyText(ctx.id, "Record ID") },
  ];
  return items.filter((i) => !i.hide);
}

function startReportAbout(ctx) {
  openReportEditor(null);
  const title = document.getElementById("report-editor-title");
  if (title && !title.value) title.value = `${ctx.name} - `;
  addLinkedEntity({ id: ctx.id, name: ctx.name, entity_type: ctx.entity_type });
  const body = document.getElementById("report-editor-body");
  if (body) body.focus();
}

function startMergeFrom(ctx) {
  switchView("entities");
  if (!entityPickMode) setEntityPickMode(true);
  entityPicked.add(ctx.id);
  loadEntities();
  showToast("Now pick the other copy, then press Merge");
}

/* Open the Map on one Location. The map may not have finished loading its
   markers yet, so this waits for the pin rather than guessing a delay. */
function focusMapOnEntity(entityId, tries = 0) {
  const marker = mapState.markersById && mapState.markersById[entityId];
  if (!marker) {
    if (tries < 25) setTimeout(() => focusMapOnEntity(entityId, tries + 1), 200);
    return;
  }
  leafletMap.setView(marker.getLatLng(), Math.max(leafletMap.getZoom(), 13));
  marker.fire("click");
}

async function openEntityMenu(entityId, x, y) {
  closeEntityMenu();
  let ctx;
  try {
    ctx = await api(`/api/entities/${encodeURIComponent(entityId)}/action-context`);
  } catch (err) { showToast(err.message, true); return; }
  entityMenuCtx = ctx;
  const menu = document.createElement("div");
  menu.className = "ctx-menu";
  menu.setAttribute("role", "menu");
  menu.innerHTML = `
    <div class="ctx-head">
      <span class="type-pill type-${escapeHtml(ctx.entity_type)}">${escapeHtml(ENTITY_TYPE_LABELS[ctx.entity_type] || ctx.entity_type)}</span>
      <strong>${escapeHtml(ctx.name)}</strong>
      <span class="ctx-sub">${ctx.relationship_count} link${ctx.relationship_count === 1 ? "" : "s"} ·
        ${ctx.report_count} report${ctx.report_count === 1 ? "" : "s"}</span>
    </div>
    ${entityMenuItems(ctx).map((it, i) => it.sep
      ? '<div class="ctx-sep"></div>'
      : `<button type="button" class="ctx-item${it.ai ? " ctx-ai" : ""}" role="menuitem" data-ctx="${i}"
                 ${it.disabled ? `disabled title="${escapeHtml(it.disabled)}"` : ""}>
           <span>${escapeHtml(it.label)}</span>
           ${it.note ? `<span class="ctx-note">${escapeHtml(it.note)}</span>` : ""}
           ${it.disabled ? `<span class="ctx-note">${escapeHtml(it.disabled)}</span>` : ""}
         </button>`).join("")}`;
  document.body.appendChild(menu);
  entityMenuEl = menu;

  const items = entityMenuItems(ctx);
  menu.querySelectorAll("[data-ctx]").forEach((btn) => {
    const item = items[Number(btn.dataset.ctx)];
    btn.addEventListener("click", () => { closeEntityMenu(); item.act(); });
  });

  // Kept on screen: a menu opened near the right or bottom edge flips back
  // over the pointer instead of running off.
  const rect = menu.getBoundingClientRect();
  const left = Math.min(x, window.innerWidth - rect.width - 8);
  const top = Math.min(y, window.innerHeight - rect.height - 8);
  menu.style.left = Math.max(8, left) + "px";
  menu.style.top = Math.max(8, top) + "px";
  const first = menu.querySelector("button.ctx-item:not([disabled])");
  if (first) first.focus();
}

// Where a right-click counts as "on a record".
function entityIdFromTarget(target) {
  const el = target.closest && target.closest(
    "[data-entity-menu],[data-open-entity],.tree-row[data-id],[data-similar-open]");
  if (!el) return null;
  return el.dataset.entityMenu || el.dataset.openEntity || el.dataset.similarOpen || el.dataset.id || null;
}

document.addEventListener("contextmenu", (e) => {
  const id = entityIdFromTarget(e.target);
  if (!id) return;
  e.preventDefault();
  openEntityMenu(id, e.clientX, e.clientY);
});


/* ---------------------------------------------------------------------------
 * The summary card
 *
 * Clicking a dot in the network used to leave the page. That is a heavy answer
 * to "who is this?", and it loses the arrangement you were reading. A click
 * now opens a small card beside the dot: what the record is, the fields worth
 * knowing at a glance, and where it connects. Opening the record is one more
 * click, and the card says so.
 * ------------------------------------------------------------------------- */

let summaryCardEl = null;

function closeSummaryCard() {
  if (summaryCardEl) { summaryCardEl.remove(); summaryCardEl = null; }
}
document.addEventListener("click", (e) => {
  if (summaryCardEl && !summaryCardEl.contains(e.target)) closeSummaryCard();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && summaryCardEl) { closeSummaryCard(); }
});
window.addEventListener("resize", closeSummaryCard);
// The card is positioned against the viewport, so a scroll would leave it
// pointing at nothing.
window.addEventListener("scroll", closeSummaryCard, true);

// The one or two facts worth reading about this kind of record, in a line.
function summaryFacts(entity) {
  const d = entity.details || {};
  const out = [];
  const push = (label, value) => { if (value) out.push([label, value]); };
  if (entity.entity_type === "person") {
    push("Alias", Array.isArray(d.aliases) ? d.aliases.join(", ") : d.aliases);
    push("Occupation", d.occupation);
    push("Born", d.date_of_birth);
  } else if (entity.entity_type === "organization") {
    push("Type", d.org_type);
    push("Founded", d.founded_date);
  } else if (entity.entity_type === "location") {
    push("Address", d.address);
    push("Environment", d.environment);
    if (d.lat != null && d.lng != null) push("Coordinates", `${d.lat}, ${d.lng}`);
  } else if (entity.entity_type === "event") {
    push("Type", d.event_type);
    push("Started", d.started_at ? d.started_at.replace("T", " ").slice(0, 16) : null);
    push("Relevant until", d.expires_at);
  } else if (entity.entity_type === "source") {
    push("Source type", d.source_type);
    push("Reliability", d.reliability_rating);
  } else if (entity.entity_type === "communication") {
    push(COMMUNICATION_MEDIUM_DETAIL_LABELS[d.medium] || "Detail", d.medium_detail);
    push("Medium", d.medium);
    push("Occurred", d.occurred_at ? d.occurred_at.replace("T", " ").slice(0, 16) : null);
  } else if (entity.entity_type === "vehicle") {
    push("Vehicle", [d.color, d.make, d.model].filter(Boolean).join(" "));
    push("Plate", d.license_plate);
  } else if (entity.entity_type === "record") {
    push("Kind", d.record_kind);
    push("Dated", d.record_date);
    push("Issued by", d.issued_by);
  }
  return out;
}

async function openSummaryCard(entityId, x, y) {
  closeSummaryCard();
  const card = document.createElement("div");
  card.className = "summary-card";
  card.innerHTML = '<p class="empty-state">Loading…</p>';
  document.body.appendChild(card);
  summaryCardEl = card;
  place(card, x, y);

  let entity;
  try {
    entity = await api(`/api/entities/${encodeURIComponent(entityId)}`);
  } catch (err) {
    card.innerHTML = `<p class="form-error">${escapeHtml(err.message)}</p>`;
    return;
  }
  if (summaryCardEl !== card) return;            // something else opened meanwhile

  const rels = entity.relationships || [];
  const facts = summaryFacts(entity);
  const alignment = entity.details && entity.details.alignment;
  // Up to four connections, the confirmed ones first: a card is a glance, not
  // the relationships list on the record's own page.
  const order = { confirmed: 0, probable: 1, possible: 2 };
  const shown = rels.slice().sort((a, b) =>
    (order[a.confidence] ?? 3) - (order[b.confidence] ?? 3)).slice(0, 4);

  card.innerHTML = `
    <div class="summary-head">
      <span class="type-pill type-${escapeHtml(entity.entity_type)}">${
        escapeHtml(ENTITY_TYPE_LABELS[entity.entity_type] || entity.entity_type)}</span>
      <strong class="summary-name">${nameHtml(entity.name, alignment)}</strong>
      ${alignment ? `<span class="summary-alignment${isHostile(alignment) ? " summary-alignment-hostile" : ""}">${escapeHtml(alignment)}</span>` : ""}
      ${entity.is_active ? "" : '<span class="card-meta">archived</span>'}
      ${personStatusBadgesHtml({ ...entity, ...(entity.details || {}) })}
    </div>
    ${entity.description ? `<p class="summary-desc">${escapeHtml(truncate(entity.description, 220))}</p>` : ""}
    ${facts.length ? `<dl class="summary-facts">${facts.map(([k, v]) =>
        `<dt>${escapeHtml(k)}</dt><dd>${escapeHtml(String(v))}</dd>`).join("")}</dl>` : ""}
    <div class="summary-counts">
      ${rels.length} link${rels.length === 1 ? "" : "s"} ·
      ${(entity.reports || []).length} report${(entity.reports || []).length === 1 ? "" : "s"} ·
      ${(entity.attachments || []).length} document${(entity.attachments || []).length === 1 ? "" : "s"}
    </div>
    ${shown.length ? `<ul class="summary-rels">${shown.map((r) => `
      <li><span class="summary-rel-type">${escapeHtml((r.reads_as || r.relationship_type).replace(/_/g, " "))}</span>
        <button type="button" class="btn-link" data-summary-open="${escapeHtml(r.other_entity_id)}"
        >${nameHtml(r.other_entity_name, r.other_entity_alignment)}</button>
        <span class="summary-conf">${escapeHtml(r.confidence || "")}</span></li>`).join("")}
      ${rels.length > shown.length ? `<li class="card-meta">+ ${rels.length - shown.length} more</li>` : ""}
    </ul>` : ""}
    <div class="summary-actions">
      <button type="button" class="btn-primary btn-sm" data-summary-record>Open record</button>
      <button type="button" class="btn-secondary btn-sm" data-summary-actions>Actions ▾</button>
    </div>`;
  place(card, x, y);

  card.querySelector("[data-summary-record]").addEventListener("click", () => {
    closeSummaryCard();
    openEntityDetail(entity.id);
  });
  card.querySelector("[data-summary-actions]").addEventListener("click", (ev) => {
    ev.stopPropagation();
    const r = ev.currentTarget.getBoundingClientRect();
    closeSummaryCard();
    openEntityMenu(entity.id, r.left, r.bottom + 4);
  });
  card.querySelectorAll("[data-summary-open]").forEach((b) => {
    // Without this the click reaches the document handler after the new card
    // has replaced the old one, and closes the card it just opened.
    b.addEventListener("click", (ev) => {
      ev.stopPropagation();
      openSummaryCard(b.dataset.summaryOpen, x, y);
    });
  });

  function place(el, px, py) {
    const r = el.getBoundingClientRect();
    el.style.left = Math.max(8, Math.min(px + 14, window.innerWidth - r.width - 8)) + "px";
    el.style.top = Math.max(8, Math.min(py - 20, window.innerHeight - r.height - 8)) + "px";
  }
}

/* ============================================================================
 * Modal helper
 * ========================================================================== */

// Has anything been typed into the dialog that is open? Set by the first
// input/change event inside it and cleared when a dialog opens or closes.
let modalDirty = false;

function openModal(html) {
  document.getElementById("modal-box").innerHTML = html;
  document.getElementById("modal-backdrop").hidden = false;
  modalDirty = false;
}
function closeModal() {
  document.getElementById("modal-backdrop").hidden = true;
  document.getElementById("modal-box").innerHTML = "";
  document.getElementById("modal-box").style.maxWidth = "";   // a wide panel is per-modal
  modalDirty = false;
}

document.getElementById("modal-box").addEventListener("input", () => { modalDirty = true; });
document.getElementById("modal-box").addEventListener("change", () => { modalDirty = true; });

/* Closing by clicking outside the box, without throwing away someone's typing.
 *
 * Two ways this used to lose an entity someone had half filled in:
 *
 *   1. A click anywhere outside the box closed it immediately. One stray click
 *      beside the dialog and twenty fields were gone.
 *   2. Selecting text by dragging from inside the box and releasing outside it
 *      fires `click` on the BACKDROP — the click event goes to the nearest
 *      common ancestor of mousedown and mouseup, not to where the drag began.
 *      So highlighting a name to retype it closed the form.
 *
 * Now the press and the release both have to happen on the backdrop, and if
 * anything has been typed the dialog asks first. A dialog nobody has touched
 * still closes on one click, which is what that gesture is for.
 */
let modalPressStartedOnBackdrop = false;
const modalBackdrop = document.getElementById("modal-backdrop");

modalBackdrop.addEventListener("mousedown", (e) => {
  modalPressStartedOnBackdrop = e.target === modalBackdrop;
});
modalBackdrop.addEventListener("click", (e) => {
  if (e.target !== modalBackdrop) return;         // the release was inside the box
  if (!modalPressStartedOnBackdrop) return;       // the press began inside it (a drag-select)
  modalPressStartedOnBackdrop = false;
  requestModalClose();
});
// A touch drag that ends outside the box behaves the same way.
modalBackdrop.addEventListener("touchstart", (e) => {
  modalPressStartedOnBackdrop = e.target === modalBackdrop;
}, { passive: true });

function requestModalClose() {
  if (modalDirty && !confirm("Close this form? What you have typed will be lost.")) return;
  closeModal();
}

document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (document.getElementById("modal-backdrop").hidden) return;
  if (entityMenuEl) return;                       // the menu closes first
  e.preventDefault();
  requestModalClose();
});

/* ============================================================================
 * Global search (topbar) — searches entities and reports together, from
 * whatever view you happen to be on, and jumps straight to whichever result
 * you pick. Deliberately a separate, simpler mechanism from the entity
 * picker below: this one is a destination-jump, not a value-selection field
 * feeding a form, so there's no hidden id to resolve and no surrounding
 * <form> to worry about accidentally submitting.
 * ========================================================================== */

function wireGlobalSearch() {
  const input = document.getElementById("global-search-input");
  const results = document.getElementById("global-search-results");
  if (!input) return;

  function closeResults() {
    results.hidden = true;
    results.innerHTML = "";
  }

  function goTo(item) {
    closeResults();
    input.value = "";
    input.blur();
    if (item.dataset.kind === "entity") openEntityDetail(item.dataset.id);
    else openReportDetail(item.dataset.id);
  }

  input.addEventListener("input", debounce(async () => {
    const q = input.value.trim();
    if (!q) { closeResults(); return; }
    const params = new URLSearchParams({ q, limit: "8" });
    try {
      const [entitiesData, reportsData] = await Promise.all([
        api("/api/entities?" + params.toString()),
        api("/api/reports?" + params.toString()),
      ]);
      const entityItems = entitiesData.items;
      const reportItems = reportsData.items;
      if (!entityItems.length && !reportItems.length) {
        results.innerHTML = '<div class="global-search-empty">No matches.</div>';
        results.hidden = false;
        return;
      }
      const entityRow = (e) => `
        <div class="global-search-item" data-kind="entity" data-id="${escapeHtml(e.id)}">
          <span class="type-pill type-${e.entity_type}">${escapeHtml(e.entity_type)}</span>
          <span>${nameHtml(e.name, e.alignment)}</span>
        </div>`;
      const reportRow = (r) => `
        <div class="global-search-item" data-kind="report" data-id="${escapeHtml(r.id)}">
          <span class="status-pill status-${r.status}">${escapeHtml(r.status)}</span>
          <span>${escapeHtml(r.title)}</span>
        </div>`;
      results.innerHTML = `
        ${entityItems.length ? `<div class="global-search-section">Entities</div>${entityItems.map(entityRow).join("")}` : ""}
        ${reportItems.length ? `<div class="global-search-section">Reports</div>${reportItems.map(reportRow).join("")}` : ""}
      `;
      results.hidden = false;
      // `mousedown`, not `click`: it fires before the input's `blur`, so
      // picking a result isn't lost to closeResults() running first — same
      // reasoning as the entity picker below.
      results.querySelectorAll(".global-search-item").forEach((item) => {
        item.addEventListener("mousedown", (e) => { e.preventDefault(); goTo(item); });
      });
    } catch (e) { /* search failures here aren't worth a toast */ }
  }, 250));

  input.addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    e.preventDefault();
    const first = results.querySelector(".global-search-item");
    if (first) goTo(first);
  });
  input.addEventListener("blur", () => setTimeout(closeResults, 150));
  input.addEventListener("focus", () => { if (results.innerHTML) results.hidden = false; });
}
wireGlobalSearch();
wireMapSourceControls();
wireMapAdmin();
wireZoneControls();

/* ============================================================================
 * Entity picker (search-as-you-type with a custom results dropdown, used
 * anywhere a real entity id needs to be chosen — relationships, report
 * linking, relationship suggestion review).
 *
 * This used to be a plain <input list="..."> + <datalist>. That's simpler,
 * but datalist's "pick an option" moment doesn't fire a reliable, consistent
 * `change` event across browsers — Safari in particular can leave the input
 * showing the selected text with no `change` ever firing, so the hidden id
 * field this whole mechanism depends on never gets set. The form then
 * (correctly, but unhelpfully) reports "pick a valid entity" even though the
 * user very much just clicked one. A hand-rolled dropdown sidesteps browser
 * datalist quirks entirely: selection is a plain click/mousedown we control.
 * ========================================================================== */

function entityPickerHtml(label, idPrefix, entityTypeFilter) {
  return `
    <div class="form-row entity-picker">
      <label>${escapeHtml(label)}</label>
      <input type="text" id="${idPrefix}-input" placeholder="Type to search…" autocomplete="off" data-type-filter="${entityTypeFilter || ""}">
      <input type="hidden" id="${idPrefix}-id">
      <div class="entity-picker-results" id="${idPrefix}-results" hidden></div>
    </div>
  `;
}

function wireEntityPicker(idPrefix) {
  const input = document.getElementById(`${idPrefix}-input`);
  const hidden = document.getElementById(`${idPrefix}-id`);
  const results = document.getElementById(`${idPrefix}-results`);
  if (!input) return;

  function closeResults() {
    results.hidden = true;
    results.innerHTML = "";
  }

  input.addEventListener("input", debounce(async () => {
    hidden.value = "";
    const q = input.value.trim();
    if (!q) { closeResults(); return; }
    const params = new URLSearchParams({ q, limit: "20" });
    if (input.dataset.typeFilter) params.set("entity_type", input.dataset.typeFilter);
    try {
      const data = await api("/api/entities?" + params.toString());
      if (!data.items.length) { closeResults(); return; }
      results.innerHTML = data.items.map((e) => {
        const optLabel = `${e.name} (${e.entity_type})`;
        return `<div class="entity-picker-item" data-id="${escapeHtml(e.id)}" data-label="${escapeHtml(optLabel)}">
          ${escapeHtml(e.name)} <span class="type-pill type-${e.entity_type}">${escapeHtml(e.entity_type)}</span>
        </div>`;
      }).join("");
      results.hidden = false;
      results.querySelectorAll(".entity-picker-item").forEach((item) => {
        // `mousedown`, not `click`: it fires before the input's `blur`, so
        // picking a result isn't lost to closeResults() running first.
        item.addEventListener("mousedown", (e) => {
          e.preventDefault();
          selectItem(item);
        });
      });
    } catch (e) { /* search failures here aren't worth a toast */ }
  }, 250));

  function selectItem(item) {
    input.value = item.dataset.label;
    hidden.value = item.dataset.id;
    closeResults();
  }

  // This input usually lives inside a larger <form> (the report form, the
  // relationship form, …) that has its own real submit button elsewhere on
  // the page. Left alone, pressing Enter here would trigger the BROWSER'S
  // default "submit the enclosing form" behavior — which for a picker with
  // its own separate "Add" button (report linking) submits the whole outer
  // form (e.g. saves the report) without ever running that Add button's
  // click handler, so a freshly-picked entity is silently dropped even
  // though the save itself reports success. This bit us for real: picking
  // an entity from the dropdown and pressing Enter (instead of clicking the
  // small Add button) saved the report with no error and no new entity
  // attached. So Enter is handled explicitly here instead of left to fall
  // through to the browser's default:
  //   - dropdown open with results showing -> Enter accepts the first
  //     suggestion, exactly like clicking it, then also clicks this
  //     picker's own "<idPrefix>-add-btn" if one exists (report linking) so
  //     Enter alone completes the same two steps a mouse user would take.
  //   - dropdown closed and an "<idPrefix>-add-btn" exists -> Enter clicks
  //     it (covers "picked earlier, pressed Enter later").
  //   - no such Add button for this picker (e.g. the relationship form,
  //     which has no per-field Add step) -> Enter is left alone so the
  //     browser's default "submit the enclosing form" behavior — which IS
  //     the intended action there — still happens.
  input.addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    const addBtn = document.getElementById(`${idPrefix}-add-btn`);
    if (!results.hidden) {
      const first = results.querySelector(".entity-picker-item");
      if (first) {
        e.preventDefault();
        selectItem(first);
        if (addBtn) addBtn.click();
        return;
      }
    }
    if (addBtn) {
      e.preventDefault();
      addBtn.click();
    }
  });

  input.addEventListener("blur", () => {
    // Give a result's mousedown handler (above) a chance to run first —
    // otherwise blur would close the dropdown before the click registers.
    setTimeout(closeResults, 150);
  });
  input.addEventListener("focus", () => {
    if (results.innerHTML) results.hidden = false;
  });
}

/* ============================================================================
 * Auth
 * ========================================================================== */

async function init() {
  // First, and unauthenticated: the login screen carries the instance name,
  // and it is drawn before anyone has a session.
  await loadBranding();

  try {
    const status = await api("/api/auth/bootstrap-status");
    if (status.needs_bootstrap) {
      showAuthScreen("bootstrap");
      return;
    }
  } catch (e) { /* fall through to trying /me */ }

  try {
    state.user = await api("/api/auth/me");
    showApp();
  } catch (e) {
    showAuthScreen("login");
  }
}

function showAuthScreen(which) {
  document.getElementById("auth-screen").hidden = false;
  document.getElementById("app").hidden = true;
  document.getElementById("bootstrap-form").hidden = which !== "bootstrap";
  document.getElementById("login-form").hidden = which !== "login";
}

function showApp() {
  state.sessionExpiredHandled = false;
  document.getElementById("auth-screen").hidden = true;
  document.getElementById("app").hidden = false;

  const isAdmin = state.user.role === "admin";
  document.getElementById("whoami").textContent = state.user.username;
  document.getElementById("user-avatar").textContent =
    (state.user.username || "?").trim().charAt(0).toUpperCase();
  document.getElementById("user-menu-username").textContent = state.user.username;
  document.getElementById("user-menu-role").textContent = isAdmin ? "Administrator" : "Analyst";
  document.getElementById("user-menu-admin").hidden = !isAdmin;
  document.getElementById("user-menu-audit").hidden = !isAdmin;
  document.getElementById("assistant-tab-btn").hidden = false;

  // Before switchView, so the dashboard's first render already has the
  // account's arrangement rather than flashing the default one.
  loadDashLayoutFromAccount();
  // Same reason, and same storage model: the look follows the person.
  loadPaletteFromAccount();

  switchView("dashboard");
  refreshQueueBadges();
  startClock();
  syncTopbarHeight(); // only measurable now that #app is no longer hidden
}

/* ---------------------------------------------------------------------------
 * Theme
 *
 * Three stored states — "system", "light", "dark" — resolved to a real
 * data-theme value on <html>. The initial resolve happens in an inline script
 * in index.html so there is no flash of the wrong theme on load; this code
 * handles changing it afterwards, and keeps System honest by following the OS
 * if it flips while the app is open.
 * ------------------------------------------------------------------------- */

const THEME_STORAGE_KEY = "humint.theme";
const prefersLight = window.matchMedia
  ? window.matchMedia("(prefers-color-scheme: light)")
  : null;

function storedTheme() {
  try { return localStorage.getItem(THEME_STORAGE_KEY) || "system"; }
  catch (e) { return "system"; }
}

function resolveTheme(choice) {
  if (choice === "light" || choice === "dark") return choice;
  return prefersLight && prefersLight.matches ? "light" : "dark";
}

function applyTheme(choice) {
  const effective = resolveTheme(choice);
  document.documentElement.setAttribute("data-theme", effective);
  document.querySelectorAll("[data-theme-choice]").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.themeChoice === choice);
  });
  // The relationship graph is drawn on a canvas by vis, which cannot read CSS
  // variables — it has to be told the new colours and redrawn.
  if (state.currentEntityId && document.getElementById("entity-graph")) {
    restyleEntityGraph();
  }
}

function setTheme(choice) {
  try { localStorage.setItem(THEME_STORAGE_KEY, choice); }
  catch (e) { /* private window — the choice just won't survive a reload */ }
  applyTheme(choice);
}

document.querySelectorAll("[data-theme-choice]").forEach((btn) => {
  btn.addEventListener("click", (e) => {
    e.stopPropagation();  // or the document listener closes the menu mid-choice
    setTheme(btn.dataset.themeChoice);
  });
});

// Only meaningful while the choice is "system": an OS-level switch (a Mac
// going dark at sunset) should follow through without a reload.
if (prefersLight) {
  const onSystemChange = () => { if (storedTheme() === "system") applyTheme("system"); };
  if (prefersLight.addEventListener) prefersLight.addEventListener("change", onSystemChange);
  else if (prefersLight.addListener) prefersLight.addListener(onSystemChange);
}

applyTheme(storedTheme());

/* ---------------------------------------------------------------------------
 * Palette
 *
 * The second appearance axis. Where light/dark is about the room the screen is
 * in and therefore stays per-browser, the palette is about the person and
 * their organisation, so it lives on the account and follows them between
 * machines — the Chromebook-per-site case the dashboard layout was moved for.
 *
 * The localStorage copy is a cache, not the authority: it exists only so the
 * pre-paint script has something to stamp before the account has answered, and
 * it is keyed per username so two people sharing a machine don't inherit each
 * other's look.
 * ------------------------------------------------------------------------- */

const PALETTES = ["terminal", "slate", "graphite", "archive"];
const PALETTE_CACHE_KEY = "humint.palette.last";
let instanceDefaultPalette = "terminal";

function paletteCacheKey() {
  return `humint.palette.${(state.user && state.user.username) || "anonymous"}`;
}

function applyPalette(palette) {
  const valid = PALETTES.includes(palette) ? palette : "terminal";
  document.documentElement.setAttribute("data-palette", valid);
  function clearPaletteCache() {
  // Only this user's keyed copy. The un-keyed pre-paint hint is deliberately
  // left: it stops the next person on a shared machine seeing a flash of the
  // stock palette before their own arrives, and it reveals nothing — it is a
  // colour scheme, not a name.
  try { localStorage.removeItem(paletteCacheKey()); } catch (e) { /* ignore */ }
}

document.querySelectorAll("[data-palette-choice]").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.paletteChoice === valid);
  });
  try {
    localStorage.setItem(paletteCacheKey(), valid);
    // A second, un-keyed copy for the pre-paint script, which runs before we
    // know who is logging in. Last writer wins, which is right: the last
    // person to use this browser is the one most likely to use it next.
    localStorage.setItem(PALETTE_CACHE_KEY, valid);
  } catch (e) { /* private window — it just won't survive a reload */ }
  // vis draws the relationship graph on a canvas and cannot read CSS
  // variables, so it has to be told the new colours and redrawn.
  if (state.currentEntityId && document.getElementById("entity-graph")) {
    restyleEntityGraph();
  }
}

async function setPalette(palette) {
  applyPalette(palette);
  try {
    await api("/api/me/preferences", {
      method: "PATCH", body: { preferences: { palette } },
    });
  } catch (err) {
    // Applied locally either way — refusing to change the look because the
    // save failed would be the wrong trade. Say so, though, rather than
    // letting it silently revert on the next machine they sit down at.
    showToast(`Theme applied, but not saved to your account: ${err.message}`, true);
  }
}

async function loadPaletteFromAccount() {
  let cached = null;
  try { cached = localStorage.getItem(paletteCacheKey()); } catch (e) { /* ignore */ }
  applyPalette(cached || instanceDefaultPalette);
  try {
    const prefs = (await api("/api/me/preferences")).preferences || {};
    if (prefs.palette) {
      applyPalette(prefs.palette);
    } else {
      // No choice of their own yet: they get whatever the admin set as the
      // instance default, and it is NOT written to their account — so an admin
      // who later changes the instance default still moves everybody who has
      // never expressed a preference, rather than only new accounts.
      applyPalette(instanceDefaultPalette);
    }
  } catch (err) { /* the cache or the instance default stands */ }
}

function clearPaletteCache() {
  // Only this user's keyed copy. The un-keyed pre-paint hint is deliberately
  // left: it stops the next person on a shared machine seeing a flash of the
  // stock palette before their own arrives, and it reveals nothing — it is a
  // colour scheme, not a name.
  try { localStorage.removeItem(paletteCacheKey()); } catch (e) { /* ignore */ }
}

document.querySelectorAll("[data-palette-choice]").forEach((btn) => {
  btn.addEventListener("click", (e) => {
    e.stopPropagation();  // or the document listener closes the menu mid-choice
    setPalette(btn.dataset.paletteChoice);
  });
});

/* ---------------------------------------------------------------------------
 * Branding
 *
 * The instance name and an optional brand accent, both set by an admin. Read
 * from an unauthenticated endpoint because the login screen needs the name
 * before anyone has logged in.
 * ------------------------------------------------------------------------- */

const BRAND_TOKENS_KEY = "humint.brand.tokens";

function hexToRgb(hex) {
  const m = /^#([0-9a-fA-F]{6})$/.exec((hex || "").trim());
  if (!m) return null;
  const n = parseInt(m[1], 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

function relativeLuminance([r, g, b]) {
  const f = (c) => {
    c /= 255;
    return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
  };
  return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
}

function contrastRatio(a, b) {
  const la = relativeLuminance(a);
  const lb = relativeLuminance(b);
  return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
}

function mix(rgb, target, amount) {
  return rgb.map((c, i) => Math.round(c + (target[i] - c) * amount));
}

function toHex(rgb) {
  return "#" + rgb.map((c) => Math.max(0, Math.min(255, c)).toString(16).padStart(2, "0")).join("");
}

/* An organisation has one brand colour, not five. This derives the rest:
 *
 *   --accent        the colour as given, used for links and outlines
 *   --accent-dim    the fill behind primary buttons and the avatar
 *   --accent-light  hover/emphasis
 *   --on-accent     the label colour ON that fill — chosen by measuring, not
 *                   by assuming, because a brand colour can be anything from
 *                   navy to lime and guessing white gets one of those wrong
 *
 * If the given colour is too light to carry white text, the fill is darkened
 * until the pair clears WCAG AA rather than shipping a button whose label
 * cannot be read. The link colour itself is left as given: it is the
 * organisation's colour and darkening it silently would be presumptuous, and
 * the contrast warning in the admin form is the honest way to raise it. */
function deriveBrandTokens(accentHex) {
  const rgb = hexToRgb(accentHex);
  if (!rgb) return null;
  const white = [255, 255, 255];
  const black = [0, 0, 0];

  let fill = rgb;
  let label = contrastRatio(fill, white) >= contrastRatio(fill, black) ? white : black;
  // Darken (or lighten, for a near-black brand colour) until the label on the
  // fill clears 4.5:1. Capped at 20 steps so a pathological input terminates.
  for (let i = 0; i < 20 && contrastRatio(fill, label) < 4.5; i++) {
    fill = mix(fill, label === white ? black : white, 0.08);
  }
  return {
    "--accent": accentHex,
    "--accent-dim": toHex(fill),
    "--accent-light": toHex(mix(rgb, white, 0.28)),
    "--accent-hover": toHex(mix(rgb, white, 0.16)),
    "--accent-rgb": rgb.join(","),
    "--on-accent": toHex(label),
  };
}

function applyBrandAccent(accentHex) {
  const root = document.documentElement;
  const tokens = accentHex ? deriveBrandTokens(accentHex) : null;
  ["--accent", "--accent-dim", "--accent-light", "--accent-hover",
   "--accent-rgb", "--on-accent"].forEach((k) => root.style.removeProperty(k));
  if (tokens) {
    Object.entries(tokens).forEach(([k, v]) => root.style.setProperty(k, v));
  }
  try {
    if (tokens) localStorage.setItem(BRAND_TOKENS_KEY, JSON.stringify(tokens));
    else localStorage.removeItem(BRAND_TOKENS_KEY);
  } catch (e) { /* private window */ }
  if (state.currentEntityId && document.getElementById("entity-graph")) {
    restyleEntityGraph();
  }
}

function applyInstanceName(name) {
  const safe = (name || "").trim() || "HUMINT Platform";
  document.title = safe;
  const badge = document.getElementById("brand-name");
  if (badge) badge.textContent = safe;
  const loginHeading = document.getElementById("login-brand-name");
  if (loginHeading) loginHeading.textContent = safe;
}

async function loadBranding() {
  try {
    const b = await api("/api/branding");
    applyInstanceName(b.instance_name);
    applyBrandAccent(b.brand_accent);
    if (PALETTES.includes(b.default_palette)) {
      instanceDefaultPalette = b.default_palette;
    }
    return b;
  } catch (err) {
    // An unreachable branding endpoint must not stop anyone logging in. The
    // markup already contains the stock name, so the page is never blank.
    return null;
  }
}

// Colours for anything drawn outside CSS (the vis graph). Read from the live
// computed style rather than hardcoded, so these can never drift from the
// stylesheet the way a second copy of a palette always does.
function themeColor(name, fallback) {
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return value || fallback;
}

/* ---------------------------------------------------------------------------
 * Account menu (top right). Everything that's about the person using the app
 * rather than about the case lives here.
 * ------------------------------------------------------------------------- */

function toggleUserMenu(open) {
  const dropdown = document.getElementById("user-menu-dropdown");
  const btn = document.getElementById("user-menu-btn");
  const shouldOpen = open === undefined ? dropdown.hidden : open;
  dropdown.hidden = !shouldOpen;
  btn.setAttribute("aria-expanded", String(shouldOpen));
  btn.classList.toggle("open", shouldOpen);
}

document.getElementById("user-menu-btn").addEventListener("click", (e) => {
  e.stopPropagation(); // or the document listener below would immediately re-close it
  toggleUserMenu();
});
// Clicking anywhere else closes it, which is what every menu like this does
// and what people expect without being told.
document.addEventListener("click", (e) => {
  if (!document.getElementById("user-menu").contains(e.target)) toggleUserMenu(false);
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") toggleUserMenu(false);
});

document.getElementById("user-menu-admin").addEventListener("click", () => {
  toggleUserMenu(false);
  switchView("admin");
});

document.getElementById("user-menu-audit").addEventListener("click", () => {
  toggleUserMenu(false);
  switchView("audit");
});

document.getElementById("bootstrap-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    state.user = await api("/api/auth/register", {
      method: "POST",
      body: {
        username: document.getElementById("bootstrap-username").value,
        password: document.getElementById("bootstrap-password").value,
      },
    });
    showApp();
  } catch (err) {
    document.getElementById("bootstrap-error").textContent = err.message;
  }
});

document.getElementById("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    state.user = await api("/api/auth/login", {
      method: "POST",
      body: {
        username: document.getElementById("login-username").value,
        password: document.getElementById("login-password").value,
      },
    });
    showApp();
  } catch (err) {
    document.getElementById("login-error").textContent = err.message;
  }
});

document.getElementById("user-menu-logout").addEventListener("click", async () => {
  toggleUserMenu(false);
  // The cache is only a cache — the account holds the real copy — so dropping
  // it on the way out costs nothing and leaves a shared device clean for
  // whoever logs in next.
  clearDashLayoutCache();
  clearPaletteCache();
  try { await api("/api/auth/logout", { method: "POST" }); } catch (e) { /* ignore */ }
  location.reload();
});

function openNewUserForm(onSuccess) {
  const html = `
    <h2>Create user</h2>
    <form id="new-user-form">
      <div class="form-row"><label>Username</label><input type="text" id="nu-username" required minlength="3" maxlength="64"></div>
      <div class="form-row"><label>Password</label><input type="password" id="nu-password" required minlength="10" maxlength="256"></div>
      <div class="form-row"><label>Role</label>
        <select id="nu-role"><option value="analyst">Analyst</option><option value="admin">Admin</option></select>
      </div>
      <p class="form-error" id="nu-error"></p>
      <div class="modal-actions">
        <button type="button" class="btn-secondary" id="nu-cancel">Cancel</button>
        <button type="submit" class="btn-primary">Create</button>
      </div>
    </form>
  `;
  openModal(html);
  document.getElementById("nu-cancel").addEventListener("click", closeModal);
  document.getElementById("new-user-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      await api("/api/auth/register", {
        method: "POST",
        body: {
          username: document.getElementById("nu-username").value,
          password: document.getElementById("nu-password").value,
          role: document.getElementById("nu-role").value,
        },
      });
      closeModal();
      showToast("User created");
      if (onSuccess) onSuccess();
    } catch (err) {
      document.getElementById("nu-error").textContent = err.message;
    }
  });
}
// The topbar's "+ User" shortcut is gone — creating an account is a rare,
// deliberately admin-only action, and it already has a button on the Admin
// page next to the user list it affects. See admin-page-new-user-btn below.

/* ============================================================================
 * Navigation
 * ========================================================================== */

document.querySelectorAll(".tab-btn").forEach((btn) => {
  btn.addEventListener("click", () => switchView(btn.dataset.view));
});
document.querySelectorAll(".back-link").forEach((btn) => {
  btn.addEventListener("click", () => switchView(btn.dataset.back));
});

function setActiveTab(view) {
  document.querySelectorAll(".tab-btn").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
}

function switchView(view) {
  // Leaving the editor mid-interview is the one navigation in this app that
  // can lose real work, so it's the one that asks first.
  if (state.reportEditorOpen && view !== "report-editor" && reportEditor.dirty) {
    if (!confirm("You have unsaved changes to this report. Leave without saving?")) return;
  }
  if (view !== "report-editor") {
    state.reportEditorOpen = false;
    stopAutosave();
  }
  // Same idea for the debrief, with a softer warning: its notes survive in the
  // local draft, so leaving loses the place rather than the work.
  if (state.debriefOpen && view !== "debrief" && debrief.dirty) {
    if (!confirm("Leave this debrief? Your notes are kept for next time.")) return;
  }
  if (view !== "debrief") state.debriefOpen = false;
  setActiveTab(view);
  switchViewRaw(view);
  if (view === "dashboard") loadDashboard();
  if (view === "entities") loadEntities();
  if (view === "reports") loadReports();
  if (view === "documents") loadDocuments();
  if (view === "map") loadMap();
  if (view === "review") loadReviewSubview();
  // Only the section being shown loads; see showAdminSection.
  if (view === "admin") openAdminSections();
  // Arriving at the audit page always shows the newest events. Filters stay
  // as they were, but a stale page-5 offset from an earlier visit would be
  // a confusing thing to land on when you open "Audit log" from the menu.
  if (view === "audit") { auditOffset = 0; loadAuditFilterOptions(); loadAuditLog(); }
}

function switchViewRaw(view) {
  document.querySelectorAll(".view").forEach((v) => v.classList.remove("active"));
  document.getElementById("view-" + view).classList.add("active");
}

/* ---------------------------------------------------------------------------
 * Review sub-tabs (extraction / correlation)
 *
 * Only the visible queue is fetched, not both — switching sub-tab is what
 * loads the other one. Two list endpoints on every visit to this tab would
 * be wasted work for whichever queue you weren't looking at.
 * ------------------------------------------------------------------------- */

function loadReviewSubview() {
  const active = document.querySelector(".subtab-btn.active");
  const which = active ? active.dataset.subtab : "extraction";
  if (which === "extraction") loadExtractionQueue();
  else loadCorrelationQueue();
  checkOllamaStatusBanner();
}

// Both queues are fed by Ollama, and all three ways that can be misconfigured
// — switched off, unreachable, or naming a model that was never pulled —
// look identical from here: an empty queue. This turns that silence into a
// sentence. Available to analysts as well as admins (see /api/ollama-status),
// since the person wondering why nothing has come through is usually not the
// person who configured it.
async function checkOllamaStatusBanner(elementId) {
  const el = document.getElementById(elementId || "review-ollama-banner");
  if (!el) return;
  let status;
  try {
    status = await api("/api/ollama-status");
  } catch (e) {
    el.hidden = true;
    return;
  }

  const isAdmin = state.user && state.user.role === "admin";
  const fix = isAdmin
    ? " Fix it under Admin settings → Ollama Settings."
    : " Ask an admin to check the Ollama settings.";

  let message = "";
  if (!status.enabled) {
    message = "Ollama is switched off, so nothing is being proposed for review." + fix;
  } else if (!status.reachable) {
    message = "Ollama can't be reached, so nothing is being proposed for review." + fix;
  } else if (status.extract_model_installed === false) {
    // Extraction has had its own model since the three-way split, and naming
    // the assistant's model here would send someone to pull the wrong one.
    message = `The extraction model (${escapeHtml(status.extract_model || status.model)}) isn't installed on the Ollama host, so documents produce no proposals.` + fix;
  } else if (status.model_installed === false) {
    message = `The assistant model (${escapeHtml(status.model)}) isn't installed on the Ollama host, so the assistant can't answer.` + fix;
  } else if (status.embed_model && status.embed_model_installed === false) {
    message = `The configured embedding model (${escapeHtml(status.embed_model)}) isn't installed, so correlation is off.` + fix;
  }

  if (!message) { el.hidden = true; el.innerHTML = ""; return; }
  el.hidden = false;
  el.innerHTML = `<div class="warn-banner">${message}</div>`;
}

document.querySelectorAll(".subtab-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".subtab-btn").forEach((b) => b.classList.toggle("active", b === btn));
    document.querySelectorAll(".subview").forEach((v) => {
      v.classList.toggle("active", v.id === "subview-" + btn.dataset.subtab);
    });
    loadReviewSubview();
  });
});

async function refreshQueueBadges() {
  try {
    const [ext, corr] = await Promise.all([
      api("/api/extraction-suggestions?status=pending&limit=1"),
      api("/api/correlation-suggestions?status=pending&limit=1"),
    ]);
    setBadge("extraction-badge", ext.total);
    setBadge("correlation-badge", corr.total);
    // The tab itself carries the combined count — with both queues behind one
    // tab, "3" on Review has to mean "3 things need you," not "3 of one kind."
    setBadge("review-badge", (ext.total || 0) + (corr.total || 0));
  } catch (e) { /* badges are a nicety, not worth surfacing an error for */ }
}
function setBadge(id, count) {
  const el = document.getElementById(id);
  el.textContent = count;
  el.hidden = count === 0;
}
setInterval(() => { if (state.user) refreshQueueBadges(); }, 30000);

/* ============================================================================
 * Dashboard: clock, at-a-glance stats, recent activity
 * ========================================================================== */

let clockStarted = false;
function startClock() {
  if (clockStarted) return; // only ever start the interval once per page load
  clockStarted = true;
  const tick = () => {
    const now = new Date();
    const clockEl = document.getElementById("dash-clock");
    const dateEl = document.getElementById("dash-date");
    const utcEl = document.getElementById("dash-utc");
    if (clockEl) clockEl.textContent = now.toLocaleTimeString();
    if (dateEl) dateEl.textContent = now.toLocaleDateString(undefined, { weekday: "long", year: "numeric", month: "long", day: "numeric" });
    if (utcEl) utcEl.textContent = "UTC " + now.toISOString().slice(11, 19);
  };
  tick();
  setInterval(tick, 1000);
}

/* ---------------------------------------------------------------------------
 * Dashboard panels: layout, and the analytics that fill them
 *
 * Everything on this page is computed from the case file with plain SQL (see
 * api/insights.py). No model, no embeddings, nothing that stops working on a
 * Pi with Ollama switched off — which is the whole point of these panels. The
 * extraction and correlation queues are the only model-dependent parts of this
 * app, and none of this is either of them.
 *
 * Every figure is also explainable: a hotspot lists the records that made it
 * hot, a rising name shows both counts it was derived from. An analyst can
 * disagree with any number here by clicking through to the rows behind it.
 * ------------------------------------------------------------------------- */

// The layout lives on the user's account (PATCH /api/me/preferences), not in
// the browser: teams keep Chromebooks at each site, so an arrangement stored
// per-device would neither follow an analyst between sites nor stay out of a
// colleague's way on a shared machine.
//
// A local copy is still kept, but only as a cache — it lets the dashboard
// paint immediately instead of waiting on a round trip, and keeps the panels
// arrangeable if the save fails. It is keyed by username so a shared device
// never shows one analyst the other's arrangement, and cleared on logout.
const DASH_LAYOUT_KEY = "humint.dashboard.layout";

// id -> label, in the default order. The order of this list IS the default
// layout, and anything added here later appears for existing users too (see
// mergeDashLayout) rather than being invisible because their stored order
// predates it.
const DASH_PANELS = [
  ["attention", "Needs attention"],
  ["hotspots", "Hotspots"],
  ["clock", "Clock"],
  ["glance", "At a glance"],
  ["tempo", "Reporting tempo"],
  ["rising", "Rising names"],
  ["recent-reports", "Recent reports"],
  ["recent-entities", "Recent entities"],
];
const DASH_PANEL_IDS = DASH_PANELS.map(([id]) => id);

let dashLayout = { order: [...DASH_PANEL_IDS], hidden: [], window: 14 };

function dashCacheKey() {
  return `${DASH_LAYOUT_KEY}.${(state.user && state.user.username) || "anonymous"}`;
}

function readDashLayout() {
  try {
    const raw = JSON.parse(localStorage.getItem(dashCacheKey()) || "null");
    return raw && typeof raw === "object" ? raw : null;
  } catch (e) { return null; }
}

function clearDashLayoutCache() {
  try {
    // Both the current key and the pre-account one, so an upgrade doesn't
    // leave a stray layout behind on a shared machine.
    localStorage.removeItem(dashCacheKey());
    localStorage.removeItem(DASH_LAYOUT_KEY);
  } catch (e) { /* nothing to clean up */ }
}

let dashSaveTimer = null;

function saveDashLayout() {
  try { localStorage.setItem(dashCacheKey(), JSON.stringify(dashLayout)); }
  catch (e) { /* private window — the server copy is still authoritative */ }
  // Debounced: dragging a panel through three positions is one intent, not
  // three saves, and the arrows are easy to click repeatedly.
  clearTimeout(dashSaveTimer);
  dashSaveTimer = setTimeout(pushDashLayout, 600);
}

async function pushDashLayout() {
  try {
    await api("/api/me/preferences", {
      method: "PATCH",
      body: { preferences: { dashboard: dashLayout } },
    });
    setDashLayoutNote("");
  } catch (err) {
    // The layout still works for this session from the local cache; say so
    // rather than pretending it saved.
    setDashLayoutNote("Layout not saved to your account — " + err.message);
  }
}

function setDashLayoutNote(text) {
  const el = document.getElementById("dash-layout-note");
  if (!el) return;
  el.textContent = text || "";
  el.hidden = !text;
}

/* Called once the user is known: the account's copy wins over the local
   cache, which exists only so the first paint isn't blank. */
async function loadDashLayoutFromAccount() {
  const cached = mergeDashLayout(readDashLayout());
  dashLayout = cached;
  applyDashLayout();
  try {
    const data = await api("/api/me/preferences");
    const stored = (data.preferences || {}).dashboard;
    if (stored) {
      dashLayout = mergeDashLayout(stored);
    } else {
      // Nothing on the account yet. If this browser has a layout from before
      // layouts were an account thing, adopt it rather than throwing away an
      // arrangement someone made.
      dashLayout = cached;
      pushDashLayout();
    }
  } catch (e) {
    setDashLayoutNote("Using this device's layout — couldn't reach your account settings.");
  }
  const windowSelect = document.getElementById("dash-window");
  if (windowSelect) windowSelect.value = String(dashLayout.window);
  applyDashLayout();
}

// A stored layout is a list of ids from whenever it was saved. Merging rather
// than trusting it outright means a panel added in a later version still shows
// up for someone who arranged their dashboard months ago, and an id that no
// longer exists is dropped instead of leaving a hole.
function mergeDashLayout(stored) {
  const known = new Set(DASH_PANEL_IDS);
  const order = (stored && Array.isArray(stored.order) ? stored.order : []).filter((id) => known.has(id));
  DASH_PANEL_IDS.forEach((id) => { if (!order.includes(id)) order.push(id); });
  const hidden = (stored && Array.isArray(stored.hidden) ? stored.hidden : []).filter((id) => known.has(id));
  const win = stored && [7, 14, 30, 90].includes(Number(stored.window)) ? Number(stored.window) : 14;
  return { order, hidden, window: win };
}

function applyDashLayout() {
  const grid = document.getElementById("dashboard-grid");
  if (!grid) return;
  dashLayout.order.forEach((id) => {
    const panel = grid.querySelector(`[data-panel="${id}"]`);
    if (!panel) return;
    panel.hidden = dashLayout.hidden.includes(id);
    grid.appendChild(panel);   // appending in order IS the reorder
  });
  renderDashCustomiseList();
}

function renderDashCustomiseList() {
  const list = document.getElementById("dash-customise-list");
  if (!list) return;
  const labels = Object.fromEntries(DASH_PANELS);
  list.innerHTML = dashLayout.order.map((id, i) => `
    <li class="dash-customise-row">
      <label><input type="checkbox" data-panel-toggle="${id}"
        ${dashLayout.hidden.includes(id) ? "" : "checked"}> ${escapeHtml(labels[id] || id)}</label>
      <span class="dash-customise-move">
        <button type="button" class="btn-link btn-sm" data-panel-up="${id}" ${i === 0 ? "disabled" : ""} title="Move up">▲</button>
        <button type="button" class="btn-link btn-sm" data-panel-down="${id}" ${i === dashLayout.order.length - 1 ? "disabled" : ""} title="Move down">▼</button>
      </span>
    </li>`).join("");

  list.querySelectorAll("[data-panel-toggle]").forEach((box) => {
    box.addEventListener("change", () => {
      const id = box.dataset.panelToggle;
      dashLayout.hidden = box.checked
        ? dashLayout.hidden.filter((x) => x !== id)
        : [...dashLayout.hidden, id];
      saveDashLayout();
      applyDashLayout();
    });
  });
  list.querySelectorAll("[data-panel-up]").forEach((btn) => {
    btn.addEventListener("click", () => moveDashPanel(btn.dataset.panelUp, -1));
  });
  list.querySelectorAll("[data-panel-down]").forEach((btn) => {
    btn.addEventListener("click", () => moveDashPanel(btn.dataset.panelDown, 1));
  });
}

function moveDashPanel(id, delta) {
  const from = dashLayout.order.indexOf(id);
  const to = from + delta;
  if (from < 0 || to < 0 || to >= dashLayout.order.length) return;
  dashLayout.order.splice(to, 0, ...dashLayout.order.splice(from, 1));
  saveDashLayout();
  applyDashLayout();
}

// Drag a panel by its title onto another panel to drop it there. The arrows in
// the customise drawer do the same job for touchscreens and keyboards, where
// HTML5 drag-and-drop is either unavailable or miserable.
function wireDashDragAndDrop() {
  const grid = document.getElementById("dashboard-grid");
  if (!grid) return;
  let draggingId = null;

  grid.querySelectorAll(".dash-panel .dash-card-title[draggable]").forEach((title) => {
    const panel = title.closest(".dash-panel");
    title.addEventListener("dragstart", (e) => {
      draggingId = panel.dataset.panel;
      panel.classList.add("dragging");
      if (e.dataTransfer) {
        e.dataTransfer.effectAllowed = "move";
        e.dataTransfer.setData("text/plain", draggingId);
      }
    });
    title.addEventListener("dragend", () => {
      panel.classList.remove("dragging");
      draggingId = null;
      grid.querySelectorAll(".drop-target").forEach((el) => el.classList.remove("drop-target"));
    });
  });

  grid.querySelectorAll(".dash-panel").forEach((panel) => {
    panel.addEventListener("dragover", (e) => {
      if (!draggingId || panel.dataset.panel === draggingId) return;
      e.preventDefault();
      panel.classList.add("drop-target");
    });
    panel.addEventListener("dragleave", () => panel.classList.remove("drop-target"));
    panel.addEventListener("drop", (e) => {
      e.preventDefault();
      panel.classList.remove("drop-target");
      const movedId = draggingId || (e.dataTransfer && e.dataTransfer.getData("text/plain"));
      const targetId = panel.dataset.panel;
      if (!movedId || movedId === targetId) return;
      const order = dashLayout.order.filter((x) => x !== movedId);
      order.splice(order.indexOf(targetId), 0, movedId);
      dashLayout.order = order;
      saveDashLayout();
      applyDashLayout();
    });
  });
}

/* --- the analytics panels ------------------------------------------------- */

function attentionListHtml(items, emptyText, rowFn) {
  if (!items.length) return `<li class="empty-state">${escapeHtml(emptyText)}</li>`;
  return items.map(rowFn).join("");
}

function renderAttention(na) {
  const el = document.getElementById("dash-attention");
  if (!el) return;
  el.innerHTML = `
    <div class="attention-block">
      <div class="attention-title">Urgent <span class="attention-count">${na.urgent_reports.length}</span></div>
      <ul class="dash-list">${attentionListHtml(na.urgent_reports, "Nothing filed Flash or Immediate.", (r) => `
        <li data-open-report="${escapeHtml(r.id)}">
          ${criticalityBadgeHtml(r)} ${escapeHtml(r.title)}
          <div class="dash-list-meta">${escapeHtml(r.status)} · ${timeAgoHtml(r.created_at)}</div>
        </li>`)}</ul>
    </div>
    <div class="attention-block">
      <div class="attention-title">Forgotten drafts <span class="attention-count">${na.stale_drafts.length}</span></div>
      <ul class="dash-list">${attentionListHtml(na.stale_drafts, `No draft untouched for ${na.stale_draft_days} days.`, (r) => `
        <li data-open-report="${escapeHtml(r.id)}">
          ${escapeHtml(r.title)}
          <div class="dash-list-meta">last touched ${timeAgoHtml(r.updated_at)}</div>
        </li>`)}</ul>
    </div>
    <div class="attention-block">
      <div class="attention-title">Ageing out <span class="attention-count">${na.expiring_events.length}</span></div>
      <ul class="dash-list">${attentionListHtml(na.expiring_events, `Nothing lapsing within ${na.expiring_within_days} days.`, (e) => `
        <li data-open-entity="${escapeHtml(e.id)}">
          ${e.already_expired ? '<span class="status-badge status-expired">expired</span> ' : ""}${escapeHtml(e.name)}
          <div class="dash-list-meta">relevant until ${escapeHtml(e.expires_at)}</div>
        </li>`)}</ul>
    </div>
    <div class="attention-block">
      <div class="attention-title">People <span class="attention-count">${na.people_of_concern.length}</span></div>
      <ul class="dash-list">${attentionListHtml(na.people_of_concern, "Nobody recorded as captured, detained, missing or evading.", (p) => `
        <li data-open-entity="${escapeHtml(p.id)}">
          ${personStatusBadgesHtml({ ...p, entity_type: "person" })} ${escapeHtml(p.name)}
        </li>`)}</ul>
    </div>`;
  wireDashLinks(el);
}

function renderHotspots(hotspots, windowDays) {
  const el = document.getElementById("dash-hotspots");
  if (!el) return;
  if (!hotspots.length) {
    el.innerHTML = `<p class="empty-state">No location has three or more linked items in the last ${windowDays} days.</p>`;
    return;
  }
  const busiest = hotspots[0].total;
  el.innerHTML = `<ul class="hotspot-list">${hotspots.map((h) => `
    <li class="hotspot">
      <div class="hotspot-head">
        <a href="#" data-open-entity="${escapeHtml(h.id)}">${escapeHtml(h.name)}</a>
        <span class="hotspot-bar"><span style="width:${Math.round((h.total / busiest) * 100)}%"></span></span>
        <span class="hotspot-count">${h.total}</span>
        <button type="button" class="hotspot-export" data-export-hotspot-id="${escapeHtml(h.id)}"
                title="Export this hotspot and everything linked to it">PDF</button>
      </div>
      <div class="dash-list-meta">
        ${h.event_count} event${h.event_count === 1 ? "" : "s"} ·
        ${h.report_count} report${h.report_count === 1 ? "" : "s"} ·
        across ${h.active_days} day${h.active_days === 1 ? "" : "s"} ·
        last ${timeAgoHtml(h.last_seen)}
      </div>
      <ul class="hotspot-records">${h.records.slice(0, 4).map((rec) => `
        <li ${rec.kind === "report" ? `data-open-report="${escapeHtml(rec.id)}"` : `data-open-entity="${escapeHtml(rec.id)}"`}>
          <span class="type-pill type-${rec.kind === "report" ? "source" : "event"}">${rec.kind}</span>
          ${escapeHtml(truncate(rec.label || "", 58))}
        </li>`).join("")}${h.total > 4 ? `<li class="hotspot-more">+ ${h.total - 4} more</li>` : ""}</ul>
    </li>`).join("")}</ul>`;
  wireDashLinks(el);
  // Exported over the window the panel is currently showing, so the package a
  // person gets is the one they were just looking at rather than a default.
  el.querySelectorAll("[data-export-hotspot-id]").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      startExport(
        `/api/exports/hotspots/${encodeURIComponent(btn.dataset.exportHotspotId)}`
        + `/package.pdf?days=${windowDays}`,
        "Hotspot package");
    });
  });
}

function renderTempo(tempo) {
  const el = document.getElementById("dash-tempo");
  if (!el) return;
  const weeks = tempo.weeks || [];
  const peak = Math.max(1, tempo.peak || 0);
  // Bars in CSS rather than a charting library: the CDN ones don't load on an
  // air-gapped deployment, and twelve bars do not need a library.
  const bars = weeks.map((w) => {
    const height = Math.round((w.reports / peak) * 100);
    return `<span class="tempo-bar" title="Week of ${escapeHtml(w.week_start)}: ${w.reports} report${w.reports === 1 ? "" : "s"}">
      <span style="height:${Math.max(w.reports ? 6 : 2, height)}%"></span>
    </span>`;
  }).join("");
  const current = tempo.current || 0;
  const median = tempo.median || 0;
  let verdict = "in line with the last few weeks";
  if (current > median * 2 && current >= 3) verdict = "well above the recent norm";
  else if (current > median) verdict = "above the recent norm";
  else if (current < median) verdict = "below the recent norm";
  el.innerHTML = `
    <div class="tempo-chart">${bars}</div>
    <div class="dash-list-meta">This week: <strong>${current}</strong> ·
      median of the previous weeks: ${median} — ${escapeHtml(verdict)}.</div>`;
}

function renderRising(rising, windowDays) {
  const el = document.getElementById("dash-rising");
  if (!el) return;
  el.innerHTML = rising.length
    ? rising.map((x) => `
      <li data-open-entity="${escapeHtml(x.id)}">
        <span class="type-pill type-${escapeHtml(x.entity_type)}">${escapeHtml(x.entity_type)}</span>
        ${escapeHtml(x.name)}
        <div class="dash-list-meta">${x.current} report${x.current === 1 ? "" : "s"} this ${windowDays} days,
          ${x.previous} the ${windowDays} before — <strong>+${x.delta}</strong></div>
      </li>`).join("")
    : '<li class="empty-state">Nothing named more often than in the previous window.</li>';
  wireDashLinks(el);
}

function wireDashLinks(el) {
  el.querySelectorAll("[data-open-report]").forEach((node) => {
    node.addEventListener("click", (e) => { e.preventDefault(); openReportDetail(node.dataset.openReport); });
  });
  el.querySelectorAll("[data-open-entity]").forEach((node) => {
    node.addEventListener("click", (e) => { e.preventDefault(); openEntityDetail(node.dataset.openEntity); });
  });
}

let dashInsightsRequest = 0;

async function loadDashInsights() {
  const note = document.getElementById("dash-window-note");
  const grid = document.getElementById("dashboard-grid");
  // Changing the window twice quickly can land the responses out of order,
  // which would leave the panels showing one window while the control says
  // another. Only the newest request is allowed to paint.
  const ticket = ++dashInsightsRequest;
  const asked = dashLayout.window;
  if (grid) grid.dataset.loading = "true";
  try {
    const data = await api("/api/dashboard/insights?days=" + asked);
    if (ticket !== dashInsightsRequest) return;
    renderAttention(data.needs_attention);
    renderHotspots(data.hotspots, data.window_days);
    renderTempo(data.tempo);
    renderRising(data.rising, data.window_days);
    if (note) note.textContent = "Computed from your case data.";
    if (grid) {
      // Which window these panels are actually showing, as opposed to which
      // one the select says. Lets anything watching — a person, or a test —
      // tell a finished render from a stale one.
      grid.dataset.window = String(data.window_days);
      delete grid.dataset.loading;
    }
  } catch (err) {
    if (ticket !== dashInsightsRequest) return;
    if (note) note.textContent = "Couldn't load the analytics: " + err.message;
    if (grid) delete grid.dataset.loading;
  }
}

(function wireDashboard() {
  const grid = document.getElementById("dashboard-grid");
  if (!grid) return;
  // Defaults only. The real layout arrives from the account in
  // loadDashLayoutFromAccount, which runs once there is a user to load it for
  // — this code runs at page load, when there isn't one yet.
  dashLayout = mergeDashLayout(null);
  const windowSelect = document.getElementById("dash-window");
  windowSelect.value = String(dashLayout.window);
  applyDashLayout();
  wireDashDragAndDrop();

  windowSelect.addEventListener("change", () => {
    dashLayout.window = Number(windowSelect.value);
    saveDashLayout();
    loadDashInsights();
  });
  document.getElementById("dash-customise-btn").addEventListener("click", () => {
    const box = document.getElementById("dash-customise");
    box.hidden = !box.hidden;
  });
  document.getElementById("exec-summary-btn").addEventListener("click", () => {
    const days = document.getElementById("exec-summary-days").value || "30";
    startExport(`/api/exports/executive-summary.pdf?days=${encodeURIComponent(days)}`,
                `Executive summary (${days} days)`);
  });
  document.getElementById("dash-reset-layout").addEventListener("click", () => {
    dashLayout = { order: [...DASH_PANEL_IDS], hidden: [], window: dashLayout.window };
    saveDashLayout();
    applyDashLayout();
  });
})();

async function loadDashboard() {
  // The analytics are a second request, fired alongside rather than after:
  // they are the slower of the two (they scan the report/relationship tables)
  // and there is no reason for the counts and the clock to wait on them.
  loadDashInsights();
  try {
    const data = await api("/api/dashboard/summary");

    const stats = data.counts;
    document.getElementById("dash-stats").innerHTML = `
      <div class="dash-stat"><div class="dash-stat-value">${stats.entities}</div><div class="dash-stat-label">Entities</div></div>
      <div class="dash-stat"><div class="dash-stat-value">${stats.reports}</div><div class="dash-stat-label">Reports</div></div>
      <div class="dash-stat"><div class="dash-stat-value ${stats.pending_extraction ? "attention" : ""}">${stats.pending_extraction}</div><div class="dash-stat-label">Pending Extraction</div></div>
      <div class="dash-stat"><div class="dash-stat-value ${stats.pending_correlation ? "attention" : ""}">${stats.pending_correlation}</div><div class="dash-stat-label">Pending Correlation</div></div>
    `;

    const reportsEl = document.getElementById("dash-recent-reports");
    reportsEl.innerHTML = data.recent_reports.length
      ? data.recent_reports.map((r) => `
          <li data-open-report="${escapeHtml(r.id)}">
            <span class="status-pill status-${r.status}">${escapeHtml(r.status)}</span>${criticalityBadgeHtml(r)} ${escapeHtml(r.title)}
            <div class="dash-list-meta">${timeAgoHtml(r.created_at)}</div>
          </li>
        `).join("")
      : '<li class="empty-state">No reports yet.</li>';
    reportsEl.querySelectorAll("[data-open-report]").forEach((li) => {
      li.addEventListener("click", () => openReportDetail(li.dataset.openReport));
    });

    const entitiesEl = document.getElementById("dash-recent-entities");
    entitiesEl.innerHTML = data.recent_entities.length
      ? data.recent_entities.map((e) => `
          <li data-open-entity="${escapeHtml(e.id)}">
            <span class="type-pill type-${e.entity_type}">${escapeHtml(e.entity_type)}</span> ${escapeHtml(e.name)}
            <div class="dash-list-meta">${timeAgoHtml(e.created_at)}</div>
          </li>
        `).join("")
      : '<li class="empty-state">No entities yet.</li>';
    entitiesEl.querySelectorAll("[data-open-entity]").forEach((li) => {
      li.addEventListener("click", () => openEntityDetail(li.dataset.openEntity));
    });
  } catch (err) {
    showToast("Failed to load dashboard: " + err.message, true);
  }
}

/* ============================================================================
 * Entities: list + form + detail
 * ========================================================================== */

document.getElementById("entity-search").addEventListener("input", debounce(loadEntities, 300));
document.getElementById("entity-type-filter").addEventListener("change", loadEntities);
document.getElementById("entity-show-archived").addEventListener("change", loadEntities);
document.getElementById("entity-hide-expired").addEventListener("change", loadEntities);
document.getElementById("new-entity-btn").addEventListener("click", () => openEntityForm(null));
document.getElementById("import-entities-btn").addEventListener("click", () => openImportForm());

const ENTITY_TYPE_LABELS = {
  person: "Person", organization: "Organization", location: "Location",
  event: "Event", source: "Source", communication: "Communication",
  vehicle: "Vehicle", record: "Record",
};

function openImportForm() {
  const typeOptions = Object.entries(ENTITY_TYPE_LABELS)
    .map(([v, label]) => `<option value="${v}">${label}</option>`).join("");
  const html = `
    <h2>Import entities from CSV</h2>
    <div class="form-row">
      <label>Entity type</label>
      <select id="import-entity-type">${typeOptions}</select>
    </div>
    <p class="view-hint">
      Download the template, add one row per entity, then upload it. Row 2 is an example and is skipped.
    </p>
    <div class="modal-actions">
      <a id="import-download-link" class="btn-secondary" href="/api/entities/import-template?entity_type=person" target="_blank">Download template</a>
    </div>
    <form id="import-upload-form">
      <div class="form-row"><label>CSV file</label><input type="file" id="import-file-input" accept=".csv,text/csv" required></div>
      <p class="form-error" id="import-error"></p>
      <div class="modal-actions">
        <button type="button" class="btn-secondary" id="import-cancel">Close</button>
        <button type="submit" class="btn-primary">Upload</button>
      </div>
    </form>
    <div id="import-results"></div>
  `;
  openModal(html);

  const typeSelect = document.getElementById("import-entity-type");
  const downloadLink = document.getElementById("import-download-link");
  const updateDownloadLink = () => {
    downloadLink.href = "/api/entities/import-template?entity_type=" + encodeURIComponent(typeSelect.value);
  };
  typeSelect.addEventListener("change", updateDownloadLink);
  updateDownloadLink();

  document.getElementById("import-cancel").addEventListener("click", () => {
    closeModal();
    loadEntities();
  });

  document.getElementById("import-upload-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const fileInput = document.getElementById("import-file-input");
    if (!fileInput.files.length) return;
    const fd = new FormData();
    fd.append("entity_type", typeSelect.value);
    fd.append("file", fileInput.files[0]);
    const errorEl = document.getElementById("import-error");
    errorEl.textContent = "";
    try {
      const result = await apiUpload("/api/entities/import", fd);
      renderImportResults(result);
      loadEntities();
    } catch (err) {
      errorEl.textContent = err.message;
    }
  });
}

function renderImportResults(result) {
  const el = document.getElementById("import-results");
  const summary = `<p><strong>${result.created_count}</strong> created, <strong>${result.error_count}</strong> failed.</p>`;
  const errorRows = result.errors.map((e) => `
    <div class="import-error-row">
      <span class="import-row-num">row ${e.row}</span>
      <span>${escapeHtml(e.name || "(blank)")}</span>
      <span class="import-row-error">${escapeHtml(e.error)}</span>
    </div>
  `).join("");
  el.innerHTML = summary + (result.errors.length
    ? `<p class="view-hint">Fix these rows, and remove the rows that already imported before re-uploading — duplicates aren't detected.</p>${errorRows}`
    : "");
}

async function loadEntities() {
  const q = document.getElementById("entity-search").value.trim();
  const entityType = document.getElementById("entity-type-filter").value;
  const showArchived = document.getElementById("entity-show-archived").checked;
  const hideExpired = document.getElementById("entity-hide-expired").checked;
  const params = new URLSearchParams({ limit: "200", active_only: showArchived ? "false" : "true" });
  if (q) params.set("q", q);
  if (entityType) params.set("entity_type", entityType);
  if (hideExpired) params.set("hide_expired", "true");
  try {
    const data = await api("/api/entities?" + params.toString());
    renderEntityList(data.items);
    // The network is drawn from its own endpoint rather than from this list:
    // it needs relationship counts and the edges between records, neither of
    // which belongs in a paginated list response. Awaited so the tree can be
    // re-rendered with the counts once they land -- the tree is useful
    // immediately, the counts are a second later.
    await loadEntityGraph();
    renderEntityList(data.items);
  } catch (err) {
    showToast("Failed to load entities: " + err.message, true);
  }
}

// A Person's life status and disposition, as list-view badges. Only these two
// detail fields make it onto a card (see _attach_person_status in
// api/entities.py): they are the ones that change what you do on seeing a
// name, and finding out someone is deceased by opening their record is a
// worse experience than a grey pill saying so.
function personStatusBadgesHtml(e) {
  const badges = [];
  if (e.life_status && e.life_status !== "Living") {
    badges.push(`<span class="status-badge status-${e.life_status.toLowerCase()}">${escapeHtml(e.life_status)}</span>`);
  }
  if (e.disposition && e.disposition !== "At liberty") {
    const slug = e.disposition.toLowerCase().replace(/\s+/g, "-");
    badges.push(`<span class="status-badge status-${slug}">${escapeHtml(e.disposition)}</span>`);
  }
  return badges.join(" ");
}

// An Event past its relevant-until date. Flagged, never hidden — see the
// hide-expired filter, which is off by default.
function eventExpiryBadgeHtml(e) {
  if (e.entity_type !== "event" || !e.is_expired) return "";
  return '<span class="status-badge status-expired">expired</span>';
}

/* ---------------------------------------------------------------------------
 * Export menu
 *
 * One button on every entity, offering the shapes that make sense for it. The
 * shapes are three framings of the same collected material — see SHAPES in
 * api/exports.py — so the menu is about the question the reader is holding,
 * not about what the export can reach.
 *
 * The recommended shape leads and is described, rather than the menu being
 * three equal-looking options: on an Organization "Organisation report" is
 * almost always what someone wants, and making them read three lines to
 * discover that is a worse menu.
 * ------------------------------------------------------------------------ */

const EXPORT_SHAPES = {
  org: {
    label: "Organisation report",
    note: "Structure, membership and holdings",
  },
  target: {
    label: "Target package",
    note: "Everything on file for this entity",
  },
  plain: {
    label: "Plain dossier",
    note: "This entity and what it connects to",
  },
};

// Which shape leads for which kind of record. Every shape stays available on
// every record — an analyst building a package on a location knows what they
// want better than this table does.
const EXPORT_SHAPE_ORDER = {
  organization: ["org", "target", "plain"],
  person: ["target", "plain", "org"],
  location: ["plain", "target", "org"],
  event: ["plain", "target", "org"],
  source: ["plain", "target", "org"],
  communication: ["plain", "target", "org"],
  vehicle: ["target", "plain", "org"],
  record: ["plain", "target", "org"],
};

function exportShapeMenuHtml(entity) {
  const order = EXPORT_SHAPE_ORDER[entity.entity_type] || ["plain", "target", "org"];
  const rows = order.map((shape) => {
    const spec = EXPORT_SHAPES[shape];
    return `
      <button type="button" class="export-menu-item" role="menuitem" data-export-shape="${shape}">
        <span class="export-menu-label">${escapeHtml(spec.label)}</span>
        <span class="export-menu-note">${escapeHtml(spec.note)}</span>
      </button>`;
  }).join("");

  // A hotspot package only means something for a Location: the calculation is
  // defined over places, and offering it on a Person would be offering an
  // export that can only ever answer "not applicable".
  const hotspot = entity.entity_type === "location" ? `
      <div class="export-menu-sep"></div>
      <button type="button" class="export-menu-item" role="menuitem" data-export-hotspot="1">
        <span class="export-menu-label">Hotspot package</span>
        <span class="export-menu-note">Activity here, and every entity behind it</span>
      </button>` : "";

  return rows + hotspot;
}

// The document-level listeners from the previous render, so they can be taken
// off before the next set goes on. The entity detail view re-renders on every
// open, and without this each visit would leave another pair of handlers
// behind, firing on a menu that has since been replaced.
let exportMenuDismissHandlers = null;

function wireExportMenu(entity) {
  const btn = document.getElementById("export-entity-btn");
  const menu = document.getElementById("export-shape-menu");

  if (exportMenuDismissHandlers) {
    document.removeEventListener("click", exportMenuDismissHandlers.click);
    document.removeEventListener("keydown", exportMenuDismissHandlers.keydown);
    exportMenuDismissHandlers = null;
  }
  if (!btn || !menu) return;

  const close = () => {
    menu.hidden = true;
    btn.setAttribute("aria-expanded", "false");
  };
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    menu.hidden = !menu.hidden;
    btn.setAttribute("aria-expanded", String(!menu.hidden));
  });
  // Clicking anywhere else, or Escape, closes it.
  const onKeydown = (e) => { if (e.key === "Escape") close(); };
  document.addEventListener("click", close);
  document.addEventListener("keydown", onKeydown);
  exportMenuDismissHandlers = { click: close, keydown: onKeydown };

  menu.querySelectorAll("[data-export-shape]").forEach((item) => {
    item.addEventListener("click", (e) => {
      e.stopPropagation();
      close();
      startExport(
        `/api/exports/entities/${encodeURIComponent(entity.id)}/dossier.pdf`
        + `?shape=${encodeURIComponent(item.dataset.exportShape)}`,
        EXPORT_SHAPES[item.dataset.exportShape].label);
    });
  });
  const hotspotItem = menu.querySelector("[data-export-hotspot]");
  if (hotspotItem) {
    hotspotItem.addEventListener("click", (e) => {
      e.stopPropagation();
      close();
      // The window the dashboard is currently showing, so the package matches
      // the panel the analyst was just looking at.
      const days = (dashLayout && dashLayout.window) || 14;
      startExport(
        `/api/exports/hotspots/${encodeURIComponent(entity.id)}/package.pdf?days=${days}`,
        "Hotspot package");
    });
  }
}

/* A PDF export is a normal authenticated GET that returns a file. Fetching it
 * rather than pointing the browser at it means an error comes back as a
 * message instead of as a blank tab showing raw JSON, which is what a failed
 * export used to look like. */
async function startExport(url, label) {
  showToast(`Building ${label}…`);
  try {
    const res = await fetch(url, { credentials: "same-origin" });
    if (!res.ok) {
      let detail = `Export failed (${res.status})`;
      try { detail = (await res.json()).detail || detail; } catch (_) { /* not JSON */ }
      throw new Error(detail);
    }
    const blob = await res.blob();
    const disposition = res.headers.get("content-disposition") || "";
    const match = /filename="([^"]+)"/.exec(disposition);
    const href = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = href;
    a.download = match ? match[1] : "export.pdf";
    document.body.appendChild(a);
    a.click();
    a.remove();
    // Revoked on a delay: revoking immediately races the download in Safari,
    // which has not finished reading the blob when click() returns.
    setTimeout(() => URL.revokeObjectURL(href), 30000);
    showToast(`${label} downloaded`);
  } catch (err) {
    showToast(err.message, true);
  }
}

/* Selection for merging is off until you ask for it. A tick box on every card
 * all the time turns a browsing view into a management view, and browsing is
 * what this page is mostly for. */
let entityPickMode = false;
const entityPicked = new Set();

/* ---------------------------------------------------------------------------
 * The entity tree
 *
 * This was a grid of cards, one per record, six columns wide. That is a fine
 * way to show thirty records and a bad way to show three hundred: every card
 * costs the same vertical space whether it is the organization at the centre
 * of the case or a phone number somebody typed in once, and a page of equal
 * boxes has no shape to read. A row per record, foldable by type, fits an
 * order of magnitude more on screen and leaves room beside it for the network
 * — which is where the shape actually lives.
 *
 * Which groups are folded is remembered per browser (see ENTITY_TREE_KEY):
 * an analyst who works mostly with people should not have to close Locations
 * every time they come back to the page.
 * ------------------------------------------------------------------------- */

const ENTITY_TREE_KEY = "humint.entityTree.collapsed";

function loadCollapsedTypes() {
  // Browser storage is a convenience here, never a source of truth: a private
  // window or cleared site data just means everything starts expanded.
  try {
    const raw = localStorage.getItem(ENTITY_TREE_KEY);
    return new Set(raw ? JSON.parse(raw) : []);
  } catch (e) { return new Set(); }
}

function saveCollapsedTypes(set) {
  try { localStorage.setItem(ENTITY_TREE_KEY, JSON.stringify([...set])); } catch (e) { /* no-op */ }
}

let entityCollapsedTypes = loadCollapsedTypes();

function entityTreeRowHtml(e) {
  const picked = entityPicked.has(e.id);
  const badges = `${personStatusBadgesHtml(e)}${eventExpiryBadgeHtml(e)}`;
  // The relationship count is the same number the network grades its dots by,
  // so the two panes agree about which records are busy. Filled in from the
  // graph payload once it lands; absent until then rather than shown as 0,
  // which would be a lie for the half-second before the count arrives.
  const degree = entityDegrees.get(e.id);
  return `
    <div class="tree-row ${e.is_active ? "" : "archived"} ${picked ? "picked" : ""}"
         role="treeitem" tabindex="0" data-id="${escapeHtml(e.id)}"
         aria-label="${escapeHtml(e.name)}">
      ${entityPickMode && e.is_active
        ? `<input type="checkbox" class="entity-pick" data-pick="${escapeHtml(e.id)}"${picked ? " checked" : ""}
                  aria-label="Select ${escapeHtml(e.name)} for merging">` : ""}
      <span class="tree-name">${nameHtml(e.name, e.alignment)}</span>
      ${badges}
      ${e.is_active ? "" : `<span class="tree-note">${e.merged_into ? "merged away" : "archived"}</span>`}
      ${degree === undefined ? "" :
        `<span class="tree-degree" title="${degree} relationship${degree === 1 ? "" : "s"}">${degree}</span>`}
    </div>
  `;
}

function renderEntityList(items) {
  const el = document.getElementById("entity-list");
  if (!items.length) {
    el.innerHTML = '<div class="empty-state">No entities found.</div>';
    renderMergeBar();
    return;
  }

  const groups = new Map();
  for (const e of items) {
    if (!groups.has(e.entity_type)) groups.set(e.entity_type, []);
    groups.get(e.entity_type).push(e);
  }
  // A type with no matches under the current filter is left out entirely
  // rather than shown as an empty branch — seven types and most searches touch
  // two of them.
  const typeOrder = Object.keys(DETAIL_FIELDS);
  const orderedTypes = [...groups.keys()].sort((a, b) => typeOrder.indexOf(a) - typeOrder.indexOf(b));

  el.innerHTML = orderedTypes.map((type) => {
    const typeItems = groups.get(type).slice().sort((a, b) => a.name.localeCompare(b.name));
    const collapsed = entityCollapsedTypes.has(type);
    return `
      <div class="tree-group ${collapsed ? "collapsed" : ""}" data-type="${escapeHtml(type)}" role="group">
        <button class="tree-group-head" data-toggle-type="${escapeHtml(type)}"
                aria-expanded="${collapsed ? "false" : "true"}">
          <span class="tree-caret" aria-hidden="true">${collapsed ? "▶" : "▼"}</span>
          <span class="type-pill type-${type}">${escapeHtml(type)}</span>
          <span class="tree-group-count">${typeItems.length}</span>
        </button>
        <div class="tree-children" ${collapsed ? "hidden" : ""}>
          ${typeItems.map(entityTreeRowHtml).join("")}
        </div>
      </div>
    `;
  }).join("");

  el.querySelectorAll("[data-toggle-type]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const type = btn.dataset.toggleType;
      if (entityCollapsedTypes.has(type)) entityCollapsedTypes.delete(type);
      else entityCollapsedTypes.add(type);
      saveCollapsedTypes(entityCollapsedTypes);
      renderEntityList(items);
    });
  });

  const activate = (row) => {
    if (entityPickMode && row.querySelector(".entity-pick")) {
      const box = row.querySelector(".entity-pick");
      box.checked = !box.checked;
      box.dispatchEvent(new Event("change", { bubbles: true }));
      return;
    }
    openEntityDetail(row.dataset.id);
  };
  el.querySelectorAll(".tree-row").forEach((row) => {
    row.addEventListener("click", (ev) => {
      if (ev.target.matches(".entity-pick")) return;   // the tick box is its own control
      activate(row);
    });
    // A tree of rows that only answers to a mouse is a tree half the people
    // who need it cannot use.
    row.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); activate(row); }
    });
    row.addEventListener("mouseenter", () => highlightGraphNode(row.dataset.id));
    row.addEventListener("mouseleave", () => highlightGraphNode(null));
  });
  el.querySelectorAll(".entity-pick").forEach((box) => {
    box.addEventListener("change", () => {
      if (box.checked) entityPicked.add(box.dataset.pick);
      else entityPicked.delete(box.dataset.pick);
      box.closest(".tree-row").classList.toggle("picked", box.checked);
      renderMergeBar();
    });
  });
  renderMergeBar();
}

/* ---------------------------------------------------------------------------
 * The relationship network
 *
 * The case file drawn as itself: a dot per record, a line per relationship,
 * and the dots graded by how many relationships they have. That grading is
 * the point. A list tells you what exists; this tells you what the file is
 * ABOUT — the organization forty things point at is a bright fat dot the
 * moment the page settles, without anybody having counted anything.
 *
 * WHY THIS IS HAND-WRITTEN
 *
 * The detail page used to draw its little graph with vis-network, loaded from
 * a CDN, and showed "graph library unavailable" when it could not be reached.
 * That is a poor trade for an app whose whole point is that you host it
 * yourself: a machine in a cupboard with no route to the internet is a normal
 * way to run this, and a headline feature that only works online is not a
 * feature. So: a few hundred lines of force simulation on a canvas, no
 * dependencies, used by both the Entities page and the entity detail page.
 *
 * WHY IT STOPS
 *
 * The simulation runs for a fixed number of cooling ticks and then stands
 * still, redrawing only when something actually changes (hover, selection,
 * resize, theme). A graph that jiggles forever costs a CPU core for nothing
 * and is harder to read than one that has settled.
 * ------------------------------------------------------------------------- */

const GRAPH_TICKS = 220;          // enough to settle; not enough to notice
const GRAPH_MIN_R = 3.5;
const GRAPH_MAX_R = 13;

// Handles the three forms a CSS custom property is realistically written in.
// Anything else falls back to a mid grey rather than throwing inside a
// render loop.
function parseColor(value) {
  const v = (value || "").trim();
  const hex = v.match(/^#([0-9a-f]{3}|[0-9a-f]{6})$/i);
  if (hex) {
    const h = hex[1].length === 3 ? hex[1].split("").map((c) => c + c).join("") : hex[1];
    return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)];
  }
  const rgb = v.match(/rgba?\(\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)/i);
  if (rgb) return [+rgb[1], +rgb[2], +rgb[3]];
  return [128, 128, 128];
}

function mixColor(a, b, t) {
  return [0, 1, 2].map((i) => Math.round(a[i] + (b[i] - a[i]) * t));
}

function graphRadius(degree, maxDegree) {
  if (!maxDegree) return GRAPH_MIN_R;
  // Square root, not linear: a record with 40 links is more important than
  // one with 4, but it is not ten times the area, and drawing it that way
  // buries everything else under it.
  const t = Math.sqrt(degree / maxDegree);
  return GRAPH_MIN_R + t * (GRAPH_MAX_R - GRAPH_MIN_R);
}

function graphColor(degree, maxDegree) {
  // The cold end is pulled halfway toward the panel background rather than
  // being the muted text colour on its own. Every theme here is a single hue
  // family, so muted-2 and accent are close cousins — on the light themes
  // both are dark, and a ramp between them is barely a ramp. Mixing the cold
  // end into the background gives "nearly invisible" at one extreme and the
  // full accent at the other, in all four themes and in either direction.
  const cold = mixColor(parseColor(themeColor("--muted-2", "#3f6b4d")),
                        parseColor(themeColor("--panel", "#0d1f14")), 0.55);
  const hot = parseColor(themeColor("--accent", "#39ff88"));
  const t = maxDegree ? Math.sqrt(degree / maxDegree) : 0;
  const c = mixColor(cold, hot, t);
  return `rgb(${c[0]}, ${c[1]}, ${c[2]})`;
}

/* One network, bound to one canvas. Two live at once — the whole case file on
 * the Entities page and one record's neighbourhood on its detail page — so
 * everything that used to be module state belongs to the instance. */
function createNetwork(options) {
  const st = {
    canvas: null,
    tip: options.tipId ? document.getElementById(options.tipId) : null,
    nodes: [],
    edges: [],
    maxDegree: 0,
    ticks: 0,
    raf: null,
    hover: null,
    highlight: null,
    focusId: options.focusId || null,   // the record the page is about, if any
    labelCount: options.labelCount === undefined ? 8 : options.labelCount,
    // A click answers "who is this?" beside the dot. Leaving the page is the
    // card's own button, because leaving loses the arrangement you just made.
    onPick: options.onPick || ((id, x, y) => openSummaryCard(id, x, y)),
    // Once the layout has settled the fit is frozen. Recomputing it every
    // frame made the whole picture creep while nodes drifted or were dragged,
    // which reads as the graph moving under your hand.
    view: null,
    drag: null,          // { node, moved, startedAt }
    wake: 0,             // frames of real simulation left after a drag
    settled: false,
    lastIdle: 0,
    onScreen: true,
    clickTimer: null,    // open-the-record, held back long enough to spot a double-click
    io: null,
    visWired: false,
  };

  function canvas() {
    // Looked up lazily: on the detail page the canvas is replaced every time
    // the record is re-rendered, so a reference captured once goes stale.
    st.canvas = document.getElementById(options.canvasId);
    return st.canvas;
  }

  function sizeCanvas() {
    const c = canvas();
    if (!c || !c.parentElement) return;
    const ratio = window.devicePixelRatio || 1;
    const w = Math.max(c.parentElement.clientWidth, 1);
    const h = Math.max(c.parentElement.clientHeight, 1);
    c.width = Math.round(w * ratio);
    c.height = Math.round(h * ratio);
    c.style.width = w + "px";
    c.style.height = h + "px";
    st.view = null;      // a different box needs a different fit
  }

  function setData(nodes, edges, maxDegree) {
    const byId = new Map();
    const n = nodes.length;
    // Seeded on a circle rather than at random: two runs over the same data
    // then produce recognisably the same picture, which matters when somebody
    // is coming back to a case file they were reading yesterday.
    st.nodes = nodes.map((node, i) => {
      const angle = (i / Math.max(n, 1)) * Math.PI * 2;
      const spread = 120 + (i % 7) * 18;
      const obj = { ...node, x: Math.cos(angle) * spread, y: Math.sin(angle) * spread, vx: 0, vy: 0 };
      byId.set(node.id, obj);
      return obj;
    });
    st.edges = edges
      .map((e) => ({ a: byId.get(e.from), b: byId.get(e.to) }))
      .filter((e) => e.a && e.b && e.a !== e.b);
    st.maxDegree = maxDegree || st.nodes.reduce((m, x) => Math.max(m, x.degree || 0), 0);
    for (const node of st.nodes) node.r = graphRadius(node.degree || 0, st.maxDegree);
    st.ticks = 0;
    st.hover = null;
    st.settled = false;
    st.view = null;
    sizeCanvas();
    start();
  }

  /* The loop has three gears.
   *
   *   1. Settling   - the cooling simulation, as before, straight after load
   *                   or a refit.
   *   2. Awake      - a short burst of real simulation after a node is
   *                   dragged, so its neighbours make room instead of the
   *                   picture going rigid around one moved dot.
   *   3. Idle drift - a barely-there wander around where each node settled.
   *                   It costs one pass over the nodes, not the n-squared
   *                   repulsion pass, and it stops when the tab is hidden,
   *                   when the canvas is scrolled out of view, on a very
   *                   large graph, and for anyone who has asked their system
   *                   for reduced motion.
   */
  const IDLE_MS = 55;                 // ~18 fps is plenty for a slow drift
  const IDLE_MAX_NODES = 400;
  const WAKE_FRAMES = 90;

  function reducedMotion() {
    return window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  }

  function idleAllowed() {
    return !document.hidden && st.onScreen && !reducedMotion()
           && st.nodes.length > 1 && st.nodes.length <= IDLE_MAX_NODES;
  }

  function settle() {
    // Where each node came to rest, and a phase so they do not all breathe
    // in time with each other.
    for (const node of st.nodes) {
      node.hx = node.x;
      node.hy = node.y;
      if (node.phase === undefined) node.phase = Math.random() * Math.PI * 2;
    }
    st.view = null;                   // fit once, now, then hold it
    st.settled = true;
  }

  function idleDrift(now) {
    const t = now / 1000;
    for (const node of st.nodes) {
      if (node.pinned || (st.drag && st.drag.node === node)) continue;
      const amp = 0.9 + node.r * 0.04;
      node.x = node.hx + Math.sin(t * 0.45 + node.phase) * amp;
      node.y = node.hy + Math.cos(t * 0.37 + node.phase * 1.3) * amp;
    }
  }

  function start() {
    if (st.raf) cancelAnimationFrame(st.raf);
    const step = () => {
      st.raf = null;
      if (st.ticks < GRAPH_TICKS) {
        tick();
        st.ticks += 1;
        draw();
        st.raf = requestAnimationFrame(step);
        return;
      }
      if (!st.settled) settle();
      if (st.wake > 0 || st.drag) {
        tick(0.22);
        if (st.wake > 0) st.wake -= 1;
        for (const node of st.nodes) { node.hx = node.x; node.hy = node.y; }
        draw();
        st.raf = requestAnimationFrame(step);
        return;
      }
      const now = performance.now();
      if (idleAllowed()) {
        if (now - st.lastIdle >= IDLE_MS) {
          idleDrift(now);
          draw();
          st.lastIdle = now;
        }
        st.raf = requestAnimationFrame(step);
        return;
      }
      draw();                          // parked: one last frame, no loop
    };
    step();
  }

  function tick(alphaOverride) {
    const nodes = st.nodes;
    const n = nodes.length;
    if (!n) return;
    // Cooling: big moves early, small ones late, so it settles instead of
    // oscillating around an arrangement it has already found. A drag passes
    // its own small alpha: enough for the neighbours to shuffle, not enough
    // to rearrange the whole picture under the analyst.
    const alpha = alphaOverride === undefined ? 1 - st.ticks / GRAPH_TICKS : alphaOverride;
    const repulsion = 900 * alpha + 120;

    for (let i = 0; i < n; i++) {
      const a = nodes[i];
      for (let j = i + 1; j < n; j++) {
        const b = nodes[j];
        let dx = a.x - b.x;
        let dy = a.y - b.y;
        let d2 = dx * dx + dy * dy;
        if (d2 < 0.01) {           // exactly coincident: nudge them apart
          dx = (Math.random() - 0.5) * 0.1;
          dy = (Math.random() - 0.5) * 0.1;
          d2 = dx * dx + dy * dy + 0.01;
        }
        const force = repulsion / d2;
        const d = Math.sqrt(d2);
        const fx = (dx / d) * force;
        const fy = (dy / d) * force;
        a.vx += fx; a.vy += fy;
        b.vx -= fx; b.vy -= fy;
      }
    }

    // Edges pull. Ideal length grows with the endpoints' size so a hub does
    // not swallow its own neighbours.
    for (const e of st.edges) {
      const dx = e.b.x - e.a.x;
      const dy = e.b.y - e.a.y;
      const d = Math.sqrt(dx * dx + dy * dy) || 0.01;
      const rest = 42 + e.a.r + e.b.r;
      const force = (d - rest) * 0.015;
      const fx = (dx / d) * force;
      const fy = (dy / d) * force;
      e.a.vx += fx; e.a.vy += fy;
      e.b.vx -= fx; e.b.vy -= fy;
    }

    for (const node of nodes) {
      // A node somebody has put somewhere stays there: it still pushes and
      // pulls on everything else, it just does not move itself.
      if (node.pinned || (st.drag && st.drag.node === node)) {
        node.vx = 0; node.vy = 0;
        continue;
      }
      node.vx -= node.x * 0.006;   // gentle pull to the middle
      node.vy -= node.y * 0.006;
      node.vx *= 0.82;             // friction
      node.vy *= 0.82;
      node.x += node.vx;
      node.y += node.vy;
    }
  }

  function currentView() {
    if (!st.view) st.view = transform();
    return st.view;
  }

  function transform() {
    // Fit whatever the simulation produced into the canvas, with room for the
    // largest dot and its label.
    const c = st.canvas;
    const ratio = window.devicePixelRatio || 1;
    const w = c.width / ratio;
    const h = c.height / ratio;
    if (!st.nodes.length) return { scale: 1, dx: w / 2, dy: h / 2, w, h, ratio };
    let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
    for (const node of st.nodes) {
      minX = Math.min(minX, node.x); maxX = Math.max(maxX, node.x);
      minY = Math.min(minY, node.y); maxY = Math.max(maxY, node.y);
    }
    const pad = GRAPH_MAX_R + 14;
    const spanX = Math.max(maxX - minX, 1);
    const spanY = Math.max(maxY - minY, 1);
    const scale = Math.min((w - pad * 2) / spanX, (h - pad * 2) / spanY, 2.4);
    return {
      scale,
      dx: w / 2 - ((minX + maxX) / 2) * scale,
      dy: h / 2 - ((minY + maxY) / 2) * scale,
      w, h, ratio,
    };
  }

  function draw() {
    const c = canvas();
    if (!c || !c.getContext || !c.width) return;
    const ctx = c.getContext("2d");
    const t = st.settled ? currentView() : transform();
    ctx.setTransform(t.ratio, 0, 0, t.ratio, 0, 0);
    ctx.clearRect(0, 0, t.w, t.h);

    const focus = st.hover
      || st.nodes.find((x) => x.id === st.highlight)
      || null;
    const near = new Set();
    if (focus) {
      near.add(focus.id);
      for (const e of st.edges) {
        if (e.a === focus) near.add(e.b.id);
        if (e.b === focus) near.add(e.a.id);
      }
    }

    ctx.lineWidth = 1;
    for (const e of st.edges) {
      const lit = focus && (e.a === focus || e.b === focus);
      ctx.strokeStyle = lit ? themeColor("--accent", "#39ff88") : themeColor("--border", "#173a24");
      ctx.globalAlpha = focus ? (lit ? 0.9 : 0.15) : 0.55;
      ctx.beginPath();
      ctx.moveTo(e.a.x * t.scale + t.dx, e.a.y * t.scale + t.dy);
      ctx.lineTo(e.b.x * t.scale + t.dx, e.b.y * t.scale + t.dy);
      ctx.stroke();
    }
    ctx.globalAlpha = 1;

    for (const node of st.nodes) {
      const x = node.x * t.scale + t.dx;
      const y = node.y * t.scale + t.dy;
      ctx.globalAlpha = !focus || near.has(node.id) ? 1 : 0.25;
      ctx.beginPath();
      ctx.arc(x, y, node.r, 0, Math.PI * 2);
      ctx.fillStyle = graphColor(node.degree || 0, st.maxDegree);
      ctx.fill();
      // A hairline keeps the palest dots from disappearing into the panel.
      ctx.lineWidth = 1;
      ctx.strokeStyle = themeColor("--border-strong", "#357a4f");
      ctx.stroke();
      if (node === focus || node.id === st.focusId) {
        ctx.lineWidth = 2;
        ctx.strokeStyle = themeColor("--text-bright", "#eaffef");
        ctx.stroke();
        ctx.lineWidth = 1;
      }
      if (node.pinned) {
        // A held node reads differently from a settled one, or nobody can
        // tell why part of the picture stopped responding.
        ctx.beginPath();
        ctx.arc(x, y, node.r + 4, 0, Math.PI * 2);
        ctx.lineWidth = 1.5;
        ctx.setLineDash([2, 3]);
        ctx.strokeStyle = themeColor("--amber", "#ffb020");
        ctx.stroke();
        ctx.setLineDash([]);
        ctx.lineWidth = 1;
      }
    }
    ctx.globalAlpha = 1;

    // Labels only where they can be read: the focused record, its neighbours,
    // and otherwise the handful of biggest dots. Labelling everything turns a
    // readable picture into a page of overlapping text.
    const labelled = focus
      ? st.nodes.filter((x) => near.has(x.id))
      : (st.labelCount < 0 ? st.nodes : st.nodes.slice(0, st.labelCount));
    ctx.font = "11px system-ui, sans-serif";
    ctx.textAlign = "center";
    ctx.textBaseline = "bottom";
    ctx.fillStyle = themeColor("--text", "#d6ffe2");
    // Two labels printed over each other are worse than one label: the
    // overlap is unreadable AND you cannot tell it is two names. So each
    // label claims a rectangle, and a name whose rectangle collides with one
    // already placed is dropped. The list is in the order the caller wants
    // them, so the important names claim their space first.
    const placed = [];
    for (const node of labelled) {
      const x = node.x * t.scale + t.dx;
      const y = node.y * t.scale + t.dy;
      const text = truncate(node.name, 26);
      const halfWidth = ctx.measureText(text).width / 2 + 2;
      const top = y - node.r - 14;
      const box = { x1: x - halfWidth, x2: x + halfWidth, y1: top, y2: top + 14 };
      const clash = placed.some((b) => box.x1 < b.x2 && box.x2 > b.x1
                                    && box.y1 < b.y2 && box.y2 > b.y1);
      // The record the page is about, and whatever is under the pointer,
      // are always named — those two are the reason anybody is looking.
      if (clash && node !== focus && node.id !== st.focusId) continue;
      placed.push(box);
      // A hostile record's name is red here too, so the network agrees with
      // every list in the app. The dot itself stays graded by degree -- that
      // is what the network is FOR, and recolouring dots by alignment would
      // destroy the one thing this picture says.
      if (isHostile(node.alignment)) {
        ctx.fillStyle = themeColor("--red", "#ff3b3b");
        ctx.fillText(text, x, y - node.r - 3);
        ctx.fillStyle = themeColor("--text", "#d6ffe2");
      } else {
        ctx.fillText(text, x, y - node.r - 3);
      }
    }
  }

  function nodeAt(clientX, clientY) {
    const c = st.canvas;
    if (!c) return null;
    const rect = c.getBoundingClientRect();
    const px = clientX - rect.left;
    const py = clientY - rect.top;
    const t = st.settled ? currentView() : transform();
    let best = null;
    let bestD = Infinity;
    for (const node of st.nodes) {
      const x = node.x * t.scale + t.dx;
      const y = node.y * t.scale + t.dy;
      const d = Math.hypot(px - x, py - y);
      // A generous target: the dots are small on purpose, and a 3px circle is
      // not something anyone can reliably hit with a mouse.
      if (d < Math.max(node.r + 6, 10) && d < bestD) { best = node; bestD = d; }
    }
    return best;
  }

  function wire() {
    const c = canvas();
    if (!c || c.dataset.wired) return;
    c.dataset.wired = "1";
    c.addEventListener("mousemove", (ev) => {
      if (st.drag) return;
      const node = nodeAt(ev.clientX, ev.clientY);
      c.style.cursor = node ? "pointer" : "default";
      if (node !== st.hover) {
        st.hover = node;
        if (!st.raf) draw();
      }
      if (node && st.tip) {
        const rect = c.getBoundingClientRect();
        st.tip.hidden = false;
        st.tip.textContent = `${node.name} — ${node.entity_type}, `
          + `${node.degree} relationship${node.degree === 1 ? "" : "s"}`;
        // Kept inside the box: a tooltip that runs off the right edge of the
        // pane is a tooltip nobody can read.
        const left = Math.min(ev.clientX - rect.left + 12, rect.width - st.tip.offsetWidth - 6);
        st.tip.style.left = Math.max(4, left) + "px";
        st.tip.style.top = Math.max(4, ev.clientY - rect.top + 14) + "px";
      } else if (st.tip) {
        st.tip.hidden = true;
      }
    });
    c.addEventListener("mouseleave", () => {
      st.hover = null;
      if (st.tip) st.tip.hidden = true;
      if (!st.raf) draw();
    });
    /* Dragging.
     *
     * Pointer events rather than mouse events so a finger works too. The
     * click that opens a record is the one that did not move: without that
     * test, every drag would also open the record you had just arranged.
     */
    c.addEventListener("pointerdown", (ev) => {
      if (ev.button !== undefined && ev.button !== 0) return;   // right-click is the menu
      const node = nodeAt(ev.clientX, ev.clientY);
      if (!node) return;
      ev.preventDefault();
      const t = st.settled ? currentView() : transform();
      const rect = c.getBoundingClientRect();
      st.drag = {
        node,
        moved: false,
        startedAt: performance.now(),
        fromX: ev.clientX,
        fromY: ev.clientY,
        // Grab offset, so the dot does not jump its own radius on the first
        // pixel of movement.
        ox: node.x - (ev.clientX - rect.left - t.dx) / t.scale,
        oy: node.y - (ev.clientY - rect.top - t.dy) / t.scale,
      };
      c.setPointerCapture(ev.pointerId);
      c.style.cursor = "grabbing";
      start();
    });

    c.addEventListener("pointermove", (ev) => {
      if (!st.drag) return;
      ev.preventDefault();
      const t = st.settled ? currentView() : transform();
      const rect = c.getBoundingClientRect();
      // Kept inside the pane: the fit is frozen while you drag, so a node
      // dropped outside the box would be invisible until the next refit.
      const pad = GRAPH_MAX_R + 6;
      const px = Math.min(Math.max(ev.clientX - rect.left, pad), rect.width - pad);
      const py = Math.min(Math.max(ev.clientY - rect.top, pad), rect.height - pad);
      const node = st.drag.node;
      node.x = (px - t.dx) / t.scale + st.drag.ox;
      node.y = (py - t.dy) / t.scale + st.drag.oy;
      node.hx = node.x; node.hy = node.y;
      node.vx = 0; node.vy = 0;
      // A few pixels of wobble while clicking is not a drag.
      if (Math.hypot(ev.clientX - st.drag.fromX, ev.clientY - st.drag.fromY) > 3) {
        st.drag.moved = true;
      }
      if (st.tip) st.tip.hidden = true;
    });

    const endDrag = (ev) => {
      if (!st.drag) return;
      const { node, moved, startedAt } = st.drag;
      st.drag = null;
      c.style.cursor = "pointer";
      try { c.releasePointerCapture(ev.pointerId); } catch (e) { /* already gone */ }
      if (moved) {
        node.pinned = true;           // it stays where it was put
        st.wake = WAKE_FRAMES;        // let the neighbours make room
        start();
      } else if (performance.now() - startedAt < 500) {
        // A press that never moved is a click — but a double-click releases a
        // held node, and the first half of a double-click looks exactly like
        // a single one. So the summary waits a moment to see whether a second
        // click is coming.
        const px = ev.clientX, py = ev.clientY;
        if (st.clickTimer) clearTimeout(st.clickTimer);
        st.clickTimer = setTimeout(() => {
          st.clickTimer = null;
          if (st.tip) st.tip.hidden = true;   // the card says more than the tip
          st.onPick(node.id, px, py);
        }, 260);
      }
    };
    c.addEventListener("pointerup", endDrag);
    c.addEventListener("pointercancel", endDrag);

    // Double-click a held node to let the layout have it back.
    c.addEventListener("dblclick", (ev) => {
      if (st.clickTimer) { clearTimeout(st.clickTimer); st.clickTimer = null; }
      const node = nodeAt(ev.clientX, ev.clientY);
      if (!node || !node.pinned) return;
      ev.preventDefault();
      node.pinned = false;
      st.wake = WAKE_FRAMES;
      start();
    });

    /* Idle drift is for a graph somebody is looking at.
     *
     * The detail page replaces its canvas every time a record is opened, so
     * wire() runs again and again on one instance: the document listener is
     * attached once, and the old observer is dropped before a new one is
     * made, or opening thirty records would leave thirty of each behind. */
    if (!st.visWired) {
      st.visWired = true;
      document.addEventListener("visibilitychange", () => { if (!document.hidden) start(); });
    }
    if (window.IntersectionObserver) {
      if (st.io) st.io.disconnect();
      st.io = new IntersectionObserver((entries) => {
        st.onScreen = entries.some((e) => e.isIntersecting);
        if (st.onScreen) start();
      }, { threshold: 0.05 });
      st.io.observe(c);
    }
    // Right-click a node for the same menu the tree and the cards have.
    c.addEventListener("contextmenu", (ev) => {
      const node = nodeAt(ev.clientX, ev.clientY);
      if (!node) return;
      ev.preventDefault();
      openEntityMenu(node.id, ev.clientX, ev.clientY);
    });
  }

  /* Re-fit whenever the CANVAS'S BOX changes, not only when the window does.
   *
   * The pane is sized from the viewport now, and it also changes size for
   * reasons a window-resize listener never hears about: the tree and the
   * network stacking at a breakpoint, the admin rail collapsing, a browser
   * zoom, a modal opening beside it. Those were the cases where the graph
   * stayed drawn at its old size and ran off the edge of a laptop screen.
   *
   * Work is deferred to the next animation frame, because a ResizeObserver
   * can fire several times during one drag and re-fitting on each of them is
   * how a window resize turns into a stutter. */
  if (typeof ResizeObserver !== "undefined") {
    let pending = false;
    const observer = new ResizeObserver(() => {
      if (pending) return;
      pending = true;
      requestAnimationFrame(() => {
        pending = false;
        const c = canvas();
        if (!c || !c.parentElement) return;
        // A hidden pane reports zero; re-fitting to nothing would blank the
        // picture and leave it blank when the view came back.
        if (!c.parentElement.clientWidth || !c.parentElement.clientHeight) return;
        sizeCanvas();
        draw();
      });
    });
    const target = canvas() && canvas().parentElement;
    if (target) observer.observe(target);
  }

  return {
    state: st,
    setData(nodes, edges, maxDegree) { setData(nodes, edges, maxDegree); wire(); },
    refit() {
      for (const node of st.nodes) node.pinned = false;
      st.ticks = 0; st.settled = false; st.view = null; st.wake = 0;
      sizeCanvas(); start();
    },
    pinnedCount() { return st.nodes.filter((n) => n.pinned).length; },
    resize() { sizeCanvas(); draw(); },
    redraw() { if (!st.raf) draw(); },
    highlight(id) {
      if (st.highlight === id) return;
      st.highlight = id;
      if (!st.raf) draw();
    },
    wire,
  };
}

/* --- the whole case file, on the Entities page ---------------------------- */

// Degree by entity id, shared with the tree so both panes agree.
let entityDegrees = new Map();

const entityNetwork = createNetwork({
  canvasId: "entity-graph-canvas",
  tipId: "graph-tip",
});
let entityGraphMeta = { total: 0, truncated: false };

async function loadEntityGraph() {
  if (!document.getElementById("entity-graph-canvas")) return;
  const params = new URLSearchParams();
  const q = document.getElementById("entity-search").value.trim();
  const entityType = document.getElementById("entity-type-filter").value;
  const showArchived = document.getElementById("entity-show-archived").checked;
  const hideExpired = document.getElementById("entity-hide-expired").checked;
  if (q) params.set("q", q);
  if (entityType) params.set("entity_type", entityType);
  params.set("active_only", showArchived ? "false" : "true");
  if (hideExpired) params.set("hide_expired", "true");
  try {
    const data = await api("/api/entities/graph?" + params.toString());
    entityDegrees = new Map(data.nodes.map((n) => [n.id, n.degree]));
    entityGraphMeta = { total: data.total, truncated: data.truncated };
    const hideIsolated = document.getElementById("graph-hide-isolated").checked;
    const nodes = data.nodes.filter((n) => !hideIsolated || n.degree > 0);
    entityNetwork.setData(nodes, data.edges, data.max_degree);

    const empty = document.getElementById("graph-empty");
    empty.hidden = nodes.length > 0;
    if (!nodes.length) {
      empty.textContent = hideIsolated && data.nodes.length
        ? "Every entity matching these filters is unconnected. Untick “Hide unconnected” to see them."
        : "Nothing to draw yet.";
    }
    updateGraphSummary();
    renderGraphLegend();
  } catch (err) {
    // The tree is the load-bearing half of this page. If the graph cannot be
    // drawn, say so in the graph's own box and leave everything else alone.
    const empty = document.getElementById("graph-empty");
    if (empty) {
      empty.hidden = false;
      empty.textContent = "The network could not be loaded: " + err.message;
    }
  }
}

function updateGraphSummary() {
  const el = document.getElementById("graph-summary");
  if (!el) return;
  const shown = entityNetwork.state.nodes.length;
  const edges = entityNetwork.state.edges.length;
  el.textContent = entityGraphMeta.truncated
    ? `${shown} most-connected of ${entityGraphMeta.total} · ${edges} link${edges === 1 ? "" : "s"}`
    : `${shown} entit${shown === 1 ? "y" : "ies"} · ${edges} link${edges === 1 ? "" : "s"}`;
}

function renderGraphLegend() {
  const scale = document.getElementById("graph-scale");
  if (!scale) return;
  const max = entityNetwork.state.maxDegree;
  const steps = 5;
  scale.innerHTML = Array.from({ length: steps }, (_, i) => {
    const degree = max * (i / (steps - 1));
    return `<span class="graph-scale-dot" style="background:${graphColor(degree, max)};`
         + `width:${graphRadius(degree, max) * 2}px;height:${graphRadius(degree, max) * 2}px"></span>`;
  }).join("");
  const label = document.getElementById("graph-legend-max");
  if (label) label.textContent = max ? `most (${max})` : "most";
}

function highlightGraphNode(id) {
  entityNetwork.highlight(id);
}

(function wireEntityGraphControls() {
  const hideIsolated = document.getElementById("graph-hide-isolated");
  if (hideIsolated) hideIsolated.addEventListener("change", () => loadEntityGraph());
  const refit = document.getElementById("graph-refit");
  if (refit) refit.addEventListener("click", () => entityNetwork.refit());

  let resizeTimer = null;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      entityNetwork.resize();
      if (detailNetwork) detailNetwork.resize();
    }, 150);
  });
})();


/* ---------------------------------------------------------------------------
 * Deleting for good
 *
 * Everything else in this app archives. This does not, and the interface has
 * to be honest about that: the dialog counts what is about to be destroyed,
 * names the reports that cite it, and makes you type the word. Admins only —
 * the buttons do not exist for anyone else, and the API would refuse them
 * anyway.
 *
 * The reason it exists: extraction proposes an entity called "ATTACHMENT A",
 * somebody accepts it, and archiving only hides it. Enough of those and the
 * tree, the network and the correlation queue are mostly debris.
 * ------------------------------------------------------------------------- */

const DELETE_CONFIRM_WORD = "DELETE";

function canDelete() {
  return !!(state.user && state.user.role === "admin");
}

async function openDeleteDialog(target, onDone) {
  if (!canDelete()) {
    showToast("Only an admin can delete entities", true);
    return;
  }
  const body = {
    entity_ids: target.entity_ids || [],
    report_ids: target.report_ids || [],
    document_ids: target.document_ids || [],
  };
  let preview;
  try {
    preview = await api("/api/admin/delete-preview", { method: "POST", body });
  } catch (err) { showToast(err.message, true); return; }

  const total = preview.entities.length + preview.reports.length + preview.documents.length;
  const sum = (key) => preview.entities.reduce((n, e) => n + (e.removes[key] || 0), 0);

  const blocked = preview.blocked.map((b) => `
    <li><strong>${escapeHtml(b.name)}</strong> is named by
        ${b.reports.map((r) => `“${escapeHtml(r.title)}”`).join(", ")}</li>`).join("");

  const lines = [];
  if (preview.entities.length) {
    lines.push(`<li><strong>${preview.entities.length}</strong> record(s):
      ${preview.entities.map((e) => escapeHtml(e.name)).join(", ")}</li>`);
    lines.push(`<li><strong>${sum("relationships")}</strong> relationship(s)</li>`);
    lines.push(`<li><strong>${sum("contacts")}</strong> contact detail(s)</li>`);
    lines.push(`<li><strong>${sum("attachments")}</strong> attachment(s), files and all</li>`);
    lines.push(`<li><strong>${sum("suggestions") + sum("correlation_rows")}</strong>
      queued or historical suggestion(s) about them</li>`);
    const merged = sum("merged_into_this");
    if (merged) {
      lines.push(`<li class="merge-warn">${merged} entit${merged === 1 ? "y was" : "ies were"} merged into ${preview.entities.length === 1 ? "this one" : "these"} and stay archived</li>`);
    }
  }
  if (preview.reports.length) {
    lines.push(`<li><strong>${preview.reports.length}</strong> report(s):
      ${preview.reports.map((r) => escapeHtml(r.title)).join(", ")}</li>`);
    const confirmed = preview.reports.filter((r) => r.warn_confirmed);
    if (confirmed.length) {
      lines.push(`<li class="merge-warn">${confirmed.length} of them
        ${confirmed.length === 1 ? "is" : "are"} confirmed, not a draft</li>`);
    }
  }
  if (preview.documents.length) {
    lines.push(`<li><strong>${preview.documents.length}</strong> document(s) and the
      uploaded file(s) on disk</li>`);
    const kept = preview.documents.reduce((n, d) => n + (d.accepted_records_kept || 0), 0);
    if (kept) {
      lines.push(`<li>${kept} record(s) already accepted from
        ${preview.documents.length === 1 ? "it" : "them"} are kept — they are part of the
        case file now</li>`);
    }
  }

  // Citations by a draft are worth seeing but do not stop anything.
  const cited = preview.entities.flatMap((e) => e.cited_by).filter((r) => r.status !== "confirmed");
  if (cited.length) {
    lines.push(`<li>${cited.length} draft report(s) lose the link, keeping their text:
      ${cited.map((r) => escapeHtml(r.title)).join(", ")}</li>`);
  }

  const html = `
    <h2>Permanently delete ${total} ${total === 1 ? "entity" : "entities"}</h2>
    <p class="view-hint">This removes them and their files permanently. Only a backup restore can bring them back.</p>

    ${blocked ? `
      <div class="delete-blocked">
        <p><strong>Can't delete:</strong> a confirmed report names it.</p>
        <ul>${blocked}</ul>
        <p class="field-hint">Archive it instead, or unlink it from the report first.</p>
      </div>` : `
      <div class="merge-preview"><ul class="merge-preview-list">${lines.join("")}</ul></div>

      <div class="form-row">
        <label for="delete-confirm-input">Type ${DELETE_CONFIRM_WORD} to confirm</label>
        <input type="text" id="delete-confirm-input" autocomplete="off" spellcheck="false">
      </div>`}

    <div class="queue-actions">
      ${blocked ? "" : `<button class="btn-danger" id="delete-confirm" disabled>
        Delete ${total === 1 ? "it" : "them"} permanently</button>`}
      <button class="btn-secondary" id="delete-cancel">${blocked ? "Close" : "Cancel"}</button>
    </div>`;

  openModal(html);
  document.getElementById("delete-cancel").addEventListener("click", closeModal);
  if (blocked) return;

  const input = document.getElementById("delete-confirm-input");
  const go = document.getElementById("delete-confirm");
  input.addEventListener("input", () => {
    go.disabled = input.value.trim() !== DELETE_CONFIRM_WORD;
  });
  input.focus();
  go.addEventListener("click", async () => {
    go.disabled = true;
    try {
      const res = await api("/api/admin/delete", { method: "POST", body });
      closeModal();
      const parts = [];
      if (res.entities.length) parts.push(`${res.entities.length} record(s)`);
      if (res.reports.length) parts.push(`${res.reports.length} report(s)`);
      if (res.documents.length) parts.push(`${res.documents.length} document(s)`);
      showToast(`Deleted ${parts.join(", ")} for good`);
      if (typeof onDone === "function") onDone(res);
    } catch (err) {
      go.disabled = false;
      showToast(err.message, true);
    }
  });
}

/* The bar that appears once you are picking. Says how many are selected and
 * refuses the merge before opening the dialog when the selection cannot be
 * merged, rather than letting you get one screen further and then fail. */
function renderMergeBar() {
  const bar = document.getElementById("entity-merge-bar");
  if (!bar) return;
  bar.hidden = !entityPickMode;
  if (!entityPickMode) return;
  const n = entityPicked.size;
  document.getElementById("entity-merge-count").textContent =
    n === 0 ? "Nothing selected" : `${n} selected`;
  document.getElementById("entity-merge-go").disabled = n < 2;
  // Merging needs two records; deleting needs one. The delete button exists
  // at all only for an admin — removed rather than disabled, since a
  // disabled control is an invitation to ask why.
  const del = document.getElementById("entity-delete-go");
  if (del) {
    del.hidden = !(state.user && state.user.role === "admin");
    del.disabled = n < 1;
  }
}

(function wireEntityMergeBar() {
  const toggle = document.getElementById("entity-pick-toggle");
  if (toggle) toggle.addEventListener("click", () => setEntityPickMode(!entityPickMode));
  const clear = document.getElementById("entity-merge-clear");
  if (clear) clear.addEventListener("click", () => { entityPicked.clear(); loadEntities(); });
  const go = document.getElementById("entity-merge-go");
  if (go) go.addEventListener("click", () => {
    openMergeDialog([...entityPicked], () => {
      entityPicked.clear();
      setEntityPickMode(false);
    });
  });
  const del = document.getElementById("entity-delete-go");
  if (del) del.addEventListener("click", () => {
    openDeleteDialog({ entity_ids: [...entityPicked] }, () => {
      entityPicked.clear();
      setEntityPickMode(false);
    });
  });
})();

function setEntityPickMode(on) {
  entityPickMode = on;
  if (!on) entityPicked.clear();
  const toggle = document.getElementById("entity-pick-toggle");
  if (toggle) toggle.textContent = on ? "Done selecting" : "Select to merge";
  loadEntities();
}

function detailFieldInputHtml(field, value) {
  const val = value === undefined || value === null ? "" : value;
  if (field.type === "text-list") {
    const joined = Array.isArray(val) ? val.join(", ") : val;
    return `<div class="form-row"><label>${escapeHtml(field.label)}</label><input type="text" data-detail="${field.key}" data-listtype="csv" value="${escapeHtml(joined)}"></div>`;
  }
  if (field.type === "textarea") {
    return `<div class="form-row"><label>${escapeHtml(field.label)}</label><textarea rows="${field.rows || 2}" data-detail="${field.key}">${escapeHtml(val)}</textarea></div>`;
  }
  if (field.type === "select") {
    // A stored value the list no longer offers is still shown, and still
    // selected. Without this, opening a record to change its occupation would
    // quietly re-save its alignment as blank because the option had been
    // retired underneath it — the app would be rewriting somebody's own
    // assessment as a side effect of an unrelated edit. Marked as retired so
    // it is obvious why it is not in the list for anything else.
    const choices = val && !field.options.includes(val)
      ? field.options.concat([val]) : field.options;
    const opts = choices.map((o) => {
      const retired = o && !field.options.includes(o);
      return `<option value="${escapeHtml(o)}" ${o === val ? "selected" : ""}>${
        o ? escapeHtml(o) + (retired ? " (retired)" : "") : "—"}</option>`;
    }).join("");
    const selectHint = field.hint ? `<p class="field-hint">${escapeHtml(field.hint)}</p>` : "";
    return `<div class="form-row"><label>${escapeHtml(field.label)}</label><select data-detail="${field.key}">${opts}</select>${selectHint}</div>`;
  }
  // type="number" defaults to step="1" — without step="any" the browser's
  // native validation silently rejects any decimal (lat/lng need several
  // places) and blocks form submission entirely, with no visible error.
  const stepAttr = field.type === "number" ? ' step="any"' : "";
  // field.clearable (currently just location's lat/lng) opts a field out of
  // collectDetails' normal "blank = leave unchanged" behavior — see there
  // for why lat/lng specifically need to be actually clearable rather than
  // just editable: it's the documented way to force a re-geocode.
  const clearableAttr = field.clearable ? ' data-clearable="true"' : "";
  // field.hint is for the fields whose name doesn't carry their meaning —
  // "Relevant until" next to "Ended at" being the case this was added for.
  const hint = field.hint ? `<p class="field-hint">${escapeHtml(field.hint)}</p>` : "";
  const ph = field.placeholder ? ` placeholder="${escapeHtml(field.placeholder)}"` : "";
  return `<div class="form-row"><label>${escapeHtml(field.label)}</label><input type="${field.type}"${stepAttr}${clearableAttr}${ph} data-detail="${field.key}" value="${escapeHtml(val)}">${hint}</div>`;
}

function collectDetails(container) {
  const details = {};
  container.querySelectorAll("[data-detail]").forEach((input) => {
    const key = input.dataset.detail;
    let val = input.value;
    if (val === "") {
      // Normally a blank field is omitted entirely rather than forcing it
      // to null/0, so an edit that doesn't touch a field can't accidentally
      // wipe it. A field marked clearable (see detailFieldInputHtml) is the
      // deliberate exception — for those, blank means "actually clear this"
      // and needs to reach the server as an explicit null, not be dropped.
      if (input.dataset.clearable === "true") details[key] = null;
      return;
    }
    if (input.dataset.listtype === "csv") {
      val = val.split(",").map((s) => s.trim()).filter(Boolean);
    } else if (input.type === "number") {
      val = parseFloat(val);
    }
    details[key] = val;
  });
  return details;
}

// Detail fields that are data rather than prose. Kept deliberately short: the
// test is whether a reader ever compares two of these character by character
// or reads one aloud. A date or an occupation does not qualify.
const MONO_DETAIL_FIELDS = new Set([
  "lat", "lng", "maidenhead_grid", "medium_detail", "website",
]);

function renderDetailFields(entityType, details) {
  const fields = DETAIL_FIELDS[entityType] || [];
  const rows = fields
    .filter((f) => details && details[f.key] !== null && details[f.key] !== undefined && details[f.key] !== "")
    .map((f) => {
      let val = details[f.key];
      if (Array.isArray(val)) val = val.join(", ");
      // medium_detail's meaning depends on medium (a phone number, a
      // frequency, a channel, ...) — show the specific label here too,
      // not just in the edit form, so "Detail: 555-0100" doesn't read as
      // more generic than it is.
      let label = f.label;
      if (entityType === "communication" && f.key === "medium_detail") {
        label = COMMUNICATION_MEDIUM_DETAIL_LABELS[details.medium] || f.label;
      }
      // Identifiers, coordinates and frequencies stay monospace on the
      // palettes whose UI face is a sans — you read a grid square character by
      // character, and a proportional face makes that harder, not prettier.
      const monoField = MONO_DETAIL_FIELDS.has(f.key);
      return `<div class="field-row"><span class="k">${escapeHtml(label)}</span>`
        + `<span class="${monoField ? "v-mono" : ""}">${escapeHtml(String(val))}</span></div>`;
    });
  // Maidenhead grid isn't in DETAIL_FIELDS.location (it's server-derived,
  // never user-editable — see api/entities.py's _apply_location_derived_fields)
  // so it doesn't go through the generic filter/map above; appended here as
  // a synthetic read-only row whenever one's been computed.
  if (entityType === "location" && details && details.maidenhead_grid) {
    rows.push(`<div class="field-row"><span class="k">Maidenhead grid</span><span>${escapeHtml(details.maidenhead_grid)}</span></div>`);
  }
  if (entityType === "record") return recordDetailHtml(rows, details || {});
  return rows.length ? rows.join("") : '<p class="empty-state">No additional details.</p>';
}

document.getElementById("entity-detail-body").addEventListener("click", (e) => {
  const btn = e.target.closest("[data-open-document]");
  if (!btn) return;
  e.preventDefault();
  openDocument(btn.dataset.openDocument);
});

// A Record's full text reads as a document, not as one more field row.
function recordDetailHtml(rows, details) {
  const meta = rows.filter((r) => !r.includes(">Full text<"));
  const original = details.source_attachment_id
    ? `<div class="field-row"><span class="k">Original file</span><span>
         <button type="button" class="btn-link" data-open-document="${Number(details.source_attachment_id)}">Open the document</button>
       </span></div>`
    : "";
  const body = (details.body || "").trim();
  return `${meta.join("")}${original}
    <div class="record-body-head">Full text</div>
    ${body ? `<pre class="record-body">${escapeHtml(body)}</pre>`
           : '<p class="empty-state">No text on this Record.</p>'}`;
}

// Location's geocode_status/geocode_error also aren't in DETAIL_FIELDS (same
// reason as the grid above), but unlike the grid they're actionable — a
// 'pending'/'processing' row is informational, a 'failed' one gets a Retry
// button — so this returns real markup for renderEntityDetail to drop into
// the page, rather than a plain field-row string like renderDetailFields.
function locationGeocodeStatusHtml(details) {
  if (!details) return "";
  const status = details.geocode_status;
  if (status === "pending" || status === "processing") {
    return '<div class="geocode-status geocode-pending">Looking up coordinates for this address…</div>';
  }
  if (status === "failed") {
    return `<div class="geocode-status geocode-failed">Geocoding failed: ${escapeHtml(details.geocode_error || "unknown error")}
      <button class="btn-link btn-sm" id="retry-geocode-btn">Retry</button></div>`;
  }
  return "";
}

// Communication's medium_detail field means something different per medium
// (phone number vs. frequency vs. channel vs. email address, ...) — this
// keeps the label/placeholder on the actual <input> in sync with whatever
// medium is currently selected in the same form. A no-op if this container
// isn't showing communication's fields (no matching selectors), so it's
// safe to call unconditionally after any detail-fields render.
function wireCommunicationMediumDetail(container) {
  const mediumSelect = container.querySelector('[data-detail="medium"]');
  const detailInput = container.querySelector('[data-detail="medium_detail"]');
  if (!mediumSelect || !detailInput) return;
  const label = detailInput.closest(".form-row").querySelector("label");
  const update = () => {
    const text = COMMUNICATION_MEDIUM_DETAIL_LABELS[mediumSelect.value] || "Detail";
    if (label) label.textContent = text;
    detailInput.placeholder = text;
  };
  mediumSelect.addEventListener("change", update);
  update();
}

// Wraps DETAIL_FIELDS[entityType].map(detailFieldInputHtml).join("") with a
// location-specific hint about the address -> coordinates automation, since
// that behavior isn't otherwise discoverable from the plain lat/lng inputs.
// Used both for the form's initial render and its type-change handler (see
// openEntityForm) so the hint shows up either way, not just on first paint.
function entityFormDetailsHtml(entityType, existingDetails) {
  let html = DETAIL_FIELDS[entityType].map((f) => detailFieldInputHtml(f, existingDetails ? existingDetails[f.key] : null)).join("");
  if (entityType === "location") {
    html += '<p class="field-hint">Leave latitude/longitude blank to look them up from the address.</p>';
  }
  return html;
}

function openEntityForm(entity, prefill, opts) {
  const isEdit = !!entity;
  // `prefill` only applies on create (e.g. dropping a pin on the Map view
  // pre-fills type=location plus the clicked lat/lng) — an edit always
  // starts from the entity's own real values.
  // prefill carries a name as well as a type and details: selecting a name
  // in a document and pressing Record opens this form with it already in.
  const type = entity ? entity.entity_type : (prefill && prefill.entity_type) || "person";
  const prefillDetails = (!isEdit && prefill && prefill.details) || null;
  const html = `
    <h2>${isEdit ? "Edit" : "New"} Entity</h2>
    <form id="entity-form">
      <div class="form-row">
        <label>Type</label>
        <select id="entity-form-type" ${isEdit ? "disabled" : ""}>
          ${Object.keys(DETAIL_FIELDS).map((t) => `<option value="${t}" ${t === type ? "selected" : ""}>${ENTITY_TYPE_LABELS[t] || t}</option>`).join("")}
        </select>
      </div>
      <div class="form-row"><label>Name</label><input type="text" id="entity-form-name" required value="${escapeHtml(isEdit ? entity.name : (prefill && prefill.name) || "")}"></div>
      <div class="form-row"><label>Description</label><textarea id="entity-form-description" rows="2">${isEdit ? escapeHtml(entity.description || "") : ""}</textarea></div>
      <div id="entity-form-details">${entityFormDetailsHtml(type, isEdit ? entity.details : prefillDetails)}</div>
      <p class="form-error" id="entity-form-error"></p>
      <div class="modal-actions">
        <button type="button" class="btn-secondary" id="entity-form-cancel">Cancel</button>
        <button type="submit" class="btn-primary">${isEdit ? "Save" : "Create"}</button>
      </div>
    </form>
  `;
  openModal(html);
  wireCommunicationMediumDetail(document.getElementById("entity-form-details"));
  document.getElementById("entity-form-cancel").addEventListener("click", closeModal);
  if (!isEdit) {
    document.getElementById("entity-form-type").addEventListener("change", (e) => {
      document.getElementById("entity-form-details").innerHTML = entityFormDetailsHtml(e.target.value, null);
      wireCommunicationMediumDetail(document.getElementById("entity-form-details"));
    });
  }
  document.getElementById("entity-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const name = document.getElementById("entity-form-name").value.trim();
    const description = document.getElementById("entity-form-description").value.trim() || null;
    const entityType = document.getElementById("entity-form-type").value;
    const details = collectDetails(document.getElementById("entity-form-details"));
    try {
      if (isEdit) {
        await api(`/api/entities/${entity.id}`, { method: "PATCH", body: { name, description, details } });
        closeModal();
        showToast("Entity updated");
        openEntityDetail(entity.id);
      } else {
        // opts.create lets a caller make the record its own way — the
        // document page posts to the document instead, so the new record
        // carries a provenance row saying which document it came out of.
        const created = opts && opts.create
          ? await opts.create({ entity_type: entityType, name, description, details })
          : await api("/api/entities", { method: "POST", body: { entity_type: entityType, name, description, details } });
        closeModal();
        showToast("Entity created");
        if (opts && opts.onCreated) opts.onCreated(created);
        else openEntityDetail(created.id);
      }
    } catch (err) {
      document.getElementById("entity-form-error").textContent = err.message;
    }
  });
}

async function openEntityDetail(id) {
  setActiveTab("entities"); // correct even when reached from Dashboard/Map, not just the Entities list
  switchViewRaw("entity-detail");
  const el = document.getElementById("entity-detail-body");
  el.innerHTML = '<p class="empty-state">Loading…</p>';
  try {
    const entity = await api(`/api/entities/${id}`);
    state.currentEntityId = id;
    renderEntityDetail(entity);
  } catch (err) {
    el.innerHTML = `<p class="empty-state">${escapeHtml(err.message)}</p>`;
  }
}

/* ---------------------------------------------------------------------------
 * Contact points
 *
 * Directory information — how you would actually reach this entity — as a
 * repeatable list rather than a phone/email/address triple, because an
 * organisation has a switchboard AND a press office AND an after-hours number
 * and picking one of them to keep is not a decision this app should force.
 *
 * A contact point is not a Communication entity. A Communication is a channel
 * that is itself of interest (a radio net being monitored); a contact point is
 * the office number. See the docstring in api/contacts.py.
 * ------------------------------------------------------------------------- */

// Must match CONTACT_KINDS in api/contacts.py.
const CONTACT_KINDS = ["Phone", "Mobile", "Email", "Address", "Radio",
                       "Messaging", "Social", "Website", "Other"];
const CONTACTABLE_TYPES = ["person", "organization", "location"];

function contactRowHtml(c) {
  const label = c.label ? ` <span class="contact-label">${escapeHtml(c.label)}</span>` : "";
  return `
    <li class="contact-row" data-contact-id="${c.id}">
      <span class="contact-main">
        <span class="contact-kind">${escapeHtml(c.kind)}</span>${label}
        <span class="contact-value">${escapeHtml(c.value)}</span>
        ${c.is_preferred ? '<span class="chip-preferred" title="Included in PDF exports">preferred</span>' : ""}
        ${c.notes ? `<span class="contact-note">${escapeHtml(c.notes)}</span>` : ""}
      </span>
      <span class="contact-actions">
        <button class="btn-link btn-sm" data-edit-contact="${c.id}">Edit</button>
        <button class="btn-link btn-sm" data-delete-contact="${c.id}">Remove</button>
      </span>
    </li>`;
}

function contactSectionHtml(entity) {
  if (!CONTACTABLE_TYPES.includes(entity.entity_type)) return "";
  const rows = (entity.contacts || []).map(contactRowHtml).join("");
  return `
    <div class="section-title">Contact</div>
    <p class="field-hint">Only <span class="chip-preferred">preferred</span> contacts appear in PDF exports.</p>
    <ul class="contact-list" id="entity-contact-list">${rows || '<li class="empty-state">None recorded.</li>'}</ul>
    <button class="btn-secondary btn-sm" id="add-contact-btn" style="margin-top:.6em;">+ Add contact</button>`;
}

function openContactForm(entity, existing) {
  const c = existing || { kind: "Phone", label: "", value: "", notes: "", is_preferred: false };
  openModal(`
    <h2>${existing ? "Edit" : "Add"} contact — ${escapeHtml(entity.name)}</h2>
    <form id="contact-form" class="modal-form">
      <label>Type
        <select id="contact-kind">
          ${CONTACT_KINDS.map((k) =>
            `<option value="${k}"${k === c.kind ? " selected" : ""}>${k}</option>`).join("")}
        </select>
      </label>
      <label>Label <span class="field-hint-inline">optional — "work", "switchboard", "after hours"</span>
        <input type="text" id="contact-label" maxlength="120" value="${escapeHtml(c.label || "")}">
      </label>
      <label>Value
        <input type="text" id="contact-value" maxlength="500" required value="${escapeHtml(c.value || "")}">
      </label>
      <label>Notes <span class="field-hint-inline">optional</span>
        <textarea id="contact-notes" rows="2">${escapeHtml(c.notes || "")}</textarea>
      </label>
      <label class="checkbox-row">
        <input type="checkbox" id="contact-preferred"${c.is_preferred ? " checked" : ""}>
        Preferred — include this one in exported PDFs
      </label>
      <p class="form-error" id="contact-error"></p>
      <div class="modal-actions">
        <button type="button" class="btn-secondary" id="contact-cancel">Cancel</button>
        <button type="submit" class="btn-primary">${existing ? "Save" : "Add"}</button>
      </div>
    </form>
  `);
  document.getElementById("contact-cancel").addEventListener("click", closeModal);
  document.getElementById("contact-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const body = {
      kind: document.getElementById("contact-kind").value,
      label: document.getElementById("contact-label").value.trim() || null,
      value: document.getElementById("contact-value").value.trim(),
      notes: document.getElementById("contact-notes").value.trim() || null,
      is_preferred: document.getElementById("contact-preferred").checked,
    };
    try {
      if (existing) {
        await api(`/api/contacts/${existing.id}`, { method: "PATCH", body });
      } else {
        await api(`/api/entities/${entity.id}/contacts`, { method: "POST", body });
      }
      closeModal();
      showToast(existing ? "Contact updated" : "Contact added");
      openEntityDetail(entity.id);
    } catch (err) {
      document.getElementById("contact-error").textContent = err.message;
    }
  });
}

function wireContactSection(el, entity) {
  const addBtn = document.getElementById("add-contact-btn");
  if (addBtn) addBtn.addEventListener("click", () => openContactForm(entity));
  el.querySelectorAll("[data-edit-contact]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const c = (entity.contacts || []).find((x) => String(x.id) === btn.dataset.editContact);
      if (c) openContactForm(entity, c);
    });
  });
  el.querySelectorAll("[data-delete-contact]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      if (!confirm("Remove this contact detail?")) return;
      try {
        await api(`/api/contacts/${btn.dataset.deleteContact}`, { method: "DELETE" });
        openEntityDetail(entity.id);
      } catch (err) { showToast(err.message, true); }
    });
  });
}

// `reads_as` is the relationship worded for the entity whose page this is
// (see entities.get_entity): the one stored row "Anna child_of Boris" reads
// as "child of Boris" on Anna's page and "parent of Anna" on Boris's. Falling
// back to relationship_type keeps this working against an older API.
// Message precedence. Must match CRITICALITY_LEVELS in api/reports.py.
const CRITICALITY_LEVELS = ["Flash", "Immediate", "Priority", "Routine"];

// Unset renders nothing at all rather than a "Routine" default: a report
// nobody has rated is not a report someone judged to be routine, and printing
// one as the other would be the app inventing an assessment.
function criticalityBadgeHtml(report) {
  if (!report || !report.criticality) return "";
  const slug = report.criticality.toLowerCase();
  return `<span class="crit-badge crit-${slug}" title="Message precedence">${escapeHtml(report.criticality)}</span>`;
}

function relWording(r) {
  return (r.reads_as || r.relationship_type).replace(/_/g, " ");
}

/* A relationship is a claim, and claims get revised. Everything extraction and
 * the signal pass produce arrives as "possible" — that is the honest default
 * for something a machine proposed — and the whole point of reviewing a case
 * file is that some of those become probable and a few become confirmed. Until
 * now the only way to say so was to delete the relationship and type it again,
 * which loses the discovery date and the notes with it. So: edit in place.
 * The API has always supported this (PATCH /api/relationships/{id}); it was
 * only ever the UI that insisted a relationship was written in stone. */
const CONFIDENCE_LEVELS_UI = ["possible", "probable", "confirmed"];

function relRowHtml(r) {
  const arrow = r.direction === "outgoing" ? "→" : "←";
  return `
    <li class="rel-row" data-rel-id="${r.id}">
      <div class="rel-line">
        <span class="rel-main">
          <span class="rel-arrow">${arrow}</span>
          <strong>${escapeHtml(relWording(r))}</strong>
          <a href="#" data-open-entity="${escapeHtml(r.other_entity_id)}">${nameHtml(r.other_entity_name, r.other_entity_alignment)}</a>
          <span class="type-pill type-${r.other_entity_type}">${escapeHtml(r.other_entity_type)}</span>
          <span class="confidence-${r.confidence}">${escapeHtml(r.confidence)}</span>
          ${r.discovery_date ? `<span class="rel-discovery-date">discovered ${escapeHtml(r.discovery_date)}</span>` : ""}
        </span>
        <span class="rel-row-actions">
          <button class="btn-link btn-sm" data-edit-rel="${r.id}" aria-expanded="false">Edit</button>
          <button class="btn-link btn-sm" data-delete-rel="${r.id}">Remove</button>
        </span>
      </div>
      ${r.notes ? `<div class="rel-notes">${escapeHtml(r.notes)}</div>` : ""}
      <form class="rel-edit" data-rel-form="${r.id}" hidden>
        <div class="rel-edit-grid">
          <label>Type
            <input type="text" data-field="relationship_type" list="rel-edit-type-list"
                   value="${escapeHtml(r.relationship_type)}" required>
          </label>
          <label>Confidence
            <select data-field="confidence">
              ${CONFIDENCE_LEVELS_UI.map((c) =>
                `<option value="${c}" ${c === r.confidence ? "selected" : ""}>${c}</option>`).join("")}
            </select>
          </label>
          <label>Discovered
            <input type="date" data-field="discovery_date" value="${escapeHtml(r.discovery_date || "")}">
          </label>
        </div>
        <label class="rel-edit-notes">Notes
          <textarea rows="2" data-field="notes">${escapeHtml(r.notes || "")}</textarea>
        </label>
        <div class="rel-edit-actions">
          <button type="submit" class="btn-primary btn-sm">Save</button>
          <button type="button" class="btn-secondary btn-sm" data-cancel-rel="${r.id}">Cancel</button>
        </div>
      </form>
    </li>
  `;
}

/* The datalist the type input points at lives once on the page rather than
 * once per relationship row — twenty rows would otherwise mean twenty copies
 * of the same eighteen options. */
function ensureRelationshipTypeList() {
  // Its own id, not the add-relationship dialog's: two datalists sharing one
  // id would leave whichever the dialog rendered second empty.
  if (!document.getElementById("rel-edit-type-list")) {
    const dl = document.createElement("datalist");
    dl.id = "rel-edit-type-list";
    document.body.appendChild(dl);
  }
  loadRelationshipTypesInto("rel-edit-type-list");
}

function wireRelationshipEditing(el, entity) {
  ensureRelationshipTypeList();

  const closeAll = () => {
    el.querySelectorAll("[data-rel-form]").forEach((f) => { f.hidden = true; });
    el.querySelectorAll("[data-edit-rel]").forEach((b) => {
      b.setAttribute("aria-expanded", "false");
      b.textContent = "Edit";
    });
  };

  el.querySelectorAll("[data-edit-rel]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const form = el.querySelector(`[data-rel-form="${btn.dataset.editRel}"]`);
      const opening = form.hidden;
      closeAll();                       // one open editor at a time
      form.hidden = !opening;
      btn.setAttribute("aria-expanded", String(opening));
      btn.textContent = opening ? "Close" : "Edit";
      if (opening) form.querySelector("[data-field=relationship_type]").focus();
    });
  });

  el.querySelectorAll("[data-cancel-rel]").forEach((btn) => {
    btn.addEventListener("click", closeAll);
  });

  el.querySelectorAll("[data-rel-form]").forEach((form) => {
    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const value = (field) => form.querySelector(`[data-field=${field}]`).value.trim();
      const body = {
        // Same normalisation the add form does, so "Employed By" typed here
        // is not rejected by the API's snake_case rule.
        relationship_type: value("relationship_type").toLowerCase().replace(/\s+/g, "_"),
        confidence: value("confidence"),
        // Cleared rather than skipped: blanking a wrong date has to be
        // possible, and PATCH with an explicit null is how you say so.
        discovery_date: value("discovery_date") || null,
        notes: value("notes") || null,
      };
      if (!body.relationship_type) {
        showToast("A relationship needs a type", true);
        return;
      }
      try {
        await api(`/api/relationships/${form.dataset.relForm}`, { method: "PATCH", body });
        showToast("Relationship updated");
        openEntityDetail(entity.id);
      } catch (err) { showToast(err.message, true); }
    });
  });
}

/* ---------------------------------------------------------------------------
 * Attachments: rows, thumbnails, and the preview lightbox
 * ------------------------------------------------------------------------- */

// Driven off the stored mime_type rather than the filename extension: the
// extension is derived from a display name the uploader controls, while
// mime_type is what the browser reported for the actual bytes.
function isPreviewableImage(a) {
  return (a.mime_type || "").startsWith("image/");
}
function isPreviewablePdf(a) {
  return (a.mime_type || "") === "application/pdf";
}
function isPreviewable(a) {
  return isPreviewableImage(a) || isPreviewablePdf(a);
}

function formatFileSize(bytes) {
  if (!bytes && bytes !== 0) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function attachmentRowHtml(a) {
  const previewable = isPreviewable(a);
  // Real images get a thumbnail of themselves; everything else gets a small
  // type label, so the list scans as "which file is which" rather than a
  // column of near-identical filenames.
  const thumb = isPreviewableImage(a)
    ? `<img class="attachment-thumb" src="/api/attachments/${a.id}/file?inline=1" alt="" loading="lazy">`
    : `<span class="attachment-thumb attachment-thumb-icon">${escapeHtml(isPreviewablePdf(a) ? "PDF" : "FILE")}</span>`;

  const meta = [
    formatFileSize(a.file_size_bytes),
    a.uploaded_at ? `uploaded ${relativeTime(a.uploaded_at)}` : "",
    a.extraction_status,
  ].filter(Boolean).join(" · ");

  const nameEl = previewable
    ? `<button type="button" class="attachment-name link-like" data-preview-att="${a.id}">${escapeHtml(a.filename)}</button>`
    : `<a class="attachment-name" href="/api/attachments/${a.id}/file" target="_blank" rel="noopener">${escapeHtml(a.filename)}</a>`;

  return `
    <li class="attachment-row" data-att-id="${a.id}">
      ${previewable ? `<button type="button" class="attachment-thumb-btn" data-preview-att="${a.id}" title="Preview">${thumb}</button>` : thumb}
      <span class="attachment-main">
        ${nameEl}
        <span class="card-meta">${escapeHtml(meta)}</span>
      </span>
      <span class="attachment-row-actions">
        ${previewable ? `<button type="button" class="btn-link btn-sm" data-preview-att="${a.id}">Preview</button>` : ""}
        <a class="btn-link btn-sm" href="/api/attachments/${a.id}/file">Download</a>
        <button type="button" class="btn-link btn-sm" data-delete-att="${a.id}">Delete</button>
      </span>
    </li>
  `;
}

// Attachments are rendered in two places (entity detail and report detail),
// so the preview wiring lives here and is called from both rather than
// duplicated. `attachments` is the array that produced the rows, so the
// preview knows each file's type without re-fetching it.
function wireAttachmentPreviews(container, attachments) {
  const byId = {};
  (attachments || []).forEach((a) => { byId[String(a.id)] = a; });
  container.querySelectorAll("[data-preview-att]").forEach((el) => {
    el.addEventListener("click", (e) => {
      e.preventDefault();
      const a = byId[el.dataset.previewAtt];
      if (a) openAttachmentPreview(a);
    });
  });
}

function openAttachmentPreview(a) {
  const backdrop = document.getElementById("preview-backdrop");
  const body = document.getElementById("preview-body");
  const src = `/api/attachments/${a.id}/file?inline=1`;

  document.getElementById("preview-title").textContent = a.filename;
  const dl = document.getElementById("preview-download");
  dl.href = `/api/attachments/${a.id}/file`;
  dl.setAttribute("download", a.filename || "");

  if (isPreviewableImage(a)) {
    body.innerHTML = `<img class="preview-image" src="${escapeHtml(src)}" alt="${escapeHtml(a.filename)}">`;
  } else if (isPreviewablePdf(a)) {
    // <iframe> rather than <embed>/<object>: it's the one that reliably falls
    // back to *something* visible in every browser, and the Download button
    // above is always there if the built-in viewer refuses.
    body.innerHTML = `<iframe class="preview-pdf" src="${escapeHtml(src)}" title="${escapeHtml(a.filename)}"></iframe>`;
  } else {
    body.innerHTML = `<p class="empty-state">No preview available for this file type — use Download.</p>`;
  }
  backdrop.hidden = false;
}

function closeAttachmentPreview() {
  const backdrop = document.getElementById("preview-backdrop");
  backdrop.hidden = true;
  // Emptied on close so a large PDF/image isn't left decoded in memory, and
  // so an <iframe> can't keep loading behind a hidden overlay.
  document.getElementById("preview-body").innerHTML = "";
}

document.getElementById("preview-close").addEventListener("click", closeAttachmentPreview);
document.getElementById("preview-backdrop").addEventListener("click", (e) => {
  if (e.target.id === "preview-backdrop") closeAttachmentPreview();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !document.getElementById("preview-backdrop").hidden) {
    closeAttachmentPreview();
  }
});

/* One record and everything it touches, drawn with the same renderer as the
 * Entities page. The focal record is ringed; its neighbours are graded by
 * their own relationship counts, which is how you spot that the person you
 * are reading about is standing next to a hub. Every name is labelled here —
 * a neighbourhood is small enough that labelling all of it is readable, which
 * is not true of the whole file. */
let detailNetwork = null;

// The entity currently drawn in the graph, kept so a theme change can redraw
// it in the new palette without a round trip to the server.
let lastGraphEntity = null;

function renderEntityGraph(entity) {
  const container = document.getElementById("entity-graph");
  if (!container) return;
  lastGraphEntity = entity;

  if (!entity.relationships.length) {
    container.innerHTML = '<p class="empty-state">No relationships yet — nothing to draw.</p>';
    detailNetwork = null;
    return;
  }

  container.innerHTML = `
    <div class="graph-canvas-wrap detail-graph-wrap">
      <canvas id="detail-graph-canvas"></canvas>
      <div id="detail-graph-tip" class="graph-tip" hidden></div>
    </div>
    <p class="graph-drag-hint detail-graph-hint">Click a dot for a summary · drag to arrange · double-click to release</p>`;

  // The focal record's own degree is its relationship count; a neighbour's is
  // whatever the tree last learned from the graph endpoint, falling back to
  // "at least this one" so a neighbour never renders as a zero-size dot.
  const nodes = [{
    id: entity.id, name: entity.name, entity_type: entity.entity_type,
    degree: entity.relationships.length,
  }];
  const seen = new Set([entity.id]);
  const edges = [];
  for (const r of entity.relationships) {
    if (!seen.has(r.other_entity_id)) {
      nodes.push({
        id: r.other_entity_id, name: r.other_entity_name,
        entity_type: r.other_entity_type,
        degree: entityDegrees.get(r.other_entity_id) || 1,
      });
      seen.add(r.other_entity_id);
    }
    // The stored direction, not reads_as: an edge is drawn between two points
    // and the stored pair is the one the wording belongs to.
    edges.push(r.direction === "outgoing"
      ? { from: entity.id, to: r.other_entity_id }
      : { from: r.other_entity_id, to: entity.id });
  }

  detailNetwork = createNetwork({
    canvasId: "detail-graph-canvas",
    tipId: "detail-graph-tip",
    focusId: entity.id,
    labelCount: -1,                       // small enough to label everything
    onPick: (id, x, y) => { if (id !== entity.id) openSummaryCard(id, x, y); },
  });
  detailNetwork.setData(nodes, edges, Math.max(...nodes.map((n) => n.degree)));
}

function restyleEntityGraph() {
  if (lastGraphEntity && document.getElementById("entity-graph")) {
    renderEntityGraph(lastGraphEntity);
  }
  // Both networks paint to a canvas, which cannot inherit anything: the
  // palette was read from the stylesheet at draw time, so a theme change
  // means drawing again or leaving the old theme's colours on the new
  // background.
  entityNetwork.redraw();
  renderGraphLegend();
}

function renderEntityDetail(entity) {
  const el = document.getElementById("entity-detail-body");
  el.innerHTML = `
    <div class="detail-panel">
      <div class="detail-header">
        <div>
          <span class="type-pill type-${entity.entity_type}">${escapeHtml(entity.entity_type)}</span>
          <h2>${nameHtml(entity.name, (entity.details || {}).alignment)} ${entity.is_active ? "" : '<span class="status-pill">ARCHIVED</span>'}
            ${personStatusBadgesHtml({ ...(entity.details || {}), entity_type: entity.entity_type })}
            ${eventExpiryBadgeHtml(entity)}</h2>
          ${entity.description ? `<p>${escapeHtml(entity.description)}</p>` : ""}
        </div>
        <div class="detail-actions">
          <button class="btn-secondary btn-sm" id="entity-actions-btn"
                  title="Also on right-click, anywhere this record appears">Actions &#9662;</button>
          <button class="btn-secondary btn-sm" id="edit-entity-btn">Edit</button>
          <button class="btn-secondary btn-sm" id="export-entity-btn" aria-expanded="false"
                  aria-controls="export-shape-menu">Export &#9662;</button>
          <button class="btn-secondary btn-sm" id="toggle-active-btn">${entity.is_active ? "Archive" : "Reactivate"}</button>
          ${canDelete() ? '<button class="btn-danger btn-sm" id="delete-entity-btn">Delete</button>' : ""}
        </div>
        <div class="export-menu" id="export-shape-menu" role="menu" hidden>
          ${exportShapeMenuHtml(entity)}
        </div>
      </div>

      <div class="section-title">Details</div>
      ${entity.entity_type === "location" ? locationGeocodeStatusHtml(entity.details) : ""}
      <div>${renderDetailFields(entity.entity_type, entity.details)}</div>
      ${entity.entity_type === "location" ? '<div id="entity-zones"></div>' : ""}

      ${contactSectionHtml(entity)}

      <div class="section-title">Relationship graph</div>
      <div id="entity-graph"></div>

      <div class="section-title">Relationships</div>
      <ul class="rel-list" id="entity-rel-list">${entity.relationships.map(relRowHtml).join("") || '<li class="empty-state">None yet.</li>'}</ul>
      <button class="btn-secondary btn-sm" id="add-rel-btn" style="margin-top:.6em;">+ Add relationship</button>

      <div class="section-title">Linked reports</div>
      <ul class="report-chip-list">${entity.reports.length ? entity.reports.map((r) => `<li><a href="#" data-open-report="${escapeHtml(r.id)}">${escapeHtml(r.title)}</a> <span class="status-pill status-${r.status}">${escapeHtml(r.status)}</span></li>`).join("") : '<li class="empty-state">None yet.</li>'}</ul>

      <div class="section-title">Attachments</div>
      <ul class="attachment-list" id="entity-attachment-list">${(entity.attachments || []).map(attachmentRowHtml).join("") || '<li class="empty-state">None yet.</li>'}</ul>
      <form id="entity-upload-form" class="form-row-inline" style="margin-top:.6em;">
        <input type="file" id="entity-upload-file" required>
        <button type="submit" class="btn-secondary btn-sm">Upload</button>
      </form>
    </div>
  `;

  renderEntityGraph(entity);

  document.getElementById("edit-entity-btn").addEventListener("click", () => openEntityForm(entity));
  if (entity.entity_type === "location") loadZonesForEntity(entity.id);
  wireExportMenu(entity);
  document.getElementById("toggle-active-btn").addEventListener("click", async () => {
    try {
      await api(`/api/entities/${entity.id}`, { method: "PATCH", body: { is_active: !entity.is_active } });
      showToast(entity.is_active ? "Archived" : "Reactivated");
      openEntityDetail(entity.id);
    } catch (err) { showToast(err.message, true); }
  });
  document.getElementById("add-rel-btn").addEventListener("click", () => openRelationshipForm(entity));
  const actionsBtn = document.getElementById("entity-actions-btn");
  if (actionsBtn) {
    actionsBtn.addEventListener("click", (ev) => {
      ev.stopPropagation();          // the document listener would close it again
      const r = actionsBtn.getBoundingClientRect();
      openEntityMenu(entity.id, r.left, r.bottom + 4);
    });
  }
  const deleteEntityBtn = document.getElementById("delete-entity-btn");
  if (deleteEntityBtn) {
    deleteEntityBtn.addEventListener("click", () => openDeleteDialog(
      { entity_ids: [entity.id] }, () => switchView("entities")));
  }
  const retryGeocodeBtn = document.getElementById("retry-geocode-btn");
  if (retryGeocodeBtn) {
    retryGeocodeBtn.addEventListener("click", async () => {
      try {
        await api(`/api/entities/${entity.id}/retry-geocode`, { method: "POST" });
        showToast("Retrying geocode…");
        openEntityDetail(entity.id);
      } catch (err) { showToast(err.message, true); }
    });
  }
  el.querySelectorAll("[data-open-entity]").forEach((a) => {
    a.addEventListener("click", (e) => { e.preventDefault(); openEntityDetail(a.dataset.openEntity); });
  });
  el.querySelectorAll("[data-open-report]").forEach((a) => {
    a.addEventListener("click", (e) => { e.preventDefault(); openReportDetail(a.dataset.openReport); });
  });
  wireRelationshipEditing(el, entity);
  el.querySelectorAll("[data-delete-rel]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      if (!confirm("Remove this relationship?")) return;
      try {
        await api(`/api/relationships/${btn.dataset.deleteRel}`, { method: "DELETE" });
        openEntityDetail(entity.id);
      } catch (err) { showToast(err.message, true); }
    });
  });
  wireContactSection(el, entity);
  wireAttachmentPreviews(el, entity.attachments);
  el.querySelectorAll("[data-delete-att]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      if (!confirm("Delete this attachment?")) return;
      try {
        await api(`/api/attachments/${btn.dataset.deleteAtt}`, { method: "DELETE" });
        openEntityDetail(entity.id);
      } catch (err) { showToast(err.message, true); }
    });
  });
  document.getElementById("entity-upload-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const fileInput = document.getElementById("entity-upload-file");
    if (!fileInput.files.length) return;
    const fd = new FormData();
    fd.append("entity_id", entity.id);
    fd.append("file", fileInput.files[0]);
    try {
      await apiUpload("/api/attachments", fd);
      showToast("Uploaded");
      openEntityDetail(entity.id);
    } catch (err) { showToast(err.message, true); }
  });
}

// Seeded with the vocabulary the API validates against so a <select> built
// before the fetch lands is still correct; refreshed from the server on load
// so adding a type there doesn't mean editing this too.
let SUGGESTED_RELATIONSHIP_TYPES = [
  "affiliated_with", "employed_by", "member_of", "family_of", "associate_of",
  "spouse_of", "significant_of", "parent_of", "child_of", "sibling_of",
  "located_at", "present_at", "communicated_with", "reported_by",
  "owns", "controls", "financed_by", "in_conflict_with",
  "drives", "seen_in",
];

async function loadRelationshipTypesInto(datalistId) {
  try {
    const data = await api("/api/relationship-types");
    if (Array.isArray(data.suggested) && data.suggested.length) {
      SUGGESTED_RELATIONSHIP_TYPES = data.suggested;
    }
    document.getElementById(datalistId).innerHTML = data.suggested.map((t) => `<option value="${escapeHtml(t)}">`).join("");
  } catch (e) { /* the free-text input still works without suggestions */ }
}

function openRelationshipForm(entity) {
  const html = `
    <h2>Add relationship from "${escapeHtml(entity.name)}"</h2>
    <form id="rel-form">
      ${entityPickerHtml("To entity", "rel-to")}
      <div class="form-row">
        <label>Relationship type</label>
        <input type="text" id="rel-type" list="rel-type-list" placeholder="e.g. employed_by" required>
        <datalist id="rel-type-list"></datalist>
      </div>
      <div class="form-row">
        <label>Confidence</label>
        <select id="rel-confidence">
          <option value="possible">Possible</option>
          <option value="probable">Probable</option>
          <option value="confirmed">Confirmed</option>
        </select>
      </div>
      <div class="form-row">
        <label>Discovery date</label>
        <input type="date" id="rel-discovery-date" value="${new Date().toISOString().slice(0, 10)}">
        <p class="field-hint">When you learned of it.</p>
      </div>
      <div class="form-row"><label>Notes</label><textarea id="rel-notes" rows="2"></textarea></div>
      <p class="form-error" id="rel-form-error"></p>
      <div class="modal-actions">
        <button type="button" class="btn-secondary" id="rel-form-cancel">Cancel</button>
        <button type="submit" class="btn-primary">Add</button>
      </div>
    </form>
  `;
  openModal(html);
  wireEntityPicker("rel-to");
  loadRelationshipTypesInto("rel-type-list");
  document.getElementById("rel-form-cancel").addEventListener("click", closeModal);
  document.getElementById("rel-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const toId = document.getElementById("rel-to-id").value;
    if (!toId) { document.getElementById("rel-form-error").textContent = "Pick a valid entity from the list."; return; }
    const body = {
      from_entity_id: entity.id,
      to_entity_id: toId,
      relationship_type: document.getElementById("rel-type").value.trim().toLowerCase().replace(/\s+/g, "_"),
      confidence: document.getElementById("rel-confidence").value,
      discovery_date: document.getElementById("rel-discovery-date").value || null,
      notes: document.getElementById("rel-notes").value.trim() || null,
    };
    try {
      await api("/api/relationships", { method: "POST", body });
      closeModal();
      showToast("Relationship added");
      openEntityDetail(entity.id);
    } catch (err) {
      document.getElementById("rel-form-error").textContent = err.message;
    }
  });
}

/* ============================================================================
 * Map: OpenStreetMap view of Location entities — lets an analyst spot
 * events (and anything else) tied to the same place at a glance, and drop
 * a new Location entity by clicking the map instead of typing coordinates.
 * ========================================================================== */

/* ============================================================================
 * Basemaps: which tiles the maps draw, and where they come from
 *
 * Two places tiles can come from, and the difference matters to whoever is
 * looking at the screen:
 *
 *   - **live** — straight from the tile server, which needs a route out.
 *   - **downloaded** — from this app, out of a pack an admin downloaded
 *     earlier. Works with the network unplugged, and is blank outside the
 *     area that was downloaded.
 *
 * Everything that shows a map (the Map view, the debrief place-picker, the
 * admin area-picker) goes through addBasemap() so they all agree about which
 * one is in use, and so a deployment that only has packs never silently tries
 * to reach the internet.
 * ========================================================================== */

// A 1x1 transparent PNG, inline so it needs no request of its own.
const TRANSPARENT_TILE = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB"
  + "CAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==";

const MAP_SOURCE_KEY = "humint.map.source";
const MAP_OFFLINE_KEY = "humint.map.offline";

const mapState = {
  sources: [],
  sourceId: null,
  offlineOnly: false,
  layer: null,
  loaded: false,
  markersById: {},          // so "Show on the map" can find one pin again
};

function currentMapSource() {
  return mapState.sources.find((s) => s.id === mapState.sourceId) || mapState.sources[0] || null;
}

async function loadMapSources(force) {
  if (mapState.loaded && !force) return mapState.sources;
  try {
    const data = await api("/api/map/sources");
    mapState.sources = data.items || [];
  } catch (err) {
    mapState.sources = [];
  }
  mapState.loaded = true;

  let stored = null;
  try { stored = Number(localStorage.getItem(MAP_SOURCE_KEY)) || null; } catch (e) { /* storage off */ }
  if (!mapState.sources.some((s) => s.id === stored)) stored = null;
  mapState.sourceId = stored || (mapState.sources[0] && mapState.sources[0].id) || null;
  applyOfflineDefault();
  return mapState.sources;
}

/** Decide whether to draw from packs, for whichever source is now selected.
 *
 * A source with downloaded areas defaults to using them: somebody went to the
 * trouble of downloading that area, and the point of doing so was not to keep
 * asking the internet anyway. A source with none cannot, whatever the stored
 * preference says. In between, an explicit choice by this user wins -- but
 * only for as long as it remains possible.
 */
function applyOfflineDefault() {
  const src = currentMapSource();
  if (!src || !src.pack_count) { mapState.offlineOnly = false; return; }
  let saved = null;
  try { saved = localStorage.getItem(MAP_OFFLINE_KEY); } catch (e) { /* storage off */ }
  mapState.offlineOnly = saved === null ? true : saved === "true";
}

function basemapLayer() {
  const src = currentMapSource();
  if (!src || typeof L === "undefined") return null;
  const opts = {
    attribution: src.attribution ? escapeHtml(src.attribution) : "",
    minZoom: src.min_zoom,
    maxZoom: src.max_zoom,
  };
  if (mapState.offlineOnly) {
    // Served from this app. A tile nobody downloaded comes back 204, which an
    // <img> treats as a failed load -- so point Leaflet's error tile at a
    // transparent pixel. The server stays honest about having nothing there;
    // the map just shows empty ground instead of a broken-image grid.
    opts.errorTileUrl = TRANSPARENT_TILE;
    return L.tileLayer(`/api/map/tiles/${src.id}/{z}/{x}/{y}`, opts);
  }
  if (src.subdomains) opts.subdomains = src.subdomains;
  return L.tileLayer(src.url_template, opts);
}

function addBasemap(map) {
  const layer = basemapLayer();
  if (layer) layer.addTo(map);
  return layer;
}

function renderMapSourceControls() {
  const select = document.getElementById("map-source-select");
  if (!select) return;
  const src = currentMapSource();
  select.innerHTML = mapState.sources.length
    ? mapState.sources.map((s) => `
        <option value="${s.id}" ${s.id === mapState.sourceId ? "selected" : ""}>
          ${escapeHtml(s.name)}${s.pack_count ? " · downloaded" : ""}
        </option>`).join("")
    : '<option value="">No sources configured</option>';

  const offlineRow = document.getElementById("map-offline-row");
  const toggle = document.getElementById("map-offline-toggle");
  if (offlineRow && toggle) {
    offlineRow.hidden = !(src && src.pack_count);
    toggle.checked = mapState.offlineOnly;
  }

  const note = document.getElementById("map-source-note");
  if (!note) return;
  if (!src) {
    note.hidden = false;
    note.textContent = "No map sources are configured. An admin can add one under "
      + "Admin settings → Maps.";
  } else if (mapState.offlineOnly) {
    note.hidden = false;
    note.textContent = "Offline: showing downloaded areas only.";
  } else if (!src.pack_count) {
    note.hidden = false;
    note.textContent = `“${src.name}” is drawn live from its tile server, so it needs an `
      + "internet connection. Download an area under Admin settings → Maps to use it offline.";
  } else {
    note.hidden = true;
  }
}

function swapBasemap() {
  if (!leafletMap) return;
  if (mapState.layer) leafletMap.removeLayer(mapState.layer);
  mapState.layer = addBasemap(leafletMap);
  renderMapSourceControls();
}

function wireMapSourceControls() {
  const select = document.getElementById("map-source-select");
  const toggle = document.getElementById("map-offline-toggle");
  if (select) {
    select.addEventListener("change", () => {
      mapState.sourceId = Number(select.value) || null;
      applyOfflineDefault();
      try { localStorage.setItem(MAP_SOURCE_KEY, String(mapState.sourceId)); } catch (e) { /* storage off */ }
      swapBasemap();
    });
  }
  if (toggle) {
    toggle.addEventListener("change", () => {
      mapState.offlineOnly = toggle.checked;
      try { localStorage.setItem(MAP_OFFLINE_KEY, String(toggle.checked)); } catch (e) { /* storage off */ }
      swapBasemap();
    });
  }
}

/* ============================================================================
 * Admin: the map source registry and downloaded packs
 * ========================================================================== */

const mapAdmin = { sources: [], packs: [], pollTimer: null, picker: null };

function mapSourcePills(s) {
  const pills = [];
  if (s.pack_count) pills.push('<span class="map-pill map-pill-offline">offline ready</span>');
  else pills.push('<span class="map-pill map-pill-online">needs internet</span>');
  if (!s.allow_download) pills.push('<span class="map-pill map-pill-locked">no bulk download</span>');
  if (!s.is_active) pills.push('<span class="map-pill map-pill-locked">hidden</span>');
  return pills.join(" ");
}

function mapSourceRowHtml(s) {
  return `
    <div class="card map-source-row" data-source-id="${s.id}">
      <div class="map-source-main">
        <div class="card-title">${escapeHtml(s.name)} ${mapSourcePills(s)}</div>
        <div class="map-source-url">${escapeHtml(s.url_template)}</div>
        <div class="card-meta">
          zoom ${s.min_zoom}–${s.max_zoom} · ${escapeHtml(s.tile_format)}
          ${s.subdomains ? ` · subdomains ${escapeHtml(s.subdomains)}` : ""}
          ${s.pack_count ? ` · ${s.pack_count} area${s.pack_count === 1 ? "" : "s"} downloaded` : ""}
          ${s.is_builtin ? " · ships with the app" : ""}
        </div>
      </div>
      <div class="map-source-controls-cell">
        <button class="btn-secondary btn-sm" data-edit-source="${s.id}">Edit</button>
        <button class="btn-secondary btn-sm" data-toggle-source="${s.id}">${s.is_active ? "Hide" : "Show"}</button>
        ${s.is_builtin ? "" : `<button class="btn-danger btn-sm" data-delete-source="${s.id}">Delete</button>`}
      </div>
    </div>`;
}

function mapPackRowHtml(p) {
  const pct = p.tiles_total ? Math.min(100, Math.round((p.tiles_done + p.tiles_failed) / p.tiles_total * 100)) : 0;
  const status = {
    pending: "Queued", downloading: `Downloading — ${pct}%`, done: "Ready",
    failed: "Failed", cancelled: "Stopped",
  }[p.status] || p.status;
  return `
    <div class="card map-pack-row" data-pack-id="${p.id}">
      <div class="map-source-main">
        <div class="card-title">${escapeHtml(p.name)}</div>
        <div class="card-meta">
          ${escapeHtml(p.source_name)} · zoom ${p.min_zoom}–${p.max_zoom} ·
          ${status} · ${Number(p.tiles_done).toLocaleString()} of ${Number(p.tiles_total).toLocaleString()} tiles
          ${p.tiles_failed ? ` · ${Number(p.tiles_failed).toLocaleString()} failed` : ""}
          ${p.bytes_total ? ` · ${formatFileSize(p.bytes_total)}` : ""}
        </div>
        ${p.last_error ? `<div class="doc-error">${escapeHtml(p.last_error)}</div>` : ""}
        ${p.status === "downloading" || p.status === "pending"
          ? `<div class="map-pack-progress"><div class="map-pack-bar" style="width:${pct}%"></div></div>` : ""}
      </div>
      <div class="map-source-controls-cell">
        ${p.status === "pending" || p.status === "downloading"
          ? `<button class="btn-secondary btn-sm" data-cancel-pack="${p.id}">Stop</button>`
          : `<button class="btn-danger btn-sm" data-delete-pack="${p.id}">Delete</button>`}
      </div>
    </div>`;
}

async function loadMapAdmin() {
  const sourceEl = document.getElementById("map-source-list");
  const packEl = document.getElementById("map-pack-list");
  if (!sourceEl || !packEl) return;
  try {
    const [sources, packs] = await Promise.all([
      api("/api/admin/map/sources"), api("/api/admin/map/packs"),
    ]);
    mapAdmin.sources = sources.items;
    mapAdmin.packs = packs.items;
  } catch (err) {
    sourceEl.innerHTML = `<p class="empty-state">${escapeHtml(err.message)}</p>`;
    return;
  }

  sourceEl.innerHTML = mapAdmin.sources.length
    ? mapAdmin.sources.map(mapSourceRowHtml).join("")
    : '<div class="empty-state">No map sources.</div>';
  packEl.innerHTML = mapAdmin.packs.length
    ? mapAdmin.packs.map(mapPackRowHtml).join("")
    : '<div class="empty-state">Nothing downloaded yet. Download an area to use the map offline.</div>';

  sourceEl.querySelectorAll("[data-edit-source]").forEach((b) => b.addEventListener("click",
    () => openMapSourceForm(mapAdmin.sources.find((s) => s.id === Number(b.dataset.editSource)))));
  sourceEl.querySelectorAll("[data-toggle-source]").forEach((b) => b.addEventListener("click",
    () => toggleMapSource(Number(b.dataset.toggleSource))));
  sourceEl.querySelectorAll("[data-delete-source]").forEach((b) => b.addEventListener("click",
    () => deleteMapSource(Number(b.dataset.deleteSource))));
  packEl.querySelectorAll("[data-cancel-pack]").forEach((b) => b.addEventListener("click",
    () => cancelMapPack(Number(b.dataset.cancelPack))));
  packEl.querySelectorAll("[data-delete-pack]").forEach((b) => b.addEventListener("click",
    () => deleteMapPack(Number(b.dataset.deletePack))));

  // Poll only while something is actually running. A download takes hours, so
  // the page has to show movement -- but an idle admin page has no business
  // asking the server anything.
  const busy = mapAdmin.packs.some((p) => p.status === "downloading" || p.status === "pending");
  clearTimeout(mapAdmin.pollTimer);
  if (busy) mapAdmin.pollTimer = setTimeout(loadMapAdmin, 4000);
}

async function toggleMapSource(id) {
  const source = mapAdmin.sources.find((s) => s.id === id);
  try {
    await api(`/api/admin/map/sources/${id}`, {
      method: "PATCH", body: { is_active: !source.is_active },
    });
    mapState.loaded = false;
    loadMapAdmin();
  } catch (err) { showToast(err.message, true); }
}

async function deleteMapSource(id) {
  const source = mapAdmin.sources.find((s) => s.id === id);
  const packs = source.pack_count;
  if (!confirm(`Delete “${source.name}”?`
      + (packs ? `\n\nIts ${packs} downloaded area${packs === 1 ? "" : "s"} will be deleted too, `
                 + "This can't be undone."
               : ""))) return;
  try {
    await api(`/api/admin/map/sources/${id}`, { method: "DELETE" });
    mapState.loaded = false;
    showToast("Map source deleted");
    loadMapAdmin();
  } catch (err) { showToast(err.message, true); }
}

async function cancelMapPack(id) {
  if (!confirm("Stop this download?\n\nDownloaded tiles are kept; restarting the same area resumes it.")) return;
  try {
    await api(`/api/admin/map/packs/${id}/cancel`, { method: "POST" });
    loadMapAdmin();
  } catch (err) { showToast(err.message, true); }
}

async function deleteMapPack(id) {
  const pack = mapAdmin.packs.find((p) => p.id === id);
  if (!confirm(`Delete “${pack.name}”?\n\nThe downloaded tiles are removed from disk. `
             + "Anywhere it covered goes blank on the offline map.")) return;
  try {
    await api(`/api/admin/map/packs/${id}`, { method: "DELETE" });
    mapState.loaded = false;
    showToast("Downloaded area deleted");
    loadMapAdmin();
  } catch (err) { showToast(err.message, true); }
}

function openMapSourceForm(source) {
  const isEdit = !!source;
  const s = source || { name: "", url_template: "", subdomains: "", min_zoom: 0, max_zoom: 19,
                        tile_format: "png", attribution: "", allow_download: false };
  openModal(`
    <h2>${isEdit ? "Edit" : "Add"} map source</h2>
    <form id="map-source-form">
      <div class="form-row"><label>Name</label>
        <input type="text" id="ms-name" required maxlength="120" value="${escapeHtml(s.name)}"></div>
      <div class="form-row"><label>Tile URL</label>
        <input type="text" id="ms-url" required class="mono" value="${escapeHtml(s.url_template)}"
               placeholder="https://{s}.example.org/{z}/{x}/{y}.png"></div>
      <p class="field-hint">An XYZ template with <code>{z}</code>, <code>{x}</code> and
        <code>{y}</code>. </p>
      <div class="form-row"><label>Subdomains</label>
        <input type="text" id="ms-subdomains" maxlength="12" class="mono"
               value="${escapeHtml(s.subdomains || "")}" placeholder="abc"></div>
      <p class="field-hint">Only needed if the URL uses <code>{s}</code>. "abc" means a, b and c.</p>
      <div class="form-row"><label>Zoom range</label>
        <span class="inline-fields">
          <input type="number" id="ms-minzoom" min="0" max="22" value="${s.min_zoom}">
          <span class="field-hint-inline">to</span>
          <input type="number" id="ms-maxzoom" min="0" max="22" value="${s.max_zoom}">
        </span></div>
      <div class="form-row"><label>Tile format</label>
        <select id="ms-format">
          ${["png", "jpg", "webp"].map((f) => `<option value="${f}" ${f === s.tile_format ? "selected" : ""}>${f}</option>`).join("")}
        </select></div>
      <div class="form-row"><label>Attribution</label>
        <input type="text" id="ms-attribution" maxlength="512" value="${escapeHtml(s.attribution || "")}"></div>
      <p class="field-hint">Shown in the corner of the map. Most tile licences require it.</p>
      ${s.is_builtin ? `
        <p class="field-hint">Built-in source. OpenStreetMap's policy doesn't allow bulk downloads.</p>`
        : `
        <label class="checkbox-row">
          <input type="checkbox" id="ms-allow-download" ${s.allow_download ? "checked" : ""}>
          This source may be downloaded for offline use
        </label>
        <p class="field-hint">Only tick this if the source's terms allow caching.</p>`}
      <p class="form-error" id="ms-error"></p>
      <div class="modal-actions">
        <button type="button" class="btn-secondary" id="ms-cancel">Cancel</button>
        <button type="submit" class="btn-primary">${isEdit ? "Save" : "Add"}</button>
      </div>
    </form>`);

  document.getElementById("ms-cancel").addEventListener("click", closeModal);
  document.getElementById("map-source-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const allow = document.getElementById("ms-allow-download");
    const body = {
      name: document.getElementById("ms-name").value.trim(),
      url_template: document.getElementById("ms-url").value.trim(),
      subdomains: document.getElementById("ms-subdomains").value.trim() || null,
      min_zoom: Number(document.getElementById("ms-minzoom").value),
      max_zoom: Number(document.getElementById("ms-maxzoom").value),
      tile_format: document.getElementById("ms-format").value,
      attribution: document.getElementById("ms-attribution").value.trim() || null,
    };
    if (allow) body.allow_download = allow.checked;
    try {
      if (isEdit) await api(`/api/admin/map/sources/${s.id}`, { method: "PATCH", body });
      else await api("/api/admin/map/sources", { method: "POST", body });
      closeModal();
      mapState.loaded = false;
      showToast(isEdit ? "Map source saved" : "Map source added");
      loadMapAdmin();
    } catch (err) {
      document.getElementById("ms-error").textContent = err.message;
    }
  });
}

function openMapImportDialog() {
  openModal(`
    <h2>Import ATAK map sources</h2>
    <p class="field-hint">Paste ATAK/MOBAC map source XML. Collections are published at <a href="https://github.com/joshuafuller/ATAK-Maps" target="_blank" rel="noopener noreferrer">joshuafuller/ATAK-Maps</a>.</p>
    <p class="field-hint"><strong>These point at other people's tile servers.</strong> Check each source's terms before relying on it.</p>
    <form id="map-import-form">
      <div class="form-row"><label>XML</label>
        <textarea id="mi-xml" rows="12" class="mono" required
                  placeholder="&lt;customMapSource&gt;…&lt;/customMapSource&gt;"></textarea></div>
      <label class="checkbox-row">
        <input type="checkbox" id="mi-allow-download">
        Mark everything in this paste as downloadable
      </label>
      <p class="field-hint">Only tick this if you've checked every source's terms. You can turn it on per source later.</p>
      <p class="form-error" id="mi-error"></p>
      <div id="mi-results"></div>
      <div class="modal-actions">
        <button type="button" class="btn-secondary" id="mi-cancel">Close</button>
        <button type="submit" class="btn-primary">Import</button>
      </div>
    </form>`);

  document.getElementById("mi-cancel").addEventListener("click", closeModal);
  document.getElementById("map-import-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const err = document.getElementById("mi-error");
    err.textContent = "";
    try {
      const res = await api("/api/admin/map/sources/import", {
        method: "POST",
        body: {
          xml: document.getElementById("mi-xml").value,
          allow_download: document.getElementById("mi-allow-download").checked,
        },
      });
      // Per-source outcomes rather than a bare count: these files hold a
      // dozen basemaps and "9 imported" without saying which three did not
      // is the kind of result that sends somebody hunting.
      document.getElementById("mi-results").innerHTML = `
        <div class="map-estimate">
          <div class="map-estimate-total">${res.created_count} imported</div>
          <ul class="plain-list">
            ${res.results.map((r) => `<li>${escapeHtml(r.name)} — ${escapeHtml(r.status)}${
              r.reason ? `: ${escapeHtml(r.reason)}` : ""}</li>`).join("")}
          </ul>
        </div>`;
      mapState.loaded = false;
      loadMapAdmin();
    } catch (e2) {
      err.textContent = e2.message;
    }
  });
}

/* --- downloading an area --------------------------------------------------
 *
 * The box is dragged on a real map rather than typed as four numbers, because
 * nobody knows what -97.44 means and everybody knows what the north edge of
 * town looks like. The estimate is recomputed on every change and shown
 * before the button does anything: tile counts quadruple per zoom level, and
 * that is the single fact most likely to turn a coffee break into a week.
 * ------------------------------------------------------------------------ */

function openMapPackDialog() {
  const downloadable = mapAdmin.sources.filter((s) => s.allow_download && s.is_active);
  if (!downloadable.length) {
    openModal(`
      <h2>Download an area</h2>
      <p class="field-hint">No source allows downloading yet. Edit a source and tick "This source may be downloaded for offline use".</p>
      <div class="modal-actions">
        <button type="button" class="btn-secondary" id="mp-cancel">Close</button>
      </div>`);
    document.getElementById("mp-cancel").addEventListener("click", closeModal);
    return;
  }

  openModal(`
    <h2>Download an area</h2>
    <div class="form-row"><label>Source</label>
      <select id="mp-source">
        ${downloadable.map((s) => `<option value="${s.id}">${escapeHtml(s.name)} (to zoom ${s.max_zoom})</option>`).join("")}
      </select></div>
    <p class="map-draw-hint" id="mp-draw-hint">Drag on the map to draw the area you want.</p>
    <div id="mp-map" class="picker-map"></div>
    <div class="form-row"><label>Zoom range</label>
      <span class="inline-fields">
        <input type="number" id="mp-minzoom" min="0" max="22" value="0">
        <span class="field-hint-inline">to</span>
        <input type="number" id="mp-maxzoom" min="0" max="22" value="14">
      </span></div>
    <p class="field-hint">Zoom 14 shows street names; 16 shows buildings. Each level is about 4× the tiles.</p>
    <div class="form-row"><label>Name</label>
      <input type="text" id="mp-name" maxlength="160" placeholder="e.g. Kettleburn and approaches"></div>
    <div class="map-estimate" id="mp-estimate">
      <div class="map-estimate-total">Draw an area to see the size</div>
    </div>
    <p class="form-error" id="mp-error"></p>
    <div class="modal-actions">
      <button type="button" class="btn-secondary" id="mp-cancel">Cancel</button>
      <button type="button" class="btn-primary" id="mp-start" disabled>Start download</button>
    </div>`);

  document.getElementById("mp-cancel").addEventListener("click", () => {
    if (mapAdmin.picker) { mapAdmin.picker.remove(); mapAdmin.picker = null; }
    closeModal();
  });

  // Leaflet sizes its canvas from the container, and the container has just
  // been dropped into a modal that was display:none a moment ago.
  setTimeout(() => {
    if (typeof L === "undefined") {
      document.getElementById("mp-map").innerHTML =
        '<p class="empty-state">Map library unavailable.</p>';
      return;
    }
    const map = L.map("mp-map").setView([20, 0], 2);
    mapAdmin.picker = map;
    addBasemap(map);
    map.invalidateSize();

    let box = null;
    let bounds = null;
    let origin = null;

    const refresh = () => {
      document.getElementById("mp-start").disabled = !bounds;
      if (!bounds) return;
      estimatePack(bounds);
    };

    // Drag-to-draw, with the map's own panning suspended while a box is being
    // drawn. Leaflet has no rectangle tool of its own and pulling in
    // Leaflet.draw for one interaction would mean another vendored library.
    map.on("mousedown", (e) => {
      origin = e.latlng;
      map.dragging.disable();
      if (box) { map.removeLayer(box); box = null; }
    });
    map.on("mousemove", (e) => {
      if (!origin) return;
      const b = L.latLngBounds(origin, e.latlng);
      if (box) box.setBounds(b);
      else box = L.rectangle(b, { color: "#39ff88", weight: 1, fillOpacity: 0.08 }).addTo(map);
    });
    map.on("mouseup", (e) => {
      if (!origin) return;
      map.dragging.enable();
      const b = L.latLngBounds(origin, e.latlng);
      origin = null;
      // A click rather than a drag: treat it as "I did not mean to draw".
      if (b.getNorth() - b.getSouth() < 1e-6 || b.getEast() - b.getWest() < 1e-6) {
        if (box) { map.removeLayer(box); box = null; }
        bounds = null;
        refresh();
        return;
      }
      bounds = b;
      document.getElementById("mp-draw-hint").textContent =
        "Drag again to redraw the area.";
      refresh();
    });

    ["mp-source", "mp-minzoom", "mp-maxzoom"].forEach((id) => {
      document.getElementById(id).addEventListener("change", refresh);
    });

    document.getElementById("mp-start").addEventListener("click", async () => {
      const err = document.getElementById("mp-error");
      err.textContent = "";
      const name = document.getElementById("mp-name").value.trim();
      if (!name) { err.textContent = "Give the area a name."; return; }
      try {
        await api("/api/admin/map/packs", { method: "POST", body: { ...packBody(bounds), name } });
        map.remove();
        mapAdmin.picker = null;
        closeModal();
        showToast("Download queued — it runs in the background");
        loadMapAdmin();
      } catch (e2) { err.textContent = e2.message; }
    });
  }, 60);
}

function packBody(bounds) {
  return {
    source_id: Number(document.getElementById("mp-source").value),
    min_lat: bounds.getSouth(), min_lon: bounds.getWest(),
    max_lat: bounds.getNorth(), max_lon: bounds.getEast(),
    min_zoom: Number(document.getElementById("mp-minzoom").value),
    max_zoom: Number(document.getElementById("mp-maxzoom").value),
  };
}

let estimateSeq = 0;
async function estimatePack(bounds) {
  const el = document.getElementById("mp-estimate");
  const seq = ++estimateSeq;
  try {
    const est = await api("/api/admin/map/packs/estimate", { method: "POST", body: packBody(bounds) });
    if (seq !== estimateSeq || !el.isConnected) return;   // a later drag won
    const heaviest = est.per_zoom[est.per_zoom.length - 1];
    el.innerHTML = `
      <div class="map-estimate-total">${Number(est.tiles).toLocaleString()} tiles ·
        about ${formatFileSize(est.estimated_bytes)}</div>
      <div class="map-estimate-zooms">${est.per_zoom.map((z) =>
        `z${z.zoom}: ${Number(z.tiles).toLocaleString()}`).join("   ")}</div>
      ${est.over_limit ? `<div class="map-estimate-warn">That is over the
        ${Number(est.max_tiles).toLocaleString()} tile limit for one download. Narrow the area or
        lower the maximum zoom.</div>` : ""}
      ${est.beyond_source_zoom ? `<div class="map-estimate-warn">This source only publishes to
        zoom ${est.source.max_zoom}.</div>` : ""}
      ${!est.over_limit && heaviest.tiles > 50000 ? `<div class="map-estimate-warn">Zoom
        ${heaviest.zoom} alone is ${Number(heaviest.tiles).toLocaleString()} tiles — expect this to
        run for hours.</div>` : ""}`;
    document.getElementById("mp-start").disabled = est.over_limit || est.beyond_source_zoom;
  } catch (err) {
    if (seq !== estimateSeq || !el.isConnected) return;
    el.innerHTML = `<div class="map-estimate-warn">${escapeHtml(err.message)}</div>`;
  }
}

function wireMapAdmin() {
  const on = (id, fn) => {
    const el = document.getElementById(id);
    if (el) el.addEventListener("click", fn);
  };
  on("map-new-source-btn", () => openMapSourceForm(null));
  on("map-import-btn", openMapImportDialog);
  on("map-new-pack-btn", openMapPackDialog);
}

/* ============================================================================
 * Map zones
 *
 * An area, an assessment of it, and a clock. A protest, a cordon, a road
 * nobody should be on tonight — visible the moment the map opens, without
 * reading a report first.
 *
 * Three rules this UI exists to keep:
 *
 *   - **Colour carries the assessment.** Permissive through Denied, read from
 *     the theme rather than hardcoded, so zones stay legible in whichever
 *     palette the deployment runs.
 *   - **Expiry dims, it never deletes.** An expired zone is drawn dashed and
 *     faint and stays exactly where it was. Where the cordon was last night is
 *     a question asked next week.
 *   - **Changing the assessment is reporting, not correcting.** The dialog
 *     asks why, and that note lands on the zone's timeline.
 * ========================================================================== */

/* The environment scale, shared by zones and by Location pins.
 *
 * Its own tokens, not the general UI ones. It used to point at
 * --accent/--amber/--vehicle, which meant the scale changed meaning with the
 * palette: in graphite --accent is a tan and --amber is nearly the same tan,
 * so permissive and semi-permissive were indistinguishable. Defined in
 * styles.css per palette, in both themes, so the traffic light stays a
 * traffic light everywhere.
 *
 * Read at draw time rather than baked in, so switching palette or theme
 * recolours what is already on the map.
 */
const ZONE_COLOURS = {
  "Permissive": "--zone-permissive",
  "Semi-permissive": "--zone-semi",
  "Non-permissive": "--zone-non",
  "Denied": "--zone-denied",
  "Unknown": "--zone-unknown",
};

/* A record assessed Hostile is marked wherever its name appears.
 *
 * Colour is doing real work here, so it is not doing it alone: the name also
 * goes bold and carries a title. Red-on-its-own would be invisible to a
 * colour-blind analyst and to anyone printing a page in greyscale, and this
 * is exactly the fact you least want quietly missed.
 *
 * Hostile only. Friendly and Neutral are not coloured, because a list where
 * every name is coloured is a list where no colour means anything.
 */
function isHostile(alignment) {
  return alignment === "Hostile";
}

function nameHtml(name, alignment) {
  const safe = escapeHtml(name || "");
  return isHostile(alignment)
    ? `<span class="hostile-name" title="Assessed hostile">${safe}</span>`
    : safe;
}

const zoneState = {
  items: [], layer: null, showExpired: true, draw: null,
  // Finishing a rectangle or a circle is a mousedown/mouseup pair, and Leaflet
  // synthesises a map `click` from exactly that pair. By the time it arrives
  // the draw is over, so the map's own handler would helpfully offer to create
  // a Location on top of the zone form that just opened. This is how long to
  // ignore that one click for.
  suppressClickUntil: 0,
};

function zoneColour(environment) {
  const varName = ZONE_COLOURS[environment] || "--zone-unknown";
  const value = getComputedStyle(document.documentElement).getPropertyValue(varName).trim();
  return value || "#888888";
}

function zoneIsExpired(zone) {
  return !!(zone.valid_until && new Date(zone.valid_until) <= new Date());
}

function zoneStyle(zone) {
  const expired = zoneIsExpired(zone);
  // Denied is drawn heavier than the rest. It sits beyond non-permissive on
  // the scale, and two reds a shade apart is a thin way to carry "difficult
  // and dangerous" against "not an option" -- especially for anyone reading
  // the map who does not distinguish those two reds easily.
  const denied = zone.environment === "Denied";
  return {
    color: zoneColour(zone.environment),
    weight: expired ? 1 : (denied ? 4 : 2),
    // Dashed and faint rather than absent: an expired zone is still the record
    // of what the ground was like, and somebody reading the map a month later
    // needs to be able to tell "was" from "is" at a glance.
    dashArray: expired ? "5,6" : null,
    fillOpacity: expired ? 0.05 : (denied ? 0.3 : 0.18),
    opacity: expired ? 0.55 : 0.9,
    // Zones and pins are both SVG paths in the same pane. The class is what
    // lets CSS -- and anything else looking -- tell an area from a record.
    className: "map-zone" + (expired ? " map-zone-expired" : ""),
  };
}

function zoneLabel(zone) {
  const bits = [escapeHtml(zone.name), `<span class="zone-env">${escapeHtml(zone.environment)}</span>`];
  if (zoneIsExpired(zone)) bits.push('<span class="zone-expired-pill">expired</span>');
  return bits.join(" ");
}

async function loadZones() {
  if (!leafletMap || typeof L === "undefined") return;
  try {
    const data = await api("/api/map/zones");
    zoneState.items = data.items || [];
  } catch (err) {
    showToast("Failed to load zones: " + err.message, true);
    return;
  }
  renderZones();
}

function renderZones() {
  if (!leafletMap) return;
  if (!zoneState.layer) zoneState.layer = L.layerGroup().addTo(leafletMap);
  zoneState.layer.clearLayers();

  zoneState.items.forEach((zone) => {
    if (!zoneState.showExpired && zoneIsExpired(zone)) return;
    const shape = zoneShapeFor(zone);
    if (!shape) return;
    shape.bindTooltip(zoneLabel(zone), { sticky: true });
    shape.on("click", (e) => {
      // Without this the map's own click handler also fires and offers to
      // create a Location underneath the zone the analyst just clicked.
      L.DomEvent.stopPropagation(e);
      openZonePanel(zone.id);
    });
    shape.addTo(zoneState.layer);
  });
}

function zoneShapeFor(zone) {
  const style = zoneStyle(zone);
  if (zone.shape === "circle") {
    const c = zone.geometry.coordinates;      // GeoJSON: [lon, lat]
    return L.circle([c[1], c[0]], { radius: zone.radius_m, ...style });
  }
  const ring = (zone.geometry.coordinates || [])[0] || [];
  if (ring.length < 4) return null;
  // Back to Leaflet's [lat, lon], dropping GeoJSON's repeated closing point.
  return L.polygon(ring.slice(0, -1).map((p) => [p[1], p[0]]), style);
}

/* --- drawing ---------------------------------------------------------------
 *
 * Hand-rolled rather than pulling in Leaflet.draw: three shapes, one
 * interaction each, and the app has a standing rule against new runtime
 * dependencies it would have to vendor.
 * ------------------------------------------------------------------------ */

function zoneDrawActive() {
  return !!(zoneState.draw && zoneState.draw.mode);
}

function startZoneDraw(mode) {
  cancelZoneDraw();
  zoneState.draw = { mode, points: [], preview: null, origin: null };
  leafletMap.getContainer().classList.add("zone-drawing");
  const hint = {
    polygon: "Click each corner. Double-click, or press Finish, to close the shape.",
    rectangle: "Drag a box over the area.",
    circle: "Press at the centre and drag out to the radius.",
  }[mode];
  showZoneDrawBar(hint, mode === "polygon");
}

function cancelZoneDraw() {
  if (zoneState.draw) {
    if (zoneState.draw.preview) leafletMap.removeLayer(zoneState.draw.preview);
    if (zoneState.draw.markers) zoneState.draw.markers.forEach((m) => leafletMap.removeLayer(m));
    leafletMap.dragging.enable();
  }
  zoneState.draw = null;
  if (leafletMap) leafletMap.getContainer().classList.remove("zone-drawing");
  hideZoneDrawBar();
}

function showZoneDrawBar(hint, withFinish) {
  const bar = document.getElementById("zone-draw-bar");
  if (!bar) return;
  bar.hidden = false;
  bar.innerHTML = `
    <span>${escapeHtml(hint)}</span>
    ${withFinish ? '<button type="button" class="btn-primary btn-sm" id="zone-finish">Finish</button>' : ""}
    <button type="button" class="btn-secondary btn-sm" id="zone-cancel-draw">Cancel</button>`;
  const finish = document.getElementById("zone-finish");
  if (finish) finish.addEventListener("click", finishPolygonDraw);
  document.getElementById("zone-cancel-draw").addEventListener("click", cancelZoneDraw);
}

function hideZoneDrawBar() {
  const bar = document.getElementById("zone-draw-bar");
  if (bar) { bar.hidden = true; bar.innerHTML = ""; }
}

function finishPolygonDraw() {
  const draw = zoneState.draw;
  if (!draw || draw.mode !== "polygon") return;
  if (draw.points.length < 3) {
    showToast("A zone needs at least three corners", true);
    return;
  }
  const points = draw.points.slice();
  cancelZoneDraw();
  openZoneForm(null, { shape: "polygon", points });
}

function wireZoneDrawing(map) {
  map.on("click", (e) => {
    const draw = zoneState.draw;
    if (!draw || draw.mode !== "polygon") return;
    draw.points.push([e.latlng.lat, e.latlng.lng]);
    draw.markers = draw.markers || [];
    draw.markers.push(L.circleMarker(e.latlng, {
      radius: 4, color: zoneColour("Unknown"), fillOpacity: 1,
    }).addTo(map));
    if (draw.preview) map.removeLayer(draw.preview);
    draw.preview = draw.points.length >= 3
      ? L.polygon(draw.points, { color: zoneColour("Unknown"), weight: 2, fillOpacity: 0.1 }).addTo(map)
      : L.polyline(draw.points, { color: zoneColour("Unknown"), weight: 2 }).addTo(map);
  });

  map.on("dblclick", (e) => {
    if (zoneState.draw && zoneState.draw.mode === "polygon") {
      L.DomEvent.stop(e);
      finishPolygonDraw();
    }
  });

  map.on("mousedown", (e) => {
    const draw = zoneState.draw;
    if (!draw || (draw.mode !== "rectangle" && draw.mode !== "circle")) return;
    draw.origin = e.latlng;
    map.dragging.disable();
  });

  map.on("mousemove", (e) => {
    const draw = zoneState.draw;
    if (!draw || !draw.origin) return;
    if (draw.preview) map.removeLayer(draw.preview);
    const style = { color: zoneColour("Unknown"), weight: 2, fillOpacity: 0.1 };
    draw.preview = draw.mode === "rectangle"
      ? L.rectangle(L.latLngBounds(draw.origin, e.latlng), style).addTo(map)
      : L.circle(draw.origin, { radius: draw.origin.distanceTo(e.latlng), ...style }).addTo(map);
  });

  map.on("mouseup", (e) => {
    const draw = zoneState.draw;
    if (!draw || !draw.origin) return;
    map.dragging.enable();
    const origin = draw.origin;
    const mode = draw.mode;
    draw.origin = null;

    zoneState.suppressClickUntil = Date.now() + 500;

    if (mode === "rectangle") {
      const b = L.latLngBounds(origin, e.latlng);
      if (b.getNorth() - b.getSouth() < 1e-6 || b.getEast() - b.getWest() < 1e-6) return;
      const points = [
        [b.getSouth(), b.getWest()], [b.getSouth(), b.getEast()],
        [b.getNorth(), b.getEast()], [b.getNorth(), b.getWest()],
      ];
      cancelZoneDraw();
      openZoneForm(null, { shape: "rectangle", points });
    } else {
      const radius = origin.distanceTo(e.latlng);
      if (radius < 1) return;
      cancelZoneDraw();
      openZoneForm(null, { shape: "circle", lat: origin.lat, lon: origin.lng, radius_m: radius });
    }
  });
}

/* --- the form -------------------------------------------------------------- */

function zoneDateTimeValue(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return "";
  return localDateTimeValue(d);
}

function openZoneForm(zone, geometry) {
  const isEdit = !!zone;
  const z = zone || { name: "", environment: "Semi-permissive", notes: "",
                      valid_from: new Date().toISOString(), valid_until: "" };
  openModal(`
    <h2>${isEdit ? "Edit zone" : "New zone"}</h2>
    <form id="zone-form">
      <div class="form-row"><label>Name</label>
        <input type="text" id="zone-name" required maxlength="200"
               value="${escapeHtml(z.name)}" placeholder="e.g. Riverside protest"></div>
      <div class="form-row"><label>Environment</label>
        <select id="zone-environment">
          ${["Permissive", "Semi-permissive", "Non-permissive", "Denied", "Unknown"]
            .map((v) => `<option value="${v}" ${v === z.environment ? "selected" : ""}>${v}</option>`).join("")}
        </select></div>
      <p class="field-hint">Sets the zone's colour. Denied means no access at all.</p>
      <div class="form-row"><label>From</label>
        <input type="datetime-local" id="zone-from" value="${zoneDateTimeValue(z.valid_from)}"></div>
      <div class="form-row"><label>Until</label>
        <input type="datetime-local" id="zone-until" value="${zoneDateTimeValue(z.valid_until)}"></div>
      <p class="field-hint">Leave Until blank for no end. Expired zones are dimmed, not removed.</p>
      ${isEdit ? `
        <div class="form-row"><label>Why the change</label>
          <input type="text" id="zone-change-note" maxlength="500"
                 placeholder="e.g. police line moved onto the bridge"></div>
        <p class="field-hint">Saved to the zone's timeline when the environment changes.</p>`
      : `
        <div class="form-row"><label>Event</label>
          <input type="text" id="zone-event" maxlength="200"
                 placeholder="e.g. Riverside protest, 17 Sep — leave blank for none"></div>
        <p class="field-hint">Creates an Event and links this zone to it. Leave blank if it isn't an event.</p>`}
      <div class="form-row"><label>Notes</label>
        <textarea id="zone-notes" rows="2">${escapeHtml(z.notes || "")}</textarea></div>
      <p class="form-error" id="zone-error"></p>
      <div class="modal-actions">
        <button type="button" class="btn-secondary" id="zone-cancel">Cancel</button>
        <button type="submit" class="btn-primary">${isEdit ? "Save" : "Create zone"}</button>
      </div>
    </form>`);

  document.getElementById("zone-cancel").addEventListener("click", closeModal);
  document.getElementById("zone-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const err = document.getElementById("zone-error");
    const toIso = (id) => {
      const v = document.getElementById(id).value;
      return v ? new Date(v).toISOString() : null;
    };
    const body = {
      name: document.getElementById("zone-name").value.trim(),
      environment: document.getElementById("zone-environment").value,
      valid_from: toIso("zone-from"),
      valid_until: toIso("zone-until"),
      notes: document.getElementById("zone-notes").value.trim() || null,
    };
    if (isEdit) {
      const note = document.getElementById("zone-change-note").value.trim();
      if (note) body.change_note = note;
    } else {
      body.geometry = geometry;
      const event = document.getElementById("zone-event").value.trim();
      if (event) body.create_event = event;
    }
    try {
      const saved = isEdit
        ? await api(`/api/map/zones/${z.id}`, { method: "PATCH", body })
        : await api("/api/map/zones", { method: "POST", body });
      closeModal();
      showToast(isEdit ? "Zone updated" : "Zone created");
      await loadZones();
      if (!isEdit && saved.event_id) {
        showToast(`Tied to the event “${saved.event_name}”`);
      }
    } catch (e2) {
      err.textContent = e2.message;
    }
  });
}

/* --- the panel ------------------------------------------------------------- */

async function openZonePanel(zoneId) {
  openModal('<p class="empty-state">Loading…</p>');
  let zone;
  try {
    zone = await api(`/api/map/zones/${zoneId}`);
  } catch (err) {
    openModal(`<p class="empty-state">${escapeHtml(err.message)}</p>`);
    return;
  }

  const expired = zoneIsExpired(zone);
  const inside = zone.locations_inside || [];
  openModal(`
    <h2>${escapeHtml(zone.name)}</h2>
    <p class="card-meta">
      <span class="zone-swatch" style="background:${zoneColour(zone.environment)}"></span>
      ${escapeHtml(zone.environment)} · ${escapeHtml(zone.shape)}
      ${expired ? ' · <span class="zone-expired-pill">expired</span>' : ""}
      ${zone.valid_until ? ` · ${expired ? "ended" : "until"} ${timeAgoHtml(zone.valid_until)}` : " · open-ended"}
    </p>
    ${zone.event_id ? `<p class="field-hint">Part of
      <a href="#" data-open-entity="${escapeHtml(zone.event_id)}">${escapeHtml(zone.event_name || "an event")}</a>
      </p>` : ""}
    ${zone.notes ? `<p>${escapeHtml(zone.notes)}</p>` : ""}

    <h3>How it has changed</h3>
    <ul class="zone-timeline">
      ${zone.history.map((h) => `
        <li>
          <span class="zone-swatch" style="background:${zoneColour(h.environment)}"></span>
          ${h.previous_environment
            ? `${escapeHtml(h.previous_environment)} → <strong>${escapeHtml(h.environment)}</strong>`
            : `<strong>${escapeHtml(h.environment)}</strong>`}
          <span class="card-meta">${timeAgoHtml(h.changed_at)}${h.changed_by ? " · " + escapeHtml(h.changed_by) : ""}</span>
          ${h.note ? `<div class="zone-note">${escapeHtml(h.note)}</div>` : ""}
        </li>`).join("")}
    </ul>

    <h3>Locations inside it <span class="card-meta">${inside.length}</span></h3>
    ${inside.length ? `<ul class="plain-list">${inside.map((l) => `
        <li><a href="#" data-open-entity="${escapeHtml(l.id)}">${escapeHtml(l.name)}</a>
        ${l.address ? `<span class="card-meta"> — ${escapeHtml(l.address)}</span>` : ""}</li>`).join("")}</ul>`
      : '<p class="field-hint">No Locations inside this zone.</p>'}

    <div class="modal-actions">
      <button type="button" class="btn-secondary" id="zone-panel-close">Close</button>
      ${canDelete() ? `<button type="button" class="btn-danger" id="zone-panel-delete">Delete</button>` : ""}
      <button type="button" class="btn-primary" id="zone-panel-edit">Edit</button>
    </div>`);

  document.getElementById("zone-panel-close").addEventListener("click", closeModal);
  document.getElementById("zone-panel-edit").addEventListener("click", () => openZoneForm(zone, null));
  const del = document.getElementById("zone-panel-delete");
  if (del) {
    del.addEventListener("click", async () => {
      if (!confirm(`Delete “${zone.name}”?\n\nThis removes the zone and its timeline. To end a zone, let it expire instead.`)) return;
      try {
        await api(`/api/map/zones/${zone.id}`, { method: "DELETE" });
        closeModal();
        showToast("Zone deleted");
        loadZones();
      } catch (err) { showToast(err.message, true); }
    });
  }
  document.querySelectorAll("#modal-box [data-open-entity]").forEach((a) => {
    a.addEventListener("click", (ev) => {
      ev.preventDefault();
      closeModal();
      openEntityDetail(a.dataset.openEntity);
    });
  });
}

/** Which zones cover this Location, on the record's own page.
 *
 * Expired ones are included, dimmed. "This address was inside the cordon that
 * night" is exactly what somebody reading the record weeks later needs, and it
 * is the reason expiry does not delete.
 */
async function loadZonesForEntity(entityId) {
  const el = document.getElementById("entity-zones");
  if (!el) return;
  let items;
  try {
    items = (await api(`/api/map/zones/containing/${encodeURIComponent(entityId)}`)).items || [];
  } catch (err) {
    return;   // A record that cannot answer this is still a usable record.
  }
  if (!items.length) return;
  el.innerHTML = `
    <div class="section-title">Zones over this place</div>
    <ul class="plain-list">
      ${items.map((z) => `
        <li>
          <span class="zone-swatch" style="background:${zoneColour(z.environment)}"></span>
          <a href="#" data-open-zone="${z.id}">${escapeHtml(z.name)}</a>
          <span class="card-meta">${escapeHtml(z.environment)}${
            z.expired ? " · ended " + timeAgoHtml(z.valid_until) : ""}</span>
        </li>`).join("")}
    </ul>`;
  el.querySelectorAll("[data-open-zone]").forEach((a) => {
    a.addEventListener("click", (e) => { e.preventDefault(); openZonePanel(Number(a.dataset.openZone)); });
  });
}


function wireZoneControls() {
  const draw = document.getElementById("zone-draw-select");
  const showExpired = document.getElementById("zone-show-expired");
  if (draw) {
    draw.addEventListener("change", () => {
      const mode = draw.value;
      draw.value = "";
      if (!mode) return;
      if (!leafletMap) { showToast("Open the map first", true); return; }
      startZoneDraw(mode);
    });
  }
  if (showExpired) {
    showExpired.addEventListener("change", () => {
      zoneState.showExpired = showExpired.checked;
      renderZones();
    });
  }
}

let leafletMap = null;
let mapMarkersLayer = null;

async function loadMap() {
  const container = document.getElementById("map-container");
  if (typeof L === "undefined") {
    container.innerHTML = '<p class="empty-state">Map library unavailable (offline?) — Location entities are still fully usable from the Entities tab.</p>';
    return;
  }

  await loadMapSources();
  renderMapSourceControls();

  if (!leafletMap) {
    leafletMap = L.map(container).setView([20, 0], 2);
    mapState.layer = addBasemap(leafletMap);
    mapMarkersLayer = L.layerGroup().addTo(leafletMap);
    leafletMap.on("click", (e) => {
      // Drawing a zone clicks the map repeatedly; without this, every corner
      // would also open a New Location form.
      if (zoneDrawActive() || Date.now() < zoneState.suppressClickUntil) return;
      openEntityForm(null, {
        entity_type: "location",
        details: { lat: e.latlng.lat.toFixed(6), lng: e.latlng.lng.toFixed(6) },
      });
    });
    wireZoneDrawing(leafletMap);
  } else {
    // The map's canvas is sized from its container at creation time; while
    // the Map view is `display:none` the container has zero size, so
    // Leaflet needs a nudge once it's visible again or tiles render into
    // the wrong area until the window is manually resized.
    setTimeout(() => leafletMap.invalidateSize(), 50);
  }

  try {
    const data = await api("/api/map/locations");
    mapMarkersLayer.clearLayers();
    mapState.markersById = {};
    const bounds = [];
    data.items.forEach((loc) => {
      const marker = locationMarker(loc).addTo(mapMarkersLayer);
      marker.bindPopup('<p class="empty-state">Loading…</p>');
      marker.on("click", () => showLocationPopup(marker, loc));
      mapState.markersById[loc.id] = marker;
      bounds.push([loc.lat, loc.lng]);
    });
    if (bounds.length) {
      leafletMap.fitBounds(bounds, { maxZoom: 12, padding: [30, 30] });
    }
    await loadZones();
  } catch (err) {
    showToast("Failed to load map locations: " + err.message, true);
  }
}

/** A Location pin, coloured by its own environment.
 *
 * A circle rather than Leaflet's default pin image, because the pin is a PNG
 * and cannot be recoloured -- and the colour is the whole point. It is also
 * the same visual language the relationship network already uses, so a dot
 * meaning "a record" is consistent across the app.
 *
 * A Location nobody has assessed is drawn in the unknown grey, deliberately
 * the same grey as an Unknown environment: "nobody has said" and "somebody
 * looked and could not tell" both mean you should not assume it is fine.
 * The white-ish stroke keeps a pin readable over dark imagery, where a
 * coloured fill alone disappears.
 */
function locationMarker(loc) {
  const colour = zoneColour(loc.environment || "Unknown");
  return L.circleMarker([loc.lat, loc.lng], {
    radius: 7,
    color: colour,
    weight: 3,
    fillColor: colour,
    fillOpacity: 0.85,
    className: "map-pin",
  });
}


async function showLocationPopup(marker, loc) {
  marker.openPopup();
  try {
    const entity = await api(`/api/entities/${loc.id}`);
    const events = entity.relationships.filter((r) => r.other_entity_type === "event");
    const others = entity.relationships.filter((r) => r.other_entity_type !== "event");
    const reports = entity.reports || [];
    const relRow = (r) => `
      <li>
        <a href="#" data-open-entity="${escapeHtml(r.other_entity_id)}">${escapeHtml(r.other_entity_name)}</a>
        <span class="type-pill type-${r.other_entity_type}">${escapeHtml(r.other_entity_type)}</span>
        (${escapeHtml(relWording(r))}, ${escapeHtml(r.confidence)})
      </li>`;
    const reportRow = (r) => `
      <li>
        <a href="#" data-open-report="${escapeHtml(r.id)}">${escapeHtml(r.title)}</a>
        <span class="status-pill status-${r.status}">${escapeHtml(r.status)}</span>
      </li>`;
    // "Events tied to this location" means two different things in this
    // app, and an analyst asking "what happened here" usually wants both:
    // a formal Event entity related to this location (the `events` graph
    // edges below), and — far more common in practice — a report that was
    // simply linked to this location without anyone also modeling a
    // separate Event entity for it. Showing only the former is what made
    // this popup say "no events" for a location that clearly had an
    // analyst-written report about it.
    const html = `
      <div class="map-popup-title">${escapeHtml(entity.name)}</div>
      ${entity.details.environment
        ? `<div class="map-popup-address"><span class="zone-swatch"
             style="background:${zoneColour(entity.details.environment)}"></span>${
             escapeHtml(entity.details.environment)}</div>`
        : ""}
      ${entity.details.address ? `<div class="map-popup-address">${escapeHtml(entity.details.address)}</div>` : ""}
      ${events.length
        ? `<div class="section-title">Events here</div><ul class="map-popup-rel-list">${events.map(relRow).join("")}</ul>`
        : ""}
      ${reports.length
        ? `<div class="section-title">Reports mentioning this location</div><ul class="map-popup-rel-list">${reports.map(reportRow).join("")}</ul>`
        : ""}
      ${!events.length && !reports.length ? '<p class="card-meta">No events or reports tied to this location yet.</p>' : ""}
      ${others.length
        ? `<div class="section-title">Other relationships</div><ul class="map-popup-rel-list">${others.map(relRow).join("")}</ul>`
        : ""}
      <div class="map-popup-actions"><button class="btn-secondary btn-sm" id="map-popup-view-btn">View full details</button></div>
    `;
    marker.setPopupContent(html);
    // Leaflet replaces the popup's inner HTML wholesale on setPopupContent,
    // so listeners have to be (re-)attached to the fresh DOM every time —
    // anything bound before this point is already gone.
    const popupEl = marker.getPopup().getElement();
    popupEl.querySelectorAll("[data-open-entity]").forEach((a) => {
      a.addEventListener("click", (e) => { e.preventDefault(); openEntityDetail(a.dataset.openEntity); });
    });
    popupEl.querySelectorAll("[data-open-report]").forEach((a) => {
      a.addEventListener("click", (e) => { e.preventDefault(); openReportDetail(a.dataset.openReport); });
    });
    const viewBtn = popupEl.querySelector("#map-popup-view-btn");
    if (viewBtn) viewBtn.addEventListener("click", () => openEntityDetail(entity.id));
  } catch (err) {
    marker.setPopupContent('<p class="empty-state">Failed to load details.</p>');
  }
}

/* ============================================================================
 * Reports: list + form + detail
 * ========================================================================== */

document.getElementById("report-search").addEventListener("input", debounce(loadReports, 300));
document.getElementById("report-status-filter").addEventListener("change", loadReports);
document.getElementById("report-criticality-filter").addEventListener("change", loadReports);
document.getElementById("report-sort").addEventListener("change", loadReports);
document.getElementById("new-report-btn").addEventListener("click", () => openReportForm(null));

async function loadReports() {
  const q = document.getElementById("report-search").value.trim();
  const status = document.getElementById("report-status-filter").value;
  const criticality = document.getElementById("report-criticality-filter").value;
  const sort = document.getElementById("report-sort").value;
  const params = new URLSearchParams({ limit: "200" });
  if (q) params.set("q", q);
  if (status) params.set("status", status);
  if (criticality) params.set("criticality", criticality);
  if (sort) params.set("sort", sort);
  try {
    const data = await api("/api/reports?" + params.toString());
    renderReportList(data.items);
  } catch (err) {
    showToast("Failed to load reports: " + err.message, true);
  }
}

function renderReportList(items) {
  const el = document.getElementById("report-list");
  if (!items.length) { el.innerHTML = '<div class="empty-state">No reports found.</div>'; return; }
  el.innerHTML = items.map((r) => `
    <div class="card" data-id="${escapeHtml(r.id)}">
      <span class="status-pill status-${r.status}">${escapeHtml(r.status)}</span>${criticalityBadgeHtml(r)}
      <div class="card-title">${escapeHtml(r.title)}</div>
      <div class="card-meta">${r.credibility_rating ? "Credibility: " + escapeHtml(r.credibility_rating) + " · " : ""}${timeAgoHtml(r.created_at)}</div>
    </div>
  `).join("");
  el.querySelectorAll(".card").forEach((card) => card.addEventListener("click", () => openReportDetail(card.dataset.id)));
}

/* ============================================================================
 * Report editor
 *
 * A full view rather than a modal, because of what it's actually for: sitting
 * with a source and writing the report AS the conversation happens, building
 * the case graph out of the prose instead of stopping to go and create every
 * person, place and event first.
 *
 * Three things make that work:
 *   - @-mentions that search existing entities and can create a new one of any
 *     type inline, never leaving the page;
 *   - a live side panel showing the cast list as it grows;
 *   - a local draft backup, so a crash or a closed tab mid-interview doesn't
 *     cost the whole session.
 *
 * Mentions are stored as ordinary markdown links to #/entities/<id> (see
 * MENTION_RE in api/reports.py). The server re-reads them on every save and
 * links whoever is named, so mentioning someone is what links them — the
 * editor doesn't have to remember to do both.
 * ========================================================================== */

const reportEditor = {
  reportId: null,
  linked: [],          // [{id, name, entity_type, isNew}]
  dirty: false,
  saving: false,
  autosaveTimer: null,
};

const DRAFT_STORAGE_KEY = "humint.report.draft";
const AUTOSAVE_MS = 15000;

const MENTIONABLE_TYPES = ["person", "organization", "location", "event", "source",
                           "communication", "vehicle", "record"];

// Up to three words in the query. Entity names routinely have two ("John
// Smith") and not rarely three ("Jean Luc Picard", "Acme Logistics Ltd"), so
// stopping at one word would drop the menu halfway through typing an
// ordinary name. Stopping at three is the other half of that trade: past
// that the user is writing a sentence rather than a name, and the menu should
// get out of the way instead of hovering over the rest of the paragraph.
// The leading group keeps an email address (a@b) or a mid-word @ from
// triggering it at all.
const MENTION_TRIGGER_RE = /(^|[\s([{,;:>—–-])@([^\s@]{0,30}(?:[  ][^\s@]{1,30}){0,2})$/;

function markReportDirty() {
  reportEditor.dirty = true;
  setEditorStatus("Unsaved changes");
  saveLocalDraft();
}

function setEditorStatus(msg) {
  const el = document.getElementById("report-editor-status-msg");
  if (el) el.textContent = msg || "";
}

/* --- local draft backup ---------------------------------------------------
 * Only ever a safety net for a report that has never been saved to the
 * server. Once it has an id, autosave takes over and this is cleared — two
 * competing copies of the same report would be worse than none.
 * ------------------------------------------------------------------------ */

function saveLocalDraft() {
  if (reportEditor.reportId) return;
  try {
    localStorage.setItem(DRAFT_STORAGE_KEY, JSON.stringify({
      title: document.getElementById("report-editor-title").value,
      body: document.getElementById("report-editor-body").value,
      status: document.getElementById("report-editor-status").value,
      credibility: document.getElementById("report-editor-credibility").value,
      criticality: document.getElementById("report-editor-criticality").value,
      linked: reportEditor.linked,
      savedAt: new Date().toISOString(),
    }));
  } catch (e) { /* private window, quota, storage disabled — the editor still works */ }
}

function readLocalDraft() {
  try {
    const raw = localStorage.getItem(DRAFT_STORAGE_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch (e) { return null; }
}

function clearLocalDraft() {
  try { localStorage.removeItem(DRAFT_STORAGE_KEY); } catch (e) { /* nothing to clean up */ }
}

/* --- opening ------------------------------------------------------------- */

function openReportEditor(report) {
  reportEditor.reportId = report ? report.id : null;
  reportEditor.linked = report ? report.entities.map((e) => ({ ...e })) : [];
  reportEditor.dirty = false;

  setActiveTab("reports");
  switchViewRaw("report-editor");
  state.reportEditorOpen = true;

  document.getElementById("report-editor-title").value = report ? report.title : "";
  document.getElementById("report-editor-body").value = report ? report.body_markdown : "";
  document.getElementById("report-editor-status").value = report ? report.status : "draft";
  document.getElementById("report-editor-credibility").value = report ? (report.credibility_rating || "") : "";
  document.getElementById("report-editor-criticality").value = report ? (report.criticality || "") : "";
  document.getElementById("report-editor-context").textContent = report
    ? `Editing · last saved ${relativeTime(report.updated_at)}`
    : "New report";
  document.getElementById("report-editor-error").textContent = "";
  setEditorStatus("");
  hideMentionMenu();
  renderEditorEntityList();

  // Offer to bring back an unsaved draft, but only for a NEW report — a
  // leftover draft must never overwrite an existing report someone opened.
  if (!report) {
    const draft = readLocalDraft();
    if (draft && (draft.title || draft.body)) {
      const when = relativeTime(draft.savedAt);
      if (confirm(`An unsaved report draft from ${when} was found (“${(draft.title || "untitled").slice(0, 60)}”). Restore it?`)) {
        document.getElementById("report-editor-title").value = draft.title || "";
        document.getElementById("report-editor-body").value = draft.body || "";
        document.getElementById("report-editor-status").value = draft.status || "draft";
        document.getElementById("report-editor-credibility").value = draft.credibility || "";
        document.getElementById("report-editor-criticality").value = draft.criticality || "";
        reportEditor.linked = draft.linked || [];
        renderEditorEntityList();
        setEditorStatus("Restored an unsaved draft");
      } else {
        clearLocalDraft();
      }
    }
  }

  document.getElementById("report-editor-title").focus();
  startAutosave();
}

// Kept so existing callers (+ New Report, Edit on a report) don't need to know
// the editor stopped being a modal.
function openReportForm(report) { openReportEditor(report); }

function closeReportEditor(destinationReportId) {
  stopAutosave();
  state.reportEditorOpen = false;
  reportEditor.dirty = false;
  if (destinationReportId) openReportDetail(destinationReportId);
  else switchView("reports");
}

/* --- linked entity list --------------------------------------------------- */

function addLinkedEntity(entity) {
  if (!reportEditor.linked.some((e) => e.id === entity.id)) {
    reportEditor.linked.push({
      id: entity.id, name: entity.name, entity_type: entity.entity_type, isNew: !!entity.isNew,
    });
    renderEditorEntityList();
  }
}

function bodyMentions(entityId) {
  return document.getElementById("report-editor-body").value.includes(`(#/entities/${entityId})`);
}

function renderEditorEntityList() {
  const el = document.getElementById("editor-entity-list");
  const count = document.getElementById("editor-entity-count");
  if (!el) return;
  count.textContent = reportEditor.linked.length;
  count.hidden = reportEditor.linked.length === 0;

  if (!reportEditor.linked.length) {
    el.innerHTML = '<li class="empty-state">Nobody yet. Mention someone with @ in the report, or search above.</li>';
    return;
  }
  el.innerHTML = reportEditor.linked.map((e) => `
    <li class="editor-entity-row" data-id="${escapeHtml(e.id)}">
      <span class="type-pill type-${escapeHtml(e.entity_type)}">${escapeHtml(e.entity_type)}</span>
      <button type="button" class="editor-entity-name link-like" data-peek="${escapeHtml(e.id)}">${escapeHtml(e.name)}</button>
      ${e.isNew ? '<span class="chip-new">new</span>' : ""}
      ${bodyMentions(e.id) ? '<span class="chip-mentioned" title="Mentioned in the report text">@</span>' : ""}
      <button type="button" class="editor-entity-remove" data-unlink="${escapeHtml(e.id)}" title="Remove from this report">&times;</button>
    </li>
  `).join("");

  el.querySelectorAll("[data-peek]").forEach((btn) => {
    btn.addEventListener("click", () => peekEntity(btn.dataset.peek));
  });
  el.querySelectorAll("[data-unlink]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const id = btn.dataset.unlink;
      // Removing it from the list alone wouldn't stick: the server re-links
      // anyone named in the prose on every save. Say so rather than letting
      // it silently reappear after saving.
      if (bodyMentions(id)) {
        showToast("Still mentioned in the report text. Delete the mention to unlink.", true);
        return;
      }
      reportEditor.linked = reportEditor.linked.filter((e) => e.id !== id);
      renderEditorEntityList();
      markReportDirty();
    });
  });
}

// A read-only look at an entity that does NOT navigate away — clicking a name
// mid-interview must never cost the draft you're writing.
async function peekEntity(entityId) {
  openModal('<h2>Loading…</h2>');
  try {
    const entity = await api(`/api/entities/${entityId}`);
    const details = Object.entries(entity.details || {})
      .filter(([k, v]) => v !== null && v !== "" && !(Array.isArray(v) && !v.length)
        && !["geocode_status", "geocode_error", "geocoded_at"].includes(k))
      .map(([k, v]) => `<div class="form-row"><label>${escapeHtml(k.replace(/_/g, " "))}</label><div>${escapeHtml(Array.isArray(v) ? v.join(", ") : String(v))}</div></div>`)
      .join("");
    const rels = (entity.relationships || []).slice(0, 8).map((r) =>
      `<li>${r.direction === "outgoing" ? "→" : "←"} ${escapeHtml(relWording(r))} ${escapeHtml(r.other_entity_name)} <span class="card-meta">(${escapeHtml(r.confidence)})</span></li>`).join("");
    openModal(`
      <h2><span class="type-pill type-${escapeHtml(entity.entity_type)}">${escapeHtml(entity.entity_type)}</span> ${escapeHtml(entity.name)}</h2>
      ${entity.description ? `<p>${escapeHtml(entity.description)}</p>` : ""}
      ${details || '<p class="card-meta">No details recorded yet.</p>'}
      ${rels ? `<div class="section-title">Relationships</div><ul class="rel-list">${rels}</ul>` : ""}
      <p class="field-hint">Read-only preview. Open it from Entities to edit.</p>
      <div class="modal-actions"><button type="button" class="btn-secondary" id="peek-close">Close</button></div>
    `);
    document.getElementById("peek-close").addEventListener("click", closeModal);
  } catch (err) {
    openModal(`<h2>Couldn't load that entity</h2><p>${escapeHtml(err.message)}</p>
      <div class="modal-actions"><button type="button" class="btn-secondary" id="peek-close">Close</button></div>`);
    document.getElementById("peek-close").addEventListener("click", closeModal);
  }
}

/* --- @-mention autocomplete ------------------------------------------------
 * Written as a factory rather than wired to one specific textarea, because
 * two places need it: the report editor and the debrief wizard's narrative
 * step. Mid-interview, being able to name someone is exactly as useful in one
 * as the other.
 *
 * The menu is positioned at the bottom of the field rather than following the
 * caret: a <textarea> exposes no caret coordinates, and the usual workaround
 * (mirroring the entire content into a hidden div to measure it) is a lot of
 * fragile machinery for a menu that's easier to aim at when it holds still.
 * ------------------------------------------------------------------------ */

function attachMentionAutocomplete(textarea, menu, onEntityLinked) {
  const ctx = { mention: null, items: [], index: 0 };

  function hide() {
    menu.hidden = true;
    ctx.mention = null;
    ctx.items = [];
    ctx.index = 0;
  }

  function render() {
    const query = ctx.mention ? ctx.mention.query : "";
    if (!query.trim()) {
      menu.hidden = false;
      menu.innerHTML = '<div class="mention-hint">Type a name to search or create.</div>';
      return;
    }
    if (!ctx.items.length) {
      menu.hidden = false;
      menu.innerHTML = '<div class="mention-hint">No matches, and nothing to create.</div>';
      return;
    }
    menu.hidden = false;
    menu.innerHTML = ctx.items.map((item, i) => {
      const active = i === ctx.index ? " active" : "";
      if (item.kind === "entity") {
        return `<div class="mention-item${active}" data-i="${i}">
          <span class="type-pill type-${escapeHtml(item.entity.entity_type)}">${escapeHtml(item.entity.entity_type)}</span>
          <span class="mention-name">${escapeHtml(item.entity.name)}</span>
        </div>`;
      }
      return `<div class="mention-item mention-create${active}" data-i="${i}">
        <span class="mention-plus">+</span>
        <span class="mention-name">Create ${escapeHtml(item.entityType)} “${escapeHtml(item.name)}”</span>
      </div>`;
    }).join("");

    menu.querySelectorAll(".mention-item").forEach((row) => {
      // mousedown, not click: it fires before the textarea loses focus, so the
      // blur handler can't tear the menu down before the pick registers.
      row.addEventListener("mousedown", (e) => {
        e.preventDefault();
        choose(Number(row.dataset.i));
      });
    });
    const active = menu.querySelector(".mention-item.active");
    if (active && active.scrollIntoView) active.scrollIntoView({ block: "nearest" });
  }

  const refresh = debounce(async () => {
    if (!ctx.mention) return;
    const query = ctx.mention.query.trim();
    let matches = [];
    if (query) {
      try {
        const data = await api("/api/entities?" + new URLSearchParams({ q: query, limit: "6" }).toString());
        matches = data.items;
      } catch (e) { matches = []; }
    }
    if (!ctx.mention) return; // the caret moved away while that was in flight

    const items = matches.map((entity) => ({ kind: "entity", entity }));
    if (query) {
      MENTIONABLE_TYPES.forEach((entityType) => {
        // Don't offer to create a duplicate of a name that already exists as
        // that type — the existing one is right there in the list above.
        const exists = matches.some(
          (m) => m.entity_type === entityType && m.name.toLowerCase() === query.toLowerCase());
        if (!exists) items.push({ kind: "create", entityType, name: query });
      });
    }
    ctx.items = items;
    ctx.index = 0;
    render();
  }, 150);

  function detect() {
    const caret = textarea.selectionStart;
    const before = textarea.value.slice(0, caret);
    const match = before.match(MENTION_TRIGGER_RE);
    if (!match) { hide(); return; }
    ctx.mention = { start: match.index + match[1].length, query: match[2] || "" };
    refresh();
  }

  function insert(entity) {
    if (!ctx.mention) return;
    const caret = textarea.selectionStart;
    // Square brackets in a name would break out of the markdown link's text,
    // so they're dropped from the label. The id is a generated slug and never
    // needs escaping.
    const label = String(entity.name).replace(/[\[\]]/g, "").trim() || entity.id;
    const link = `[${label}](#/entities/${entity.id})`;
    const before = textarea.value.slice(0, ctx.mention.start);
    const after = textarea.value.slice(caret);
    textarea.value = before + link + after;
    const pos = before.length + link.length;
    textarea.setSelectionRange(pos, pos);
    textarea.focus();
    hide();
    if (onEntityLinked) onEntityLinked(entity);
  }

  async function choose(i) {
    const item = ctx.items[i];
    if (!item) return;
    if (item.kind === "entity") { insert(item.entity); return; }
    menu.innerHTML = `<div class="mention-hint">Creating ${escapeHtml(item.entityType)} “${escapeHtml(item.name)}”…</div>`;
    try {
      const entity = await api("/api/entities", {
        method: "POST",
        body: { entity_type: item.entityType, name: item.name },
      });
      entity.isNew = true;
      insert(entity);
      showToast(`Created ${item.entityType}: ${entity.name}`);
    } catch (err) {
      hide();
      showToast("Couldn't create that entity: " + err.message, true);
    }
  }

  textarea.addEventListener("input", detect);
  textarea.addEventListener("click", detect);
  textarea.addEventListener("blur", () => setTimeout(hide, 150));
  textarea.addEventListener("keydown", (e) => {
    if (!ctx.mention || menu.hidden || !ctx.items.length) {
      if (e.key === "Escape") hide();
      return;
    }
    if (e.key === "ArrowDown") {
      e.preventDefault();
      ctx.index = (ctx.index + 1) % ctx.items.length;
      render();
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      ctx.index = (ctx.index - 1 + ctx.items.length) % ctx.items.length;
      render();
    } else if (e.key === "Enter" || e.key === "Tab") {
      e.preventDefault();
      choose(ctx.index);
    } else if (e.key === "Escape") {
      e.preventDefault();
      hide();
    }
  });

  return { hide, detect };
}

let editorMentions = null;

function hideMentionMenu() {
  if (editorMentions) editorMentions.hide();
}

/* --- side-panel quick add -------------------------------------------------- */

const searchEditorEntities = debounce(async () => {
  const input = document.getElementById("editor-entity-search");
  const results = document.getElementById("editor-entity-results");
  const query = input.value.trim();
  if (!query) { results.hidden = true; results.innerHTML = ""; return; }
  let matches = [];
  try {
    const data = await api("/api/entities?" + new URLSearchParams({ q: query, limit: "6" }).toString());
    matches = data.items;
  } catch (e) { /* fall through to the create options */ }

  const existing = matches.map((e) => `
    <div class="editor-entity-result" data-add="${escapeHtml(e.id)}" data-name="${escapeHtml(e.name)}" data-type="${escapeHtml(e.entity_type)}">
      <span class="type-pill type-${escapeHtml(e.entity_type)}">${escapeHtml(e.entity_type)}</span> ${escapeHtml(e.name)}
    </div>`).join("");
  const creates = MENTIONABLE_TYPES.map((t) => `
    <div class="editor-entity-result editor-entity-create" data-create="${t}" data-name="${escapeHtml(query)}">
      <span class="mention-plus">+</span> Create ${t} “${escapeHtml(truncate(query, 30))}”
    </div>`).join("");
  results.innerHTML = existing + '<div class="editor-entity-sep">Not there yet?</div>' + creates;
  results.hidden = false;

  results.querySelectorAll("[data-add]").forEach((row) => {
    row.addEventListener("mousedown", (e) => {
      e.preventDefault();
      addLinkedEntity({ id: row.dataset.add, name: row.dataset.name, entity_type: row.dataset.type });
      markReportDirty();
      input.value = "";
      results.hidden = true;
    });
  });
  results.querySelectorAll("[data-create]").forEach((row) => {
    row.addEventListener("mousedown", async (e) => {
      e.preventDefault();
      try {
        const entity = await api("/api/entities", {
          method: "POST",
          body: { entity_type: row.dataset.create, name: row.dataset.name },
        });
        entity.isNew = true;
        addLinkedEntity(entity);
        markReportDirty();
        showToast(`Created ${row.dataset.create}: ${entity.name}`);
      } catch (err) {
        showToast("Couldn't create that entity: " + err.message, true);
      }
      input.value = "";
      results.hidden = true;
    });
  });
}, 250);

/* --- saving ---------------------------------------------------------------- */

function editorPayload() {
  return {
    title: document.getElementById("report-editor-title").value.trim(),
    body_markdown: document.getElementById("report-editor-body").value,
    status: document.getElementById("report-editor-status").value,
    credibility_rating: document.getElementById("report-editor-credibility").value || null,
    criticality: document.getElementById("report-editor-criticality").value || null,
    entity_ids: reportEditor.linked.map((e) => e.id),
  };
}

async function saveReportEditor({ silent = false } = {}) {
  if (reportEditor.saving) return null;
  const payload = editorPayload();
  if (!payload.title) {
    if (!silent) document.getElementById("report-editor-error").textContent = "Give the report a title before saving.";
    return null;
  }
  reportEditor.saving = true;
  setEditorStatus(silent ? "Saving…" : "Saving…");
  try {
    const result = reportEditor.reportId
      ? await api(`/api/reports/${reportEditor.reportId}`, { method: "PATCH", body: payload })
      : await api("/api/reports", { method: "POST", body: payload });
    reportEditor.reportId = result.id;
    reportEditor.dirty = false;
    // The server links anyone mentioned in the prose, so its idea of the cast
    // list is authoritative — take it back rather than trusting the local one.
    reportEditor.linked = result.entities.map((e) => ({
      ...e,
      isNew: (reportEditor.linked.find((x) => x.id === e.id) || {}).isNew || false,
    }));
    renderEditorEntityList();
    clearLocalDraft();
    document.getElementById("report-editor-error").textContent = "";
    setEditorStatus("Saved just now");
    document.getElementById("report-editor-context").textContent = "Editing · saved just now";
    return result;
  } catch (err) {
    if (!silent) document.getElementById("report-editor-error").textContent = err.message;
    setEditorStatus("Not saved — " + err.message);
    return null;
  } finally {
    reportEditor.saving = false;
  }
}

function startAutosave() {
  stopAutosave();
  reportEditor.autosaveTimer = setInterval(() => {
    // Only for a report that already exists. Autosaving a brand new one would
    // create a permanent report from an abandoned draft — and this app has no
    // way to delete a report. The local draft backup covers that case instead.
    if (reportEditor.reportId && reportEditor.dirty && !reportEditor.saving) {
      saveReportEditor({ silent: true });
    }
  }, AUTOSAVE_MS);
}

function stopAutosave() {
  if (reportEditor.autosaveTimer) {
    clearInterval(reportEditor.autosaveTimer);
    reportEditor.autosaveTimer = null;
  }
}

/* --- wiring ---------------------------------------------------------------- */

(function wireReportEditor() {
  const body = document.getElementById("report-editor-body");
  const title = document.getElementById("report-editor-title");
  if (!body || !title) return;

  editorMentions = attachMentionAutocomplete(
    body, document.getElementById("mention-menu"), (entity) => {
      addLinkedEntity(entity);
      markReportDirty();
    });

  body.addEventListener("input", markReportDirty);

  title.addEventListener("input", markReportDirty);
  document.getElementById("report-editor-status").addEventListener("change", markReportDirty);
  document.getElementById("report-editor-credibility").addEventListener("change", markReportDirty);
  document.getElementById("report-editor-criticality").addEventListener("change", markReportDirty);

  document.getElementById("editor-entity-search").addEventListener("input", searchEditorEntities);
  document.getElementById("editor-entity-search").addEventListener("blur", () => {
    setTimeout(() => { document.getElementById("editor-entity-results").hidden = true; }, 200);
  });

  document.getElementById("report-editor-save").addEventListener("click", async () => {
    const result = await saveReportEditor();
    if (result) {
      showToast("Report saved");
      closeReportEditor(result.id);
    }
  });

  document.getElementById("report-editor-back").addEventListener("click", () => {
    if (reportEditor.dirty && !confirm("Leave without saving your changes?")) return;
    closeReportEditor(reportEditor.reportId);
  });

  // Last line of defence for a closed tab or a reload mid-interview.
  window.addEventListener("beforeunload", (e) => {
    if (state.reportEditorOpen && reportEditor.dirty) {
      saveLocalDraft();
      e.preventDefault();
      e.returnValue = "";
    }
  });
})();

async function openReportDetail(id) {
  setActiveTab("reports"); // correct even when reached from the Dashboard, not just the Reports list
  switchViewRaw("report-detail");
  const el = document.getElementById("report-detail-body");
  el.innerHTML = '<p class="empty-state">Loading…</p>';
  try {
    const report = await api(`/api/reports/${id}`);
    state.currentReportId = id;
    renderReportDetail(report);
  } catch (err) {
    el.innerHTML = `<p class="empty-state">${escapeHtml(err.message)}</p>`;
  }
}

// Entity mentions are markdown links to #/entities/<id> — see MENTION_RE in
// api/reports.py.
const MENTION_LINK_RE = /\[([^\]]+)\]\(#\/entities\/([A-Za-z0-9_-]+)\)/g;

function renderMarkdown(text) {
  if (window.DOMPurify && window.marked) {
    return DOMPurify.sanitize(marked.parse(text || ""));
  }
  // Offline fallback, for the air-gapped/LAN-only deployments this app
  // explicitly supports: the markdown CDN is unreachable, so the body renders
  // as plain text. Mentions are still turned into real links even here —
  // without this, every report written using @-mentions would display raw
  // `[Name](#/entities/…)` syntax on exactly the deployments least able to
  // fix it. Escaping happens first, so the only markup that survives is the
  // anchors added afterward.
  const escaped = escapeHtml(text || "");
  const withMentions = escaped.replace(
    MENTION_LINK_RE,
    (_match, label, entityId) => `<a href="#/entities/${entityId}">${label}</a>`,
  );
  return `<pre>${withMentions}</pre>`;
}

function renderReportDetail(report) {
  const el = document.getElementById("report-detail-body");
  el.innerHTML = `
    <div class="detail-panel">
      <div class="detail-header">
        <div>
          <span class="status-pill status-${report.status}">${escapeHtml(report.status)}</span>${criticalityBadgeHtml(report)}
          ${report.credibility_rating ? `<span class="card-meta">Credibility: ${escapeHtml(report.credibility_rating)}</span>` : ""}
          <h2>${escapeHtml(report.title)}</h2>
        </div>
        <div class="detail-actions">
          <!-- Plain link, not a fetch: lets the browser stream the PDF
               straight to disk with its own download UI, same reasoning as
               the backup download. -->
          <a class="btn-secondary btn-sm" id="export-report-btn" href="/api/reports/${encodeURIComponent(report.id)}/export.pdf">Export PDF</a>
          <button class="btn-secondary btn-sm" id="edit-report-btn">Edit</button>
          ${canDelete() ? '<button class="btn-danger btn-sm" id="delete-report-btn">Delete</button>' : ""}
        </div>
      </div>
      <div class="markdown-body">${renderMarkdown(report.body_markdown)}</div>

      <div class="section-title">Linked entities</div>
      <ul class="report-chip-list">${report.entities.length ? report.entities.map((e) => `<li><a href="#" data-open-entity="${escapeHtml(e.id)}">${escapeHtml(e.name)}</a> <span class="type-pill type-${e.entity_type}">${escapeHtml(e.entity_type)}</span></li>`).join("") : '<li class="empty-state">None linked.</li>'}</ul>

      <div class="section-title">Attachments</div>
      <ul class="attachment-list" id="report-attachment-list">${report.attachments.map(attachmentRowHtml).join("") || '<li class="empty-state">None yet.</li>'}</ul>
      <form id="report-upload-form" class="form-row-inline" style="margin-top:.6em;">
        <input type="file" id="report-upload-file" required>
        <button type="submit" class="btn-secondary btn-sm">Upload</button>
      </form>
    </div>
  `;
  document.getElementById("edit-report-btn").addEventListener("click", () => openReportForm(report));
  const deleteReportBtn = document.getElementById("delete-report-btn");
  if (deleteReportBtn) {
    deleteReportBtn.addEventListener("click", () => openDeleteDialog(
      { report_ids: [report.id] }, () => switchView("reports")));
  }

  // Mentions render as links to #/entities/<id> (see MENTION_RE in
  // api/reports.py). Intercept them so they open the record in-app instead of
  // just moving the URL fragment and doing nothing visible.
  el.querySelectorAll('.markdown-body a[href^="#/entities/"]').forEach((a) => {
    a.classList.add("entity-mention");
    a.addEventListener("click", (e) => {
      e.preventDefault();
      openEntityDetail(a.getAttribute("href").replace("#/entities/", ""));
    });
  });
  el.querySelectorAll("[data-open-entity]").forEach((a) => {
    a.addEventListener("click", (e) => { e.preventDefault(); openEntityDetail(a.dataset.openEntity); });
  });
  wireAttachmentPreviews(el, report.attachments);
  el.querySelectorAll("[data-delete-att]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      if (!confirm("Delete this attachment?")) return;
      try {
        await api(`/api/attachments/${btn.dataset.deleteAtt}`, { method: "DELETE" });
        openReportDetail(report.id);
      } catch (err) { showToast(err.message, true); }
    });
  });
  document.getElementById("report-upload-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const fileInput = document.getElementById("report-upload-file");
    if (!fileInput.files.length) return;
    const fd = new FormData();
    fd.append("report_id", report.id);
    fd.append("file", fileInput.files[0]);
    try {
      await apiUpload("/api/attachments", fd);
      showToast("Uploaded");
      openReportDetail(report.id);
    } catch (err) { showToast(err.message, true); }
  });
}

/* ============================================================================
 * Guided debrief
 *
 * The report editor assumes you know what you're writing. This is for the
 * other case: someone is sitting in front of you and the report doesn't exist
 * yet. Each step asks for one thing, and every name mentioned along the way
 * becomes a real entity at the moment it's mentioned — which is the whole
 * point. Building the cast list first, in another tab, then coming back to
 * write, is the thing this replaces.
 *
 * Everything here is assembled client-side and saved through the ordinary
 * report and entity endpoints. There is deliberately no "debrief" object in
 * the database: what a debrief produces is a report and some entities, and
 * inventing a third thing to own them would mean a schema migration for
 * every existing deployment in exchange for nothing an analyst can see.
 * ========================================================================== */

/* --- reusable entity picker ------------------------------------------------
 * Search-or-create against a fixed set of types. Used four times below with
 * different types and different follow-up fields.
 * ------------------------------------------------------------------------ */

function createEntityPicker(container, opts) {
  const types = opts.types;
  const multiple = !!opts.multiple;
  const roleField = opts.roleField || null;
  const items = [];

  container.classList.add("entity-picker");
  container.innerHTML = `
    <div class="picker-search">
      <input type="search" class="picker-input" placeholder="${escapeHtml(opts.placeholder || "Search or create…")}" autocomplete="off">
      <div class="picker-results" hidden></div>
    </div>
    <ul class="picker-selected"></ul>`;

  const input = container.querySelector(".picker-input");
  const results = container.querySelector(".picker-results");
  const selected = container.querySelector(".picker-selected");

  function changed() {
    renderSelected();
    if (opts.onChange) opts.onChange(items);
  }

  function renderSelected() {
    selected.innerHTML = items.map((item, i) => {
      const showRole = roleField && (!roleField.newOnly || item.isNew);
      return `
      <li class="picker-item">
        <div class="picker-item-head">
          <span class="type-pill type-${escapeHtml(item.entity_type)}">${escapeHtml(item.entity_type)}</span>
          <span class="picker-item-name">${escapeHtml(item.name)}</span>
          ${item.isNew ? '<span class="chip-new">new</span>' : ""}
          <button type="button" class="btn-link picker-remove" data-i="${i}" title="Remove">&times;</button>
        </div>
        ${showRole ? `<input type="text" class="picker-role" data-i="${i}"
            placeholder="${escapeHtml(roleField.placeholder || "")}"
            value="${escapeHtml(item.role || "")}">` : ""}
      </li>`;
    }).join("");

    selected.querySelectorAll(".picker-remove").forEach((btn) => {
      btn.addEventListener("click", () => {
        items.splice(Number(btn.dataset.i), 1);
        changed();
      });
    });
    selected.querySelectorAll(".picker-role").forEach((box) => {
      // `change`, not `input`: this can write back to the entity record, and
      // one PATCH per keystroke would be absurd.
      box.addEventListener("change", async () => {
        const item = items[Number(box.dataset.i)];
        if (!item) return;
        item.role = box.value.trim();
        if (opts.onChange) opts.onChange(items);
        if (roleField.apply && item.role) {
          const body = roleField.apply(item, item.role);
          if (body) {
            try {
              await api(`/api/entities/${item.id}`, { method: "PATCH", body });
            } catch (err) {
              showToast(`Saved to the report, but not to ${item.name}'s record: ${err.message}`, true);
            }
          }
        }
      });
    });
  }

  function pick(entity) {
    if (items.some((e) => e.id === entity.id)) {
      input.value = "";
      results.hidden = true;
      return;
    }
    if (!multiple) items.length = 0;
    items.push({ id: entity.id, name: entity.name, entity_type: entity.entity_type,
                 isNew: !!entity.isNew, role: "" });
    input.value = "";
    results.hidden = true;
    changed();
  }

  const search = debounce(async () => {
    const query = input.value.trim();
    if (!query) { results.hidden = true; results.innerHTML = ""; return; }

    // One request per allowed type rather than one unfiltered request that
    // gets filtered here — /api/entities takes a single entity_type, and
    // filtering a shared limit client-side would silently drop matches.
    let matches = [];
    try {
      const pages = await Promise.all(types.map((t) => api(
        "/api/entities?" + new URLSearchParams({ q: query, entity_type: t, limit: "5" }).toString())));
      matches = pages.flatMap((p) => p.items);
    } catch (e) { /* offer creation anyway */ }

    const existing = matches.map((e) => `
      <div class="picker-result" data-add="${escapeHtml(e.id)}">
        <span class="type-pill type-${escapeHtml(e.entity_type)}">${escapeHtml(e.entity_type)}</span> ${escapeHtml(e.name)}
      </div>`).join("");
    const creates = types
      .filter((t) => !matches.some((m) => m.entity_type === t && m.name.toLowerCase() === query.toLowerCase()))
      .map((t) => `
      <div class="picker-result picker-create" data-create="${t}">
        <span class="mention-plus">+</span> Create ${t} “${escapeHtml(truncate(query, 30))}”
      </div>`).join("");

    results.innerHTML = existing + (creates ? '<div class="editor-entity-sep">Not on file?</div>' + creates : "");
    results.hidden = false;

    results.querySelectorAll("[data-add]").forEach((row) => {
      row.addEventListener("mousedown", (e) => {
        e.preventDefault();
        pick(matches.find((m) => m.id === row.dataset.add));
      });
    });
    results.querySelectorAll("[data-create]").forEach((row) => {
      row.addEventListener("mousedown", async (e) => {
        e.preventDefault();
        const entityType = row.dataset.create;
        results.innerHTML = `<div class="mention-hint">Creating ${escapeHtml(entityType)} “${escapeHtml(query)}”…</div>`;
        try {
          const extra = opts.newDetails ? (opts.newDetails(entityType) || {}) : {};
          const entity = await api("/api/entities", {
            method: "POST",
            body: { entity_type: entityType, name: query, ...extra },
          });
          entity.isNew = true;
          pick(entity);
          showToast(`Created ${entityType}: ${entity.name}`);
        } catch (err) {
          results.hidden = true;
          showToast("Couldn't create that entity: " + err.message, true);
        }
      });
    });
  }, 250);

  input.addEventListener("input", search);
  input.addEventListener("blur", () => setTimeout(() => { results.hidden = true; }, 200));

  return {
    items,
    set(list) {
      items.length = 0;
      (list || []).forEach((e) => items.push({ ...e }));
      renderSelected();
    },
    clear() { items.length = 0; input.value = ""; results.hidden = true; renderSelected(); },
  };
}

/* --- state ---------------------------------------------------------------- */

const DEBRIEF_STORAGE_KEY = "humint.debrief.draft";
const DEBRIEF_STEPS = 5;

// Interview scaffolding, not a form: an analyst mid-debrief is listening, not
// remembering a checklist. These drop into the narrative as headings so the
// answers end up under the question that produced them.
const DEBRIEF_PROMPTS = [
  "What did you see?",
  "When did it start and when did it end?",
  "Who else was there?",
  "What were they wearing, driving, carrying?",
  "What was said, and by whom?",
  "How do you know this?",
  "Has this happened before?",
  "What did you not see or hear?",
  "Is there anything you're unsure about?",
];

const debrief = {
  step: 0,
  dirty: false,
  saving: false,
  titleTouched: false,
  mentioned: [],       // entities linked by @-mention in the prose
  pickers: {},         // built once, on first open
  // A place the source pointed at rather than named: {lat, lng, name}. Held
  // here, not written to the case file, until the report is created -- an
  // abandoned debrief must leave nothing behind, same as everything else in
  // this wizard.
  place: null,
  map: null,
  marker: null,
};

const RELIABILITY_TEXT = {
  A: "Completely reliable", B: "Usually reliable", C: "Fairly reliable",
  D: "Not usually reliable", E: "Unreliable", F: "Reliability cannot be judged",
};

function debriefEl(id) { return document.getElementById("debrief-" + id); }

function markDebriefDirty() {
  debrief.dirty = true;
  saveDebriefDraft();
  renderDebriefEntities();
  if (!debrief.titleTouched) debriefEl("title").value = defaultDebriefTitle();
}

function localDateTimeValue(d) {
  // <input type="datetime-local"> wants local wall-clock time with no zone,
  // which is exactly what toISOString() does not give.
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function readableDateTime(value) {
  return value ? String(value).replace("T", " ") : "";
}

/* --- draft persistence -----------------------------------------------------
 * A debrief is a live conversation; a stray refresh in the middle of one is
 * exactly when losing the notes hurts most. The entities are already saved
 * server-side by then — this is only the prose and the choices around it.
 * ------------------------------------------------------------------------ */

function debriefSnapshot() {
  return {
    step: debrief.step,
    source: debrief.pickers.source ? debrief.pickers.source.items : [],
    location: debrief.pickers.location ? debrief.pickers.location.items : [],
    event: debrief.pickers.event ? debrief.pickers.event.items : [],
    involved: debrief.pickers.involved ? debrief.pickers.involved.items : [],
    mentioned: debrief.mentioned,
    place: debrief.place,
    reliability: debriefEl("reliability").value,
    when: debriefEl("when").value,
    occurred: debriefEl("occurred").value,
    narrative: debriefEl("narrative").value,
    assessment: debriefEl("assessment").value,
    credibility: debriefEl("credibility").value,
    criticality: debriefEl("criticality").value,
    status: debriefEl("status").value,
    title: debriefEl("title").value,
    titleTouched: debrief.titleTouched,
    savedAt: new Date().toISOString(),
  };
}

function saveDebriefDraft() {
  try { localStorage.setItem(DEBRIEF_STORAGE_KEY, JSON.stringify(debriefSnapshot())); }
  catch (e) { /* storage off or full — the debrief still works */ }
}

function readDebriefDraft() {
  try { return JSON.parse(localStorage.getItem(DEBRIEF_STORAGE_KEY) || "null"); }
  catch (e) { return null; }
}

function clearDebriefDraft() {
  try { localStorage.removeItem(DEBRIEF_STORAGE_KEY); } catch (e) { /* nothing to clean up */ }
}

function applyDebriefDraft(draft) {
  debrief.pickers.source.set(draft.source);
  debrief.pickers.location.set(draft.location);
  debrief.pickers.event.set(draft.event);
  debrief.pickers.involved.set(draft.involved);
  debrief.mentioned = draft.mentioned || [];
  debrief.place = draft.place || null;
  renderDebriefPlace();
  debriefEl("reliability").value = draft.reliability || "";
  debriefEl("when").value = draft.when || "";
  debriefEl("occurred").value = draft.occurred || "";
  debriefEl("narrative").value = draft.narrative || "";
  debriefEl("assessment").value = draft.assessment || "";
  debriefEl("credibility").value = draft.credibility || "";
  debriefEl("criticality").value = draft.criticality || "";
  debriefEl("status").value = draft.status || "draft";
  debrief.titleTouched = !!draft.titleTouched;
  debriefEl("title").value = draft.title || defaultDebriefTitle();
  gotoDebriefStep(draft.step || 0);
  // A restored debrief is a debrief in progress: leaving it should warn on the
  // way out exactly as it would have before the interruption.
  debrief.dirty = true;
}

/* --- opening and closing --------------------------------------------------- */

function openDebrief() {
  buildDebriefPickers();
  setActiveTab("reports");
  switchViewRaw("debrief");
  state.debriefOpen = true;

  resetDebrief();
  const draft = readDebriefDraft();
  const hasContent = draft && (draft.narrative || draft.assessment || (draft.source || []).length);
  if (hasContent && confirm(
      `An unfinished debrief from ${relativeTime(draft.savedAt)} was found. Pick it up where you left off?`)) {
    applyDebriefDraft(draft);
  } else if (draft) {
    clearDebriefDraft();
  }
  renderDebriefEntities();
}

function resetDebrief() {
  debrief.step = 0;
  debrief.dirty = false;
  debrief.titleTouched = false;
  debrief.mentioned = [];
  debrief.place = null;
  renderDebriefPlace();
  // Fold the map away between debriefs. Left open, the next debrief starts
  // with a map of the last one's ground showing, which reads as though the
  // place carried over -- and the toggle would then close it rather than
  // open it.
  const mapPanel = document.getElementById("debrief-map-panel");
  if (mapPanel) mapPanel.hidden = true;
  Object.values(debrief.pickers).forEach((p) => p.clear && p.clear());
  ["narrative", "assessment"].forEach((k) => { debriefEl(k).value = ""; });
  debriefEl("reliability").value = "";
  debriefEl("credibility").value = "";
  debriefEl("criticality").value = "";
  debriefEl("status").value = "draft";
  debriefEl("error").textContent = "";
  debriefEl("status-msg").textContent = "";
  const now = localDateTimeValue(new Date());
  debriefEl("when").value = now;
  debriefEl("occurred").value = now;
  debriefEl("title").value = defaultDebriefTitle();
  gotoDebriefStep(0);
}

function closeDebrief(view) {
  state.debriefOpen = false;
  switchView(view || "reports");
}

/* --- steps ----------------------------------------------------------------- */

function gotoDebriefStep(n) {
  debrief.step = Math.max(0, Math.min(DEBRIEF_STEPS - 1, n));
  document.querySelectorAll("#view-debrief .wizard-panel").forEach((panel) => {
    panel.hidden = Number(panel.dataset.panel) !== debrief.step;
  });
  document.querySelectorAll("#debrief-steps li").forEach((li) => {
    const i = Number(li.dataset.step);
    li.classList.toggle("active", i === debrief.step);
    li.classList.toggle("done", i < debrief.step);
  });
  debriefEl("back").hidden = debrief.step === 0;
  const last = debrief.step === DEBRIEF_STEPS - 1;
  debriefEl("next").hidden = last;
  debriefEl("finish").hidden = !last;
  debriefEl("error").textContent = "";
  if (last) renderDebriefReview();

  // The source picker only offers to write a reliability rating back to a
  // record that has somewhere to put one; a person entity does not.
  const src = debrief.pickers.source ? debrief.pickers.source.items[0] : null;
  debriefEl("save-reliability-row").hidden = !(src && src.entity_type === "source");
}

/* --- the cast list --------------------------------------------------------- */

function debriefEntities() {
  const all = [];
  const push = (e) => { if (e && !all.some((x) => x.id === e.id)) all.push(e); };
  ["source", "location", "event", "involved"].forEach((key) => {
    const picker = debrief.pickers[key];
    if (picker) picker.items.forEach(push);
  });
  debrief.mentioned.forEach(push);
  return all;
}

function renderDebriefEntities() {
  const list = document.getElementById("debrief-entity-list");
  const count = document.getElementById("debrief-entity-count");
  if (!list) return;
  const all = debriefEntities();
  count.hidden = all.length === 0;
  count.textContent = String(all.length);
  list.innerHTML = all.length
    ? all.map((e) => `
      <li class="editor-entity-row">
        <span class="type-pill type-${escapeHtml(e.entity_type)}">${escapeHtml(e.entity_type)}</span>
        <span class="editor-entity-name">${escapeHtml(e.name)}</span>
        ${e.isNew ? '<span class="chip-new">new</span>' : ""}
        <button type="button" class="btn-link" data-peek="${escapeHtml(e.id)}" title="Open read-only">@</button>
      </li>`).join("")
    : '<li class="empty-state">Nothing yet.</li>';
  list.querySelectorAll("[data-peek]").forEach((btn) => {
    btn.addEventListener("click", () => peekEntity(btn.dataset.peek));
  });
}

/* --- assembling the report -------------------------------------------------
 * The structure is the point: a debrief that comes out as one undifferentiated
 * block of prose is the thing this workflow exists to stop. Source, timing,
 * cast, account, and assessment each get their own heading, and an empty
 * section is left out rather than left blank.
 * ------------------------------------------------------------------------ */

function mentionLink(entity) {
  const label = String(entity.name).replace(/[\[\]]/g, "").trim() || entity.id;
  return `[${label}](#/entities/${entity.id})`;
}

function defaultDebriefTitle() {
  const src = debrief.pickers.source ? debrief.pickers.source.items[0] : null;
  const when = (debriefEl("when") && debriefEl("when").value || "").slice(0, 10);
  const who = src ? src.name : "source";
  return `Debrief — ${who}${when ? " — " + when : ""}`;
}

function debriefMarkdown() {
  const parts = [];
  const src = debrief.pickers.source.items[0];
  const loc = debrief.pickers.location.items[0];
  const evt = debrief.pickers.event.items[0];
  const involved = debrief.pickers.involved.items;

  // Bullets rather than one line each: a markdown hard line break (two
  // trailing spaces) survives the in-app renderer but is folded back into a
  // running paragraph by the PDF exporter, which would run the location and
  // the event together in the exported package. A list means the same thing
  // in both, and reads like a header block either way.
  const sourceLines = [];
  if (src) {
    const rating = debriefEl("reliability").value;
    const suffix = rating ? ` — reliability ${rating} (${RELIABILITY_TEXT[rating]})` : "";
    sourceLines.push(`${mentionLink(src)}${suffix}`);
  }
  const when = readableDateTime(debriefEl("when").value);
  if (when) sourceLines.push(`Debriefed ${when} by ${state.user ? state.user.username : "an analyst"}`);
  if (sourceLines.length) parts.push("## Source\n\n" + sourceLines.map((l) => `- ${l}`).join("\n"));

  const whereLines = [];
  const occurred = readableDateTime(debriefEl("occurred").value);
  if (occurred) whereLines.push(`Occurred ${occurred}`);
  if (loc) whereLines.push(`Location: ${mentionLink(loc)}`);
  if (evt) whereLines.push(`Event: ${mentionLink(evt)}`);
  if (whereLines.length) parts.push("## When and where\n\n" + whereLines.map((l) => `- ${l}`).join("\n"));

  if (involved.length) {
    parts.push("## Who was involved\n\n" + involved.map((e) => {
      const role = (e.role || "").trim();
      return `- ${mentionLink(e)}${role ? " — " + role : ""}`;
    }).join("\n"));
  }

  const narrative = debriefEl("narrative").value.trim();
  if (narrative) parts.push("## Narrative\n\n" + narrative);

  const assessment = debriefEl("assessment").value.trim();
  if (assessment) parts.push("## Assessment\n\n" + assessment);

  return parts.join("\n\n");
}

function renderDebriefReview() {
  const el = document.getElementById("debrief-review");
  if (!el) return;
  const all = debriefEntities();
  const fresh = all.filter((e) => e.isNew);
  const narrative = debriefEl("narrative").value.trim();
  const rows = [
    ["Source", debrief.pickers.source.items.map((e) => e.name).join(", ") || "— none picked"],
    ["Location", debrief.pickers.location.items.map((e) => e.name).join(", ") || "— not recorded"],
    ["Event", debrief.pickers.event.items.map((e) => e.name).join(", ") || "— none"],
    ["Others involved", debrief.pickers.involved.items.length
      ? `${debrief.pickers.involved.items.length} (${debrief.pickers.involved.items.map((e) => e.name).join(", ")})`
      : "— none"],
    ["Narrative", narrative ? `${narrative.split(/\s+/).length} words` : "— empty"],
    ["Criticality", debriefEl("criticality").value || "— not set"],
    ["Entities linked", String(all.length)],
    ["Created during this debrief", fresh.length ? fresh.map((e) => e.name).join(", ") : "none"],
  ];
  el.innerHTML = `
    <div class="editor-side-title">Before you file it</div>
    <table class="wizard-review-table">
      ${rows.map(([k, v]) => `<tr><th>${escapeHtml(k)}</th><td>${escapeHtml(v)}</td></tr>`).join("")}
    </table>`;
}

async function finishDebrief() {
  if (debrief.saving) return;
  const title = debriefEl("title").value.trim();
  const err = debriefEl("error");
  if (!title) { err.textContent = "Give the report a title before filing it."; return; }
  const body = debriefMarkdown();
  if (!body.trim()) { err.textContent = "There's nothing in this debrief yet."; return; }

  debrief.saving = true;
  err.textContent = "";
  debriefEl("status-msg").textContent = "Filing…";
  let placeEntity = null;
  try {
    // A place the source pointed at is written now, not when they pointed at
    // it, so an abandoned debrief leaves no orphan behind. It goes in before
    // the report because the report has to be able to reference it.
    placeEntity = await createDebriefPlace();
    // Everything gathered is linked explicitly, including entities that were
    // picked but never named in the prose. The server unions this with
    // whatever it finds mentioned in the body, so nothing here can drop
    // someone out of the report.
    const report = await api("/api/reports", {
      method: "POST",
      body: {
        title,
        body_markdown: body,
        status: debriefEl("status").value,
        credibility_rating: debriefEl("credibility").value || null,
        criticality: debriefEl("criticality").value || null,
        entity_ids: debriefEntities().map((e) => e.id)
          .concat(placeEntity ? [placeEntity.id] : []),
      },
    });

    const src = debrief.pickers.source.items[0];
    const rating = debriefEl("reliability").value;
    if (src && src.entity_type === "source" && rating && debriefEl("save-reliability").checked) {
      try {
        await api(`/api/entities/${src.id}`, {
          method: "PATCH", body: { details: { reliability_rating: rating } },
        });
      } catch (e) {
        // The report is already filed; the rating is a nicety on top of it.
        showToast("Report filed, but the reliability rating didn't save on the source.", true);
      }
    }

    clearDebriefDraft();
    debrief.dirty = false;
    state.debriefOpen = false;
    showToast("Debrief filed as a report");
    openReportDetail(report.id);
  } catch (e) {
    // If the Location was created and the report then failed, say so plainly
    // rather than leaving a record the analyst does not know exists.
    err.textContent = placeEntity
      ? `${e.message} (The place you pointed at was saved as “${placeEntity.name}” — `
        + "it is on the Entities page.)"
      : e.message;
    debriefEl("status-msg").textContent = "";
  } finally {
    debrief.saving = false;
  }
}

/* --- wiring ---------------------------------------------------------------- */

/* --- pointing at a place instead of naming one -----------------------------
 *
 * The case this exists for: a source knows exactly where something happened
 * and cannot give you an address for it. They can point at the roof. Typing
 * "behind the grain store" into a text field loses the only precise thing
 * they actually knew.
 *
 * The pin lives in the debrief draft, not in the case file. It becomes a
 * Location record at the moment the report is created, and never before --
 * so a debrief that is abandoned halfway leaves no half-named Location behind
 * for somebody to find and wonder about later.
 * ------------------------------------------------------------------------ */

function renderDebriefPlace() {
  const summary = document.getElementById("debrief-place-summary");
  const clear = document.getElementById("debrief-place-clear");
  const nameInput = document.getElementById("debrief-place-name");
  if (!summary || !clear) return;

  if (debrief.place) {
    summary.textContent = `${debrief.place.lat.toFixed(5)}, ${debrief.place.lng.toFixed(5)}`
      + " — saved as a Location when you file the report";
    clear.hidden = false;
    if (nameInput && debrief.place.name && nameInput.value !== debrief.place.name) {
      nameInput.value = debrief.place.name;
    }
  } else {
    summary.textContent = "";
    clear.hidden = true;
    if (nameInput) nameInput.value = "";
  }

  if (debrief.marker && !debrief.place) {
    debrief.map.removeLayer(debrief.marker);
    debrief.marker = null;
  }
}

async function openDebriefMap() {
  const panel = document.getElementById("debrief-map-panel");
  if (!panel) return;
  panel.hidden = false;

  if (typeof L === "undefined") {
    document.getElementById("debrief-map").innerHTML =
      '<p class="empty-state">Map library unavailable.</p>';
    return;
  }

  await loadMapSources();

  if (!debrief.map) {
    debrief.map = L.map("debrief-map").setView([20, 0], 2);
    addBasemap(debrief.map);
    debrief.map.on("click", (e) => {
      debrief.place = {
        lat: e.latlng.lat,
        lng: e.latlng.lng,
        name: (document.getElementById("debrief-place-name").value || "").trim(),
      };
      if (debrief.marker) debrief.marker.setLatLng(e.latlng);
      else debrief.marker = L.marker(e.latlng).addTo(debrief.map);
      renderDebriefPlace();
      markDebriefDirty();
    });
  }

  // The panel was display:none until a moment ago, so Leaflet sized its
  // canvas to nothing. Same nudge the Map view needs on re-entry.
  setTimeout(() => {
    debrief.map.invalidateSize();
    if (debrief.place) {
      const point = [debrief.place.lat, debrief.place.lng];
      if (debrief.marker) debrief.marker.setLatLng(point);
      else debrief.marker = L.marker(point).addTo(debrief.map);
      debrief.map.setView(point, Math.max(debrief.map.getZoom(), 13));
    }
  }, 60);
}

function wireDebriefMap() {
  const toggle = document.getElementById("debrief-map-toggle");
  const clear = document.getElementById("debrief-place-clear");
  const nameInput = document.getElementById("debrief-place-name");
  if (!toggle) return;

  toggle.addEventListener("click", () => {
    const panel = document.getElementById("debrief-map-panel");
    if (panel.hidden) openDebriefMap();
    else panel.hidden = true;
  });

  if (clear) {
    clear.addEventListener("click", () => {
      debrief.place = null;
      renderDebriefPlace();
      markDebriefDirty();
    });
  }

  if (nameInput) {
    nameInput.addEventListener("input", () => {
      if (!debrief.place) return;
      debrief.place.name = nameInput.value.trim();
      markDebriefDirty();
    });
  }
}

/** Create the Location the source pointed at, if they pointed at one.
 *
 * Called from finishDebrief before the report is written, because the report
 * needs the new record's id in its entity list. Returns the entity or null.
 */
async function createDebriefPlace() {
  if (!debrief.place) return null;
  const name = (debrief.place.name || "").trim()
    || `Place given during debrief (${debrief.place.lat.toFixed(4)}, ${debrief.place.lng.toFixed(4)})`;
  return api("/api/entities", {
    method: "POST",
    body: {
      entity_type: "location",
      name,
      description: "Pointed out on the map during a debrief; no address was given.",
      details: {
        lat: debrief.place.lat.toFixed(6),
        lng: debrief.place.lng.toFixed(6),
      },
    },
  });
}

function buildDebriefPickers() {
  if (debrief.pickers.source) return;

  debrief.pickers.source = createEntityPicker(
    document.getElementById("debrief-source-picker"), {
      types: ["source", "person"],
      placeholder: "Name of the person being debriefed…",
      onChange: () => {
        markDebriefDirty();
        const src = debrief.pickers.source.items[0];
        debriefEl("save-reliability-row").hidden = !(src && src.entity_type === "source");
      },
    });

  debrief.pickers.location = createEntityPicker(
    document.getElementById("debrief-location-picker"), {
      types: ["location"],
      placeholder: "Where did it happen?",
      roleField: {
        newOnly: true,
        placeholder: "Address, grid reference or landmark (optional)",
        // Goes onto the location record, which is also what puts it on the map.
        apply: (item, value) => ({ details: { address: value } }),
      },
      onChange: markDebriefDirty,
    });

  debrief.pickers.event = createEntityPicker(
    document.getElementById("debrief-event-picker"), {
      types: ["event"],
      placeholder: "Meeting, movement, incident…",
      newDetails: () => {
        const occurred = debriefEl("occurred").value;
        return occurred ? { details: { started_at: occurred } } : {};
      },
      onChange: markDebriefDirty,
    });

  debrief.pickers.involved = createEntityPicker(
    document.getElementById("debrief-involved-picker"), {
      types: ["person", "organization", "communication"],
      multiple: true,
      placeholder: "Add a person, organisation or means of communication…",
      roleField: {
        placeholder: "Their part in this",
        // Only ever fills in a blank description — never overwrites what
        // someone already wrote on an existing record.
        apply: (item, value) => (item.isNew ? { description: value } : null),
      },
      onChange: markDebriefDirty,
    });
}

(function wireDebrief() {
  const narrative = document.getElementById("debrief-narrative");
  if (!narrative) return;

  wireDebriefMap();

  const linkMentioned = (entity) => {
    if (!debrief.mentioned.some((e) => e.id === entity.id)) {
      debrief.mentioned.push({ id: entity.id, name: entity.name,
                               entity_type: entity.entity_type, isNew: !!entity.isNew });
    }
    markDebriefDirty();
  };

  attachMentionAutocomplete(narrative, document.getElementById("debrief-narrative-menu"), linkMentioned);
  attachMentionAutocomplete(document.getElementById("debrief-assessment"),
                            document.getElementById("debrief-assessment-menu"), linkMentioned);

  narrative.addEventListener("input", markDebriefDirty);
  document.getElementById("debrief-assessment").addEventListener("input", markDebriefDirty);
  ["reliability", "when", "occurred", "credibility", "criticality", "status"].forEach((k) => {
    debriefEl(k).addEventListener("change", markDebriefDirty);
  });
  debriefEl("title").addEventListener("input", () => {
    debrief.titleTouched = true;
    saveDebriefDraft();
  });

  const prompts = document.getElementById("debrief-prompt-list");
  prompts.innerHTML = DEBRIEF_PROMPTS.map((q, i) =>
    `<li><button type="button" class="btn-link" data-prompt="${i}">${escapeHtml(q)}</button></li>`).join("");
  prompts.querySelectorAll("[data-prompt]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const q = DEBRIEF_PROMPTS[Number(btn.dataset.prompt)];
      const existing = narrative.value.replace(/\s+$/, "");
      narrative.value = (existing ? existing + "\n\n" : "") + `**${q}**\n\n`;
      narrative.focus();
      narrative.setSelectionRange(narrative.value.length, narrative.value.length);
      markDebriefDirty();
    });
  });

  debriefEl("next").addEventListener("click", () => {
    if (debrief.step === 0 && !debrief.pickers.source.items.length) {
      debriefEl("error").textContent =
        "Pick or create the person you're debriefing first.";
      return;
    }
    gotoDebriefStep(debrief.step + 1);
  });
  debriefEl("back").addEventListener("click", () => gotoDebriefStep(debrief.step - 1));
  debriefEl("finish").addEventListener("click", finishDebrief);
  debriefEl("exit").addEventListener("click", () => closeDebrief("reports"));

  document.querySelectorAll("#debrief-steps li").forEach((li) => {
    // Only backwards: skipping ahead past the source would leave the wizard
    // in a state its own Next button refuses to reach.
    li.addEventListener("click", () => {
      const target = Number(li.dataset.step);
      if (target < debrief.step) gotoDebriefStep(target);
    });
  });

  const start = document.getElementById("start-debrief-btn");
  if (start) start.addEventListener("click", openDebrief);
})();

/* ============================================================================
 * Extraction review queue
 * ========================================================================== */

/* The queue's filters. Entities before relationships is already the server's
 * default order, because a relationship can only be accepted once both its
 * ends exist — these let you commit to that rather than just benefit from it,
 * and let you take one document at a time, which is how the material arrived. */
const EXTRACTION_FILTERS = [
  ["extraction-status-filter", "status"],
  ["extraction-source-filter", "source"],
  ["extraction-kind-filter", "suggestion_type"],
  ["extraction-entity-type-filter", "entity_type"],
  ["extraction-document-filter", "attachment_id"],
];

EXTRACTION_FILTERS.forEach(([id]) => {
  const el = document.getElementById(id);
  if (el) el.addEventListener("change", () => loadExtractionQueue());
});

const clearFiltersBtn = document.getElementById("extraction-clear-filters");
if (clearFiltersBtn) {
  clearFiltersBtn.addEventListener("click", () => {
    EXTRACTION_FILTERS.forEach(([id, param]) => {
      const el = document.getElementById(id);
      // Status is not a filter you clear — "everything at every status" is not
      // a view of a review queue, it is a view of its history.
      if (el && param !== "status") el.value = "";
    });
    loadExtractionQueue();
  });
}

function extractionQuery() {
  const params = new URLSearchParams();
  const kind = (document.getElementById("extraction-kind-filter") || {}).value;
  for (const [id, param] of EXTRACTION_FILTERS) {
    const el = document.getElementById(id);
    if (!el || !el.value) continue;
    // An entity type means nothing to a relationship, and sending a stale one
    // after switching kind returns an empty queue — which reads as "there is
    // nothing here" rather than "you have contradictory filters on". The
    // control is also reset when the facets come back; this is the guard that
    // makes the very first request after the switch correct.
    if (param === "entity_type" && kind !== "entity") continue;
    params.set(param, el.value);
  }
  params.set("limit", "200");
  return params;
}

async function loadExtractionQueue() {
  const params = extractionQuery();
  try {
    const data = await api("/api/extraction-suggestions?" + params.toString());
    renderExtractionQueue(data.items);
    extractionShown = data.items.length;
    updateExtractionSummary();
  } catch (err) {
    showToast("Failed to load queue: " + err.message, true);
  }
  loadExtractionFacets();
}

/* Counts on the filters themselves. At the top of a fifty-item queue the useful
 * question is not "can I filter to people" but "how many people are waiting" —
 * and a filter that turns out empty after you pick it is a wasted click. */
async function loadExtractionFacets() {
  const status = document.getElementById("extraction-status-filter").value;
  let facets;
  try {
    facets = await api("/api/extraction-suggestions/facets?status=" + encodeURIComponent(status));
  } catch (e) { return; }

  const kind = document.getElementById("extraction-kind-filter");
  const ents = facets.by_type.entity || 0;
  const rels = facets.by_type.relationship || 0;
  kind.options[0].textContent = `Entities and relationships (${ents + rels})`;
  kind.options[1].textContent = `Entities only (${ents})`;
  kind.options[2].textContent = `Relationships only (${rels})`;

  // The entity-type filter is meaningless while relationships are in view, so
  // it appears only when the queue is entities.
  const typeSelect = document.getElementById("extraction-entity-type-filter");
  const showTypes = kind.value === "entity" && facets.by_entity_type.length > 1;
  typeSelect.hidden = !showTypes;
  if (!showTypes && typeSelect.value) { typeSelect.value = ""; }
  rebuildSelect(typeSelect, "Any type", facets.by_entity_type,
                (row) => [row.entity_type, `${row.entity_type} (${row.count})`]);

  rebuildSelect(document.getElementById("extraction-document-filter"),
                "Every document", facets.by_document,
                (row) => [row.attachment_id === null ? "" : String(row.attachment_id),
                          `${row.label} (${row.count})`],
                // A row with no attachment is the signal pass and the
                // assistant; filtering to "" would mean "no filter", so that
                // row is dropped rather than made into a broken option.
                (row) => row.attachment_id !== null);

  const anyFilter = EXTRACTION_FILTERS.some(([id, param]) =>
    param !== "status" && (document.getElementById(id) || {}).value);
  if (clearFiltersBtn) clearFiltersBtn.hidden = !anyFilter;

  // The queue's own `total` is the count AFTER filtering, so comparing against
  // it would always read "50 of 50". The unfiltered total for this status only
  // exists here.
  extractionTotal = facets.total;
  updateExtractionSummary();
}

/* Replace a select's options while keeping the current selection if it still
 * exists — otherwise every refresh after an accept would silently reset the
 * filter the reviewer is working inside. */
function rebuildSelect(select, allLabel, rows, toOption, keep) {
  if (!select) return;
  const current = select.value;
  const options = [`<option value="">${escapeHtml(allLabel)}</option>`];
  for (const row of rows) {
    if (keep && !keep(row)) continue;
    const [value, label] = toOption(row);
    options.push(`<option value="${escapeHtml(value)}">${escapeHtml(label)}</option>`);
  }
  select.innerHTML = options.join("");
  if ([...select.options].some((o) => o.value === current)) select.value = current;
}

let extractionShown = 0;
let extractionTotal = null;

function updateExtractionSummary() {
  const el = document.getElementById("extraction-summary");
  if (!el) return;
  const filtered = EXTRACTION_FILTERS.some(([id, param]) =>
    param !== "status" && (document.getElementById(id) || {}).value);
  if (!filtered) {
    el.textContent = "Proposals from extraction, correlation and the assistant. Nothing enters the case until you accept it.";
    return;
  }
  const of = extractionTotal === null ? "" : ` of ${extractionTotal}`;
  el.textContent = `Showing ${extractionShown}${of}. `
    + "Nothing here is in the case graph until you accept it.";
}

// What each source is, in the reviewer's terms rather than the schema's. The
// distinction is not decoration: "a rule matched two records you already
// entered" and "a language model read a document" deserve different amounts of
// trust, and the card should say which one you're looking at before you click
// Accept.
const SUGGESTION_SOURCE_LABELS = {
  extraction: { label: "From a document", hint: "A model read an attachment and proposed this." },
  signal: { label: "Correlation signal", hint: "A rule matched existing entities. No model involved." },
  assistant: { label: "Assistant", hint: "Proposed by the assistant." },
  manual: { label: "Added by hand", hint: "Added by hand from this document." },
};

function sourcePillHtml(source) {
  const meta = SUGGESTION_SOURCE_LABELS[source];
  if (!meta || source === "extraction") return "";
  return `<span class="source-pill source-${escapeHtml(source)}" title="${escapeHtml(meta.hint)}">${escapeHtml(meta.label)}</span>`;
}

// A signal name like "shared_contact" is a key, not a sentence. These are the
// sentences; an unknown key falls back to the key with its underscores opened
// out, so a rule added later still renders as something rather than nothing.
const SIGNAL_LABELS = {
  implied_sibling: "Same recorded parent",
  shared_contact: "Shared contact detail",
  co_mention: "Named together in reports",
  shared_surname: "Shared surname",
  shared_location: "Recorded at the same place",
  assistant: "Assistant",
};

function signalLabel(name) {
  return SIGNAL_LABELS[name] || String(name || "").replace(/_/g, " ");
}

/* The evidence block. The whole promise of this queue is that you can see why
 * something was proposed before you accept it, so the headline is always
 * visible and the particulars (which phone number, which report, what the
 * assistant was asked) sit one click away rather than behind a page load. */
function evidenceHtml(s) {
  const ev = s.evidence;
  if (!ev || typeof ev !== "object") return "";
  const headline = ev.rule ? `<div class="evidence-headline">${escapeHtml(ev.rule)}</div>` : "";
  const chips = (ev.signals || []).map((name) =>
    `<span class="signal-chip">${escapeHtml(signalLabel(name))}</span>`).join("");
  const details = (ev.all || []).map((entry) => {
    if (!entry || typeof entry !== "object") return "";
    const bits = [];
    if (entry.value) bits.push(`${escapeHtml(entry.kind || "value")}: ${escapeHtml(entry.value)}`);
    if (entry.parent) bits.push(`parent: ${escapeHtml(entry.parent)}`);
    if (entry.surname) bits.push(`surname: ${escapeHtml(entry.surname)}`);
    if (entry.place) bits.push(`place: ${escapeHtml(entry.place)}`);
    if (entry.reports) bits.push(`${escapeHtml(String(entry.reports))} shared report(s)`);
    if (Array.isArray(entry.examples) && entry.examples.length) {
      bits.push("e.g. " + entry.examples.slice(0, 2).map((t) => `&ldquo;${escapeHtml(String(t))}&rdquo;`).join(", "));
    }
    if (entry.instruction) bits.push(`you asked: &ldquo;${escapeHtml(entry.instruction)}&rdquo;`);
    if (entry.reason) bits.push(escapeHtml(entry.reason));
    if (entry.suggested_type && entry.suggested_type !== s.suggested_relationship_type) {
      bits.push(`suggested <code>${escapeHtml(entry.suggested_type)}</code>`);
    }
    if (entry.model) bits.push(`model: ${escapeHtml(entry.model)}`);
    return `<li><strong>${escapeHtml(signalLabel(entry.signal))}</strong>${bits.length ? " &mdash; " + bits.join("; ") : ""}</li>`;
  }).join("");
  if (!headline && !chips && !details) return "";
  return `
    <div class="evidence">
      ${headline}
      ${chips ? `<div class="signal-chips">${chips}</div>` : ""}
      ${details ? `<details class="evidence-detail"><summary>Why</summary><ul>${details}</ul></details>` : ""}
    </div>`;
}

/* A suggestion that already knows which two records it means — everything from
 * the signal pass and the assistant. There is nothing for the reviewer to
 * identify, so the card is a decision rather than a form: the two names link
 * to the records, the type is editable because a rule's guess at the type is
 * the weakest thing about it, and Accept posts with no body. */
function resolvedRelationshipCardHtml(s) {
  const typeOptions = SUGGESTED_RELATIONSHIP_TYPES.map((t) =>
    `<option value="${escapeHtml(t)}"${t === s.suggested_relationship_type ? " selected" : ""}>${escapeHtml(t)}</option>`).join("");
  return `
    <div class="queue-card resolved" data-suggestion-id="${s.id}">
      <div class="queue-card-head">
        <span>
          ${sourcePillHtml(s.source)}
          <a href="#" class="entity-link" data-open-entity="${escapeHtml(s.suggested_from_entity_id)}"><strong>${escapeHtml(s.suggested_from_name)}</strong></a>
          <span class="rel-arrow">→</span>
          <a href="#" class="entity-link" data-open-entity="${escapeHtml(s.suggested_to_entity_id)}"><strong>${escapeHtml(s.suggested_to_name)}</strong></a>
        </span>
        <span class="queue-score">${s.confidence == null ? "" : Math.round(s.confidence * 100) + "% confidence"}</span>
      </div>
      ${evidenceHtml(s)}
      ${s.status === "pending" ? `
        <div class="queue-actions resolved-actions">
          <label class="inline-label" for="sugg-type-${s.id}">as</label>
          <select id="sugg-type-${s.id}" class="rel-type-select">${typeOptions}</select>
          <button class="btn-primary btn-sm" data-accept-resolved="${s.id}">Accept</button>
          <button class="btn-danger btn-sm" data-reject="${s.id}">Dismiss</button>
        </div>
      ` : `<div class="card-meta">${escapeHtml(s.status)}</div>`}
    </div>
  `;
}

/* ---------------------------------------------------------------------------
 * One end of a relationship pulled out of a document
 *
 * The model only ever saw text, so it gives two NAMES and no records. Before
 * this, every such card was two empty search boxes, and a name the case file
 * had never heard of meant leaving the queue, creating the record by hand, and
 * coming back — for something the app already knew the name and probable type
 * of. The server now says which existing records each name could be, so there
 * are three cases and each gets the smallest control that settles it:
 *
 *   one match     already decided — show it, offer to change it
 *   several       a search box, with the matches offered first
 *   none          offer to create it, with the type the relationship implies
 * ------------------------------------------------------------------------ */

const ENTITY_TYPES_FOR_CREATE = ["person", "organization", "location", "event",
                                 "source", "communication", "vehicle"];

function endpointHtml(s, side) {
  const name = side === "from" ? s.suggested_from_name : s.suggested_to_name;
  const candidates = (side === "from" ? s.from_candidates : s.to_candidates) || [];
  const hint = (side === "from" ? s.from_entity_type_hint : s.to_entity_type_hint) || "person";
  const idPrefix = `sugg-${side}-${s.id}`;
  const single = candidates.length === 1 ? candidates[0] : null;

  const typeOptions = ENTITY_TYPES_FOR_CREATE.map((t) =>
    `<option value="${t}"${t === hint ? " selected" : ""}>${t}</option>`).join("");

  return `
    <div class="endpoint" data-endpoint="${side}" data-suggestion="${s.id}">
      <div class="endpoint-head">
        <span class="endpoint-name">${escapeHtml(name || "(unnamed)")}</span>
        ${single
          ? `<span class="endpoint-matched" title="Already in the case file">matched
               <span class="type-pill type-${escapeHtml(single.entity_type)}">${escapeHtml(single.entity_type)}</span></span>`
          : candidates.length
            ? `<span class="endpoint-ambiguous">${candidates.length} entities share this name</span>`
            : `<span class="endpoint-missing">not in the case file</span>`}
        <button type="button" class="btn-link btn-sm endpoint-toggle" data-endpoint-toggle="${idPrefix}">
          ${single ? "Use a different entity" : candidates.length ? "Search instead" : "Use an existing entity"}
        </button>
      </div>

      ${single ? `<input type="hidden" id="${idPrefix}-id" value="${escapeHtml(single.id)}">` : ""}

      ${!single && candidates.length ? `
        <div class="endpoint-choices">
          ${candidates.map((c, i) => `
            <label class="endpoint-choice">
              <input type="radio" name="${idPrefix}-choice" value="${escapeHtml(c.id)}"${i === 0 ? " checked" : ""}>
              ${escapeHtml(c.name)} <span class="type-pill type-${escapeHtml(c.entity_type)}">${escapeHtml(c.entity_type)}</span>
            </label>`).join("")}
          <input type="hidden" id="${idPrefix}-id" value="${escapeHtml(candidates[0].id)}">
        </div>` : ""}

      ${!candidates.length ? `
        <div class="endpoint-create">
          <label class="inline-label" for="${idPrefix}-type">create as</label>
          <select id="${idPrefix}-type" class="rel-type-select">${typeOptions}</select>
          <span class="endpoint-create-note">a new entity will be created when you accept</span>
          <input type="hidden" id="${idPrefix}-id" value="">
        </div>` : ""}

      <div class="endpoint-picker" id="${idPrefix}-picker" hidden>
        ${entityPickerHtml("Search entities", idPrefix + "-search")}
      </div>
    </div>`;
}

/* The picker writes into its own hidden field; this mirrors that choice onto
 * the endpoint's field, so the accept handler has exactly one place to read
 * whichever of the three controls the reviewer ended up using. */
function wireEndpoint(s, side) {
  const idPrefix = `sugg-${side}-${s.id}`;
  const hidden = document.getElementById(`${idPrefix}-id`);

  document.querySelectorAll(`input[name="${idPrefix}-choice"]`).forEach((radio) => {
    radio.addEventListener("change", () => { if (hidden) hidden.value = radio.value; });
  });

  const toggle = document.querySelector(`[data-endpoint-toggle="${idPrefix}"]`);
  const picker = document.getElementById(`${idPrefix}-picker`);
  if (toggle && picker) {
    toggle.addEventListener("click", () => {
      picker.hidden = !picker.hidden;
      if (!picker.hidden) {
        wireEntityPicker(`${idPrefix}-search`);
        const searchHidden = document.getElementById(`${idPrefix}-search-id`);
        if (searchHidden) {
          // The picker has no change event of its own — it writes the id into
          // a hidden input on mousedown — so watch the value rather than
          // reaching into wireEntityPicker and changing how every other
          // picker in the app behaves.
          const seen = searchHidden.value;
          const poll = setInterval(() => {
            if (searchHidden.value && searchHidden.value !== seen) {
              if (hidden) hidden.value = searchHidden.value;
              markEndpointOverridden(idPrefix);
              clearInterval(poll);
            }
            if (!document.body.contains(searchHidden)) clearInterval(poll);
          }, 200);
        }
      }
    });
  }
}

function markEndpointOverridden(idPrefix) {
  const create = document.querySelector(`#${idPrefix}-type`);
  // Choosing an existing record cancels any pending creation — otherwise
  // accepting would make a duplicate of the record just chosen.
  if (create) create.closest(".endpoint-create").hidden = true;
  const note = document.querySelector(`[data-endpoint-toggle="${idPrefix}"]`);
  if (note) note.textContent = "Record chosen";
}

/* What the accept call should say about one end: an id if one is settled, or
 * an instruction to create it. */
function endpointPayload(s, side) {
  const idPrefix = `sugg-${side}-${s.id}`;
  const hidden = document.getElementById(`${idPrefix}-id`);
  const chosen = hidden ? hidden.value : "";
  if (chosen) return { id: chosen };
  const typeSelect = document.getElementById(`${idPrefix}-type`);
  const createBox = typeSelect ? typeSelect.closest(".endpoint-create") : null;
  if (typeSelect && createBox && !createBox.hidden) {
    return { create: { entity_type: typeSelect.value } };
  }
  return {};
}

/* Accepting a relationship that came out of a document. Both ends have to be
 * settled — chosen or being created — and the error says which one is not,
 * because "pick both entities first" on a card where one is already matched is
 * a confusing thing to be told. */
async function acceptDocumentRelationship(s, reload) {
  const from = endpointPayload(s, "from");
  const to = endpointPayload(s, "to");
  const unsettled = [];
  if (!from.id && !from.create) unsettled.push(s.suggested_from_name);
  if (!to.id && !to.create) unsettled.push(s.suggested_to_name);
  if (unsettled.length) {
    showToast(`Say which entity ${unsettled.join(" and ")} ${unsettled.length > 1 ? "are" : "is"}, or create it`, true);
    return;
  }
  const body = {};
  if (from.id) body.from_entity_id = from.id; else body.create_from = from.create;
  if (to.id) body.to_entity_id = to.id; else body.create_to = to.create;
  try {
    const res = await api(`/api/extraction-suggestions/${s.id}/accept`,
                          { method: "POST", body });
    const made = (res.created_entities || []).map((c) => c.name).filter(Boolean);
    showToast(made.length
      ? `Accepted — created ${made.join(" and ")}`
      : "Accepted");
    reload();
    refreshQueueBadges();
  } catch (err) { showToast(err.message, true); }
}

function extractionCardHtml(s) {
  if (s.suggestion_type === "relationship" && s.suggested_from_entity_id && s.suggested_to_entity_id) {
    return resolvedRelationshipCardHtml(s);
  }
  if (s.suggestion_type === "entity") {
    const details = s.details || {};
    const detailLines = Object.entries(details).map(([k, v]) => `<li>${escapeHtml(k)}: ${escapeHtml(Array.isArray(v) ? v.join(", ") : String(v))}</li>`).join("");
    // The type is editable before accepting. A model reading a CV routinely
    // calls an employer a person, and being made to accept the wrong type and
    // then fix the record afterwards is worse than being asked once, here.
    const typeOptions = ENTITY_TYPES_FOR_CREATE.map((t) =>
      `<option value="${t}"${t === s.suggested_entity_type ? " selected" : ""}>${t}</option>`).join("");
    return `
      <div class="queue-card" data-suggestion-id="${s.id}">
        <div class="queue-card-head">
          <span><span class="type-pill type-${s.suggested_entity_type}">${escapeHtml(s.suggested_entity_type)}</span> <strong>${escapeHtml(s.suggested_name)}</strong></span>
          <span class="queue-score">${Math.round((s.confidence || 0) * 100)}% confidence</span>
        </div>
        ${detailLines ? `<ul class="queue-detail-list">${detailLines}</ul>` : ""}
        ${s.looks_like ? `<div class="suggestion-warning">
            This is ${escapeHtml(s.looks_like)}, not a name. Dismiss it, or rename it if you know whose it is.
          </div>` : ""}
        ${s.status === "pending"
          ? `<div class="queue-actions">
               <label class="inline-label" for="sugg-type-entity-${s.id}">as</label>
               <select id="sugg-type-entity-${s.id}" class="rel-type-select">${typeOptions}</select>
               <button class="btn-primary btn-sm" data-accept="${s.id}">Accept</button>
               <button class="btn-danger btn-sm" data-reject="${s.id}">Reject</button>
             </div>`
          : `<div class="card-meta">${escapeHtml(s.status)}</div>`}
      </div>
    `;
  }
  return `
    <div class="queue-card" data-suggestion-id="${s.id}">
      <div class="queue-card-head">
        <span><strong>${escapeHtml(s.suggested_from_name)}</strong> &mdash; ${escapeHtml(s.suggested_relationship_type)} → <strong>${escapeHtml(s.suggested_to_name)}</strong></span>
        <span class="queue-score">${Math.round((s.confidence || 0) * 100)}% confidence</span>
      </div>
      ${s.status === "pending" ? `
        <div id="rel-accept-${s.id}">
          ${endpointHtml(s, "from")}
          ${endpointHtml(s, "to")}
          <div class="queue-actions">
            <button class="btn-primary btn-sm" data-accept-rel="${s.id}">Accept</button>
            <button class="btn-danger btn-sm" data-reject="${s.id}">Reject</button>
          </div>
        </div>
      ` : `<div class="card-meta">${escapeHtml(s.status)}</div>`}
    </div>
  `;
}

function wireExtractionCard(s) {
  document.querySelectorAll(`[data-suggestion-id="${s.id}"] .entity-link`).forEach((a) => {
    a.addEventListener("click", (e) => { e.preventDefault(); openEntityDetail(a.dataset.openEntity); });
  });
  if (s.status !== "pending") return;

  const resolvedBtn = document.querySelector(`[data-accept-resolved="${s.id}"]`);
  if (resolvedBtn) {
    resolvedBtn.addEventListener("click", async () => {
      // No entity ids in the body: the suggestion already stores both, and
      // re-sending them from the page would only create a way for them to
      // disagree. The type is sent because the reviewer can change it.
      const sel = document.getElementById(`sugg-type-${s.id}`);
      try {
        await api(`/api/extraction-suggestions/${s.id}/accept`, {
          method: "POST", body: { relationship_type: sel ? sel.value : undefined },
        });
        showToast("Accepted — relationship added");
        loadExtractionQueue();
        refreshQueueBadges();
      } catch (err) { showToast(err.message, true); }
    });
  }

  if (s.suggestion_type === "entity") {
    const btn = document.querySelector(`[data-accept="${s.id}"]`);
    if (btn) btn.addEventListener("click", async () => {
      const sel = document.getElementById(`sugg-type-entity-${s.id}`);
      const chosen = sel ? sel.value : null;
      try {
        await api(`/api/extraction-suggestions/${s.id}/accept`, {
          method: "POST",
          // Details the model inferred are dropped when the reviewer changes
          // the type: an "occupation" makes no sense on an organization, and
          // the server would silently discard it anyway.
          body: chosen && chosen !== s.suggested_entity_type
            ? { entity_type: chosen, details: {} }
            : {},
        });
        showToast("Accepted");
        loadExtractionQueue();
        refreshQueueBadges();
      } catch (err) { showToast(err.message, true); }
    });
  } else if (!resolvedBtn) {
    // Only the document-extraction shape has endpoints to settle — a resolved
    // card knows its two records and renders none.
    wireEndpoint(s, "from");
    wireEndpoint(s, "to");
    const btn = document.querySelector(`[data-accept-rel="${s.id}"]`);
    if (btn) btn.addEventListener("click", () => acceptDocumentRelationship(s, loadExtractionQueue));
  }
  const rejectBtn = document.querySelector(`[data-reject="${s.id}"]`);
  if (rejectBtn) rejectBtn.addEventListener("click", async () => {
    try {
      await api(`/api/extraction-suggestions/${s.id}/reject`, { method: "POST" });
      showToast("Rejected");
      loadExtractionQueue();
      refreshQueueBadges();
    } catch (err) { showToast(err.message, true); }
  });
}

function renderExtractionQueue(items) {
  const el = document.getElementById("extraction-list");
  if (!items.length) {
    // An empty queue filtered to one source usually means that source hasn't
    // run, which is a different problem from "nothing was found" — so say which.
    const source = document.getElementById("extraction-source-filter").value;
    const empty = {
      extraction: "Nothing from documents yet.",
      signal: "No correlation signals. (Off unless LINK_SIGNALS_ENABLED is set.)",
      assistant: "The assistant hasn't proposed anything. Ask it to, above.",
    }[source] || "Nothing here.";
    el.innerHTML = `<div class="empty-state">${escapeHtml(empty)}</div>`;
    return;
  }
  el.innerHTML = items.map(extractionCardHtml).join("");
  items.forEach(wireExtractionCard);
}

/* Ask the assistant to propose links between records that already exist. The
 * only thing this button can do is add rows to the queue below it. */
(function wireProposeBox() {
  const btn = document.getElementById("propose-btn");
  const input = document.getElementById("propose-instruction");
  const out = document.getElementById("propose-result");
  if (!btn || !input || !out) return;

  async function propose() {
    const instruction = input.value.trim();
    if (instruction.length < 3) {
      out.hidden = false;
      out.className = "propose-result warn";
      out.textContent = "Say what to look at — a family name, an organisation, a place.";
      return;
    }
    btn.disabled = true;
    const previous = btn.textContent;
    btn.textContent = "Thinking…";
    out.hidden = false;
    out.className = "propose-result";
    out.textContent = "Asking the assistant…";
    try {
      const res = await api("/api/suggestions/propose", { method: "POST", body: { instruction } });
      out.className = "propose-result ok";
      out.textContent = res.message || "Done.";
      if (res.created || res.annotated) {
        // Show what it produced without making them find the filter.
        document.getElementById("extraction-status-filter").value = "pending";
        loadExtractionQueue();
        refreshQueueBadges();
      }
    } catch (err) {
      out.className = "propose-result warn";
      out.textContent = err.message;
    } finally {
      btn.disabled = false;
      btn.textContent = previous;
    }
  }

  btn.addEventListener("click", propose);
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); propose(); } });
})();

/* ============================================================================
 * Correlation review queue
 * ========================================================================== */

document.getElementById("correlation-status-filter").addEventListener("change", () => {
  correlationPicked.clear();
  loadCorrelationQueue();
});
document.getElementById("correlation-score-filter").addEventListener("change", () => {
  correlationPicked.clear();
  loadCorrelationQueue();
});

/* Which suggestions are ticked, and what the current filters match in total.
 *
 * The total matters as much as the selection: the queue shows at most 200, and
 * the whole point of "select everything matching" is the case where there are
 * two thousand. The bar has to be able to say which number it is about. */
const correlationPicked = new Set();
let correlationFilterTotal = 0;

function correlationScoreBand() {
  const raw = document.getElementById("correlation-score-filter").value;
  if (!raw) return {};
  const [low, high] = raw.split("-").map(Number);
  // Sent as fractions, which is what the API stores; the dropdown talks in
  // percentages because that is what the cards show.
  return { min_score: low / 100, max_score: high / 100 };
}

async function loadCorrelationQueue() {
  const status = document.getElementById("correlation-status-filter").value;
  const band = correlationScoreBand();
  const params = new URLSearchParams({ status, limit: "200" });
  if (band.min_score !== undefined) params.set("min_score", String(band.min_score));
  if (band.max_score !== undefined) params.set("max_score", String(band.max_score));
  try {
    const data = await api("/api/correlation-suggestions?" + params.toString());
    correlationFilterTotal = data.total;
    // Anything ticked that is no longer on screen is dropped: acting on a row
    // the analyst can no longer see is exactly the surprise to avoid.
    const visible = new Set(data.items.map((x) => x.id));
    [...correlationPicked].forEach((id) => { if (!visible.has(id)) correlationPicked.delete(id); });
    renderCorrelationQueue(data.items);
    renderCorrelationBulkBar(data.items.length);
  } catch (err) {
    showToast("Failed to load queue: " + err.message, true);
  }
}

function renderCorrelationBulkBar(shown) {
  const bar = document.getElementById("correlation-bulk-bar");
  if (!bar) return;
  const picked = correlationPicked.size;
  const pending = document.getElementById("correlation-status-filter").value === "pending";
  bar.hidden = !picked || !pending;
  if (bar.hidden) return;

  document.getElementById("correlation-bulk-count").textContent =
    `${picked} selected`;
  const all = document.getElementById("correlation-bulk-all");
  const parts = [];
  // Ticking 150 boxes to clear a filtered band is not review, it is typing.
  if (picked < shown) {
    parts.push(`<button type="button" class="btn-link btn-sm" id="correlation-select-shown">Select
      all ${shown.toLocaleString()} shown</button>`);
  }
  // The whole-filter action appears only when the filter matches more than
  // fits on screen -- that is the situation it exists for, and offering it
  // otherwise invites someone to reach for it when ticking four rows would
  // have done.
  if (correlationFilterTotal > shown) {
    parts.push(`These filters match ${correlationFilterTotal.toLocaleString()} in total.
      <button type="button" class="btn-link btn-sm" id="correlation-select-all">Act on all
      ${correlationFilterTotal.toLocaleString()} instead</button>`);
  }
  all.innerHTML = parts.join(" · ");

  const shownBtn = document.getElementById("correlation-select-shown");
  if (shownBtn) {
    shownBtn.addEventListener("click", () => {
      document.querySelectorAll("[data-pick-correlation]").forEach((box) => {
        box.checked = true;
        correlationPicked.add(Number(box.dataset.pickCorrelation));
      });
      renderCorrelationBulkBar(shown);
    });
  }
  const allBtn = document.getElementById("correlation-select-all");
  if (allBtn) allBtn.addEventListener("click", () => bulkResolveCorrelations(null, true));
}

async function bulkResolveCorrelations(action, chooseAll) {
  if (chooseAll) {
    // Two steps on purpose: pick the verb, then confirm the number. A single
    // click that resolves two thousand records is not something to offer.
    const verb = confirm(
      `Act on all ${correlationFilterTotal.toLocaleString()} suggestions matching these filters?\n\n`
      + "OK = mark them all NOT A MATCH (dismiss)\n"
      + "Cancel = go back and use the buttons for a smaller selection")
      ? "dismissed" : null;
    if (!verb) return;
    if (!confirm(`Dismiss ${correlationFilterTotal.toLocaleString()} suggestions?\n\n`
               + "They move to Dismissed. Nothing in the case changes.")) return;
    action = verb;
  }

  const body = { action };
  if (chooseAll) {
    Object.assign(body, { all_matching: true, expected_count: correlationFilterTotal },
                  correlationScoreBand());
  } else {
    body.ids = [...correlationPicked];
  }
  try {
    const result = await api("/api/correlation-suggestions/bulk", { method: "POST", body });
    correlationPicked.clear();
    showToast(`${result.resolved.toLocaleString()} ${action === "dismissed" ? "dismissed" : "confirmed"}`
              + (result.skipped_not_pending ? ` · ${result.skipped_not_pending} were already decided` : ""));
    loadCorrelationQueue();
    refreshQueueBadges();
  } catch (err) {
    showToast(err.message, true);
  }
}

function wireCorrelationBulk() {
  const on = (id, fn) => {
    const el = document.getElementById(id);
    if (el) el.addEventListener("click", fn);
  };
  on("correlation-bulk-dismiss", () => bulkResolveCorrelations("dismissed"));
  on("correlation-bulk-confirm", () => bulkResolveCorrelations("confirmed"));
  on("correlation-bulk-clear", () => {
    correlationPicked.clear();
    loadCorrelationQueue();
  });
}
wireCorrelationBulk();

// subject_type "report_event" is a fixed-role cross-type pair (see
// db/init.sql / worker/correlate.py): subject_a is always a report,
// subject_b is always an Event entity — never sorted like the same-type
// pairs, so each side needs its own "kind" for the open-detail link and a
// friendlier label than the raw subject_type string.
function subjectKinds(subjectType) {
  return subjectType === "report_event" ? ["report", "entity"] : [subjectType, subjectType];
}
function subjectTypeLabel(subjectType) {
  return subjectType === "report_event" ? "report ↔ event" : subjectType;
}

/* ============================================================================
 * Merging duplicate records
 *
 * Extraction produces duplicates — the same person named four ways across six
 * documents, one address read as a street, a town and a postcode. Confirming a
 * correlation match used to record an opinion and leave the work; this does
 * the work.
 *
 * The dialog is a preview rather than a form: you choose which record survives
 * and, if you like, what it should be called, and it tells you what will move
 * and what blanks will be filled. Everything else is decided by one rule — the
 * survivor wins, its blanks are filled from the others — because with five
 * copies of one record, a field-by-field negotiation is a lot of clicking to
 * reach an obvious answer.
 * ========================================================================== */

let mergeCandidates = [];      // [{id, name, entity_type}]
let mergeSurvivorId = null;
let mergeTypeChange = false;       // the selection spans more than one kind
let mergeTypeAcknowledged = false; // and you have said you meant to
let mergeKeepType = null;          // the kind the merged record will be

async function openMergeDialog(ids, onDone) {
  if (!ids || ids.length < 2) {
    showToast("Pick at least two entities to merge", true);
    return;
  }
  let records;
  try {
    records = await Promise.all(ids.map((id) => api(`/api/entities/${id}`)));
  } catch (err) { showToast(err.message, true); return; }

  const already = records.find((r) => r.merged_into);
  if (already) {
    showToast(`"${already.name}" has already been merged into another entity`, true);
    return;
  }

  mergeCandidates = records;
  // Mixed kinds used to be refused outright, with a toast, before the dialog
  // ever opened. That was right about the risk and wrong about the remedy:
  // extraction really does file one company as both a Person and an
  // Organization, and being told "no" with no way forward just means doing it
  // by hand. So the dialog opens, and the crossing is something you turn on
  // deliberately, having been told what it costs.
  mergeTypeChange = new Set(records.map((r) => r.entity_type)).size > 1;
  mergeTypeAcknowledged = false;
  mergeKeepType = bestMergeType(records);
  mergeSurvivorId = defaultSurvivorId(records, mergeTypeChange ? mergeKeepType : null);
  renderMergeDialog(onDone);
}

/* A detail column's own label, so the merge dialog can say "date of birth"
 * rather than "date_of_birth". The edit form already knows these; this is the
 * same list read the other way round. Anything not in it (a column the form
 * does not expose) falls back to the column name with its underscores
 * knocked out, which is still better than raw SQL. */
function fieldLabel(entityType, key) {
  const field = (DETAIL_FIELDS[entityType] || []).find((f) => f.key === key);
  if (!field) return key.replace(/_/g, " ");
  // "Aliases (comma-separated)" is a label for an input, not for prose.
  return field.label.replace(/\s*\(.*\)\s*$/, "").toLowerCase();
}

// "a person", "an organization". Only ever applied to the entity types.
function withArticle(word) {
  return ("aeiou".includes((word || "")[0]) ? "an " : "a ") + word;
}

/* Which kind to suggest keeping: the one whose records carry the most between
 * them, then the one with the most records, then alphabetical so the
 * suggestion does not wander between two equal answers. */
function bestMergeType(records) {
  const byType = new Map();
  for (const r of records) {
    const t = byType.get(r.entity_type) || { weight: 0, count: 0, type: r.entity_type };
    t.weight += mergeWeight(r);
    t.count += 1;
    byType.set(r.entity_type, t);
  }
  return [...byType.values()].sort((a, b) =>
    b.weight - a.weight || b.count - a.count || a.type.localeCompare(b.type))[0].type;
}

// Default survivor: the one carrying the most, since it is the one whose
// content the merge preserves outright. Confined to one kind when a kind has
// been chosen — the survivor is what decides the merged record's kind, so the
// two choices are the same choice.
function defaultSurvivorId(records, keepType) {
  const pool = keepType ? records.filter((r) => r.entity_type === keepType) : records;
  return [...pool].sort((a, b) => mergeWeight(b) - mergeWeight(a))[0].id;
}

/* How much a record is carrying. Only used to pick the default survivor, and
 * relationships count double because they are the expensive thing to rebuild
 * by hand if you choose wrong. */
function mergeWeight(r) {
  return (r.relationships || []).length * 2
    + (r.attachments || []).length
    + (r.contacts || []).length
    + Object.values(r.details || {}).filter((v) =>
        v !== null && v !== "" && !(Array.isArray(v) && !v.length)).length
    + (r.description ? 1 : 0);
}

function renderMergeDialog(onDone) {
  const survivor = mergeCandidates.find((r) => r.id === mergeSurvivorId);
  const losers = mergeCandidates.filter((r) => r.id !== mergeSurvivorId);

  // While kinds are crossed but not yet acknowledged, nothing is survivable
  // and nothing is chosen: the first decision is which kind this is going to
  // be, and offering a survivor before that is offering the second question
  // first.
  const locked = mergeTypeChange && !mergeTypeAcknowledged;

  const rows = mergeCandidates.map((r) => {
    const chosen = !locked && r.id === mergeSurvivorId;
    const wrongType = mergeTypeChange && mergeTypeAcknowledged && r.entity_type !== mergeKeepType;
    const role = locked ? ""
      : chosen ? "keeps its own values"
      : wrongType ? `folded in — loses its ${escapeHtml(r.entity_type)} fields`
      : "folded in";
    return `
      <label class="merge-option ${chosen ? "chosen" : ""} ${wrongType ? "recast" : ""}">
        <input type="radio" name="merge-survivor" value="${escapeHtml(r.id)}"
               ${chosen ? " checked" : ""}${locked || wrongType ? " disabled" : ""}>
        <span class="merge-option-body">
          <strong>${escapeHtml(r.name)}</strong>
          <span class="merge-option-meta">
            <span class="type-pill type-${r.entity_type}">${escapeHtml(r.entity_type)}</span>
            ${(r.relationships || []).length} link(s) ·
            ${(r.attachments || []).length} attachment(s) ·
            ${(r.contacts || []).length} contact(s)
            ${r.description ? " · has a description" : ""}
          </span>
        </span>
        <span class="merge-option-role">${role}</span>
      </label>`;
  }).join("");

  const typeCounts = new Map();
  for (const r of mergeCandidates) {
    typeCounts.set(r.entity_type, (typeCounts.get(r.entity_type) || 0) + 1);
  }
  const typeBlock = !mergeTypeChange ? "" : `
    <div class="merge-typecast">
      <label class="inline-check">
        <input type="checkbox" id="merge-allow-type"${mergeTypeAcknowledged ? " checked" : ""}>
        <span>These are <strong>${typeCounts.size} different types</strong> of entity (${[...typeCounts].map(([t, n]) => `${n} ${escapeHtml(t)}`).join(", ")}) —
          merge them anyway</span>
      </label>
      <p class="field-hint">For when the same thing was filed twice under different types.</p>
      ${!mergeTypeAcknowledged ? "" : `
        <div class="merge-typepick">
          <span class="merge-typepick-label">Keep it as</span>
          ${[...typeCounts.keys()].sort().map((t) => `
            <label class="merge-type-option ${t === mergeKeepType ? "chosen" : ""}">
              <input type="radio" name="merge-keep-type" value="${escapeHtml(t)}"
                     ${t === mergeKeepType ? " checked" : ""}>
              <span class="type-pill type-${t}">${escapeHtml(t)}</span>
              <span class="merge-option-meta">${typeCounts.get(t)} record(s)</span>
            </label>`).join("")}
        </div>`}
    </div>`;

  const html = `
    <h2>Merge ${mergeCandidates.length} entities into one</h2>
    <p class="view-hint">The one you keep wins any field it has filled in. Everything linked moves to it, and the rest are archived.</p>

    ${typeBlock}

    <div class="merge-options">${rows}</div>

    ${locked ? "" : `
      <div class="form-row">
        <label for="merge-name">Name of the merged entity</label>
        <input type="text" id="merge-name" maxlength="300" value="${escapeHtml(survivor.name)}">
        <p class="field-hint">Old names are kept as aliases on people.</p>
      </div>

      <div id="merge-preview" class="merge-preview">Working out what will move…</div>`}

    <div class="queue-actions">
      <button class="btn-primary" id="merge-confirm"${locked ? " disabled" : ""}>
        ${locked ? "Choose a kind first" : `Merge ${losers.length} into this one`}</button>
      <button class="btn-secondary" id="merge-cancel">Cancel</button>
    </div>`;

  openModal(html);

  document.querySelectorAll('input[name="merge-survivor"]').forEach((radio) => {
    radio.addEventListener("change", () => {
      mergeSurvivorId = radio.value;
      renderMergeDialog(onDone);
    });
  });
  const allowType = document.getElementById("merge-allow-type");
  if (allowType) {
    allowType.addEventListener("change", () => {
      mergeTypeAcknowledged = allowType.checked;
      // Picking the kind picks the survivor, so the survivor has to be
      // re-chosen whenever the kind changes -- otherwise the merge would keep
      // a record of the kind you just said you did not want.
      mergeSurvivorId = defaultSurvivorId(mergeCandidates, mergeKeepType);
      renderMergeDialog(onDone);
    });
  }
  document.querySelectorAll('input[name="merge-keep-type"]').forEach((radio) => {
    radio.addEventListener("change", () => {
      mergeKeepType = radio.value;
      mergeSurvivorId = defaultSurvivorId(mergeCandidates, mergeKeepType);
      renderMergeDialog(onDone);
    });
  });
  document.getElementById("merge-cancel").addEventListener("click", closeModal);
  document.getElementById("merge-confirm").addEventListener("click", () => runMerge(onDone));
  if (!locked) loadMergePreview();
}

/* One preview call per record being folded in. Counts only — the question at
 * this point is "am I about to lose something", and a number answers it. */
async function loadMergePreview() {
  const el = document.getElementById("merge-preview");
  const losers = mergeCandidates.filter((r) => r.id !== mergeSurvivorId);
  const cross = mergeTypeChange && mergeTypeAcknowledged ? "&allow_type_change=true" : "";
  const survivorType = (mergeCandidates.find((r) => r.id === mergeSurvivorId) || {}).entity_type;
  try {
    const previews = await Promise.all(losers.map((r) =>
      api(`/api/entities/${r.id}/merge-preview?into=`
          + encodeURIComponent(mergeSurvivorId) + cross)));
    const total = (key) => previews.reduce((n, p) => n + (p.moves[key] || 0), 0);
    const fills = {};
    previews.forEach((p) => Object.entries(p.fields_filled || {}).forEach(([k, v]) => {
      if (!(k in fills)) fills[k] = v;
    }));
    const dropped = total("self_edges_dropped");
    // Named, not counted. "Three fields will be lost" is not a decision
    // anybody can make; "date of birth, aliases and physical description will
    // be lost" is.
    const lost = previews
      .filter((p) => p.type_change && Object.keys(p.dropped_details || {}).length)
      .map((p) => `<li class="merge-warn"><strong>${escapeHtml(p.loser.name)}</strong>
             (${escapeHtml(p.loser.entity_type)}) loses
             <strong>${Object.keys(p.dropped_details)
               .map((k) => escapeHtml(fieldLabel(p.loser.entity_type, k))).join(", ")}</strong> —
             ${escapeHtml(withArticle(p.survivor.entity_type))} has nowhere to keep
             ${Object.keys(p.dropped_details).length === 1 ? "it" : "them"}</li>`)
      .join("");
    el.innerHTML = `
      <ul class="merge-preview-list">
        <li><strong>${total("relationships")}</strong> relationship(s) move across</li>
        <li><strong>${total("reports")}</strong> report link(s)</li>
        <li><strong>${total("attachments")}</strong> attachment(s)</li>
        <li><strong>${total("contacts")}</strong> contact detail(s)</li>
        ${Object.keys(fills).length
          ? `<li>fills in <strong>${Object.keys(fills)
              .map((k) => escapeHtml(fieldLabel(survivorType, k))).join(", ")}</strong></li>`
          : "<li>no blank fields to fill</li>"}
        ${dropped
          ? `<li class="merge-warn">${dropped} relationship(s) <em>between</em> these entities will be dropped</li>` : ""}
        ${lost}
      </ul>`;
  } catch (err) {
    el.innerHTML = `<span class="merge-warn">${escapeHtml(err.message)}</span>`;
  }
}

async function runMerge(onDone) {
  const btn = document.getElementById("merge-confirm");
  btn.disabled = true;
  const name = document.getElementById("merge-name").value.trim();
  const losers = mergeCandidates.filter((r) => r.id !== mergeSurvivorId).map((r) => r.id);
  try {
    const res = await api("/api/entities/merge", {
      method: "POST",
      body: {
        survivor_id: mergeSurvivorId,
        merge_ids: losers,
        survivor_name: name || undefined,
        allow_type_change: mergeTypeChange && mergeTypeAcknowledged,
      },
    });
    closeModal();
    const moved = res.moved || {};
    const recast = Object.keys(res.dropped_details || {}).length;
    showToast(`Merged ${res.merged.length} into "${res.survivor_name}" — `
      + `${moved.relationships || 0} link(s), ${moved.reports || 0} report(s) moved`
      + (recast ? `, kept as ${withArticle(res.survivor_type)}` : ""));
    if (typeof onDone === "function") onDone(res);
  } catch (err) {
    btn.disabled = false;
    showToast(err.message, true);
  }
}

function renderCorrelationQueue(items) {
  const el = document.getElementById("correlation-list");
  if (!items.length) { el.innerHTML = '<div class="empty-state">Nothing here.</div>'; return; }
  el.innerHTML = items.map((s) => {
    const [aKind, bKind] = subjectKinds(s.subject_type);
    return `
    <div class="queue-card">
      <div class="queue-card-head">
        <span>
          ${s.status === "pending"
            ? `<input type="checkbox" class="queue-pick" data-pick-correlation="${s.id}"
                      ${correlationPicked.has(s.id) ? "checked" : ""}
                      aria-label="Select this suggestion">` : ""}
          <a href="#" data-open="${aKind}:${escapeHtml(s.subject_a_id)}">${escapeHtml(s.subject_a_label || s.subject_a_id)}</a>
          &harr;
          <a href="#" data-open="${bKind}:${escapeHtml(s.subject_b_id)}">${escapeHtml(s.subject_b_label || s.subject_b_id)}</a>
          <span class="type-pill">${escapeHtml(subjectTypeLabel(s.subject_type))}</span>
        </span>
        <span class="queue-score">${Math.round(s.similarity_score * 100)}% similar</span>
      </div>
      ${s.status === "pending"
        ? `<div class="queue-actions">
             ${s.subject_type === "entity"
               // Confirming records an opinion; merging acts on it. Both are
               // offered because they are genuinely different answers: two
               // records can be the same subject and still be worth keeping
               // apart, and confirming has always been the way to say so.
               ? `<button class="btn-primary btn-sm" data-merge-pair="${escapeHtml(s.subject_a_id)}|${escapeHtml(s.subject_b_id)}">Merge into one</button>` : ""}
             <button class="btn-secondary btn-sm" data-confirm="${s.id}">Same, but keep both</button>
             <button class="btn-danger btn-sm" data-dismiss="${s.id}">Not a match</button>
           </div>`
        : `<div class="card-meta">${escapeHtml(s.status)}</div>`}
    </div>
  `;
  }).join("");

  el.querySelectorAll("[data-pick-correlation]").forEach((box) => {
    box.addEventListener("change", () => {
      const id = Number(box.dataset.pickCorrelation);
      if (box.checked) correlationPicked.add(id); else correlationPicked.delete(id);
      renderCorrelationBulkBar(items.length);
    });
  });
  el.querySelectorAll("[data-open]").forEach((a) => {
    a.addEventListener("click", (e) => {
      e.preventDefault();
      const [type, id] = a.dataset.open.split(":");
      if (type === "entity") openEntityDetail(id); else openReportDetail(id);
    });
  });
  el.querySelectorAll("[data-merge-pair]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const ids = btn.dataset.mergePair.split("|");
      openMergeDialog(ids, () => { loadCorrelationQueue(); refreshQueueBadges(); });
    });
  });
  el.querySelectorAll("[data-confirm]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      try {
        await api(`/api/correlation-suggestions/${btn.dataset.confirm}/confirm`, { method: "POST" });
        showToast("Confirmed");
        loadCorrelationQueue();
        refreshQueueBadges();
      } catch (err) { showToast(err.message, true); }
    });
  });
  el.querySelectorAll("[data-dismiss]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      try {
        await api(`/api/correlation-suggestions/${btn.dataset.dismiss}/dismiss`, { method: "POST" });
        showToast("Dismissed");
        loadCorrelationQueue();
        refreshQueueBadges();
      } catch (err) { showToast(err.message, true); }
    });
  });
}

/* ============================================================================
 * Admin settings — model activity
 *
 * Three questions, in the order people ask them: is it working, what is it
 * being spent on, is this box keeping up. The headline is the failure rate
 * rather than a token count, because a model that has quietly started timing
 * out on every extraction looks exactly like an empty queue from the outside
 * and that is the failure worth catching.
 * ========================================================================== */

function fmtMs(ms) {
  if (ms === null || ms === undefined) return "—";
  if (ms < 1000) return `${ms} ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)} s`;
  return `${Math.round(ms / 60000)} min`;
}

function fmtCount(n) {
  if (n === null || n === undefined) return "—";
  if (n >= 1000000) return `${(n / 1000000).toFixed(1)}M`;
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
  return String(n);
}

async function loadOllamaUsage() {
  const el = document.getElementById("ollama-usage-panel");
  if (!el) return;
  const days = document.getElementById("ollama-usage-days").value || "7";
  el.innerHTML = '<p class="empty-state">Loading…</p>';
  let d;
  try {
    d = await api(`/api/admin/ollama-usage?days=${encodeURIComponent(days)}`);
  } catch (err) {
    el.innerHTML = `<p class="empty-state">Couldn't load model activity: ${escapeHtml(err.message)}</p>`;
    return;
  }

  const s = d.summary;
  if (!s.calls) {
    el.innerHTML = `<p class="empty-state">No calls to Ollama in the last ${d.window_days} day(s).</p>`;
    return;
  }

  const failPct = s.failure_rate === null ? null : Math.round(s.failure_rate * 1000) / 10;
  // Amber rather than red below 10%: a self-hosted model on a small box drops
  // the occasional call, and a panel that shouts at 2% teaches people to stop
  // reading it.
  const healthClass = failPct === null ? "" : failPct >= 10 ? "usage-bad" : failPct > 0 ? "usage-warn" : "usage-ok";
  const health = failPct === null ? "—"
    : failPct === 0 ? "all calls succeeded" : `${failPct}% failed`;

  const tile = (label, value, note) => `
    <div class="usage-tile">
      <div class="usage-tile-label">${escapeHtml(label)}</div>
      <div class="usage-tile-value">${value}</div>
      ${note ? `<div class="usage-tile-note">${note}</div>` : ""}
    </div>`;

  const rowsFor = (list, keyLabel) => list.length ? `
    <div class="usage-table-wrap"><table class="usage-table">
      <thead><tr>
        <th>${escapeHtml(keyLabel)}</th><th>Calls</th><th>Problems</th>
        <th>Median time</th><th>Tokens/s</th><th>Tokens in</th><th>Tokens out</th>
      </tr></thead>
      <tbody>${list.map((r) => `
        <tr>
          <td>${escapeHtml(r.label || r.key)}</td>
          <td>${r.calls}</td>
          <td class="${r.problems ? "usage-bad" : ""}">${r.problems || "—"}</td>
          <td>${fmtMs(r.median_duration_ms)}</td>
          <td>${r.median_tokens_per_sec === null ? "—" : r.median_tokens_per_sec}</td>
          <td>${fmtCount(r.prompt_tokens)}</td>
          <td>${fmtCount(r.eval_tokens)}</td>
        </tr>`).join("")}</tbody>
    </table></div>` : '<p class="empty-state">Nothing in this window.</p>';

  const peak = Math.max(...d.daily.map((x) => x.calls), 1);
  const spark = `
    <div class="usage-spark" role="img"
         aria-label="Calls per day over the last ${d.window_days} days">
      ${d.daily.map((x) => `
        <span class="usage-spark-col" title="${escapeHtml(x.day)}: ${x.calls} call(s), ${x.problems} problem(s)">
          <span class="usage-spark-bar" style="height:${Math.round((x.calls / peak) * 100)}%"></span>
          ${x.problems ? `<span class="usage-spark-bad" style="height:${Math.round((x.problems / peak) * 100)}%"></span>` : ""}
        </span>`).join("")}
    </div>`;

  el.innerHTML = `
    <div class="usage-tiles">
      ${tile("Health", `<span class="${healthClass}">${escapeHtml(health)}</span>`,
             `${s.calls} call(s), ${s.failures + s.timeouts} problem(s)`)}
      ${tile("Typical call", fmtMs(s.median_duration_ms),
             `slowest 5% over ${fmtMs(s.p95_duration_ms)}`)}
      ${tile("Throughput",
             s.median_tokens_per_sec === null ? "—" : `${s.median_tokens_per_sec} tok/s`,
             "median across calls that reported it")}
      ${tile("Cold starts", String(s.cold_starts),
             s.cold_starts
               ? `model reload, typically ${fmtMs(s.median_load_ms)} `
               : "the model stayed loaded")}
      ${tile("Tokens in", fmtCount(s.prompt_tokens), "prompt")}
      ${tile("Tokens out", fmtCount(s.eval_tokens), "generated")}
    </div>

    <div class="usage-block">
      <div class="dash-card-title">Calls per day</div>
      ${spark}
      <p class="field-hint">Red marks failed calls. Kept for ${d.retention_days} days.</p>
    </div>

    <div class="usage-block">
      <div class="dash-card-title">What the model is spent on</div>
      ${rowsFor(d.by_operation, "Feature")}
      <p class="field-hint">Embeddings don't report tokens or timings, so those columns are blank.</p>
    </div>

    <div class="usage-block">
      <div class="dash-card-title">By model</div>
      ${rowsFor(d.by_model, "Model")}
      <p class="field-hint">Compare tokens/s to judge whether a smaller model would suit this hardware.</p>
    </div>

    <div class="usage-block">
      <div class="dash-card-title">By account</div>
      ${d.by_user.length ? `
        <div class="usage-table-wrap"><table class="usage-table">
          <thead><tr><th>Account</th><th>Calls</th><th>Model time</th><th>Tokens in</th><th>Tokens out</th></tr></thead>
          <tbody>${d.by_user.map((u) => `
            <tr>
              <td>${escapeHtml(u.username)}</td>
              <td>${u.calls}</td>
              <td>${fmtMs(u.total_duration_ms)}</td>
              <td>${fmtCount(u.prompt_tokens)}</td>
              <td>${fmtCount(u.eval_tokens)}</td>
            </tr>`).join("")}</tbody>
        </table></div>` : '<p class="empty-state">Nothing in this window.</p>'}
      <p class="field-hint">Extraction and correlation appear as "(background work)".</p>
    </div>

    ${d.recent_failures.length ? `
      <div class="usage-block">
        <div class="dash-card-title">Recent problems</div>
        <ul class="usage-failures">${d.recent_failures.map((f) => `
          <li>
            <span class="usage-failure-what">${escapeHtml(f.outcome)} · ${escapeHtml(f.operation)} · ${escapeHtml(f.model || "unknown model")}</span>
            <span class="dash-list-meta">${timeAgoHtml(f.occurred_at)} · ${escapeHtml(f.source)}</span>
            ${f.error ? `<div class="usage-failure-error mono">${escapeHtml(f.error)}</div>` : ""}
          </li>`).join("")}</ul>
      </div>` : ""}
  `;
}

document.getElementById("ollama-usage-days").addEventListener("change", loadOllamaUsage);

/* ============================================================================
 * Documents — the inbox
 *
 * A document here is an attachment with no parent record: something dropped in
 * before anyone knows what is in it. The worker OCRs it, extracts its text and
 * proposes entities from it exactly as it would for a file attached to a
 * report — the only difference is that nothing has been filed yet.
 *
 * The detail view puts the extracted text beside those proposals on purpose.
 * The question a reviewer actually has about a proposed name is "where does it
 * say that", and an answer that needs a second screen is an answer nobody
 * checks.
 * ========================================================================== */

const DOC_STATUS_LABELS = {
  pending: { label: "Waiting", hint: "In the worker's queue. Nothing has read it yet." },
  processing: { label: "Reading", hint: "Being OCR'd or text-extracted right now." },
  done: { label: "Read", hint: "Text extracted. Any proposals are below." },
  failed: { label: "Failed", hint: "Extraction failed — the reason is on the document." },
  skipped: { label: "Skipped", hint: "Nothing to extract from this file type." },
};

let documentListSeq = 0;      // guards against a slow response overwriting a newer one
let documentPollTimer = null;

function docTitle(d) {
  return d.title || d.filename || `Document ${d.id}`;
}

async function loadDocuments() {
  const q = document.getElementById("doc-search").value.trim();
  const status = document.getElementById("doc-status-filter").value;
  const archived = document.getElementById("doc-archived-filter").value;
  const seq = ++documentListSeq;
  try {
    const data = await api(`/api/documents?status=${encodeURIComponent(status)}`
      + `&archived=${encodeURIComponent(archived)}`
      + (q ? `&q=${encodeURIComponent(q)}` : ""));
    if (seq !== documentListSeq) return;   // a later search already answered
    renderDocumentList(data.items, archived === "true");
    scheduleDocumentPoll(data.items);
  } catch (err) {
    showToast("Couldn't load documents: " + err.message, true);
  }
  refreshDocumentBadge();
  checkOllamaStatusBanner("documents-ollama-banner");
}

/* A document sits in "Waiting" until the worker's next poll, which by default
 * is up to twenty seconds away. Without this the page looks broken for those
 * twenty seconds — you dropped a file and nothing happened. Polls only while
 * something is actually in flight, and only while this tab is on screen. */
function scheduleDocumentPoll(items) {
  clearTimeout(documentPollTimer);
  const working = items.some((d) => d.extraction_status === "pending"
    || d.extraction_status === "processing");
  if (!working) return;
  documentPollTimer = setTimeout(() => {
    if (document.getElementById("view-documents").classList.contains("active")) loadDocuments();
  }, 4000);
}

async function refreshDocumentBadge() {
  const badge = document.getElementById("documents-badge");
  if (!badge) return;
  try {
    const data = await api("/api/documents?status=all&limit=1");
    // The badge counts the inbox, not unread proposals — those are the Review
    // tab's job, and two badges counting overlapping things is how a person
    // learns to ignore both.
    badge.textContent = data.total;
    badge.hidden = !data.total;
  } catch (e) { badge.hidden = true; }
}

function documentCardHtml(d, archived) {
  const status = DOC_STATUS_LABELS[d.extraction_status] || { label: d.extraction_status, hint: "" };
  const counts = [];
  if (d.pending_suggestion_count) {
    counts.push(`<span class="doc-pending">${d.pending_suggestion_count} to review</span>`);
  } else if (d.suggestion_count) {
    counts.push(`<span class="doc-reviewed">${d.suggestion_count} reviewed</span>`);
  } else if (d.extraction_status === "done") {
    counts.push(`<span class="doc-none">nothing proposed</span>`);
  }
  return `
    <div class="queue-card doc-card" data-document-id="${d.id}">
      <div class="queue-card-head">
        <span class="doc-head-left">
          <a href="#" class="doc-open" data-open-document="${d.id}"><strong>${escapeHtml(docTitle(d))}</strong></a>
          ${d.is_pasted ? '<span class="source-pill" title="Typed or pasted in, not uploaded as a file">Pasted</span>' : ""}
          <span class="doc-status doc-status-${escapeHtml(d.extraction_status)}" title="${escapeHtml(status.hint)}">${escapeHtml(status.label)}</span>
        </span>
        <span class="doc-meta">${counts.join("")}</span>
      </div>
      <div class="doc-sub">
        ${d.title && d.filename !== d.title ? `<span class="mono">${escapeHtml(d.filename)}</span>` : ""}
        ${d.file_size_bytes ? `<span>${escapeHtml(formatFileSize(d.file_size_bytes))}</span>` : ""}
        <span>added ${timeAgoHtml(d.uploaded_at)}</span>
        ${d.source_note ? `<span class="doc-source">${escapeHtml(d.source_note)}</span>` : ""}
      </div>
      ${d.extraction_status === "failed" && d.extraction_error
        ? `<div class="doc-error">${escapeHtml(d.extraction_error)}</div>` : ""}
      <div class="queue-actions">
        <button class="btn-secondary btn-sm" data-open-document="${d.id}">Open</button>
        ${archived
          ? `<button class="btn-secondary btn-sm" data-restore-document="${d.id}">Restore</button>`
          : `<button class="btn-secondary btn-sm" data-archive-document="${d.id}">Archive</button>`}
        ${archived ? "" :
          // Offered at every status. The case that needs it most does not look
          // like a failure: a document read while Ollama was down is marked
          // "Read" and simply produced nothing.
          `<button class="btn-secondary btn-sm" data-reextract-document="${d.id}">Read again</button>`}
      </div>
    </div>
  `;
}

function renderDocumentList(items, archived) {
  const el = document.getElementById("document-list");
  if (!items.length) {
    el.innerHTML = `<div class="empty-state">${archived
      ? "Nothing archived."
      : "Nothing in the inbox. Drop a document above."}</div>`;
    return;
  }
  el.innerHTML = items.map((d) => documentCardHtml(d, archived)).join("");
  el.querySelectorAll("[data-open-document]").forEach((b) => {
    b.addEventListener("click", (e) => { e.preventDefault(); openDocument(b.dataset.openDocument); });
  });
  el.querySelectorAll("[data-archive-document]").forEach((b) => {
    b.addEventListener("click", () => documentAction(b.dataset.archiveDocument, "archive", "Archived"));
  });
  el.querySelectorAll("[data-restore-document]").forEach((b) => {
    b.addEventListener("click", () => documentAction(b.dataset.restoreDocument, "restore", "Back in the inbox"));
  });
  el.querySelectorAll("[data-reextract-document]").forEach((b) => {
    b.addEventListener("click", () => documentAction(b.dataset.reextractDocument, "re-extract", "Queued to be read again"));
  });
}

/* Re-read the whole batch. An Ollama outage leaves every document uploaded
 * during it marked "Read" with nothing proposed; clicking through thirty of
 * those one at a time is the kind of chore that does not get done. */
(function wireReReadAll() {
  const btn = document.getElementById("doc-reread-all");
  if (!btn) return;
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    try {
      const res = await api("/api/documents/re-read-all",
                            { method: "POST", body: { only_empty: true } });
      intakeStatus(escapeHtml(res.message), res.queued ? "ok" : "");
      loadDocuments();
    } catch (err) {
      intakeStatus(escapeHtml(err.message), "warn");
    } finally { btn.disabled = false; }
  });
})();

async function documentAction(id, verb, toast) {
  try {
    await api(`/api/documents/${id}/${verb}`, { method: "POST" });
    showToast(toast);
    loadDocuments();
  } catch (err) { showToast(err.message, true); }
}

/* ---------------------------------------------------------------------------
 * Intake: the drop zone and the paste box
 * ------------------------------------------------------------------------ */

(function wireDocumentIntake() {
  const zone = document.getElementById("doc-drop-zone");
  const input = document.getElementById("doc-file-input");
  if (!zone || !input) return;

  zone.addEventListener("click", () => input.click());
  zone.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.click(); }
  });
  input.addEventListener("change", () => {
    if (input.files.length) uploadDocuments([...input.files]);
    input.value = "";   // so choosing the same file twice in a row still fires
  });

  // dragover must be cancelled or the browser navigates to the dropped file,
  // which throws away the page and whatever else was in flight.
  ["dragenter", "dragover"].forEach((evt) => zone.addEventListener(evt, (e) => {
    e.preventDefault();
    zone.classList.add("drag-over");
  }));
  ["dragleave", "drop"].forEach((evt) => zone.addEventListener(evt, (e) => {
    e.preventDefault();
    zone.classList.remove("drag-over");
  }));
  zone.addEventListener("drop", (e) => {
    const files = [...(e.dataTransfer ? e.dataTransfer.files : [])];
    if (files.length) uploadDocuments(files);
  });
})();

function intakeStatus(html, kind) {
  const el = document.getElementById("doc-intake-status");
  if (!el) return;
  el.hidden = !html;
  el.className = "intake-status" + (kind ? " " + kind : "");
  el.innerHTML = html;
}

/* Files upload one at a time rather than in parallel: a person dropping a
 * folder of scans is the normal case here, and twenty concurrent multipart
 * uploads against a Pi is how you get timeouts instead of documents. Each
 * one's outcome is reported separately so a single bad file doesn't read as
 * "the whole drop failed". */
async function uploadDocuments(files) {
  let done = 0;
  const failed = [];
  for (const file of files) {
    intakeStatus(`Uploading ${escapeHtml(file.name)}… (${done + 1} of ${files.length})`);
    const fd = new FormData();
    fd.append("file", file);
    try {
      await apiUpload("/api/attachments", fd);
      done += 1;
    } catch (err) {
      failed.push(`${file.name}: ${err.message}`);
    }
  }
  if (failed.length) {
    intakeStatus(`Added ${done} of ${files.length}. ${failed.map(escapeHtml).join("; ")}`, "warn");
  } else {
    intakeStatus(`Added ${done} document${done === 1 ? "" : "s"}. Reading ${done === 1 ? "it" : "them"} now — this can take a minute for a scanned page.`, "ok");
  }
  loadDocuments();
}

(function wireDocumentPaste() {
  const btn = document.getElementById("doc-paste-btn");
  const text = document.getElementById("doc-paste-text");
  const title = document.getElementById("doc-paste-title");
  if (!btn || !text) return;
  btn.addEventListener("click", async () => {
    const body = text.value.trim();
    if (!body) { intakeStatus("Nothing to save — paste or type something first.", "warn"); return; }
    btn.disabled = true;
    try {
      await api("/api/documents/text", {
        method: "POST",
        body: { text: body, title: title.value.trim() || undefined },
      });
      text.value = "";
      title.value = "";
      intakeStatus("Saved. Reading it now.", "ok");
      loadDocuments();
    } catch (err) {
      intakeStatus(escapeHtml(err.message), "warn");
    } finally {
      btn.disabled = false;
    }
  });
})();

document.getElementById("doc-search").addEventListener("input", debounce(loadDocuments, 250));
document.getElementById("doc-status-filter").addEventListener("change", loadDocuments);
document.getElementById("doc-archived-filter").addEventListener("change", loadDocuments);

/* ---------------------------------------------------------------------------
 * Document detail: the text on one side, what was proposed from it on the other
 * ------------------------------------------------------------------------ */

let currentDocument = null;

/* Save a whole document as a Record entity. */
function openToRecordForm(d) {
  const text = d.extracted_text || "";
  const linked = d.linked_entities || [];
  const linkRow = (e, checked) => `
    <label class="record-link-row">
      <input type="checkbox" data-record-link="${escapeHtml(e.id)}" ${checked ? "checked" : ""}>
      <span class="type-pill type-${escapeHtml(e.entity_type)}">${escapeHtml(ENTITY_TYPE_LABELS[e.entity_type] || e.entity_type)}</span>
      ${escapeHtml(e.name)}
    </label>`;
  const filed = d.report_id || d.entity_id;
  openModal(`
    <h3>Save as Record</h3>
    <form id="to-record-form">
      <div class="form-row"><label for="tr-name">Name</label>
        <input type="text" id="tr-name" maxlength="256" required value="${escapeHtml(docTitle(d))}"></div>
      <div class="form-row-inline">
        <div class="form-row"><label for="tr-kind">Kind</label>
          <input type="text" id="tr-kind" maxlength="120" placeholder="Letter, statement, registration…"></div>
        <div class="form-row"><label for="tr-date">Date on the document</label>
          <input type="date" id="tr-date"></div>
      </div>
      <div class="form-row"><label for="tr-issuer">Issued by</label>
        <input type="text" id="tr-issuer" maxlength="256"></div>
      <div class="form-row"><label for="tr-desc">Summary <span class="muted">(optional)</span></label>
        <input type="text" id="tr-desc" placeholder="One line on why it matters"></div>

      <div class="form-row"><label>Link to</label>
        <div class="record-link-list" id="tr-links">
          ${linked.length ? linked.map((e) => linkRow(e, true)).join("")
                          : '<p class="field-hint">Nothing has been accepted from this document yet. Add links below.</p>'}
        </div>
      </div>
      ${entityPickerHtml("Add another", "tr-add")}
      <button type="button" class="btn-secondary btn-sm" id="tr-add-btn">Add</button>

      <details class="record-text-edit">
        <summary>Full text (${text.length.toLocaleString()} characters) — edit before saving</summary>
        <textarea id="tr-body" rows="10">${escapeHtml(text)}</textarea>
      </details>

      <label class="inline-check"><input type="checkbox" id="tr-file" ${filed ? "disabled" : "checked"}>
        ${filed ? "The original is already filed elsewhere and stays there"
                : "Move the original file onto the Record"}</label>

      <div class="form-actions">
        <button type="submit" class="btn-primary">Save Record</button>
        <button type="button" class="btn-secondary" id="tr-cancel">Cancel</button>
      </div>
    </form>`);

  wireEntityPicker("tr-add");
  document.getElementById("tr-cancel").addEventListener("click", closeModal);
  document.getElementById("tr-add-btn").addEventListener("click", () => {
    const id = document.getElementById("tr-add-id").value;
    const label = document.getElementById("tr-add-input").value;
    if (!id) { showToast("Pick an entity from the list first", true); return; }
    const list = document.getElementById("tr-links");
    if (list.querySelector(`[data-record-link="${CSS.escape(id)}"]`)) {
      list.querySelector(`[data-record-link="${CSS.escape(id)}"]`).checked = true;
    } else {
      const m = label.match(/^(.*) \((\w+)\)$/);
      const hint = list.querySelector(".field-hint");
      if (hint) hint.remove();
      list.insertAdjacentHTML("beforeend",
        linkRow({ id, name: m ? m[1] : label, entity_type: m ? m[2] : "" }, true));
    }
    document.getElementById("tr-add-input").value = "";
    document.getElementById("tr-add-id").value = "";
  });

  document.getElementById("to-record-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const body = document.getElementById("tr-body").value;
    const payload = {
      name: document.getElementById("tr-name").value.trim(),
      description: document.getElementById("tr-desc").value.trim() || null,
      record_kind: document.getElementById("tr-kind").value.trim() || null,
      record_date: document.getElementById("tr-date").value || null,
      issued_by: document.getElementById("tr-issuer").value.trim() || null,
      body,
      link_entity_ids: [...document.querySelectorAll("[data-record-link]:checked")]
        .map((b) => b.dataset.recordLink),
      file_original: document.getElementById("tr-file").checked,
    };
    try {
      const res = await api(`/api/documents/${d.id}/to-record`, { method: "POST", body: payload });
      closeModal();
      showToast(res.links_created
        ? `Record saved and linked to ${res.links_created} ${res.links_created === 1 ? "entity" : "entities"}`
        : "Record saved");
      openEntityDetail(res.id);
    } catch (err) { showToast(err.message, true); }
  });
}

async function openDocument(id) {
  try {
    currentDocument = await api(`/api/documents/${id}`);
  } catch (err) {
    showToast(err.message, true);
    return;
  }
  renderDocumentDetail(currentDocument);
  setActiveTab("documents");        // the detail view is a sub-page of Documents
  switchViewRaw("document-detail");
}

function docSuggestionHtml(s) {
  const pending = s.status === "pending";
  const body = s.suggestion_type === "entity"
    ? `<span class="type-pill type-${escapeHtml(s.suggested_entity_type || "")}">${escapeHtml(s.suggested_entity_type || "")}</span>
       <strong>${escapeHtml(s.suggested_name || "")}</strong>`
    : `<strong>${escapeHtml(s.suggested_from_name || "")}</strong>
       <span class="rel-arrow">→</span>
       <strong>${escapeHtml(s.suggested_to_name || "")}</strong>
       <span class="mono">${escapeHtml(s.suggested_relationship_type || "")}</span>`;
  const findable = s.suggested_name || s.suggested_from_name || "";
  const details = s.details && Object.keys(s.details).length
    ? `<ul class="queue-detail-list">${Object.entries(s.details).map(([k, v]) =>
        `<li>${escapeHtml(k)}: ${escapeHtml(Array.isArray(v) ? v.join(", ") : String(v))}</li>`).join("")}</ul>`
    : "";
  return `
    <div class="doc-suggestion ${pending ? "" : "resolved-suggestion"}" data-doc-suggestion="${s.id}">
      <div class="doc-suggestion-head">
        <span>${body}</span>
        <span class="queue-score">${s.confidence == null ? "" : Math.round(s.confidence * 100) + "%"}</span>
      </div>
      ${details}
      ${pending && s.suggestion_type !== "entity" ? `
        <div class="doc-endpoints">
          ${endpointHtml(s, "from")}
          ${endpointHtml(s, "to")}
        </div>` : ""}
      ${pending ? `
        <div class="queue-actions">
          ${s.suggestion_type === "entity"
            ? `<button class="btn-primary btn-sm" data-doc-accept="${s.id}">Accept</button>`
            // A relationship names two records that may not exist yet. They can
            // be settled here now — matched to what is already in the file, or
            // created with the type the relationship implies — so this stopped
            // being a detour to another tab.
            : `<button class="btn-primary btn-sm" data-doc-accept-rel="${s.id}">Accept</button>`}
          <button class="btn-danger btn-sm" data-doc-reject="${s.id}">Dismiss</button>
          ${findable ? `<button class="btn-link btn-sm" data-doc-find="${escapeHtml(findable)}">Find in text</button>` : ""}
        </div>`
        : `<div class="card-meta">${escapeHtml(s.status)}</div>`}
    </div>`;
}

function renderDocumentDetail(d) {
  const status = DOC_STATUS_LABELS[d.extraction_status] || { label: d.extraction_status, hint: "" };
  const pending = d.suggestions.filter((s) => s.status === "pending");
  const text = d.extracted_text || "";

  // A relationship suggestion from a document names two people as strings, not
  // records — the model only ever saw text. Those still have to be resolved in
  // the Review tab, where the entity pickers live, so this view is honest
  // about which ones it can finish and which it cannot.
  const resolvable = pending.filter((s) => s.suggestion_type === "entity");
  const needsReview = pending.filter((s) => s.suggestion_type !== "entity");
  // Records made by hand from this document get their own visible group
  // rather than being folded into "already decided" with the rest. Somebody
  // working through a document records several in a row, and watching them
  // accumulate is the feedback that the last one worked.
  const byHand = d.suggestions.filter((s) => s.source === "manual");
  const decided = d.suggestions.filter((s) => s.status !== "pending" && s.source !== "manual");

  document.getElementById("document-detail-body").innerHTML = `
    <div class="doc-detail-head">
      <div>
        <h2 id="doc-detail-title">${escapeHtml(docTitle(d))}</h2>
        <div class="doc-sub">
          <span class="doc-status doc-status-${escapeHtml(d.extraction_status)}" title="${escapeHtml(status.hint)}">${escapeHtml(status.label)}</span>
          ${d.is_pasted ? '<span class="source-pill">Pasted</span>' : `<span class="mono">${escapeHtml(d.filename)}</span>`}
          ${d.file_size_bytes ? `<span>${escapeHtml(formatFileSize(d.file_size_bytes))}</span>` : ""}
          <span>added ${timeAgoHtml(d.uploaded_at)}${d.uploaded_by_name ? " by " + escapeHtml(d.uploaded_by_name) : ""}</span>
        </div>
      </div>
      <div class="doc-detail-actions">
        <button class="btn-secondary btn-sm" id="doc-rename-btn">Rename</button>
        <button class="btn-secondary btn-sm" id="doc-retry-btn"
                title="Read this document again. Decisions already made are kept.">Read again</button>
        <a class="btn-secondary btn-sm" href="/api/attachments/${d.id}/file" download>Download</a>
        ${d.records && d.records.length ? "" : '<button class="btn-primary btn-sm" id="doc-to-record-btn">Save as Record</button>'}
        <button class="btn-secondary btn-sm" id="doc-file-under-btn">File under…</button>
        <button class="btn-secondary btn-sm" id="doc-archive-btn">Archive</button>
        ${canDelete() ? '<button class="btn-danger btn-sm" id="doc-delete-btn">Delete</button>' : ""}
      </div>
    </div>
    ${d.source_note ? `<p class="doc-source-note">Where it came from: ${escapeHtml(d.source_note)}</p>` : ""}
    ${d.records && d.records.length ? `<p class="doc-record-note">Saved as Record:
      ${d.records.map((r) => `<a href="#" data-open-entity="${escapeHtml(r.id)}">${escapeHtml(r.name)}</a>`).join(", ")}</p>` : ""}
    ${d.extraction_status === "failed" && d.extraction_error
      ? `<div class="doc-error">${escapeHtml(d.extraction_error)}</div>` : ""}

    <div id="doc-file-under-panel" class="file-under-panel" hidden>
      <p class="view-hint">Moves the document onto that entity's attachments.</p>
      ${entityPickerHtml("Entity", "doc-file-entity")}
      <div class="queue-actions">
        <button class="btn-primary btn-sm" id="doc-file-confirm">File it</button>
        <button class="btn-secondary btn-sm" id="doc-file-cancel">Cancel</button>
      </div>
    </div>

    <div class="doc-split">
      <section class="doc-text-pane">
        <div class="doc-text-head">
          <h3>What it says</h3>
          <!-- The model misses things. A reporter\'s byline, a second company
               named once in passing, the registration of a van — all real,
               all easy to read straight past in the Review queue because
               nothing proposed them. Reading the document is when you notice,
               so recording it belongs here rather than three screens away. -->
          <button class="btn-secondary btn-sm" id="doc-add-entity-btn">+ Add entity</button>
        </div>
        <p class="view-hint" id="doc-select-hint">Select a name in the text to add it as an entity, or use the button.</p>
        ${text.trim()
          ? `<pre class="doc-text" id="doc-text">${escapeHtml(text)}</pre>`
          : `<div class="empty-state">${d.extraction_status === "done"
              ? "No text found in this document."
              : "Nothing extracted yet."}</div>`}
      </section>
      <section class="doc-suggestions-pane">
        <h3>What was proposed from it</h3>
        ${!d.suggestions.length
          ? `<div class="empty-state">${d.extraction_status === "done"
              ? "Nothing proposed."
              : "Nothing yet — it hasn't been read."}</div>`
          : `
            ${resolvable.map(docSuggestionHtml).join("")}
            ${needsReview.length ? `
              <div class="doc-needs-review">
                <p class="view-hint">${needsReview.length} proposed relationship${needsReview.length === 1 ? "" : "s"} — match each end to an existing entity or create it when you accept.</p>
                ${needsReview.map(docSuggestionHtml).join("")}
              </div>` : ""}
            ${byHand.length ? `
              <div class="doc-by-hand">
                <p class="view-hint">${byHand.length} added by hand from this document.</p>
                ${byHand.map(docSuggestionHtml).join("")}
              </div>` : ""}
            ${decided.length
              ? `<details class="doc-resolved"><summary>${decided.length} already decided</summary>
                   ${decided.map(docSuggestionHtml).join("")}
                 </details>` : ""}
          `}
      </section>
    </div>
  `;
  wireDocumentDetail(d);
}

/* The sentence a selected name sits in. Kept as the evidence on a hand-made
 * record, because "where did this come from" deserves the line it came out
 * of and not just the file name. Falls back to a window around the match when
 * the text has no sentence breaks in it, which OCR output frequently does
 * not. */
function sentenceAround(haystack, needle) {
  const at = (haystack || "").indexOf(needle);
  if (at === -1) return needle;
  const before = haystack.lastIndexOf(".", at);
  const afterDot = haystack.indexOf(".", at + needle.length);
  let start = before === -1 ? Math.max(0, at - 120) : before + 1;
  let end = afterDot === -1 ? Math.min(haystack.length, at + needle.length + 120) : afterDot + 1;
  if (end - start > 400) {
    start = Math.max(0, at - 160);
    end = Math.min(haystack.length, at + needle.length + 160);
  }
  return haystack.slice(start, end).trim();
}

function wireDocumentDetail(d) {
  const on = (id, fn) => {
    const el = document.getElementById(id);
    if (el) el.addEventListener("click", fn);
  };

  on("doc-rename-btn", async () => {
    const next = prompt("What should this document be called?", docTitle(d));
    if (next === null) return;
    try {
      currentDocument = await api(`/api/documents/${d.id}`, {
        method: "PATCH", body: { title: next.trim() },
      });
      renderDocumentDetail(currentDocument);
      showToast("Renamed");
    } catch (err) { showToast(err.message, true); }
  });

  /* Recording something the model missed.
   *
   * Two ways in, because there are two situations. The name is written in the
   * document -- select it and the form opens with it filled in, and the
   * sentence it came out of is kept as the evidence. Or the document implies
   * something it never spells out ("our reporter" with a byline further up),
   * in which case the button opens the same form empty. */
  const recordFromDocument = (name, quote) => {
    openEntityForm(null, name ? { name } : null, {
      create: (body) => api(`/api/documents/${d.id}/record`, {
        method: "POST",
        body: { ...body, quote: quote || null },
      }),
      onCreated: async () => {
        // Stay on the document. Somebody recording what the model missed is
        // usually still reading, and there is normally more than one.
        try {
          currentDocument = await api(`/api/documents/${d.id}`);
          renderDocumentDetail(currentDocument);
        } catch (err) { showToast(err.message, true); }
      },
    });
    // The form opens with the type dropdown focused rather than the name,
    // because a selected name is already right and the type usually is not.
    const typeSelect = document.getElementById("entity-form-type");
    if (typeSelect && name) typeSelect.focus();
  };

  on("doc-add-entity-btn", () => recordFromDocument("", ""));

  const textEl = document.getElementById("doc-text");
  if (textEl) {
    const popup = document.createElement("button");
    popup.className = "btn-primary btn-sm doc-select-popup";
    popup.type = "button";
    popup.hidden = true;
    textEl.parentElement.appendChild(popup);

    const hidePopup = () => { popup.hidden = true; };
    popup.addEventListener("mousedown", (ev) => ev.preventDefault());  // keep the selection
    popup.addEventListener("click", () => {
      const name = (popup.dataset.name || "").trim();
      hidePopup();
      recordFromDocument(name, popup.dataset.quote || "");
    });

    textEl.addEventListener("mouseup", () => {
      // A moment, so the selection has settled before it is read.
      setTimeout(() => {
        const sel = window.getSelection();
        const text = (sel ? sel.toString() : "").trim();
        // A name, not a paragraph: anything this long is somebody selecting
        // to read rather than to record, and offering a button then is noise.
        if (!text || text.length > 120 || !textEl.contains(sel.anchorNode)) {
          hidePopup();
          return;
        }
        popup.dataset.name = text;
        popup.dataset.quote = sentenceAround(textEl.textContent, text);
        popup.textContent = `+ Record “${truncate(text, 32)}”`;
        popup.hidden = false;
        const rect = sel.getRangeAt(0).getBoundingClientRect();
        const host = textEl.parentElement.getBoundingClientRect();
        popup.style.left = Math.max(0, rect.left - host.left) + "px";
        popup.style.top = Math.max(0, rect.bottom - host.top + 6) + "px";
      }, 10);
    });
    textEl.addEventListener("scroll", hidePopup);
    document.addEventListener("keydown", (ev) => { if (ev.key === "Escape") hidePopup(); });
  }

  on("doc-delete-btn", () => openDeleteDialog(
    { document_ids: [d.id] }, () => switchView("documents")));

  on("doc-archive-btn", async () => {
    try {
      await api(`/api/documents/${d.id}/archive`, { method: "POST" });
      showToast("Archived");
      switchView("documents");
    } catch (err) { showToast(err.message, true); }
  });

  on("doc-retry-btn", async () => {
    try {
      const res = await api(`/api/documents/${d.id}/re-extract`, { method: "POST" });
      const cleared = res.cleared_pending_suggestions || 0;
      showToast(cleared
        ? `Queued to be read again — ${cleared} undecided proposal(s) will be replaced`
        : "Queued to be read again");
      switchView("documents");
    } catch (err) { showToast(err.message, true); }
  });

  on("doc-to-record-btn", () => openToRecordForm(d));
  document.querySelectorAll(".doc-record-note [data-open-entity]").forEach((a) => {
    a.addEventListener("click", (e) => { e.preventDefault(); openEntityDetail(a.dataset.openEntity); });
  });

  const panel = document.getElementById("doc-file-under-panel");
  on("doc-file-under-btn", () => {
    panel.hidden = !panel.hidden;
    if (!panel.hidden) wireEntityPicker("doc-file-entity");
  });
  on("doc-file-cancel", () => { panel.hidden = true; });
  on("doc-file-confirm", async () => {
    const entityId = document.getElementById("doc-file-entity-id").value;
    if (!entityId) { showToast("Pick an entity from the list first", true); return; }
    try {
      await api(`/api/documents/${d.id}/file-under`, {
        method: "POST", body: { entity_id: entityId },
      });
      showToast("Filed on that entity's attachments");
      switchView("documents");
    } catch (err) { showToast(err.message, true); }
  });

  document.querySelectorAll("[data-doc-accept]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      try {
        await api(`/api/extraction-suggestions/${btn.dataset.docAccept}/accept`,
                  { method: "POST", body: {} });
        showToast("Accepted");
        openDocument(d.id);
        refreshQueueBadges();
      } catch (err) { showToast(err.message, true); }
    });
  });
  document.querySelectorAll("[data-doc-reject]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      try {
        await api(`/api/extraction-suggestions/${btn.dataset.docReject}/reject`, { method: "POST" });
        showToast("Dismissed");
        openDocument(d.id);
        refreshQueueBadges();
      } catch (err) { showToast(err.message, true); }
    });
  });

  // "Find in text" is the whole argument for this layout: a proposed name is
  // only checkable against the sentence it came from.
  document.querySelectorAll("[data-doc-find]").forEach((btn) => {
    btn.addEventListener("click", () => highlightInDocument(btn.dataset.docFind));
  });
  (d.suggestions || []).forEach((s) => {
    if (s.status !== "pending" || s.suggestion_type === "entity") return;
    wireEndpoint(s, "from");
    wireEndpoint(s, "to");
    const btn = document.querySelector(`[data-doc-accept-rel="${s.id}"]`);
    if (btn) btn.addEventListener("click", () =>
      acceptDocumentRelationship(s, () => openDocument(d.id)));
  });
}

function highlightInDocument(needle) {
  const pre = document.getElementById("doc-text");
  if (!pre || !needle) return;
  const text = currentDocument.extracted_text || "";
  const at = text.toLowerCase().indexOf(needle.toLowerCase());
  if (at === -1) {
    // Worth saying rather than silently doing nothing: a proposed name that is
    // nowhere in the text is the clearest possible sign the model invented it.
    showToast(`"${needle}" doesn't appear in the text — check this proposal.`, true);
    return;
  }
  pre.innerHTML = escapeHtml(text.slice(0, at))
    + `<mark id="doc-hit">${escapeHtml(text.slice(at, at + needle.length))}</mark>`
    + escapeHtml(text.slice(at + needle.length));
  const hit = document.getElementById("doc-hit");
  if (hit) hit.scrollIntoView({ block: "center", behavior: "smooth" });
}

/* ============================================================================
 * Admin settings — branding
 *
 * The instance name, an optional brand colour, and the theme people get before
 * they choose one. Saving applies it immediately in this browser rather than
 * waiting for a reload, because an admin picking a brand colour is looking at
 * the result while they pick it — a form that needed a refresh to show the
 * effect would have them saving three times to find the right shade.
 * ========================================================================== */

async function loadBrandingForm() {
  const statusEl = document.getElementById("branding-status");
  try {
    const data = await api("/api/admin/branding");
    const f = data.fields;
    document.getElementById("branding-name").value =
      f.instance_name.overridden ? f.instance_name.value : "";
    document.getElementById("branding-name").placeholder = f.instance_name.env_default;
    const accent = f.brand_accent.overridden ? f.brand_accent.value : "";
    document.getElementById("branding-accent").value = accent;
    document.getElementById("branding-accent-picker").value = accent || currentAccentHex();
    document.getElementById("branding-palette").value = f.default_palette.value;
    statusEl.textContent = data.updated_by
      ? `Last changed by ${data.updated_by}` : "Using .env defaults";
    checkBrandAccentContrast(accent);
  } catch (err) {
    statusEl.textContent = `Could not load branding: ${err.message}`;
  }
}

function currentAccentHex() {
  const v = getComputedStyle(document.documentElement).getPropertyValue("--accent").trim();
  return /^#[0-9a-fA-F]{6}$/.test(v) ? v : "#39ff88";
}

/* A brand colour is whatever the organisation's brand guide says, and some of
 * those are genuinely hard to read as link text. The fill behind a button is
 * darkened automatically until its label is legible, but the link colour is
 * left as given — so this says plainly when that colour will be hard to read
 * on the page, rather than either ignoring it or quietly changing the
 * company's colour to something it isn't. */
function checkBrandAccentContrast(hex) {
  const box = document.getElementById("branding-accent-warning");
  const rgb = hexToRgb(hex);
  if (!rgb) { box.hidden = true; return; }
  const style = getComputedStyle(document.documentElement);
  // Both surfaces a link in this colour actually sits on, and the worse of the
  // two is the one that matters. Checking only the panel would have called a
  // colour fine that is unreadable everywhere outside a card — which is most
  // of the page.
  const surfaces = [
    ["panel background", hexToRgb(style.getPropertyValue("--panel").trim())],
    ["page background", hexToRgb(style.getPropertyValue("--bg").trim())],
  ].filter(([, c]) => c);
  if (!surfaces.length) { box.hidden = true; return; }

  let worst = null;
  for (const [name, surface] of surfaces) {
    const ratio = contrastRatio(rgb, surface);
    if (!worst || ratio < worst.ratio) worst = { name, ratio };
  }
  if (worst.ratio >= 4.5) {
    box.hidden = true;
    return;
  }
  box.hidden = false;
  box.textContent =
    `This colour measures ${worst.ratio.toFixed(2)}:1 against the current theme's `
    + `${worst.name}, below the 4.5:1 that normal text needs. Buttons stay `
    + `readable — the fill behind a label is darkened until it clears — but links `
    + `and outlines in this colour will be hard to read for some people. A darker `
    + `shade of the same hue usually fixes it without leaving your brand.`;
}

document.getElementById("branding-accent-picker").addEventListener("input", (e) => {
  document.getElementById("branding-accent").value = e.target.value;
  checkBrandAccentContrast(e.target.value);
});
document.getElementById("branding-accent").addEventListener("input", (e) => {
  const v = e.target.value.trim();
  if (/^#[0-9a-fA-F]{6}$/.test(v)) {
    document.getElementById("branding-accent-picker").value = v;
  }
  checkBrandAccentContrast(v);
});
document.getElementById("branding-accent-clear").addEventListener("click", () => {
  document.getElementById("branding-accent").value = "";
  checkBrandAccentContrast("");
});

document.getElementById("branding-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const name = document.getElementById("branding-name").value.trim();
  const accent = document.getElementById("branding-accent").value.trim();
  const palette = document.getElementById("branding-palette").value;
  if (accent && !/^#[0-9a-fA-F]{6}$/.test(accent)) {
    showToast("Brand colour must be a six-digit hex value, e.g. #1f6feb", true);
    return;
  }
  try {
    // Empty string rather than omitted: it means "clear the override and go
    // back to the .env default", which is what an emptied field should do.
    const saved = await api("/api/admin/branding", {
      method: "PATCH",
      body: { instance_name: name || null, brand_accent: accent || null,
              default_palette: palette },
    });
    applyInstanceName(saved.instance_name);
    applyBrandAccent(saved.brand_accent);
    instanceDefaultPalette = saved.default_palette;
    showToast("Branding saved");
    loadBrandingForm();
  } catch (err) {
    showToast(err.message, true);
  }
});

/* ============================================================================
 * Admin settings — user management (list, change role, activate/deactivate).
 * Account creation itself still goes through the shared openNewUserForm()
 * modal defined above (also used by the topbar's "+ User" shortcut).
 * ========================================================================== */

document.getElementById("admin-page-new-user-btn").addEventListener("click", () => openNewUserForm(loadAdminUsers));

/* ---------------------------------------------------------------------------
 * Admin sections
 *
 * Each section loads only when it is opened. That is not only tidiness: the
 * model panel asks Ollama what models it has and the usage panel runs a
 * report, and doing both on every visit to change a user's role was work
 * nobody asked for -- on a deployment with no model configured it was also a
 * visible pause for an answer that was never going to be useful.
 * ------------------------------------------------------------------------ */

const ADMIN_SECTION_KEY = "humint.admin.section";

const ADMIN_LOADERS = {
  users: () => loadAdminUsers(),
  feeds: () => loadRssFeeds(),
  maps: () => loadMapAdmin(),
  brand: () => loadBrandingForm(),
  model: () => { loadOllamaSettings(); loadOllamaUsage(); },
  audit: () => {},
  backup: () => {},
};

function showAdminSection(key, remember) {
  const panels = document.querySelectorAll("[data-admin-panel]");
  if (!panels.length) return;
  const known = [...panels].map((p) => p.dataset.adminPanel);
  if (!known.includes(key)) key = known[0];

  panels.forEach((panel) => { panel.hidden = panel.dataset.adminPanel !== key; });
  document.querySelectorAll("[data-admin-section]").forEach((btn) => {
    const active = btn.dataset.adminSection === key;
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-current", active ? "page" : "false");
  });

  if (remember !== false) {
    try { localStorage.setItem(ADMIN_SECTION_KEY, key); } catch (e) { /* storage off */ }
  }
  const load = ADMIN_LOADERS[key];
  if (load) load();
}

function openAdminSections() {
  let key = "users";
  try { key = localStorage.getItem(ADMIN_SECTION_KEY) || "users"; } catch (e) { /* storage off */ }
  showAdminSection(key, false);
}

(function wireAdminNav() {
  document.querySelectorAll("[data-admin-section]").forEach((btn) => {
    btn.addEventListener("click", () => showAdminSection(btn.dataset.adminSection));
  });
})();


async function loadAdminUsers() {
  const el = document.getElementById("admin-user-list");
  el.innerHTML = '<p class="empty-state">Loading…</p>';
  try {
    const data = await api("/api/users");
    renderAdminUserList(data.items);
  } catch (err) {
    el.innerHTML = `<p class="empty-state">${escapeHtml(err.message)}</p>`;
  }
}

function renderAdminUserList(items) {
  const el = document.getElementById("admin-user-list");
  if (!items.length) { el.innerHTML = '<div class="empty-state">No users found.</div>'; return; }

  const isLocked = (u) => u.locked_until && new Date(u.locked_until) > new Date();

  el.innerHTML = items.map((u) => `
    <div class="card admin-user-row" data-id="${u.id}">
      <div class="admin-user-main">
        <div class="card-title">${escapeHtml(u.username)}${u.id === state.user.id ? ' <span class="card-meta">(you)</span>' : ""}</div>
        <div class="card-meta">
          ${u.is_active ? "" : "Deactivated · "}${isLocked(u) ? "Locked out (too many failed logins) · " : ""}Created ${timeAgoHtml(u.created_at)}
        </div>
      </div>
      <div class="admin-user-controls">
        <select class="admin-role-select" data-user-id="${u.id}">
          <option value="analyst" ${u.role === "analyst" ? "selected" : ""}>Analyst</option>
          <option value="admin" ${u.role === "admin" ? "selected" : ""}>Admin</option>
        </select>
        <button class="btn-secondary btn-sm admin-toggle-active-btn" data-user-id="${u.id}" data-active="${u.is_active}">
          ${u.is_active ? "Deactivate" : "Reactivate"}
        </button>
      </div>
    </div>
  `).join("");

  // Every action re-loads the whole list rather than patching the DOM in
  // place — including on failure. That's deliberate: a <select> already
  // shows whatever the user just picked regardless of whether the request
  // behind it succeeds, so on a rejected change (e.g. "can't remove the
  // last admin") a full reload is what puts the dropdown back to the real,
  // server-confirmed value instead of leaving it showing a change that
  // never actually took effect.
  //
  // The one case a plain reload of THIS list can't handle: changing your
  // OWN account. Role/active checks happen fresh on every request, not just
  // at login, so the moment you demote or deactivate yourself your very
  // next request (the reload this handler is about to make) comes back 403
  // "Admin access required" — which would otherwise just render as a broken
  // error where the user list used to be, with the Admin tab still sitting
  // there in the topbar as if nothing happened. A full page reload instead
  // re-runs the normal login bootstrap, which correctly hides the Admin tab
  // (no longer admin) or logs you out entirely (no longer active) and shows
  // you the app in whatever state you actually left yourself in.
  function afterUserChange(userId, selfLostAccess) {
    if (String(userId) === String(state.user.id) && selfLostAccess) {
      showToast("That changed your own access — reloading…");
      setTimeout(() => location.reload(), 800);
    } else {
      loadAdminUsers();
    }
  }

  el.querySelectorAll(".admin-role-select").forEach((select) => {
    select.addEventListener("change", async () => {
      let selfLostAccess = false;
      try {
        await api(`/api/users/${select.dataset.userId}`, { method: "PATCH", body: { role: select.value } });
        showToast("Role updated");
        selfLostAccess = select.value !== "admin";
      } catch (err) {
        showToast(err.message, true);
      } finally {
        afterUserChange(select.dataset.userId, selfLostAccess);
      }
    });
  });
  el.querySelectorAll(".admin-toggle-active-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const currentlyActive = btn.dataset.active === "true";
      let selfLostAccess = false;
      try {
        await api(`/api/users/${btn.dataset.userId}`, { method: "PATCH", body: { is_active: !currentlyActive } });
        showToast(currentlyActive ? "User deactivated" : "User reactivated");
        selfLostAccess = currentlyActive;
      } catch (err) {
        showToast(err.message, true);
      } finally {
        afterUserChange(btn.dataset.userId, selfLostAccess);
      }
    });
  });
}

/* ============================================================================
 * RSS feed management (Admin page) — see api/rss.py / worker/rss_ingest.py.
 * No feeds ship built in; every row here is one an admin added themselves.
 * ========================================================================== */

document.getElementById("rss-new-feed-btn").addEventListener("click", () => openNewFeedForm());

/** Delete a feed, having said what it put in the case file.
 *
 * The counts come first because the decision depends on them: a feed added an
 * hour ago by mistake and a feed that has been running a month look identical
 * on the list, and only one of them has records somebody has built on.
 */
async function openDeleteFeedDialog(feedId) {
  openModal('<p class="empty-state">Counting what this feed created…</p>');
  let counts;
  try {
    counts = await api(`/api/rss-feeds/${feedId}/records`);
  } catch (err) {
    openModal(`<p class="empty-state">${escapeHtml(err.message)}</p>`);
    return;
  }

  const cited = counts.cited_by_confirmed_reports;
  openModal(`
    <h2>Delete “${escapeHtml(counts.label)}”</h2>
    <p>This feed created <strong>${counts.records.toLocaleString()}</strong>
       Event${counts.records === 1 ? "" : "s"}${
       counts.archived ? ` (${counts.archived.toLocaleString()} already archived)` : ""}.</p>
    ${cited ? `<p class="field-hint"><strong>${cited}</strong> of them
      ${cited === 1 ? "is" : "are"} cited by a confirmed report and will be archived instead of deleted.</p>` : ""}
    <div class="form-row"><label>Its Events</label>
      <select id="df-records">
        <option value="leave">Leave them — only stop polling</option>
        <option value="archive">Archive them — hidden, but every row kept</option>
        <option value="delete">Delete them for good</option>
      </select></div>
    <p class="field-hint">Deleting can only be undone from a backup. Archiving can be reversed.</p>
    <p class="form-error" id="df-error"></p>
    <div class="modal-actions">
      <button type="button" class="btn-secondary" id="df-cancel">Cancel</button>
      <button type="button" class="btn-danger" id="df-confirm">Delete feed</button>
    </div>`);

  document.getElementById("df-cancel").addEventListener("click", closeModal);
  document.getElementById("df-confirm").addEventListener("click", async () => {
    const mode = document.getElementById("df-records").value;
    if (mode === "delete" && counts.records - cited > 0
        && !confirm(`Permanently delete ${(counts.records - cited).toLocaleString()} Event` + "s?\n\nThis cannot be undone.")) return;
    try {
      const result = await api(`/api/rss-feeds/${feedId}?records=${mode}`, { method: "DELETE" });
      closeModal();
      const bits = ["Feed deleted"];
      if (result.records_deleted) bits.push(`${result.records_deleted.toLocaleString()} records removed`);
      if (result.records_archived) bits.push(`${result.records_archived.toLocaleString()} archived`);
      showToast(bits.join(" · "));
      loadRssFeeds();
      refreshQueueBadges();
    } catch (err) {
      document.getElementById("df-error").textContent = err.message;
    }
  });
}


function openNewFeedForm() {
  const html = `
    <h2>Add RSS feed</h2>
    <form id="new-feed-form">
      <div class="form-row"><label>Feed URL</label><input type="url" id="nf-url" required maxlength="2048" placeholder="https://example.com/feed.xml"></div>
      <div class="form-row"><label>Label</label><input type="text" id="nf-label" required maxlength="256" placeholder="e.g. Tulsa World — Local"></div>
      <div class="form-row"><label>Only items from the last</label>
        <span class="inline-fields">
          <input type="number" id="nf-max-age" min="1" max="3650" value="7">
          <span class="field-hint-inline">days</span>
        </span></div>
      <div class="form-row"><label>At most per poll</label>
        <span class="inline-fields">
          <input type="number" id="nf-max-items" min="1" max="1000" value="50">
          <span class="field-hint-inline">items</span>
        </span></div>
      <p class="field-hint">Limits stop a busy feed from flooding the correlation queue on its first poll.</p>
      <p class="form-error" id="nf-error"></p>
      <div class="modal-actions">
        <button type="button" class="btn-secondary" id="nf-cancel">Cancel</button>
        <button type="submit" class="btn-primary">Add feed</button>
      </div>
    </form>
  `;
  openModal(html);
  document.getElementById("nf-cancel").addEventListener("click", closeModal);
  document.getElementById("new-feed-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      await api("/api/rss-feeds", {
        method: "POST",
        body: {
          url: document.getElementById("nf-url").value.trim(),
          label: document.getElementById("nf-label").value.trim(),
          max_age_days: Number(document.getElementById("nf-max-age").value) || null,
          max_items_per_poll: Number(document.getElementById("nf-max-items").value) || null,
        },
      });
      closeModal();
      showToast("Feed added");
      loadRssFeeds();
    } catch (err) {
      document.getElementById("nf-error").textContent = err.message;
    }
  });
}

async function loadRssFeeds() {
  const el = document.getElementById("rss-feed-list");
  if (!el) return;
  el.innerHTML = '<p class="empty-state">Loading…</p>';
  try {
    const data = await api("/api/rss-feeds");
    renderRssFeedList(data.items);
  } catch (err) {
    el.innerHTML = `<p class="empty-state">${escapeHtml(err.message)}</p>`;
  }
}

function renderRssFeedList(items) {
  const el = document.getElementById("rss-feed-list");
  if (!items.length) {
    el.innerHTML = '<div class="empty-state">No feeds yet — add one to start ingesting Events from it.</div>';
    return;
  }
  el.innerHTML = items.map((f) => `
    <div class="card admin-user-row" data-id="${f.id}">
      <div class="admin-user-main">
        <div class="card-title">${escapeHtml(f.label)}${f.is_active ? "" : ' <span class="card-meta">(paused)</span>'}</div>
        <div class="card-meta rss-feed-url">${escapeHtml(f.url)}</div>
        <div class="card-meta">
          ${f.item_count} item${f.item_count === 1 ? "" : "s"} ingested
          · ${f.max_age_days ? `last ${f.max_age_days}d` : "no age limit"}
          · ${f.max_items_per_poll ? `max ${f.max_items_per_poll}/poll` : "no per-poll cap"}
          · ${f.last_polled_at ? "last polled " + timeAgoHtml(f.last_polled_at) : "not polled yet"}
          ${f.last_error ? `· <span class="rss-feed-error">error: ${escapeHtml(f.last_error)}</span>` : ""}
        </div>
      </div>
      <div class="admin-user-controls">
        <button class="btn-secondary btn-sm rss-toggle-active-btn" data-feed-id="${f.id}" data-active="${f.is_active}">
          ${f.is_active ? "Pause" : "Resume"}
        </button>
        <button class="btn-danger btn-sm rss-delete-feed-btn" data-feed-id="${f.id}">Delete</button>
      </div>
    </div>
  `).join("");

  el.querySelectorAll(".rss-toggle-active-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const currentlyActive = btn.dataset.active === "true";
      try {
        await api(`/api/rss-feeds/${btn.dataset.feedId}`, { method: "PATCH", body: { is_active: !currentlyActive } });
        showToast(currentlyActive ? "Feed paused" : "Feed resumed");
      } catch (err) {
        showToast(err.message, true);
      } finally {
        loadRssFeeds();
      }
    });
  });
  el.querySelectorAll(".rss-delete-feed-btn").forEach((btn) => {
    btn.addEventListener("click", () => openDeleteFeedDialog(Number(btn.dataset.feedId)));
  });
  el.querySelectorAll(".rss-never-used-placeholder").forEach((btn) => {
    btn.addEventListener("click", async () => {
      try {
        await api(`/api/rss-feeds/${btn.dataset.feedId}`, { method: "DELETE" });
        showToast("Feed deleted");
      } catch (err) {
        showToast(err.message, true);
      } finally {
        loadRssFeeds();
      }
    });
  });
}

/* ============================================================================
 * Admin: Ollama settings
 * ========================================================================== */

const OLLAMA_SETTINGS_FIELDS = [
  { key: "ollama_base_url", label: "Base URL", type: "text",
    hint: "e.g. http://ollama:11434 or http://192.168.1.50:11434" },
  { key: "ollama_model", label: "Assistant model", type: "model-picker",
    hint: "Used by the AI Assistant, and for extraction if that's blank." },
  { key: "ollama_extract_model", label: "Extraction model", type: "model-picker", allowBlank: true,
    blankLabel: "(use the assistant model)",
    hint: "Reads uploaded documents. Blank uses the assistant model." },
  { key: "ollama_embed_model", label: "Embed model", type: "model-picker", allowBlank: true,
    hint: "Blank turns correlation off." },
  { key: "ollama_enabled", label: "Enabled", type: "checkbox",
    hint: "Off skips extraction and correlation. Documents are still read and stored." },
  { key: "ollama_timeout_seconds", label: "Timeout (seconds)", type: "number",
    hint: "How long to wait for one Ollama call." },
];

// Sentinel <option> value meaning "I want to type something that isn't in the
// list." Kept as an unlikely literal rather than "" so it can't collide with
// the legitimately-empty embed model value.
const MODEL_CUSTOM_VALUE = "__custom__";

let ollamaSettingsData = null;
let ollamaModelData = null;   // {base_url, reachable, models[]} from the last lookup
let pullPollTimer = null;

async function loadOllamaSettings() {
  const el = document.getElementById("ollama-settings-panel");
  if (!el) return;
  el.innerHTML = '<p class="empty-state">Loading…</p>';
  try {
    ollamaSettingsData = await api("/api/admin/ollama-settings");
    // Ask the host what it has before the first render, so the fields come up
    // as populated pickers rather than visibly turning into them a moment
    // later. A failure here is not fatal — renderOllamaSettingsPanel falls
    // back to plain text inputs, which is exactly the old behavior.
    ollamaModelData = await fetchOllamaModels(ollamaSettingsData.fields.ollama_base_url.value);
    renderOllamaSettingsPanel(ollamaSettingsData);
  } catch (err) {
    el.innerHTML = `<p class="empty-state">${escapeHtml(err.message)}</p>`;
  }
}

async function fetchOllamaModels(baseUrl) {
  try {
    const params = baseUrl ? "?" + new URLSearchParams({ base_url: baseUrl }).toString() : "";
    return await api("/api/admin/ollama-models" + params);
  } catch (e) {
    return { base_url: baseUrl || "", reachable: false, models: [], error: e.message };
  }
}

function modelIsInstalled(name) {
  if (!ollamaModelData || !ollamaModelData.reachable || !name) return null;
  return ollamaModelData.models.some((m) => m.name === name || m.name === name + ":latest");
}

function formatModelOption(m) {
  const bits = [m.parameter_size, m.quantization].filter(Boolean).join(", ");
  const size = m.size_bytes ? formatFileSize(m.size_bytes) : "";
  const meta = [bits, size].filter(Boolean).join(" · ");
  return meta ? `${m.name}  (${meta})` : m.name;
}

// A <select> of what's installed, plus a "custom" escape hatch. The escape
// hatch matters: an admin may legitimately want to name a model they're about
// to pull, or point at a host that's temporarily down, and a picker that made
// those impossible would be a downgrade from the free-text field it replaced.
function modelPickerHtml(f, currentValue) {
  const models = (ollamaModelData && ollamaModelData.models) || [];
  const known = models.some((m) => m.name === currentValue);
  const isCustom = Boolean(currentValue) && !known;

  const options = [
    // Blank means different things for the two optional models — "no
    // correlation at all" for the embed model, "use the assistant's" for
    // extraction — and a picker that said "disabled" for both would be wrong
    // half the time.
    f.allowBlank ? `<option value=""${currentValue === "" ? " selected" : ""}>${escapeHtml(f.blankLabel || "(none — disabled)")}</option>` : "",
    ...models.map((m) =>
      `<option value="${escapeHtml(m.name)}"${m.name === currentValue ? " selected" : ""}>${escapeHtml(formatModelOption(m))}</option>`),
    `<option value="${MODEL_CUSTOM_VALUE}"${isCustom ? " selected" : ""}>Type a model name…</option>`,
  ].join("");

  return `
    <select data-setting="${f.key}" data-model-picker="${f.key}">${options}</select>
    <input type="text" data-model-custom="${f.key}" placeholder="e.g. llama3.1:8b"
           value="${escapeHtml(isCustom ? currentValue : "")}" ${isCustom ? "" : "hidden"}>
  `;
}

function ollamaConnectionHtml() {
  if (!ollamaModelData) return "";
  if (ollamaModelData.reachable) {
    const n = ollamaModelData.models.length;
    return `<span class="conn-status conn-ok">Connected${
      n ? ` — ${n} model${n === 1 ? "" : "s"} installed` : " — no models installed yet"}</span>`;
  }
  return `<span class="conn-status conn-bad">Can't reach ${escapeHtml(ollamaModelData.base_url || "that host")}</span>`;
}

// Only ever a warning, never a blocker: the model genuinely might be pulled a
// minute from now, and refusing to save a value the host doesn't have yet
// would make the "set it up before you pull it" order of operations
// impossible.
function missingModelWarningHtml(data) {
  if (!ollamaModelData || !ollamaModelData.reachable) return "";
  // Three fields, three jobs, and each can be missing on its own — naming the
  // wrong job next to a model name sends someone to pull the wrong thing.
  // Extraction falls back to the assistant's model when blank, so a blank
  // extraction field is not a problem; a named-but-absent one is.
  const jobs = [
    ["ollama_model", "the AI Assistant and link proposals"],
    ["ollama_extract_model", "reading uploaded documents"],
    ["ollama_embed_model", "correlation"],
  ];
  const problems = [];
  const seen = new Set();
  for (const [key, job] of jobs) {
    const name = data.fields[key] && data.fields[key].value;
    if (!name || seen.has(name) || modelIsInstalled(name) !== false) continue;
    seen.add(name);
    problems.push(`<strong>${escapeHtml(name)}</strong> (${escapeHtml(job)})`);
  }
  if (!problems.length) return "";
  const list = problems.length === 1
    ? problems[0]
    : problems.slice(0, -1).join(", ") + " and " + problems[problems.length - 1];
  return `
    <div class="warn-banner">
      Not installed on this Ollama host: ${list}.
      Until ${problems.length === 1 ? "it is" : "they are"}, those features fail silently —
      pick an installed model above, or pull ${problems.length === 1 ? "it" : "them"} below.
    </div>
  `;
}

function renderOllamaSettingsPanel(data) {
  const el = document.getElementById("ollama-settings-panel");
  const rows = OLLAMA_SETTINGS_FIELDS.map((f) => {
    const field = data.fields[f.key];
    const badge = field.overridden
      ? '<span class="status-pill">override saved</span>'
      : `<span class="card-meta">using .env default${f.type === "checkbox" ? "" : ": " + escapeHtml(String(field.env_default))}</span>`;
    let input;
    if (f.type === "checkbox") {
      input = `<input type="checkbox" data-setting="${f.key}" ${field.value ? "checked" : ""}>`;
    } else if (f.type === "model-picker" && ollamaModelData && ollamaModelData.reachable) {
      input = modelPickerHtml(f, String(field.value == null ? "" : field.value));
    } else {
      // Unreachable host (or a lookup that failed): fall straight back to the
      // free-text field, so the page is never *less* usable than before the
      // picker existed.
      input = `<input type="${f.type === "model-picker" ? "text" : f.type}" data-setting="${f.key}" value="${escapeHtml(String(field.value))}"${f.type === "number" ? ' step="1" min="1"' : ""}>`;
    }
    const extra = f.key === "ollama_base_url"
      ? `<div class="conn-row">${ollamaConnectionHtml()}<button type="button" class="btn-link btn-sm" id="ollama-refresh-models">Check connection &amp; refresh models</button></div>`
      : "";
    return `
      <div class="form-row">
        <label>${escapeHtml(f.label)} ${badge}</label>
        ${input}
        ${extra}
        <p class="field-hint">${escapeHtml(f.hint)}</p>
        ${field.overridden ? `<button type="button" class="btn-link btn-sm" data-reset="${f.key}">Reset to .env default</button>` : ""}
      </div>
    `;
  }).join("");

  const updatedNote = data.updated_at
    ? `<p class="card-meta">Last changed ${timeAgoHtml(data.updated_at)}${data.updated_by ? " by user #" + data.updated_by : ""}.</p>`
    : "";

  el.innerHTML = `
    ${missingModelWarningHtml(data)}
    <form id="ollama-settings-form">
      ${rows}
      ${updatedNote}
      <div class="modal-actions" style="justify-content:flex-start;">
        <button type="submit" class="btn-primary">Save</button>
      </div>
      <p class="form-error" id="ollama-settings-error"></p>
    </form>
    <div class="pull-box">
      <h4>Pull a model</h4>
      <p class="field-hint">Downloads a model onto the Ollama host. Large models take a while; it keeps going if you leave the page.</p>
      <div class="pull-row">
        <input type="text" id="ollama-pull-model" placeholder="llama3.1:8b" autocomplete="off">
        <button type="button" class="btn-secondary" id="ollama-pull-btn">Pull</button>
      </div>
      <div class="pull-suggestions">
        Common choices:
        <button type="button" class="btn-link btn-sm" data-suggest="llama3.1:8b">llama3.1:8b</button>
        <button type="button" class="btn-link btn-sm" data-suggest="mistral:7b">mistral:7b</button>
        <button type="button" class="btn-link btn-sm" data-suggest="nomic-embed-text">nomic-embed-text</button>
        <span class="card-meta">(the last one is an embedding model, for correlation)</span>
      </div>
      <div id="ollama-pull-progress" class="pull-progress" hidden></div>
    </div>
  `;

  el.querySelectorAll("[data-reset]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      try {
        await api("/api/admin/ollama-settings", { method: "PATCH", body: { [btn.dataset.reset]: null } });
        showToast("Reset to .env default");
        loadOllamaSettings();
      } catch (err) {
        showToast(err.message, true);
      }
    });
  });

  // --- model picker: reveal the free-text box when "custom" is chosen ---
  el.querySelectorAll("[data-model-picker]").forEach((select) => {
    select.addEventListener("change", () => {
      const custom = el.querySelector(`[data-model-custom="${select.dataset.modelPicker}"]`);
      const isCustom = select.value === MODEL_CUSTOM_VALUE;
      if (custom) {
        custom.hidden = !isCustom;
        if (isCustom) custom.focus();
      }
    });
  });

  // --- re-check the host using whatever URL is currently typed, saved or not ---
  const refreshBtn = document.getElementById("ollama-refresh-models");
  if (refreshBtn) {
    refreshBtn.addEventListener("click", async () => {
      refreshBtn.disabled = true;
      refreshBtn.textContent = "Checking…";
      await reloadOllamaModels();
      showToast(ollamaModelData.reachable
        ? `Found ${ollamaModelData.models.length} model(s)`
        : "Couldn't reach that Ollama host", !ollamaModelData.reachable);
    });
  }

  document.getElementById("ollama-settings-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    // Only fields that actually changed from what's currently in effect go
    // into the PATCH body — matches the server's "present (even if null) =
    // apply, absent = leave alone" semantics (see api/settings.py), and
    // means clicking Save with nothing edited is a genuine no-op rather
    // than re-saving every field's current value as a fresh override.
    const body = {};
    OLLAMA_SETTINGS_FIELDS.forEach((f) => {
      const newVal = readOllamaFieldValue(f);
      if (newVal === undefined) return;
      if (newVal !== data.fields[f.key].value) body[f.key] = newVal;
    });
    if (!Object.keys(body).length) { showToast("No changes to save"); return; }
    try {
      await api("/api/admin/ollama-settings", { method: "PATCH", body });
      showToast("Ollama settings saved — the worker picks this up within about 20 seconds");
      loadOllamaSettings();
    } catch (err) {
      document.getElementById("ollama-settings-error").textContent = err.message;
    }
  });

  wireModelPullControls();
}

/* --- reading and restoring the form across re-renders ---------------------
 * The panel re-renders whenever the model list changes (refresh, or a pull
 * finishing). Re-fetching the saved settings at that point would silently
 * discard edits the admin hasn't saved yet, so the values on screen are
 * captured first and put back afterward.
 * ------------------------------------------------------------------------ */

function readOllamaFieldValue(f) {
  const input = document.querySelector(`[data-setting="${f.key}"]`);
  if (!input) return undefined;
  if (f.type === "checkbox") return input.checked;
  if (f.type === "number") return input.value === "" ? null : parseInt(input.value, 10);
  if (f.type === "model-picker" && input.tagName === "SELECT") {
    if (input.value !== MODEL_CUSTOM_VALUE) return input.value;
    const custom = document.querySelector(`[data-model-custom="${f.key}"]`);
    return custom ? custom.value.trim() : "";
  }
  return input.value;
}

function collectOllamaFormValues() {
  const out = {};
  OLLAMA_SETTINGS_FIELDS.forEach((f) => { out[f.key] = readOllamaFieldValue(f); });
  return out;
}

function applyOllamaFormValues(values) {
  OLLAMA_SETTINGS_FIELDS.forEach((f) => {
    const value = values[f.key];
    if (value === undefined) return;
    const input = document.querySelector(`[data-setting="${f.key}"]`);
    if (!input) return;
    if (f.type === "checkbox") { input.checked = !!value; return; }
    if (f.type === "model-picker" && input.tagName === "SELECT") {
      const known = Array.from(input.options).some((o) => o.value === value);
      if (known) { input.value = value; return; }
      input.value = MODEL_CUSTOM_VALUE;
      const custom = document.querySelector(`[data-model-custom="${f.key}"]`);
      if (custom) { custom.value = value == null ? "" : value; custom.hidden = false; }
      return;
    }
    input.value = value == null ? "" : value;
  });
}

async function reloadOllamaModels() {
  const urlInput = document.querySelector('[data-setting="ollama_base_url"]');
  const pending = collectOllamaFormValues();
  ollamaModelData = await fetchOllamaModels(urlInput ? urlInput.value.trim() : "");
  renderOllamaSettingsPanel(ollamaSettingsData);
  applyOllamaFormValues(pending);
}

/* --- pulling a model ---------------------------------------------------- */

// Tracks which completion has already been acted on. Without it, the
// re-render that follows a successful pull would re-run wireModelPullControls
// -> refreshPullStatus -> "it succeeded!" -> re-render, forever.
let lastHandledPullFinish = null;
// Whether this page session has looked at the pull state at least once. A
// pull that finished before the page was even opened is history, not news:
// without this, every visit to the Admin page would re-announce a pull from
// hours ago and re-render the panel on top of whatever was being edited.
let pullStatusPrimed = false;

function stopPullPolling() {
  if (pullPollTimer) { clearInterval(pullPollTimer); pullPollTimer = null; }
}

function wireModelPullControls() {
  const btn = document.getElementById("ollama-pull-btn");
  const input = document.getElementById("ollama-pull-model");
  if (!btn || !input) return;

  document.querySelectorAll("[data-suggest]").forEach((s) => {
    s.addEventListener("click", () => { input.value = s.dataset.suggest; input.focus(); });
  });

  btn.addEventListener("click", async () => {
    const model = input.value.trim();
    if (!model) { showToast("Enter a model name to pull", true); return; }
    const urlInput = document.querySelector('[data-setting="ollama_base_url"]');
    try {
      await api("/api/admin/ollama-pull", {
        method: "POST",
        body: { model, base_url: urlInput ? urlInput.value.trim() : null },
      });
      lastHandledPullFinish = null; // this is a new pull; its completion is worth acting on
      refreshPullStatus();
    } catch (err) {
      showToast(err.message, true);
    }
  });

  // Picks up a pull that's already running — the download survives a page
  // reload (it's happening on the server), so the progress display should too.
  refreshPullStatus();
}

async function refreshPullStatus() {
  const box = document.getElementById("ollama-pull-progress");
  if (!box) { stopPullPolling(); return; }
  let state;
  try {
    state = await api("/api/admin/ollama-pull-status");
  } catch (e) {
    return; // a transient failure shouldn't kill the polling loop
  }
  if (!state.model) { pullStatusPrimed = true; box.hidden = true; return; }
  renderPullProgress(box, state);

  if (!pullStatusPrimed) {
    pullStatusPrimed = true;
    // Already finished when we first looked — show it, but don't treat it as
    // something that just happened.
    if (!state.active) lastHandledPullFinish = state.finished_at;
  }

  if (state.active) {
    if (!pullPollTimer) pullPollTimer = setInterval(refreshPullStatus, 1500);
    return;
  }
  stopPullPolling();

  if (state.finished_at && state.finished_at !== lastHandledPullFinish) {
    lastHandledPullFinish = state.finished_at;
    if (state.ok) {
      showToast(`Pulled ${state.model}`);
      await reloadOllamaModels(); // so the new model appears in the pickers immediately
    }
  }
}

function renderPullProgress(box, state) {
  box.hidden = false;
  const pct = typeof state.percent === "number" ? state.percent : null;
  let line;
  if (state.active) {
    line = `Pulling ${escapeHtml(state.model)} — ${escapeHtml(state.status || "working")}${pct === null ? "" : ` (${pct}%)`}`;
  } else if (state.ok) {
    line = `Pulled ${escapeHtml(state.model)} successfully.`;
  } else {
    line = `Pull of ${escapeHtml(state.model)} failed: ${escapeHtml(state.error || "unknown error")}`;
  }
  box.className = "pull-progress" + (state.active ? "" : state.ok ? " ok" : " error");
  const bar = pct === null
    ? '<div class="pull-bar indeterminate"><div></div></div>'
    : `<div class="pull-bar"><div style="width:${pct}%"></div></div>`;
  box.innerHTML = `<div class="pull-status">${line}</div>${state.active ? bar : ""}`;
}

/* ============================================================================
 * Audit Log (Admin page)
 * ========================================================================== */

const AUDIT_PAGE_SIZE = 50;
let auditOffset = 0;

// Plain-language labels. The stored action codes are stable and greppable
// (that's what a machine reading the syslog stream wants); this is what a
// person reading the page wants.
const AUDIT_ACTION_LABELS = {
  "auth.login": "Logged in",
  "auth.login_failed": "Failed login",
  "auth.logout": "Logged out",
  "user.create": "Created account",
  "user.update": "Changed account",
  "entity.create": "Created entity",
  "entity.update": "Edited entity",
  "entity.archive": "Archived entity",
  "entity.reactivate": "Reactivated entity",
  "entity.retry_geocode": "Retried geocode",
  "entity.bulk_import": "Bulk-imported entities",
  "relationship.create": "Created relationship",
  "relationship.update": "Edited relationship",
  "relationship.delete": "Deleted relationship",
  "report.create": "Created report",
  "report.update": "Edited report",
  "report.confirm": "Confirmed report",
  "report.export_pdf": "Exported report as PDF",
  "attachment.upload": "Uploaded attachment",
  "attachment.download": "Downloaded attachment",
  "attachment.delete": "Deleted attachment",
  "document.paste": "Pasted a document",
  "document.update": "Renamed a document",
  "document.file_under": "Filed a document",
  "document.archive": "Archived a document",
  "document.restore": "Restored a document",
  "document.re_extract": "Re-ran extraction",
  "extraction.accept": "Accepted suggestion",
  "extraction.reject": "Rejected suggestion",
  "suggestion.propose": "Asked for proposed links",
  "branding.update": "Changed branding",
  "correlation.confirmed": "Confirmed correlation",
  "correlation.dismissed": "Dismissed correlation",
  "rss_feed.create": "Added RSS feed",
  "rss_feed.update": "Changed RSS feed",
  "rss_feed.delete": "Deleted RSS feed",
  "settings.update": "Changed Ollama settings",
  "ollama.pull": "Pulled a model",
  "backup.download": "Downloaded a backup",
  "backup.restore": "Restored from backup",
  "assistant.query": "Asked the AI Assistant",
  "assistant.clear": "Cleared assistant history",
  "audit.export": "Exported the audit log",
};

// Actions where the interesting thing is that data left the system. Called
// out visually so a page of routine edits doesn't bury the one download.
const AUDIT_EGRESS_ACTIONS = new Set([
  "attachment.download", "report.export_pdf", "backup.download", "audit.export", "assistant.query",
]);

function auditFilterParams() {
  const params = new URLSearchParams();
  const actor = document.getElementById("audit-filter-actor").value;
  const action = document.getElementById("audit-filter-action").value;
  const objectType = document.getElementById("audit-filter-object").value;
  const outcome = document.getElementById("audit-filter-outcome").value;
  const q = document.getElementById("audit-filter-q").value.trim();
  if (actor) params.set("actor", actor);
  if (action) params.set("action", action);
  if (objectType) params.set("object_type", objectType);
  if (outcome) params.set("outcome", outcome);
  if (q) params.set("q", q);
  return params;
}

async function loadAuditFilterOptions() {
  try {
    const data = await api("/api/admin/audit-log/actions");
    const fill = (id, values, labeller) => {
      const sel = document.getElementById(id);
      const current = sel.value;
      const first = sel.options[0].outerHTML;
      sel.innerHTML = first + values.map((v) =>
        `<option value="${escapeHtml(v)}">${escapeHtml(labeller ? labeller(v) : v)}</option>`).join("");
      sel.value = current;
    };
    fill("audit-filter-actor", data.actors);
    fill("audit-filter-action", data.actions, (a) => AUDIT_ACTION_LABELS[a] || a);
    fill("audit-filter-object", data.object_types);
    renderSyslogStatus(data.syslog);
  } catch (e) { /* the log itself still loads without the dropdowns */ }
}

function renderSyslogStatus(syslog) {
  const el = document.getElementById("audit-syslog-status");
  if (!el || !syslog) return;
  if (!syslog.enabled) {
    el.className = "audit-syslog";
    el.innerHTML = 'Syslog forwarding is <strong>off</strong>. Set <code>AUDIT_SYSLOG_ENABLED</code> in .env to send a copy elsewhere.';
    return;
  }
  if (!syslog.active) {
    el.className = "audit-syslog audit-syslog-bad";
    el.innerHTML = 'Syslog forwarding is switched on but <strong>could not start</strong> — check the api container '
      + 'logs. Entries are still being written to the database.';
    return;
  }
  el.className = "audit-syslog audit-syslog-ok";
  el.innerHTML = `Forwarding to <strong>${escapeHtml(syslog.host)}:${syslog.port}</strong> `
    + `over ${escapeHtml(syslog.protocol.toUpperCase())} (facility ${escapeHtml(syslog.facility)})`
    + (syslog.dropped ? ` — <strong>${syslog.dropped}</strong> event(s) dropped because the collector wasn't keeping up; all are still in the database.` : ".");
}

async function loadAuditLog() {
  const el = document.getElementById("audit-log-list");
  if (!el) return;
  el.innerHTML = '<p class="empty-state">Loading…</p>';
  const params = auditFilterParams();
  params.set("limit", String(AUDIT_PAGE_SIZE));
  params.set("offset", String(auditOffset));
  try {
    const data = await api("/api/admin/audit-log?" + params.toString());
    renderAuditLog(data);
  } catch (err) {
    el.innerHTML = `<p class="empty-state">${escapeHtml(err.message)}</p>`;
  }
}

function auditDetailSummary(entry) {
  const d = entry.detail;
  if (!d || typeof d !== "object") return "";
  const bits = [];
  if (d.entity_type) bits.push(escapeHtml(d.entity_type));
  if (Array.isArray(d.fields) && d.fields.length) bits.push("changed: " + escapeHtml(d.fields.join(", ")));
  if (d.changes && typeof d.changes === "object") {
    bits.push(Object.entries(d.changes).map(([k, v]) => `${escapeHtml(k)} → ${escapeHtml(String(v))}`).join(", "));
  }
  if (typeof d.created === "number") bits.push(`${d.created} created, ${d.errors} failed`);
  if (typeof d.retrieved_count === "number") bits.push(`${d.retrieved_count} record(s) retrieved`);
  if (typeof d.rows === "number") bits.push(`${d.rows} row(s)`);
  if (Array.isArray(d.entities_included)) bits.push(`${d.entities_included.length} entity dossier(s) included`);
  if (typeof d.size_bytes === "number") bits.push(formatFileSize(d.size_bytes));
  if (d.reason) bits.push(escapeHtml(d.reason));
  if (d.inline === true) bits.push("previewed in-app");
  if (d.source === "rss") bits.push("via RSS" + (d.feed ? `: ${escapeHtml(d.feed)}` : ""));
  return bits.join(" · ");
}

function renderAuditLog(data) {
  const el = document.getElementById("audit-log-list");
  if (!data.items.length) {
    el.innerHTML = '<div class="empty-state">Nothing recorded matching these filters.</div>';
  } else {
    el.innerHTML = data.items.map((entry) => {
      const label = AUDIT_ACTION_LABELS[entry.action] || entry.action;
      const actor = entry.actor_kind === "system"
        ? '<span class="audit-actor audit-actor-system">system</span>'
        : `<span class="audit-actor">${escapeHtml(entry.actor_username || "unknown")}</span>`;
      const target = entry.object_label
        ? `<span class="audit-target">${escapeHtml(truncate(entry.object_label, 70))}</span>`
        : (entry.object_id ? `<span class="audit-target">${escapeHtml(entry.object_id)}</span>` : "");
      const detail = auditDetailSummary(entry);
      const classes = ["audit-row"];
      if (entry.outcome !== "success") classes.push("audit-row-" + entry.outcome);
      if (AUDIT_EGRESS_ACTIONS.has(entry.action)) classes.push("audit-row-egress");
      return `
        <div class="${classes.join(" ")}">
          <div class="audit-when">${timeAgoHtml(entry.occurred_at)}</div>
          <div class="audit-main">
            <div>${actor} <span class="audit-action">${escapeHtml(label)}</span> ${target}</div>
            ${detail ? `<div class="card-meta">${detail}</div>` : ""}
          </div>
          <div class="audit-meta">
            ${entry.outcome !== "success" ? `<span class="audit-outcome">${escapeHtml(entry.outcome)}</span>` : ""}
            ${entry.ip_address ? `<span class="card-meta">${escapeHtml(entry.ip_address)}</span>` : ""}
          </div>
        </div>
      `;
    }).join("");
  }

  const from = data.total ? auditOffset + 1 : 0;
  const to = Math.min(auditOffset + data.items.length, data.total);
  document.getElementById("audit-range").textContent = `${from}–${to} of ${data.total}`;
  document.getElementById("audit-prev").disabled = auditOffset === 0;
  document.getElementById("audit-next").disabled = to >= data.total;
}

["audit-filter-actor", "audit-filter-action", "audit-filter-object", "audit-filter-outcome"].forEach((id) => {
  document.getElementById(id).addEventListener("change", () => { auditOffset = 0; loadAuditLog(); });
});
document.getElementById("audit-filter-q").addEventListener("input", debounce(() => {
  auditOffset = 0;
  loadAuditLog();
}, 300));
document.getElementById("audit-refresh-btn").addEventListener("click", () => { loadAuditFilterOptions(); loadAuditLog(); });
document.getElementById("audit-prev").addEventListener("click", () => {
  auditOffset = Math.max(0, auditOffset - AUDIT_PAGE_SIZE);
  loadAuditLog();
});
document.getElementById("audit-next").addEventListener("click", () => {
  auditOffset += AUDIT_PAGE_SIZE;
  loadAuditLog();
});
document.getElementById("audit-export-btn").addEventListener("click", () => {
  showToast("Building the CSV — the download will start shortly");
  window.location.href = "/api/admin/audit-log.csv?" + auditFilterParams().toString();
});

// The audit log has its own page, but it is still an administrator's tool, so
// Admin settings keeps a pointer to it. The way back is the shared .back-link
// handler above, driven by data-back — giving this button its own click
// handler as well would fire switchView twice, once with an undefined view.
document.getElementById("admin-open-audit").addEventListener("click", () => switchView("audit"));

/* ============================================================================
 * Backup & Restore (Admin page)
 *
 * Download is a plain navigation rather than a fetch-to-blob: a backup of a
 * case with real attachments in it can be hundreds of megabytes, and letting
 * the browser stream it straight to disk gives a native download with real
 * progress instead of buffering the whole thing in a Blob first. The button
 * only renders for admins, so the 403 case this gives up handling isn't
 * reachable in practice.
 * ========================================================================== */

document.getElementById("backup-download-btn").addEventListener("click", () => {
  showToast("Building your backup — the download will start shortly");
  window.location.href = "/api/admin/backup";
});

const RESTORE_CONFIRM_PHRASE = "REPLACE ALL DATA"; // must match api/backup.py

document.getElementById("restore-start-btn").addEventListener("click", () => {
  const input = document.getElementById("restore-file-input");
  const resultEl = document.getElementById("restore-result");
  const file = input.files && input.files[0];
  if (!file) {
    resultEl.hidden = false;
    resultEl.className = "restore-result error";
    resultEl.textContent = "Choose a backup .zip first.";
    return;
  }
  resultEl.hidden = true;
  openRestoreConfirm(file);
});

function openRestoreConfirm(file) {
  const sizeMb = (file.size / (1024 * 1024)).toFixed(1);
  openModal(`
    <h2>Restore from backup?</h2>
    <p class="restore-warn">This replaces <strong>all</strong> data in this app with <strong>${escapeHtml(file.name)}</strong> (${sizeMb} MB), including user accounts. There's no undo.</p>
    <p class="restore-warn">Everyone is logged out afterwards. Sign back in with an account from the backup.</p>
    <form id="restore-form">
      <div class="form-row">
        <label>Type <code>${RESTORE_CONFIRM_PHRASE}</code> to confirm</label>
        <input type="text" id="restore-confirm-input" autocomplete="off" spellcheck="false" required>
      </div>
      <p class="form-error" id="restore-modal-error"></p>
      <div class="modal-actions">
        <button type="button" class="btn-secondary" id="restore-cancel">Cancel</button>
        <button type="submit" class="btn-danger" id="restore-submit">Restore</button>
      </div>
    </form>
  `);

  document.getElementById("restore-cancel").addEventListener("click", closeModal);
  document.getElementById("restore-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const phrase = document.getElementById("restore-confirm-input").value.trim();
    const errorEl = document.getElementById("restore-modal-error");
    if (phrase !== RESTORE_CONFIRM_PHRASE) {
      errorEl.textContent = `Type ${RESTORE_CONFIRM_PHRASE} exactly to confirm.`;
      return;
    }
    const cancelBtn = document.getElementById("restore-cancel");
    const submitBtn = document.getElementById("restore-submit");
    cancelBtn.disabled = true;
    submitBtn.disabled = true;
    submitBtn.textContent = "Restoring…";
    errorEl.textContent = "Uploading and restoring — don't close this tab.";

    const fd = new FormData();
    fd.append("file", file);
    fd.append("confirm", phrase);
    try {
      const result = await apiUpload("/api/admin/restore", fd);
      // Drop the local user BEFORE anything else can fire: the server just
      // truncated the sessions table, so the next background call (the 30s
      // queue-badge refresh) would 401 and yank this modal away behind a
      // "your session has expired" screen while the summary is still being
      // read. handleSessionExpired() is gated on state.user, so clearing it
      // here keeps the summary on screen until the admin chooses to move on.
      state.user = null;
      renderRestoreSummary(result);
    } catch (err) {
      cancelBtn.disabled = false;
      submitBtn.disabled = false;
      submitBtn.textContent = "Restore";
      errorEl.textContent = err.message;
    }
  });
}

function renderRestoreSummary(result) {
  const counts = Object.entries(result.row_counts || {})
    .filter(([, n]) => n > 0)
    .sort((a, b) => b[1] - a[1])
    .map(([table, n]) => `<li>${escapeHtml(table)}: ${n}</li>`)
    .join("");
  const emptied = (result.tables_emptied_not_in_backup || []).length
    ? `<p class="field-hint">Not present in this backup, so left empty: ${
        escapeHtml((result.tables_emptied_not_in_backup || []).join(", "))}.</p>`
    : "";
  openModal(`
    <h2>Restore complete</h2>
    <p>Restored from a backup taken ${result.backup_created_at ? escapeHtml(new Date(result.backup_created_at).toLocaleString()) : "at an unknown time"}.</p>
    <ul class="restore-summary">${counts || "<li>No rows in that backup.</li>"}</ul>
    <p class="field-hint">Attachments restored: ${result.attachment_files_restored}. Unused files removed: ${result.unreferenced_files_removed}.</p>
    ${emptied}
    <p class="restore-warn">You've been logged out — log back in with an account from the restored backup.</p>
    <div class="modal-actions">
      <button type="button" class="btn-primary" id="restore-relogin">Log in again</button>
    </div>
  `);
  document.getElementById("restore-relogin").addEventListener("click", () => location.reload());
}

/* ============================================================================
 * AI Assistant: case-data-aware chat, app-wide expandable side panel.
 * Reachable from every view (see index.html's placement of this markup
 * outside #app) -- deliberately independent of switchView()/state.currentEntityId
 * so opening it never disturbs whatever view is on screen underneath.
 * ========================================================================== */

const ASSISTANT_EMPTY_HINT_HTML =
  '<p class="assistant-empty-hint" id="assistant-empty-hint">Ask about anything in this case file, ' + 'e.g. “what do we know about John Smith”. Answers come only from your case data.</p>';

// The assistant panel is anchored below the topbar so the nav and account
// menu stay reachable while it's open (see styles.css). The topbar's height
// isn't fixed — it wraps to a second row on a narrow window — so it's
// measured rather than hardcoded, and re-measured whenever the window
// changes size.
function syncTopbarHeight() {
  const topbar = document.querySelector(".topbar");
  if (topbar && topbar.offsetHeight) {
    document.documentElement.style.setProperty("--topbar-h", topbar.offsetHeight + "px");
  }
}
window.addEventListener("resize", debounce(syncTopbarHeight, 100));

function toggleAssistantPanel(open) {
  const panel = document.getElementById("assistant-panel");
  const tab = document.getElementById("assistant-tab-btn");
  const shouldOpen = open === undefined ? panel.hidden : open;
  if (shouldOpen) syncTopbarHeight();
  panel.hidden = !shouldOpen;
  tab.classList.toggle("panel-open", shouldOpen);
  if (shouldOpen) {
    if (!state.assistantLoaded) loadAssistantMessages();
    document.getElementById("assistant-input").focus();
  }
}
document.getElementById("assistant-tab-btn").addEventListener("click", () => toggleAssistantPanel(true));
document.getElementById("assistant-close-btn").addEventListener("click", () => toggleAssistantPanel(false));

function assistantMessageHtml(msg) {
  const isUser = msg.role === "user";
  const bodyHtml = isUser ? escapeHtml(msg.content) : renderMarkdown(msg.content);
  const citationsHtml = (msg.citations || []).map((c) =>
    `<span class="assistant-citation" data-kind="${escapeHtml(c.kind)}" data-id="${escapeHtml(c.id)}">${escapeHtml(c.label)}</span>`
  ).join("");
  return `
    <div class="assistant-msg assistant-msg-${isUser ? "user" : "assistant"}">
      ${bodyHtml}
      ${citationsHtml ? `<div class="assistant-msg-citations">${citationsHtml}</div>` : ""}
    </div>
  `;
}

// `mousedown` isn't needed here the way the global search dropdown needs it
// (these citations aren't inside a blur-on-focus-loss input), so a plain
// `click` is fine.
function wireAssistantCitations(container) {
  container.querySelectorAll(".assistant-citation").forEach((el) => {
    el.addEventListener("click", () => {
      toggleAssistantPanel(false);
      if (el.dataset.kind === "entity") openEntityDetail(el.dataset.id);
      else openReportDetail(el.dataset.id);
    });
  });
}

function appendAssistantMessage(msg) {
  const el = document.getElementById("assistant-messages");
  const hint = document.getElementById("assistant-empty-hint");
  if (hint) hint.remove();
  el.insertAdjacentHTML("beforeend", assistantMessageHtml(msg));
  wireAssistantCitations(el);
  el.scrollTop = el.scrollHeight;
}

async function loadAssistantMessages() {
  const el = document.getElementById("assistant-messages");
  try {
    const data = await api("/api/assistant/messages");
    state.assistantLoaded = true;
    if (!data.items.length) return; // leave the empty-state hint in place
    const hint = document.getElementById("assistant-empty-hint");
    if (hint) hint.remove();
    el.insertAdjacentHTML("beforeend", data.items.map(assistantMessageHtml).join(""));
    wireAssistantCitations(el);
    el.scrollTop = el.scrollHeight;
  } catch (e) { /* leave the hint showing; sending a new question still works */ }
}

document.getElementById("assistant-clear-btn").addEventListener("click", async () => {
  if (!confirm("Clear this conversation? This can't be undone.")) return;
  try {
    await api("/api/assistant/messages", { method: "DELETE" });
    document.getElementById("assistant-messages").innerHTML = ASSISTANT_EMPTY_HINT_HTML;
    showToast("Conversation cleared");
  } catch (err) {
    showToast(err.message, true);
  }
});

// Enter sends (matching an ordinary chat box); Shift+Enter inserts a
// newline for a longer, multi-part question.
document.getElementById("assistant-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    document.getElementById("assistant-form").requestSubmit();
  }
});

document.getElementById("assistant-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = document.getElementById("assistant-input");
  const question = input.value.trim();
  if (!question) return;
  const statusEl = document.getElementById("assistant-status");
  const submitBtn = e.target.querySelector("button[type=submit]");

  appendAssistantMessage({ role: "user", content: question });
  input.value = "";
  input.disabled = true;
  submitBtn.disabled = true;
  statusEl.hidden = false;
  statusEl.classList.remove("error");
  statusEl.textContent = "Thinking…";

  try {
    const reply = await api("/api/assistant/messages", { method: "POST", body: { question } });
    appendAssistantMessage(reply);
    statusEl.hidden = true;
  } catch (err) {
    // The question itself is never persisted server-side when the call
    // fails (see api/assistant.py) -- surfacing the error here without
    // rendering a fake assistant bubble keeps the visible transcript
    // matching exactly what's actually saved.
    statusEl.classList.add("error");
    statusEl.textContent = err.message;
  } finally {
    input.disabled = false;
    submitBtn.disabled = false;
    input.focus();
  }
});

/* ============================================================================ */

document.addEventListener("DOMContentLoaded", init);
