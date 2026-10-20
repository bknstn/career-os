(() => {
  "use strict";

  const API_BASE = new URL("api/", document.baseURI);
  const STORAGE_KEY = "career-radar.saved.v1";
  const root = document.getElementById("app");
  const breadcrumb = document.getElementById("breadcrumb");
  const savedCount = document.getElementById("saved-count");
  const refreshButton = document.getElementById("refresh-button");
  const skipLink = document.querySelector(".skip-link");
  const dateFormatter = new Intl.DateTimeFormat("en-GB", {
    dateStyle: "medium",
    timeStyle: "short",
    timeZone: "Europe/Warsaw",
  });
  let renderSequence = 0;
  let savedJobs = readSavedJobs();
  let showAllSources = false;

  const ISSUE_COPY = {
    source_stale: "Some sources have not had a successful full scan recently.",
    occupation_unknown_above_threshold: "The share of unclassified roles in the base panel exceeds the comparison threshold.",
    occupation_unknown_above_reference: "The share of unclassified roles in the base panel exceeds the reference level.",
    description_missing: "Some postings have no description, so skill recognition is incomplete.",
  };
  const HISTORY_COPY = {
    missing_history: "No eligible immutable observations are available for this period.",
    panel_not_registered: "The source panel has not been registered for historical comparison.",
    panel_source_mismatch: "Historical data does not match the registered source panel.",
    panel_manifest_unavailable: "The registered source panel could not be verified.",
    source_outside_panel: "The selected source is outside the registered historical panel.",
    missing_history_tables: "The saved data does not yet include tables for scan comparisons.",
    missing_history_columns: "The saved data does not yet include the fields needed to verify history.",
    coverage_below_threshold: "Too few comparable scans are available to meet the coverage requirement.",
    board_coverage_below_threshold: "Some sources have too few observations for comparison.",
    occupation_unknown_above_threshold: "Too many roles remain unclassified for a reliable role comparison.",
    specialty_unresolved_above_threshold: "Too many specialties remain unresolved for comparison.",
    description_coverage_below_threshold: "Too few descriptions are available to compare skill mentions.",
    category_sample_below_threshold: "Too few paired observations are available for this category.",
    zero_denominator: "A share cannot be calculated because one period has no comparable postings.",
    panel_identity_mismatch: "The source panel or its settings changed, so the periods are not comparable.",
    history_present: "No paired successful scans are available for this window.",
    overall_coverage: "Overall paired-scan coverage is below the comparison threshold.",
    board_coverage: "Some sources have too few observations for comparison.",
    occupation_unknown_threshold: "Too many roles remain unclassified for a reliable role comparison.",
    specialty_unresolved_threshold: "Too many specialties remain unresolved for comparison.",
    description_coverage_threshold: "Too few descriptions are available to compare skills.",
    sample_threshold: "Too few postings or companies are available in the paired windows for this metric.",
    positive_denominator: "A share comparison requires postings in both periods.",
  };

  function issueMessage(code) {
    return ISSUE_COPY[String(code)] || "Data coverage is limited; see the methodology for details.";
  }

  function historyReasonMessage(reason) {
    return HISTORY_COPY[String(reason)] || "Not enough comparable data is available for this historical comparison.";
  }

  function historyStatusLabel(status, direction = "") {
    if (direction === "uncertain" || status === "uncertain") return "Uncertain";
    if (status === "insufficient") return "Not enough data";
    if (status === "available" || status === "provisional" || status === "sufficient") return "Comparison available";
    return "History unverified";
  }

  function element(tag, className = "", value = undefined) {
    const item = document.createElement(tag);
    if (className) item.className = className;
    if (value !== undefined && value !== null) item.textContent = String(value);
    return item;
  }

  function append(parent, ...children) {
    for (const child of children) if (child) parent.append(child);
    return parent;
  }

  function text(tag, className, value) {
    return element(tag, className, value);
  }

  function readSavedJobs() {
    try {
      const stored = JSON.parse(window.localStorage.getItem(STORAGE_KEY) || "[]");
      if (!Array.isArray(stored)) return [];
      return stored.filter((item) => item && typeof item.id === "string").slice(0, 500);
    } catch {
      return [];
    }
  }

  function persistSavedJobs() {
    try {
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(savedJobs));
    } catch {
      // The current page remains usable if browser storage is disabled or full.
    }
    savedCount.textContent = String(savedJobs.length);
  }

  function isSaved(id) {
    return savedJobs.some((job) => job.id === String(id));
  }

  function saveJob(job) {
    const id = String(job.id || "");
    if (!id) return;
    if (isSaved(id)) {
      savedJobs = savedJobs.filter((item) => item.id !== id);
    } else {
      const saved = {
        id,
        title: String(job.title || "Untitled posting"),
        company_name: String(job.company_name || "Company not specified"),
        source_key: String(job.source_key || ""),
        source_url: String(job.source_url || ""),
        apply_url: String(job.apply_url || ""),
        last_seen_at_utc: String(job.last_seen_at_utc || ""),
        status: String(job.status || ""),
        occupation: job.occupation || null,
        specialty: job.specialty || null,
        skills: Array.isArray(job.skills) ? job.skills.map((skill) => ({
          id: String(skill.id || ""),
          label: String(skill.label || ""),
        })) : [],
        description_excerpt: String(job.description_excerpt || ""),
        saved_at: new Date().toISOString(),
      };
      savedJobs = [saved, ...savedJobs.filter((item) => item.id !== id)].slice(0, 500);
    }
    persistSavedJobs();
    renderRoute();
  }

  function routeFromHash() {
    const value = window.location.hash.replace(/^#/, "") || "/overview";
    const queryAt = value.indexOf("?");
    const path = queryAt < 0 ? value : value.slice(0, queryAt);
    const query = queryAt < 0 ? "" : value.slice(queryAt + 1);
    let segments;
    try {
      segments = path.split("/").filter(Boolean).map((part) => decodeURIComponent(part));
    } catch {
      segments = ["overview"];
    }
    return { segments, params: new URLSearchParams(query) };
  }

  function navigate(path, params = null) {
    const query = params ? params.toString() : "";
    window.location.hash = `/${path}${query ? `?${query}` : ""}`;
  }

  function formatDate(value) {
    if (!value) return "Date unknown";
    const date = new Date(value);
    return Number.isNaN(date.valueOf()) ? "Date unknown" : dateFormatter.format(date);
  }

  function formatNumber(value) {
    if (value === null || value === undefined || !Number.isFinite(Number(value))) return "—";
    return new Intl.NumberFormat("en-GB").format(Number(value));
  }

  function formatPercent(value) {
    if (value === null || value === undefined || !Number.isFinite(Number(value))) return "—";
    const numeric = Number(value);
    const percentage = Math.abs(numeric) <= 1 ? numeric * 100 : numeric;
    return `${new Intl.NumberFormat("en-GB", { maximumFractionDigits: 1 }).format(percentage)}%`;
  }

  const LABELS = {
    technical_engineering: "Engineering roles",
    engineering_management: "Engineering management",
    solutions_architecture: "Solutions architecture",
    nontechnical: "Nontechnical roles",
    unknown: "Unclassified",
    backend_engineering: "Backend engineering",
    frontend_engineering: "Frontend engineering",
    fullstack_engineering: "Full-stack engineering",
    data_engineering: "Data engineering",
    product_engineering: "Product engineering",
    platform_infrastructure_engineering: "Platform and infrastructure",
    machine_learning_engineering: "Machine learning engineering",
    ai_engineering: "AI engineering",
    ai_product_engineering: "AI product engineering",
  };
  const SPECIALTY_STATES = {
    resolved: "Identified",
    missing: "Not identified",
    unsupported: "Outside the current taxonomy",
    ambiguous: "Ambiguous",
    not_assessed: "Not assessed",
    not_applicable: "Not applicable",
  };
  const JOB_STATES = {
    active: "Active in saved snapshot",
    inactive: "Inactive in saved snapshot",
  };

  function displayLabel(value, fallback = "Unclassified") {
    if (value && typeof value === "object") return String(LABELS[value.id] || value.label || value.id || fallback);
    if (value === null || value === undefined || value === "") return fallback;
    return LABELS[String(value)] || String(value);
  }

  function displayState(value, dictionary) {
    if (!value) return "Not specified";
    return dictionary[String(value)] || String(value);
  }

  function baseCoverageDescription(data) {
    const quality = data.quality;
    if (!quality || typeof quality !== "object") return "";
    const found = new Map();
    const visit = (value) => {
      if (!value || typeof value !== "object") return;
      for (const [key, item] of Object.entries(value)) {
        if (typeof item === "number" && key.startsWith("base_")) found.set(key, item);
        else if (item && typeof item === "object") visit(item);
      }
    };
    visit(quality);
    const unknown = found.get("base_unknown_occupation_count") ?? found.get("base_unknown_count");
    const missingDescription = found.get("base_missing_description_count");
    const total = found.get("base_source_cohort_posting_count") ?? found.get("base_posting_count") ?? found.get("base_count");
    const share = found.get("base_unknown_occupation_share") ?? found.get("base_unknown_share");
    const missingShare = found.get("base_missing_description_share");
    if (unknown === undefined && missingDescription === undefined && total === undefined && share === undefined && missingShare === undefined) return "";
    const unknownShare = share !== undefined ? formatPercent(share)
      : total ? formatPercent(unknown === undefined ? null : unknown / total) : "Share unknown";
    const parts = [];
    if (unknown !== undefined || share !== undefined) {
      parts.push(`${unknown === undefined ? "Count unknown" : `${formatNumber(unknown)} unclassified ${unknown === 1 ? "role" : "roles"}`} (${unknownShare})`);
    }
    if (missingDescription !== undefined || missingShare !== undefined) {
      const descriptionShare = missingShare !== undefined ? formatPercent(missingShare)
        : total ? formatPercent(missingDescription / total) : "Share unknown";
      parts.push(`${missingDescription === undefined ? "Count unknown" : `${formatNumber(missingDescription)} without a description`} (${descriptionShare})`);
    }
    const totalText = total === undefined ? "in the base panel" : `out of ${formatNumber(total)} base-panel postings`;
    return `Base panel: ${parts.join("; ")} ${totalText}; result filters do not change this assessment.`;
  }

  function shortDate(value) {
    if (!value) return "Last successful scan unknown";
    return `Scan: ${formatDate(value)}`;
  }

  function safeExternalLink(raw) {
    if (typeof raw !== "string" || !raw.trim()) return null;
    try {
      const url = new URL(raw);
      if ((url.protocol !== "http:" && url.protocol !== "https:") || url.username || url.password) return null;
      return url.href;
    } catch {
      return null;
    }
  }

  function externalAnchor(raw, label, className = "button") {
    const href = safeExternalLink(raw);
    if (!href) return null;
    const link = element("a", className, label);
    link.href = href;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    return link;
  }

  async function getJson(path, params = null) {
    const url = new URL(path, API_BASE);
    if (params) for (const [key, value] of params.entries()) url.searchParams.set(key, value);
    const response = await fetch(url, { headers: { Accept: "application/json" }, cache: "no-store" });
    let payload;
    try {
      payload = await response.json();
    } catch {
      payload = null;
    }
    if (!response.ok) {
      const detail = payload && (payload.detail || payload.message);
      throw new Error(typeof detail === "string" ? detail : `The server returned status ${response.status}.`);
    }
    if (!payload || typeof payload !== "object") throw new Error("The server returned an unexpected response.");
    return payload;
  }

  async function getAllPages(path) {
    const items = [];
    let total = 0;
    let page = 1;
    do {
      const params = new URLSearchParams({ page: String(page), page_size: "100" });
      const response = await getJson(path, params);
      const rows = Array.isArray(response.items) ? response.items : [];
      total = Number(response.total || 0);
      items.push(...rows);
      if (!rows.length || items.length >= total) break;
      page += 1;
    } while (page < 10000);
    if (items.length !== total) throw new Error("The research listing was incomplete; reload to retry.");
    return items;
  }

  function setBusy(busy) {
    root.setAttribute("aria-busy", busy ? "true" : "false");
  }

  function loadingView() {
    root.replaceChildren();
    const state = element("div", "loading-state");
    const inner = element("div");
    append(inner, element("div", "loading-mark"), text("span", "", "Loading the saved snapshot…"));
    state.append(inner);
    root.append(state);
  }

  function errorView(error) {
    root.replaceChildren();
    const wrap = element("section", "error-banner");
    const copy = element("div");
    append(copy,
      text("strong", "", "Could not load data"),
      text("p", "", error instanceof Error ? error.message : "Check the connection to the local app."));
    const retry = element("button", "button button-small", "Retry");
    retry.type = "button";
    retry.addEventListener("click", renderRoute);
    append(wrap, copy, retry);
    root.append(wrap);
  }

  function updateNav(section, title) {
    breadcrumb.replaceChildren(text("span", "", "Career Radar"), text("span", "", "/"), text("span", "", title));
    for (const link of document.querySelectorAll("[data-nav]")) {
      const active = link.dataset.nav === section;
      if (active) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    }
    savedCount.textContent = String(savedJobs.length);
  }

  function pageHeading(kicker, title, description, actions = null) {
    const heading = element("div", "page-heading");
    const copy = element("div");
    const eyebrow = text("p", "eyebrow", kicker);
    const prompt = element("span", "terminal-prompt");
    prompt.setAttribute("aria-hidden", "true");
    append(prompt, text("span", "", ">"), text("span", "terminal-cursor", "_"));
    eyebrow.prepend(prompt);
    append(copy, eyebrow, text("h1", "", title));
    if (description) copy.append(text("p", "", description));
    heading.append(copy);
    if (actions) heading.append(actions);
    return heading;
  }

  function actionGroup(...buttons) {
    const actions = element("div", "heading-actions");
    append(actions, ...buttons.filter(Boolean));
    return actions;
  }

  function snapshotBanner(data) {
    const quality = data.quality || {};
    const warning = quality.state && !["ok", "fresh", "complete", "good", "ready"].includes(String(quality.state).toLowerCase());
    const banner = element("section", `snapshot-banner${warning ? " warning" : ""}`);
    const copy = element("div", "snapshot-copy");
    append(copy,
      text("span", "snapshot-symbol", warning ? "!" : "↗"),
      element("div")
    );
    const copyBody = copy.lastChild;
    copyBody.append(text("div", "snapshot-title", "Saved job snapshot"));
    const asOf = data.data_as_of_utc ? formatDate(data.data_as_of_utc) : "Snapshot date unknown";
    const qualityNote = Array.isArray(quality.issues) && quality.issues.length
      ? [...new Set(quality.issues.map(issueMessage))].join(" · ")
      : "Active status reflects the last saved state; the posting may have since closed.";
    const baseCoverage = baseCoverageDescription(data);
    copyBody.append(text("div", "snapshot-description", `Snapshot as of ${asOf}. ${qualityNote}${baseCoverage ? ` ${baseCoverage}` : ""}`));
    const sources = Array.isArray(data.sources) ? data.sources : [];
    const staleCount = Number(quality.stale_source_count || sources.filter((source) => source.stale).length || 0);
    const meta = text("div", "snapshot-meta", staleCount > 0 ? `${formatNumber(staleCount)} ${staleCount === 1 ? "source needs" : "sources need"} attention` : `Generated ${formatDate(data.generated_at_utc)}`);
    append(banner, copy, meta);
    return banner;
  }

  function statCard(label, value, caption, icon = "•") {
    const card = element("section", "stat-card");
    const top = element("div", "stat-label");
    append(top, text("span", "", label), text("span", "stat-icon", icon));
    append(card, top, text("div", "stat-value", formatNumber(value)), text("div", "stat-caption", caption));
    return card;
  }

  function panel(title, subtitle = "", link = null) {
    const box = element("section", "panel");
    const header = element("div", "panel-header");
    const label = element("div");
    append(label, text("h2", "panel-title", title));
    if (subtitle) label.append(text("p", "panel-subtitle", subtitle));
    append(header, label, link);
    box.append(header);
    return { box, header, body: element("div", "panel-body") };
  }

  function distributionPanel(title, items, total, routeType, denominatorLabel, unknownCount = null) {
    const link = element("a", "panel-link", "All jobs →");
    link.href = "#/explore";
    const view = panel(title, denominatorLabel, link);
    const list = element("div", "distribution-list");
    const rows = Array.isArray(items) ? items.slice(0, 6) : [];
    const maxCount = Math.max(1, ...rows.map((item) => Number(item.count) || 0), Number(unknownCount) || 0);
    for (const item of rows) {
      const label = displayLabel({ id: item.id, label: item.label }, "Untitled");
      const row = element("button", "distribution-row");
      row.type = "button";
      row.setAttribute("aria-label", `View jobs: ${label}, ${formatNumber(item.count)}`);
      append(row,
        text("span", "distribution-name", label),
        text("span", "distribution-value", `${formatNumber(item.count)} · ${formatPercent(item.share)}`)
      );
      const track = element("span", "bar-track");
      const fill = element("span", "bar-fill");
      fill.style.width = `${Math.max(0, Math.min(100, (Number(item.count) || 0) / maxCount * 100))}%`;
      track.append(fill);
      row.append(track);
      row.addEventListener("click", () => {
        const id = item.id === null || item.id === undefined ? "" : String(item.id);
        if (routeType === "occupation" && id) navigate(`occupation/${encodeURIComponent(id)}`);
        else if (routeType === "skill" && id) navigate(`skill/${encodeURIComponent(id)}`);
        else if (routeType === "specialty" && id) navigate("explore", new URLSearchParams({ specialty: id }));
        else navigate("explore");
      });
      list.append(row);
    }
    const hasUnknown = Number.isFinite(Number(unknownCount)) && Number(unknownCount) > 0;
    const hasUnknownItem = rows.some((item) => /unknown|unclassified/i.test(`${item.id || ""} ${item.label || ""}`));
    if (hasUnknown && !hasUnknownItem && routeType === "occupation") {
      const row = element("button", "distribution-row unknown-row");
      row.type = "button";
      row.setAttribute("aria-label", `View unclassified roles: ${formatNumber(unknownCount)}`);
      append(row,
        text("span", "distribution-name", "Unclassified"),
        text("span", "distribution-value", `${formatNumber(unknownCount)} · ${formatPercent(Number(total) ? Number(unknownCount) / Number(total) : null)}`)
      );
      const track = element("span", "bar-track");
      const fill = element("span", "bar-fill");
      fill.style.width = `${Math.max(0, Math.min(100, Number(unknownCount) / maxCount * 100))}%`;
      track.append(fill);
      row.append(track);
      row.addEventListener("click", () => navigate("explore", new URLSearchParams({ occupation: "unknown" })));
      list.append(row);
    }
    if (!rows.length && !hasUnknown) list.append(text("div", "empty-inline", "No data is available for this summary yet."));
    view.body.append(list);
    view.body.append(text("p", "mini-note", routeType === "skill"
      ? "Shares count postings with a literal skill mention. Postings without recognized text stay in the denominator. Mentions do not establish requirements or demand."
      : "Labels come from classifier rules. Unclassified postings stay in the snapshot."));
    view.box.append(view.body);
    return view.box;
  }

  function sourceStatus(source) {
    const status = String(source.status || "never").toLowerCase();
    if (source.stale || ["never", "failed", "no recent result", "no_recent_result"].includes(status) || !source.last_success_at_utc) {
      const label = status === "never" || status.startsWith("no recent") || status === "no_recent_result" ? "No successful scan" : "Stale / failed";
      return { label, style: "status-pill status-error" };
    }
    if (status === "partial") return { label: "Partial scan", style: "status-pill status-warning" };
    if (status === "succeeded") return { label: "Successful scan", style: "status-pill" };
    return { label: "Status unverified", style: "status-pill status-warning" };
  }

  function sourcesPanel(sources) {
    const items = Array.isArray(sources) ? sources : [];
    const expand = items.length > 7 ? element("button", "panel-link source-toggle", showAllSources ? "Show fewer" : `Show all ${formatNumber(items.length)}`) : null;
    if (expand) {
      expand.type = "button";
      expand.setAttribute("aria-expanded", showAllSources ? "true" : "false");
      expand.setAttribute("aria-controls", "radar-sources-list");
    }
    const view = panel("Sources", "Last successful full scan", expand);
    const list = element("div", "source-list");
    list.id = "radar-sources-list";
    const renderRows = () => {
      list.replaceChildren();
      const visibleItems = showAllSources ? items : items.slice(0, 7);
      for (const source of visibleItems) {
        const row = element("div", "source-row");
        const identity = element("div");
        append(identity,
          text("div", "source-name", source.company_name || source.key || "Source"),
          text("div", "source-detail", shortDate(source.last_success_at_utc))
        );
        const status = sourceStatus(source);
        append(row, identity, text("span", status.style, status.label));
        list.append(row);
      }
      if (!visibleItems.length) list.append(text("p", "mini-note", "No source list is available yet."));
    };
    renderRows();
    if (expand) expand.addEventListener("click", () => {
      showAllSources = !showAllSources;
      expand.textContent = showAllSources ? "Show fewer" : `Show all ${formatNumber(items.length)}`;
      expand.setAttribute("aria-expanded", showAllSources ? "true" : "false");
      renderRows();
    });
    view.body.append(list);
    view.box.append(view.body);
    return view.box;
  }

  function detailHref(job) {
    return `#/job/${encodeURIComponent(String(job.id || ""))}`;
  }

  function jobCard(job, options = {}) {
    const card = element("article", "job-card");
    const top = element("div", "job-topline");
    const identity = element("div");
    const title = element("h3", "job-title");
    const titleLink = element("a", "", job.title || "Untitled posting");
    titleLink.href = detailHref(job);
    title.append(titleLink);
    append(identity, title, text("div", "job-company", job.company_name || "Company not specified"));
    const actions = element("div", "job-actions");
    if (options.allowSave !== false) {
      const saved = isSaved(job.id);
      const button = element("button", "save-button", saved ? "♥" : "♡");
      button.type = "button";
      button.setAttribute("aria-pressed", saved ? "true" : "false");
      button.setAttribute("aria-label", saved ? "Remove saved job" : "Save job");
      button.title = saved ? "Remove saved job" : "Save job";
      button.addEventListener("click", () => saveJob(job));
      actions.append(button);
    }
    append(top, identity, actions);
    card.append(top);

    const tags = element("div", "job-meta");
    if (job.occupation) tags.append(text("span", "tag tag-accent", displayLabel(job.occupation)));
    if (job.specialty) tags.append(text("span", "tag tag-blue", displayLabel(job.specialty)));
    if (job.status) tags.append(text("span", "tag tag-muted", displayState(job.status, JOB_STATES)));
    if (Array.isArray(job.skills)) {
      for (const skill of job.skills.slice(0, 4)) tags.append(text("span", "tag", skill.label || skill.id));
    }
    if (tags.childElementCount) card.append(tags);
    if (job.description_excerpt) card.append(text("p", "job-excerpt", job.description_excerpt));
    const tail = element("div", "job-tail");
    const seen = text("span", "", `Last observed ${formatDate(job.last_seen_at_utc)}`);
    append(tail, seen);
    const sourceLink = externalAnchor(job.source_url, "Source ↗", "");
    if (sourceLink) tail.append(sourceLink);
    card.append(tail);
    return card;
  }

  function emptyState(icon, title, description, action = null) {
    const state = element("section", "empty-state");
    append(state, text("div", "empty-icon", icon), text("h2", "", title), text("p", "", description));
    if (action) state.append(action);
    return state;
  }

  function trendPanel(history, metrics = [], route = null) {
    const title = `Comparison over ${formatNumber(history && history.period_days || 90)} days`;
    const tabs = element("div", "period-tabs");
    tabs.setAttribute("role", "group");
    tabs.setAttribute("aria-label", "Historical comparison period");
    const selectedPeriod = Number(history && history.period_days || route && route.params.get("period_days") || 90);
    for (const periodDays of [30, 90, 180]) {
      const tab = element("button", "period-tab", `${periodDays} days`);
      tab.type = "button";
      tab.setAttribute("aria-pressed", periodDays === selectedPeriod ? "true" : "false");
      tab.addEventListener("click", () => changePeriod(periodDays, route));
      tabs.append(tab);
    }
    const view = panel(title, "Historical comparisons require comparable scans.", tabs);
    const info = history || {};
    const state = String(info.status || "insufficient").toLowerCase();
    const reason = historyReasonMessage(info.reason);
    const cards = element("div", "history-grid");
    const historyMetrics = Array.isArray(metrics) ? metrics : [];
    const selected = historyMetrics.filter((item) => !item.period_days || Number(item.period_days) === Number(info.period_days || 90));
    const groups = selected.filter((item) => ["occupation", "skill", "specialty", "technical_union"].includes(item.level));
    const focusId = route && ["occupation", "skill"].includes(route.segments[0]) ? route.segments[1] : null;
    const ordered = focusId ? [...groups].sort((left, right) => Number(String(right.id) === focusId) - Number(String(left.id) === focusId)) : groups;
    const supported = ordered.filter((item) => !["insufficient", "uncertain"].includes(String(item.status || "").toLowerCase())
      && (item.delta_pp !== null && item.delta_pp !== undefined
        || item.current_share !== null && item.current_share !== undefined
        || item.current_count !== null && item.current_count !== undefined));
    const canShowMetricHistory = supported.length > 0 || !["insufficient", "uncertain"].includes(state);
    const shown = canShowMetricHistory ? (supported.length ? supported : ordered).slice(0, 3) : [];
    if (shown.length) {
      for (const item of shown) {
        const card = element("div", "history-card");
        const metricLabel = `${item.label || displayLabel(item.id || item.level, "Metric")}${item.previous_count !== null && item.previous_count !== undefined && item.current_count !== null && item.current_count !== undefined ? ` · ${formatNumber(item.previous_count)} → ${formatNumber(item.current_count)}` : ""}`;
        append(card,
          text("div", "history-period", metricLabel),
          text("div", "history-state", item.delta_pp === null || item.delta_pp === undefined
            ? historyStatusLabel(item.status, item.direction)
            : `${Number(item.delta_pp) > 0 ? "+" : ""}${formatNumber(item.delta_pp)} pp`),
          text("div", "history-reason", item.reason ? historyReasonMessage(item.reason) : item.direction === "uncertain" ? "The direction is still uncertain." : `Current share: ${formatPercent(item.current_share)}; previous: ${formatPercent(item.previous_share)}.`)
        );
        cards.append(card);
      }
    } else {
      const card = element("div", "history-card");
      append(card,
        text("div", "history-period", `Window: ${formatNumber(info.period_days || 90)} days`),
        text("div", "history-state", historyStatusLabel(state, info.direction)),
        text("div", "history-reason", reason)
      );
      cards.append(card);
    }
    view.body.append(cards);
    view.body.append(text("div", "metric-note", "Comparisons use paired immutable observations and quality checks. Missing history does not mean zero demand."));
    view.box.append(view.body);
    return view.box;
  }

  function changePeriod(periodDays, route) {
    const current = route || routeFromHash();
    const params = new URLSearchParams(current.params);
    params.set("period_days", String(periodDays));
    params.delete("page");
    const page = current.segments[0] === "occupation" || current.segments[0] === "skill"
      ? current.segments.map((part) => encodeURIComponent(part)).join("/")
      : current.segments[0] === "overview" ? "overview" : "explore";
    navigate(page, params);
  }

  function jobsPanel(items, total, title = "Recently observed jobs", showAll = true) {
    const link = showAll ? element("a", "panel-link", "View all →") : null;
    if (link) link.href = "#/explore";
    const view = panel(title, "Cards show saved descriptions and the last observation time.", link);
    const list = element("div", "job-list");
    const rows = Array.isArray(items) ? items : [];
    if (rows.length) for (const job of rows) list.append(jobCard(job));
    else list.append(emptyState("⌕", "No jobs yet", "Postings will appear here after a successful collection. Unclassified roles remain visible in the results."));
    view.body.append(list);
    if (total !== null && total !== undefined) {
      const footer = element("div", "list-footer");
      footer.append(text("span", "mini-note", `Total in this snapshot: ${formatNumber(total)}`));
      view.body.append(footer);
    }
    view.box.append(view.body);
    return view.box;
  }

  function renderOverview(data) {
    const summary = data.summary || {};
    const heading = pageHeading("CAREER RADAR", "Roles and skills in real job postings", "Explore postings from collected company job boards. Summaries show detected mentions and keep unknown cases visible.", actionGroup(buttonLink("#/explore", "All jobs", "button"), buttonLink("#/explore?occupation=technical_engineering", "Engineering jobs", "button button-primary")));
    heading.classList.add("overview-heading");
    const scope = element("div", "radar-scope");
    scope.setAttribute("aria-hidden", "true");
    heading.append(scope);
    append(root, heading, snapshotBanner(data));
    const stats = element("div", "stat-grid");
    append(stats,
      statCard("Jobs in the snapshot", summary.posting_count, "Saved active postings", "▤"),
      statCard("Companies", summary.company_count, `${formatNumber(summary.source_count)} sources in the panel`, "⌂"),
      statCard("Engineering roles", summary.technical_engineering_count, "Based on classifier rules", "⌘"),
      statCard("Unclassified roles", summary.unknown_occupation_count, `${formatNumber(summary.missing_description_count)} without a description`, "?"));
    root.append(stats);

    const grid = element("div", "content-grid");
    const left = element("div", "stack");
    const right = element("div", "stack");
    left.append(distributionPanel("Role distribution", data.occupations, summary.posting_count, "occupation", "Share of all postings, including unknown roles", summary.unknown_occupation_count));
    left.append(distributionPanel("Specialties", data.specialties, summary.technical_engineering_count, "specialty", "Share of engineering postings"));
    left.append(distributionPanel("Common skill mentions", data.skills, summary.posting_count, "skill", "Share of postings with a detected mention"));
    left.append(jobsPanel((data.jobs && data.jobs.items || []).slice(0, 5), data.jobs && data.jobs.total, "Recent observations"));
    right.append(sourcesPanel(data.sources));
    right.append(trendPanel(data.history, data.history && data.history.metrics, routeFromHash()));
    grid.append(left, right);
    root.append(grid);
  }

  function buttonLink(href, label, className = "button") {
    const link = element("a", className, label);
    link.href = href;
    return link;
  }

  function routeFilterParams(route) {
    const params = new URLSearchParams(route.params);
    if (route.segments[0] === "occupation" && route.segments[1]) params.set("occupation", route.segments[1]);
    if (route.segments[0] === "skill" && route.segments[1]) params.set("skill", route.segments[1]);
    return params;
  }

  function requestParams(route) {
    const formParams = routeFilterParams(route);
    const allowed = ["q", "occupation", "specialty", "skill", "source", "period_days", "include_inactive", "enabled_only", "page", "page_size"];
    const params = new URLSearchParams();
    for (const key of allowed) if (formParams.has(key)) params.set(key, formParams.get(key));
    if (!params.has("page")) params.set("page", "1");
    if (!params.has("page_size")) params.set("page_size", "25");
    return params;
  }

  function selectField(label, name, options, currentValue, placeholder) {
    const wrapper = element("div", "field");
    const select = element("select");
    select.name = name;
    select.id = `radar-filter-${name}`;
    if (placeholder !== null) {
      const first = element("option", "", placeholder);
      first.value = "";
      select.append(first);
    }
    for (const option of options) {
      const id = option && (option.id ?? option.key);
      if (id === null || id === undefined) continue;
      const optionLabel = LABELS[String(id)] || option.label || option.company_name || option.name || option.key || id;
      const item = element("option", "", `${optionLabel}${option.count !== undefined ? ` (${formatNumber(option.count)})` : ""}`);
      item.value = String(id);
      select.append(item);
    }
    select.value = currentValue || "";
    if (currentValue && select.value !== currentValue) {
      const retained = element("option", "", `Current filter · ${LABELS[currentValue] || currentValue}`);
      retained.value = currentValue;
      retained.selected = true;
      select.append(retained);
      select.value = currentValue;
    }
    const fieldLabel = text("label", "", label);
    fieldLabel.htmlFor = select.id;
    append(wrapper, fieldLabel, select);
    return wrapper;
  }

  function buildFilterForm(data, route) {
    const params = routeFilterParams(route);
    const form = element("form", "filter-panel");
    form.setAttribute("role", "search");
    const grid = element("div", "filter-grid");
    const queryField = element("div", "field");
    const queryInput = element("input");
    queryInput.name = "q";
    queryInput.id = "radar-filter-query";
    queryInput.type = "search";
    queryInput.placeholder = "Title, company or description";
    queryInput.value = params.get("q") || "";
    queryInput.setAttribute("aria-label", "Search jobs");
    const queryLabel = text("label", "", "Search");
    queryLabel.htmlFor = queryInput.id;
    append(queryField, queryLabel, queryInput);
    grid.append(queryField);
    const catalogs = data.filter_options || {};
    grid.append(selectField("Role", "occupation", catalogs.occupations || data.occupations || [], params.get("occupation"), "All roles"));
    grid.append(selectField("Specialty", "specialty", catalogs.specialties || data.specialties || [], params.get("specialty"), "All specialties"));
    grid.append(selectField("Skill", "skill", catalogs.skills || data.skills || [], params.get("skill"), "Any skill"));
    const sourceOptions = catalogs.sources || (data.sources || []).map((source) => ({ key: source.key, company_name: source.company_name }));
    grid.append(selectField("Source", "source", sourceOptions, params.get("source"), "All sources"));
    const period = selectField("History window", "period_days", [
      { id: "30", label: "30 days" }, { id: "90", label: "90 days" }, { id: "180", label: "180 days" },
    ], params.get("period_days") || "90", null);
    grid.append(period);
    const submitWrap = element("div", "field filter-submit");
    const submit = element("button", "button button-primary", "Apply filters");
    submit.type = "submit";
    submitWrap.append(submit);
    grid.append(submitWrap);
    form.append(grid);

    const bottom = element("div", "filter-bottom");
    const checks = element("div");
    const includeInactive = element("input");
    includeInactive.type = "checkbox";
    includeInactive.name = "include_inactive";
    includeInactive.value = "true";
    includeInactive.checked = params.get("include_inactive") === "true";
    const includeLabel = element("label", "check-field", "Include inactive postings");
    includeLabel.prepend(includeInactive);
    const enabledOnly = element("input");
    enabledOnly.type = "checkbox";
    enabledOnly.name = "enabled_only";
    enabledOnly.value = "true";
    enabledOnly.checked = params.get("enabled_only") !== "false";
    const enabledLabel = element("label", "check-field", "Enabled sources only");
    enabledLabel.prepend(enabledOnly);
    append(checks, includeLabel, element("span", "", "　"), enabledLabel);
    const hint = text("span", "filter-status", "The period changes the historical comparison only, not the saved job inventory.");
    append(bottom, checks, hint);
    form.append(bottom);
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      const next = new URLSearchParams();
      const values = new FormData(form);
      for (const key of ["q", "occupation", "specialty", "skill", "source", "period_days"]) {
        const value = String(values.get(key) || "").trim();
        if (value) next.set(key, value);
      }
      if (includeInactive.checked) next.set("include_inactive", "true");
      if (!enabledOnly.checked) next.set("enabled_only", "false");
      next.set("page", "1");
      navigate("explore", next);
    });
    return form;
  }

  function renderExplore(data, route) {
    const segments = route.segments;
    const isOccupation = segments[0] === "occupation";
    const isSkill = segments[0] === "skill";
    const params = requestParams(route);
    const selected = isOccupation ? (data.occupations || []).find((item) => String(item.id) === params.get("occupation"))
      : isSkill ? (data.skills || []).find((item) => String(item.id) === params.get("skill")) : null;
    const headingTitle = isOccupation ? (selected ? displayLabel(selected) : "Jobs by role")
      : isSkill ? (selected && selected.label || "Jobs mentioning this skill") : "Search jobs";
    const headingCopy = isOccupation
      ? "Roles are assigned by Career Radar rules. A matching label is not confirmation from the employer."
      : isSkill ? "Explore postings where the term appears in the title or description. A mention does not establish a required skill." : "Filter saved postings and inspect their original text and matching evidence.";
    append(root, pageHeading(isOccupation ? "ROLE" : isSkill ? "SKILL" : "EXPLORE", headingTitle, headingCopy));
    root.append(snapshotBanner(data));
    root.append(buildFilterForm(data, route));

    const split = element("div", "content-grid");
    const mainStack = element("div", "stack");
    const sideStack = element("div", "stack");
    const summary = data.summary || {};
    const jobs = data.jobs || { items: [], total: 0, page: 1, page_size: 25, total_pages: 0 };
    const results = element("section");
    const resultsHeading = element("div", "results-header");
    append(resultsHeading, text("h2", "", "Matching postings"), text("span", "results-count", `${formatNumber(jobs.total)} ${Number(jobs.total) === 1 ? "job" : "jobs"}`));
    results.append(resultsHeading);
    if (Array.isArray(jobs.items) && jobs.items.length) {
      const list = element("div", "panel job-list");
      for (const job of jobs.items) list.append(jobCard(job));
      results.append(list);
      results.append(pagination(jobs));
    } else {
      const none = emptyState("⌕", "No results", "Change your search or reset some filters. Unknown roles and descriptions without detected skills are not automatically excluded.", buttonLink("#/explore", "Reset filters", "button"));
      results.append(none);
    }
    mainStack.append(results);
    sideStack.append(distributionPanel("Roles in this snapshot", data.occupations, summary.posting_count, "occupation", "Share of filtered postings", summary.unknown_occupation_count));
    sideStack.append(distributionPanel("Specialties", data.specialties, summary.technical_engineering_count, "specialty", "Share of selected engineering postings"));
    sideStack.append(distributionPanel("Skill mentions", data.skills, summary.posting_count, "skill", "Share of filtered postings"));
    sideStack.append(trendPanel(data.history, data.history && data.history.metrics, route));
    split.append(mainStack, sideStack);
    root.append(split);
  }

  function pagination(jobs) {
    const page = Number(jobs.page) || 1;
    const totalPages = Number(jobs.total_pages) || 0;
    const wrap = element("div", "pagination panel");
    const start = Number(jobs.total) ? (page - 1) * (Number(jobs.page_size) || 25) + 1 : 0;
    const end = Math.min(Number(jobs.total) || 0, page * (Number(jobs.page_size) || 25));
    wrap.append(text("span", "pagination-label", `${formatNumber(start)}–${formatNumber(end)} of ${formatNumber(jobs.total || 0)}`));
    const actions = element("div", "pagination-actions");
    const previous = element("button", "button button-small", "← Previous");
    previous.type = "button";
    previous.disabled = page <= 1;
    previous.addEventListener("click", () => setPage(page - 1));
    const next = element("button", "button button-small", "Next →");
    next.type = "button";
    next.disabled = page >= totalPages;
    next.addEventListener("click", () => setPage(page + 1));
    append(actions, previous, next);
    wrap.append(actions);
    return wrap;
  }

  function setPage(page) {
    const route = routeFromHash();
    const params = routeFilterParams(route);
    params.set("page", String(Math.max(1, page)));
    navigate("explore", params);
  }

  function renderSaved() {
    const actions = actionGroup();
    const explore = buttonLink("#/explore", "Find jobs", "button button-primary");
    actions.append(explore);
    append(root,
      pageHeading("YOUR SHORTLIST", "Saved jobs", "Your shortlist stays in this browser. Return to postings you want to explore further.", actions),
      snapshotInfoLocal()
    );
    if (!savedJobs.length) {
      root.append(emptyState("♡", "Your shortlist starts here", "Select the heart on a job card to save it for later.", buttonLink("#/explore", "Browse jobs", "button button-primary")));
      return;
    }
    const list = element("section", "panel job-list");
    for (const job of savedJobs) {
      const card = jobCard(job);
      const footer = card.querySelector(".job-tail");
      if (footer) footer.prepend(text("span", "", `Saved on ${formatDate(job.saved_at)}`));
      list.append(card);
    }
    const clear = element("button", "button button-danger button-small", "Clear shortlist");
    clear.type = "button";
    clear.addEventListener("click", () => {
      savedJobs = [];
      persistSavedJobs();
      renderRoute();
    });
    const tools = element("div", "filter-bottom");
    append(tools, text("span", "filter-status", `${formatNumber(savedJobs.length)} ${savedJobs.length === 1 ? "job" : "jobs"} · in this browser only`), clear);
    list.append(tools);
    root.append(list);
  }

  function snapshotInfoLocal() {
    const banner = element("section", "snapshot-banner");
    const copy = element("div", "snapshot-copy");
    const body = element("div");
    append(copy, text("span", "snapshot-symbol", "⌂"), body);
    append(body,
      text("div", "snapshot-title", "Your browser-local shortlist"),
      text("div", "snapshot-description", "Saved jobs stay in your browser. Cards reflect the values recorded when you saved them.")
    );
    banner.append(copy);
    return banner;
  }

  async function renderJobDetail(id, seq) {
    const detail = await getJson(`jobs/${encodeURIComponent(id)}`);
    if (seq !== renderSequence) return;
    root.replaceChildren();
    const job = detail.job && typeof detail.job === "object" ? detail.job : detail;
    const summary = job.summary && typeof job.summary === "object" ? job.summary : job;
    const description = typeof detail.description === "string" ? detail.description
      : typeof job.description === "string" ? job.description : null;
    const roleEvidence = Array.isArray(detail.role_evidence) ? detail.role_evidence : job.role_evidence;
    const skillEvidence = Array.isArray(detail.skill_evidence) ? detail.skill_evidence : job.skill_evidence;
    const title = summary.title || "Untitled posting";
    updateNav("explore", "Job posting");
    const back = buttonLink("#/explore", "← Back to jobs", "button");
    const savedButton = element("button", "button", isSaved(summary.id || id) ? "♥ Saved" : "♡ Save");
    savedButton.type = "button";
    savedButton.setAttribute("aria-pressed", isSaved(summary.id || id) ? "true" : "false");
    savedButton.addEventListener("click", () => saveJob({ ...summary, id: summary.id || id }));
    append(root, pageHeading("JOB POSTING", title, summary.company_name || "Company not specified", actionGroup(back, savedButton)));

    const linkActions = element("div", "detail-links");
    const source = externalAnchor(summary.source_url, "View source ↗", "button button-primary");
    const apply = externalAnchor(summary.apply_url, "Apply ↗", "button");
    append(linkActions, source, apply);
    const metaTags = element("div", "job-meta");
    if (summary.occupation) metaTags.append(text("span", "tag tag-accent", displayLabel(summary.occupation)));
    if (summary.specialty) metaTags.append(text("span", "tag tag-blue", displayLabel(summary.specialty)));
    if (summary.status) metaTags.append(text("span", "tag tag-muted", displayState(summary.status, JOB_STATES)));

    const layout = element("div", "detail-layout");
    const main = element("section", "panel");
    const desc = element("section", "detail-section");
    desc.append(text("h2", "", "Job description"));
    desc.append(text("p", "", description === null ? "The full description is unavailable." : description));
    main.append(desc);
    main.append(evidenceSection("Role classification evidence", roleEvidence));
    main.append(evidenceSection("Detected skill mentions", skillEvidence));
    const side = element("aside", "stack detail-side");
    const meta = panel("Observation details", "Saved source fields");
    const metaBody = meta.body;
    const values = [
      ["Source", summary.source_key || "Not specified"],
      ["Last observed", formatDate(summary.last_seen_at_utc)],
      ["Status", displayState(summary.status, JOB_STATES)],
      ["Specialty", displayLabel(summary.specialty, "Not identified")],
      ["Specialty status", displayState(summary.specialty_state, SPECIALTY_STATES)],
      ["Detected skills", formatNumber(Array.isArray(summary.skills) ? summary.skills.length : 0)],
    ];
    for (const [key, value] of values) {
      const row = element("div", "key-value");
      append(row, text("span", "", key), text("span", "", value));
      metaBody.append(row);
    }
    meta.box.append(metaBody);
    side.append(meta.box);
    const note = panel("How to read this posting", "");
    note.body.append(text("p", "mini-note", "Roles and skills come from automatic rule matches against saved text. The source may have since changed or closed the posting."));
    note.box.append(note.body);
    side.append(note.box);
    append(layout, main, side);
    root.append(metaTags, linkActions, layout);
  }

  function evidenceSection(title, evidence) {
    const section = element("section", "detail-section");
    section.append(text("h2", "", title));
    if (!Array.isArray(evidence) || !evidence.length) {
      section.append(text("p", "", "No exact matches are available for this section."));
      return section;
    }
    const list = element("div", "evidence-list");
    for (const item of evidence) {
      const card = element("div", "evidence-item");
      const matched = item.matched_text || item.text || item.skill_label || item.label || item.occupation_label || "Match";
      const rule = item.rule || item.rule_id || item.section || item.field || "";
      append(card,
        text("div", "evidence-main", matched),
        rule ? text("div", "evidence-detail", rule) : null
      );
      list.append(card);
    }
    section.append(list);
    return section;
  }

  const RESEARCH_SOURCES = {
    anthropic_economics: "Anthropic Economic Research",
    openai_economic_research: "OpenAI Economic Research",
    linkedin_economic_graph: "LinkedIn Economic Graph",
    indeed_hiring_lab: "Indeed Hiring Lab",
    mckinsey_state_of_ai: "McKinsey State of AI",
    deloitte_state_of_ai: "Deloitte State of AI",
    stanford_hai_ai_index: "Stanford HAI AI Index",
    metr: "METR",
  };
  const RESEARCH_METHODS = {
    observed: "Observed data",
    survey: "Survey",
    experiment: "Experiment",
    forecast: "Forecast",
    model_estimate: "Model estimate",
    case_study: "Case study",
    secondary_synthesis: "Secondary synthesis",
  };

  function publisherLabel(sourceKey) {
    return RESEARCH_SOURCES[String(sourceKey)] || String(sourceKey || "Research source");
  }

  function researchDocumentTitle(item) {
    const version = item.latest_version || {};
    const sourceTitle = String(version.title || "").trim();
    if (sourceTitle && sourceTitle !== "Title not reported in source" && sourceTitle !== "StateofAI_Report_vF") {
      return { title: sourceTitle, note: "" };
    }
    let filename = "Publication";
    try {
      const pathname = new URL(item.canonical_url).pathname;
      filename = decodeURIComponent(pathname.split("/").filter(Boolean).pop() || "Publication");
    } catch {
      // Keep a neutral label when the stored URL cannot supply a filename.
    }
    return {
      title: `${publisherLabel(item.source_key)} · ${filename}`,
      note: "Source title not reported; label shown from the official URL filename.",
    };
  }

  function researchDocumentCard(item) {
    const card = element("article", "research-document");
    const heading = researchDocumentTitle(item);
    const titleLink = element("a", "research-document-title", heading.title);
    const version = item.latest_version || {};
    titleLink.href = `#/research/document/${encodeURIComponent(item.id)}/${encodeURIComponent(version.id || "")}`;
    const meta = element("div", "research-meta");
    append(meta,
      text("span", "tag tag-accent", publisherLabel(item.source_key)),
      text("span", "tag", version.media_type || "Media type unknown"),
      text("span", "tag", `Retrieved ${formatDate(version.retrieved_at)}`),
      text("span", "tag", version.publication_date && version.publication_date.value
        ? `Published ${version.publication_date.value}` : "Publication date unknown")
    );
    const note = heading.note ? text("p", "research-note", heading.note) : null;
    const error = version.extraction_error
      ? text("p", "research-warning", `Text extraction unavailable: ${version.extraction_error}`)
      : null;
    const external = externalAnchor(item.canonical_url, "Official source ↗", "research-source-link");
    append(card, titleLink, meta, note, error, external);
    return card;
  }

  function researchClaimCard(claim, { current = null } = {}) {
    const isCurrent = current === null ? claim.current_evidence === true : current;
    const card = element("article", `research-claim${isCurrent ? " research-claim-current" : ""}`);
    const top = element("div", "research-claim-top");
    append(top,
      text("span", `status-pill${isCurrent ? "" : " status-warning"}`, isCurrent ? "Accepted · current" : `${claim.review_state || "Candidate"} · not current`),
      text("span", "research-claim-question", claim.question || "Question not specified")
    );
    const document = claim.document || {};
    const version = claim.version || {};
    const versionId = claim.document_version_id || version.id;
    const title = researchDocumentTitle({ source_key: document.source_key, canonical_url: document.canonical_url, latest_version: version });
    const source = externalAnchor(document.canonical_url, `${publisherLabel(document.source_key)} · ${title.title} ↗`, "research-citation-link");
    const versionLink = versionId && document.id
      ? buttonLink(`#/research/document/${encodeURIComponent(document.id)}/${encodeURIComponent(versionId)}`, "View quoted source version", "research-citation-link")
      : null;
    const quote = element("blockquote", "research-quote", claim.exact_quote || "Exact quote not reported.");
    const location = claim.location || {};
    const locator = location.page_number !== null && location.page_number !== undefined
      ? `Page ${location.page_number}`
      : location.section || "Text offset";
    const method = RESEARCH_METHODS[claim.evidence_kind] || claim.evidence_kind || "Method not reported";
    const detail = claim.evidence_details || {};
    const facts = element("div", "research-facts");
    const dataPeriod = detail.data_period || {};
    const periodLabel = dataPeriod.start || dataPeriod.end
      ? `${dataPeriod.start || "Start unknown"} to ${dataPeriod.end || "end unknown"}`
      : `Data period ${missingReasonLabel(dataPeriod.state || detail.missing_reasons?.data_period)}`;
    const metadata = [
      ["metric_definition", "Metric"],
      ["metric_value", "Value"],
      ["geography", "Geography"],
      ["sample_description", "Sample"],
      ["sample_size", "Sample size"],
      ["denominator", "Denominator"],
      ["limitations", "Limitations"],
    ];
    const metadataValue = (key) => detail[key] === null || detail[key] === undefined
      ? missingReasonLabel((detail.missing_reasons || {})[key])
      : key === "sample_size" ? formatNumber(detail[key]) : detail[key];
    for (const [label, value] of [
      ["Method", method],
      ["Locator", locator],
      ...metadata.map(([key, label]) => [label, metadataValue(key)]),
      ["Data period", periodLabel],
      ["Lineage", claim.lineage_kind || "Unknown"],
    ]) {
      const row = element("div", "research-fact");
      append(row, text("span", "research-fact-label", label), text("span", "research-fact-value", value));
      facts.append(row);
    }
    const history = Array.isArray(claim.review_history) ? claim.review_history : [];
    const trail = text("p", "research-note", history.length
      ? `Review history: ${history.map((event) => `${event.state} by ${event.reviewer} (${event.reason})`).join(" · ")}`
      : "No review event has been recorded.");
    const publication = version.publication_date && version.publication_date.value
      ? `Published ${version.publication_date.value}`
      : `Publication date ${missingReasonLabel(version.publication_date && version.publication_date.state)}`;
    const citationVersion = text("p", "research-note", `${publication} · retrieved ${formatDate(version.retrieved_at)} · version ${formatNumber(version.version_number)} (${version.version_relation || "initial"})`);
    append(card,
      top,
      text("p", "research-claim-statement", claim.statement || "Claim statement unavailable."),
      quote,
      text("p", "research-locator", `${locator} · text offsets ${location.start_offset ?? "?"}–${location.end_offset ?? "?"}`),
      facts,
      source,
      versionLink,
      citationVersion,
      trail
    );
    return card;
  }

  function missingReasonLabel(reason) {
    return {
      not_reported: "not reported",
      not_applicable: "not applicable",
      unclear: "unclear",
      reported: "reported",
      not_assessed: "not assessed",
    }[String(reason || "")] || "unknown";
  }

  function researchQuestionCard(question, monthlyProduct) {
    const card = element("article", "research-question");
    const hypothesisId = {
      developer_tasks: "developer_task_mix_change",
      experience_seniority: "seniority_differentiation",
      production_ai_skills: "production_ai_skills_shift",
    }[question.id];
    const hypothesis = (Array.isArray(monthlyProduct && monthlyProduct.hypotheses) ? monthlyProduct.hypotheses : [])
      .find((item) => item.id === hypothesisId);
    const statusValue = hypothesis && hypothesis.status;
    const hasCurrentReview = hypothesis && hypothesis.review_state === "reviewed" && !hypothesis.requires_review;
    const assessment = hasCurrentReview
      ? String(statusValue || "insufficient")
      : hypothesis && hypothesis.requires_review
        ? `Review required · previously ${hypothesis.published_status || "reviewed"}`
        : "Insufficient · no reviewed synthesis";
    const note = hasCurrentReview && hypothesis.rationale
      ? `${hypothesis.rationale} Reviewed by ${monthlyProduct.reviewer || "reviewer not reported"} on ${formatDate(monthlyProduct.reviewed_at)}.`
      : hypothesis && hypothesis.requires_review
        ? "Saved review is historical because one or more cited claims are no longer current."
        : question.assessment_note || question.reading_note || "No synthesis has been reviewed.";
    append(card,
      text("div", "research-question-status", assessment),
      text("h3", "", question.label || question.id),
      text("p", "research-question-text", question.question || "Question text unavailable."),
      text("p", "research-note", note),
      text("p", "research-note", `Current accepted claims: ${formatNumber(question.current_evidence_claim_count)} · all annotations: ${formatNumber(question.all_claim_count)}`)
    );
    const context = question.context || {};
    const links = element("div", "research-context-links");
    for (const term of Array.isArray(context.skill_search_terms) ? context.skill_search_terms : []) {
      links.append(text("span", "tag", `Job skill context: ${term}`));
    }
    for (const item of Array.isArray(context.job_radar_searches) ? context.job_radar_searches : []) {
      const params = new URLSearchParams({ q: item.term || "" });
      links.append(buttonLink(`#/explore?${params.toString()}`, `Search jobs: ${item.term || "related roles"}`, "button button-small"));
    }
    append(card, links);
    return card;
  }

  async function renderResearch(seq) {
    updateNav("research", "Research");
    const [overview, questionResponse, documents, claims, products] = await Promise.all([
      getJson("research/overview"),
      getJson("research/questions"),
      getAllPages("research/documents"),
      getAllPages("research/claims"),
      getJson("research/products"),
    ]);
    if (seq !== renderSequence) return;
    root.replaceChildren();
    const claimsOverview = overview.claims || {};
    const documentOverview = overview.documents || {};
    const currentClaims = claims.filter((item) => item.current_evidence === true);
    const candidateClaims = claims.filter((item) => item.review_state === "candidate");
    const excludedClaims = claims.filter((item) => item.current_evidence !== true && item.review_state !== "candidate");
    append(root,
      pageHeading("RESEARCH STREAM", "Evidence about AI and work", "Source-grounded documents and reviewed claims. Research samples stay separate from job-posting counts and skill mentions.", actionGroup(buttonLink("#/methodology", "How to read evidence", "button"))),
      text("p", "research-scope-note", "Daily discovery stores source versions. Annotations remain candidates until explicit review; only accepted claims bound to the current normalized text count as current evidence.")
    );
    const stats = element("div", "stat-grid research-stat-grid");
    append(stats,
      statCard("Documents", documentOverview.body_bearing_count, "Stored publication records", "▤"),
      statCard("Text extracted", documentOverview.extractable_count, "Non-null deterministic text extractions", "⌕"),
      statCard("Accepted · current", claimsOverview.current_evidence_count, "Exact quote and locator still match current text", "✓"),
      statCard("Candidates", candidateClaims.length, "Unreviewed annotations", "…")
    );
    root.append(stats);

    const questionsPanel = panel("Research questions", "Hypothesis status requires an explicit monthly review.");
    const questionList = element("div", "research-question-grid");
    const monthlyReviews = Array.isArray(products.monthly_reviews) ? products.monthly_reviews : [];
    const reviewedProduct = monthlyReviews.length ? monthlyReviews[monthlyReviews.length - 1].latest : null;
    const proposalProduct = products.monthly_proposal && products.monthly_proposal.product;
    const monthlyProduct = !reviewedProduct || (proposalProduct && proposalProduct.period_id > reviewedProduct.period_id)
      ? proposalProduct : reviewedProduct;
    for (const question of Array.isArray(questionResponse.items) ? questionResponse.items : []) {
      questionList.append(researchQuestionCard(question, monthlyProduct));
    }
    questionsPanel.body.append(questionList);
    questionsPanel.box.append(questionsPanel.body);
    root.append(questionsPanel.box);

    const sourcePanel = panel("Publisher coverage", "Stored documents and latest acquisition status; source counts do not measure evidence quality.");
    const sourceList = element("div", "source-list");
    const sourceRows = Array.isArray(documentOverview.by_source) ? documentOverview.by_source : [];
    for (const source of sourceRows) {
      const row = element("div", "source-row research-source-row");
      const copy = element("div");
      const attempt = source.latest_attempt || {};
      const outcome = attempt.outcome || "No acquisition attempt";
      const detail = `${formatNumber(source.document_count)} documents · ${formatNumber(source.extractable_count)} text extracted · latest: ${outcome}${attempt.http_status ? ` (${attempt.http_status})` : ""}`;
      append(copy,
        text("div", "source-name", source.publisher || publisherLabel(source.source_key)),
        text("div", "source-detail", detail)
      );
      const statusClass = /failed|blocked|error/i.test(outcome) ? "status-error" : /partial|timeout|unavailable/i.test(outcome) ? "status-warning" : "";
      append(row, copy, text("span", `status-pill ${statusClass}`, source.enabled === false ? "Disabled" : outcome));
      sourceList.append(row);
    }
    sourcePanel.body.append(sourceList);
    sourcePanel.box.append(sourcePanel.body);
    root.append(sourcePanel.box);

    const productsPanel = panel("Latest reviewed products", "Weekly items only use accepted current evidence. Unresolved monthly proposals do not carry a conclusion.");
    const weekly = products.weekly && products.weekly.product;
    if (weekly) {
      const week = element("section", "research-product-block");
      const items = Array.isArray(weekly.items) ? weekly.items : [];
      const window = weekly.window || {};
      append(week,
        text("h3", "", `Weekly evidence · ${formatNumber(items.length)} ${items.length === 1 ? "item" : "items"}`),
        text("p", "research-note", `Coverage ${window.start || "unknown"} to ${window.end || weekly.generated_at || "unknown"}. Historical output is checked against current accepted claims.`)
      );
      if (!items.length) week.append(text("p", "empty-inline", "No accepted current items were available for this period."));
      for (const item of items) {
        const currentLabel = item.current_eligibility || "unverified";
        const line = element("div", "research-product-item");
        const citations = element("span", "research-note");
        for (const citation of Array.isArray(item.citations) ? item.citations : []) {
          const link = externalAnchor(citation.canonical_url, `${citation.title || publisherLabel(citation.source_key)} · ${citation.version_relation || "version"} ↗`, "research-citation-link");
          if (link) citations.append(link);
        }
        append(line,
          text("span", `status-pill${currentLabel === "current" ? "" : " status-warning"}`, currentLabel),
          text("strong", "", item.plain_language_summary || "Evidence item"),
          citations
        );
        week.append(line);
      }
      productsPanel.body.append(week);
    } else {
      productsPanel.body.append(text("p", "empty-inline", "No weekly product has been written yet."));
    }
    const monthlyBlock = element("section", "research-product-block");
    if (monthlyProduct) {
      append(monthlyBlock,
        text("h3", "", `Monthly review · ${monthlyProduct.period_id || "period unknown"}`),
        text("p", "research-note", monthlyProduct.review_state === "reviewed" ? `Reviewed by ${monthlyProduct.reviewer || "reviewer not reported"} on ${formatDate(monthlyProduct.reviewed_at)}.` : "Proposal only. All hypothesis statuses remain unresolved until explicit review.")
      );
      for (const hypothesis of Array.isArray(monthlyProduct.hypotheses) ? monthlyProduct.hypotheses : []) {
        const currentStatus = hypothesis.requires_review
          ? `review required · previously ${hypothesis.published_status || "reviewed"}`
          : hypothesis.status || "unresolved";
        append(monthlyBlock,
          text("div", "research-product-item", `${hypothesis.id || "Hypothesis"}: ${currentStatus}${hypothesis.requires_review ? " · review required" : ""}`),
          hypothesis.rationale ? text("p", "research-note", `Rationale: ${hypothesis.rationale}`) : null,
          Array.isArray(hypothesis.alternative_explanations) && hypothesis.alternative_explanations.length
            ? text("p", "research-note", `Alternative explanations: ${hypothesis.alternative_explanations.join("; ")}`) : null,
          Array.isArray(hypothesis.limitations) && hypothesis.limitations.length
            ? text("p", "research-note", `Limitations: ${hypothesis.limitations.join("; ")}`) : null,
          ...(Array.isArray(hypothesis.evidence) ? hypothesis.evidence.map((evidence) => {
            const citation = evidence.citation || {};
            return externalAnchor(citation.canonical_url, `${citation.title || publisherLabel(citation.source_key)} · view citation ↗`, "research-citation-link");
          }) : []),
          hypothesis.requires_review ? text("p", "research-warning", "Some cited claims are no longer current; the saved review remains historical.") : null
        );
      }
    } else {
      monthlyBlock.append(text("p", "empty-inline", "No monthly proposal or review has been written yet."));
    }
    productsPanel.body.append(monthlyBlock);
    productsPanel.box.append(productsPanel.body);
    root.append(productsPanel.box);

    append(root,
      renderClaimGroup("Accepted current evidence", currentClaims, "These exact quotations still match the latest normalized document text.", true),
      renderClaimGroup("Unreviewed candidates", candidateClaims, "Candidate annotations are not counted as evidence until explicitly accepted.", false),
      renderClaimGroup("Excluded or historical annotations", excludedClaims, "Rejected, superseded, retracted, and obsolete-version claims remain visible for audit.", false)
    );

    const docsPanel = panel("Collected publications", `${formatNumber(documents.length)} records · original HTML/PDF bodies remain in the local database and are not served here.`);
    const docsList = element("div", "research-document-list");
    if (documents.length) for (const item of documents) docsList.append(researchDocumentCard(item));
    else docsList.append(text("p", "empty-inline", "No research documents have been collected yet."));
    docsPanel.body.append(docsList);
    docsPanel.box.append(docsPanel.body);
    root.append(docsPanel.box);
  }

  function renderClaimGroup(title, claims, description, current) {
    const view = panel(title, description);
    const list = element("div", "research-claim-list");
    if (claims.length) for (const claim of claims) list.append(researchClaimCard(claim, { current }));
    else list.append(text("p", "empty-inline", current ? "No accepted current claims are available." : "None recorded."));
    view.body.append(list);
    view.box.append(view.body);
    return view.box;
  }

  async function renderResearchVersion(documentId, versionId, seq) {
    const evidence = await getJson(`research/documents/${encodeURIComponent(documentId)}/versions/${encodeURIComponent(versionId)}`);
    if (seq !== renderSequence) return;
    root.replaceChildren();
    updateNav("research", "Research evidence");
    const document = evidence.document || {};
    const version = evidence.version || {};
    const info = researchDocumentTitle({
      id: document.id,
      source_key: document.source_key,
      canonical_url: document.canonical_url,
      latest_version: version,
    });
    append(root,
      pageHeading("DOCUMENT VERSION", info.title, `${publisherLabel(document.source_key)} · version ${formatNumber(version.version_number)} · retrieved ${formatDate(version.retrieved_at)}`, actionGroup(buttonLink("#/research", "← Back to research", "button"))),
      info.note ? text("p", "research-note", info.note) : null
    );
    const provenance = panel("Version provenance", "Immutable source-body and extraction details");
    const values = [
      ["Official document", document.canonical_url || "Not reported"],
      ["Retrieved URL", version.retrieved_url || "Not reported"],
      ["Publication date", version.publication_date && version.publication_date.value || "Not reported"],
      ["Data period", version.data_period && (version.data_period.start || version.data_period.end) ? `${version.data_period.start || "Unknown"} to ${version.data_period.end || "unknown"}` : "Unknown"],
      ["Media type", version.media_type || "Not reported"],
      ["Original body SHA-256", version.raw_sha256 || "Not available"],
      ["Normalized text SHA-256", version.normalized_text_sha256 || "Not available"],
      ["Extraction version", version.extraction_version || "Not reported"],
      ["Version relation", version.version_relation || "Initial"],
    ];
    const factList = element("div", "research-facts");
    for (const [label, value] of values) {
      const row = element("div", "research-fact");
      append(row, text("span", "research-fact-label", label), text("span", "research-fact-value", value));
      factList.append(row);
    }
    provenance.body.append(factList);
    const officialLink = externalAnchor(document.canonical_url, "Open official source ↗", "button");
    append(provenance.body, officialLink, version.extraction_error ? text("p", "research-warning", `Text extraction error: ${version.extraction_error}`) : null);
    provenance.box.append(provenance.body);
    root.append(provenance.box);

    append(root, renderClaimGroup("Claims bound to this version", evidence.claims && evidence.claims.items || [], "Current eligibility and review history are shown for each claim.", null));
    const regions = Array.isArray(version.extraction_regions) ? version.extraction_regions : [];
    const regionPanel = panel("Extracted text and locators", `${formatNumber(regions.length)} page or section regions · offsets refer to this immutable normalized text.`);
    const textValue = evidence.normalized_text;
    if (typeof textValue === "string") {
      const pre = element("pre", "research-source-text", textValue);
      regionPanel.body.append(pre);
    } else {
      regionPanel.body.append(text("p", "empty-inline", "No normalized text is available for this version."));
    }
    const regionList = element("div", "research-region-list");
    for (const region of regions) {
      append(regionList, text("div", "research-region", `${region.page_number ? `Page ${region.page_number}` : region.section || "Section"} · offsets ${region.start_offset}–${region.end_offset}`));
    }
    regionPanel.body.append(regionList);
    regionPanel.box.append(regionPanel.body);
    root.append(regionPanel.box);
  }

  async function renderMethodology(seq) {
    const methodology = await getJson("methodology");
    if (seq !== renderSequence) return;
    root.replaceChildren();
    updateNav("methodology", "Methodology");
    root.append(pageHeading("METHODOLOGY", methodology.title || "How the data works", "Understand what is counted, how to read summaries and what the available evidence supports."));
    const grid = element("div", "methodology-grid");
    const copyPanel = panel("What Career Radar shows", "Methodology version: " + String(methodology.version || methodology.schema || "Not specified"));
    const copy = element("div", "methodology-copy");
    append(copy,
      text("h2", "", "Current snapshot"),
      text("p", "", "The unit is one source posting. Jobs reflect the latest saved state of enabled sources. Active is a stored status, not confirmation that a job is still open."),
      text("h3", "", "Roles and specialties"),
      text("p", "", "Role labels come from versioned classifier rules. Unknown and ambiguous titles stay in the sample. Role shares use selected postings; specialty shares use selected engineering postings."),
      text("h3", "", "Skill mentions"),
      text("p", "", "Terms are matched against saved titles and descriptions. Each skill counts once per posting. The share shows how many selected postings mention the term. It does not establish a requirement, market demand or the absence of an unmentioned skill."),
      text("h3", "", "History"),
      text("p", "", "Comparisons over 30, 90 or 180 days require paired immutable scans and passing quality checks. When evidence is insufficient, values stay empty with an explanation. Missing history is never replaced with a measured zero."),
      text("h3", "", "Freshness and sources"),
      text("p", "", "A posting's last observation and a source's last successful scan are different timestamps. Partial or failed scans do not establish a complete refresh. Reload snapshot rereads saved app data without collecting from company sites."));
    const provenance = methodology.provenance || methodology.versions || {};
    const chips = element("div", "version-chips");
    for (const [key, value] of Object.entries(provenance)) {
      if (value === null || value === undefined || typeof value === "object") continue;
      chips.append(text("span", "tag", `${key}: ${value}`));
    }
    if (chips.childElementCount) copy.append(chips);
    copyPanel.body.append(copy);
    copyPanel.box.append(copyPanel.body);
    grid.append(copyPanel.box);

    const side = panel("Interpretation limits", "Read before drawing conclusions");
    const limits = element("div");
    const knownLimitText = [
      "This is a panel of collected company job boards, not the entire labor market.",
      "Titles and descriptions may be incomplete, change or contain noise.",
      "Rule-based recognition may miss new titles and synonyms.",
      "The data does not measure applicant counts, hiring outcomes or requirements confirmed after hiring.",
      "Your saved shortlist stays in this browser.",
    ];
    const limitsFromApi = Array.isArray(methodology.limitations) ? methodology.limitations : null;
    for (const [index, item] of (limitsFromApi || knownLimitText).entries()) {
      const row = element("div", "limit-item");
      const raw = typeof item === "string" ? item : item.text || item.description || "";
      append(row, text("span", "limit-number", String(index + 1)), text("span", "", raw));
      limits.append(row);
    }
    side.body.append(limits);
    side.box.append(side.body);
    grid.append(side.box);
    root.append(grid);
  }

  async function renderRoute() {
    const seq = ++renderSequence;
    const route = routeFromHash();
    const page = route.segments[0] || "overview";
    setBusy(true);
    loadingView();
    try {
      if (page === "saved") {
        updateNav("saved", "Saved");
        root.replaceChildren();
        renderSaved();
        return;
      }
      if (page === "methodology") {
        await renderMethodology(seq);
        return;
      }
      if (page === "job" && route.segments[1]) {
        await renderJobDetail(route.segments[1], seq);
        return;
      }
      if (page === "research" && route.segments[1] === "document" && route.segments[2] && route.segments[3]) {
        await renderResearchVersion(route.segments[2], route.segments[3], seq);
        return;
      }
      if (page === "research") {
        await renderResearch(seq);
        return;
      }
      const isOverview = page === "overview";
      updateNav(isOverview ? "overview" : "explore", isOverview ? "Overview" : "Jobs");
      const snapshot = await getJson("snapshot", requestParams(route));
      if (seq !== renderSequence) return;
      root.replaceChildren();
      if (isOverview) renderOverview(snapshot);
      else renderExplore(snapshot, route);
    } catch (error) {
      if (seq === renderSequence) errorView(error);
    } finally {
      if (seq === renderSequence) setBusy(false);
    }
  }

  window.addEventListener("hashchange", () => {
    window.scrollTo(0, 0);
    renderRoute();
  });
  refreshButton.addEventListener("click", renderRoute);
  skipLink.addEventListener("click", (event) => {
    event.preventDefault();
    document.getElementById("main").focus();
  });
  persistSavedJobs();
  renderRoute();
})();
