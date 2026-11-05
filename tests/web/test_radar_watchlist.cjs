const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const core = require("../../src/career_os/radar_web/watchlist.js");

class MemoryStorage {
  constructor(entries = {}) {
    this.values = new Map(Object.entries(entries));
  }

  getItem(key) {
    return this.values.has(key) ? this.values.get(key) : null;
  }

  setItem(key, value) {
    this.values.set(key, String(value));
  }
}

function validMetric(overrides = {}) {
  return {
    id: "technical_engineering",
    level: "occupation",
    selected: false,
    status: "provisional",
    reason: null,
    direction: "stable",
    previous_count: 100,
    current_count: 101,
    previous_denominator: 200,
    current_denominator: 200,
    previous_share: 0.5,
    current_share: 0.505,
    delta_pp: 0.5,
    quality: { passed: true },
    ...overrides,
  };
}

test("canonical targets ignore pagination and flags, deduplicate periods and cap storage", () => {
  const filters = core.normalizeFilters({
    q: "  Go roles  ", occupation: "technical_engineering", period_days: "90",
    page: "7", include_inactive: "true", enabled_only: "false",
  });
  assert.deepEqual(filters, {
    q: "Go roles", occupation: "technical_engineering", period_days: 90,
  });
  const first = { filters, label: "Go roles" };
  const second = { filters: { ...filters, q: "go ROLES", period_days: 30 }, label: "Duplicate slice" };
  assert.equal(core.deduplicate([first, second]).length, 1);

  let targets = [];
  for (let index = 0; index < core.MAX_TARGETS + 3; index += 1) {
    targets = core.add(targets, {
      filters: { occupation: `role_${index}`, period_days: 90 }, label: `Role ${index}`,
    }).targets;
  }
  assert.equal(targets.length, core.MAX_TARGETS);
});

test("storage skips invalid records, preserves unreadable payloads and reports write failures", () => {
  const key = core.STORAGE_KEY;
  const storage = new MemoryStorage({
    [key]: JSON.stringify({ version: 1, targets: [
      { filters: { source: "board-a", period_days: 90 }, label: "Board A" },
      { filters: { source: "board-a", period_days: 30 }, label: "Duplicate board" },
      { filters: { page: 3 }, label: "No substantive filter" },
    ] }),
  });
  const loaded = core.read(storage);
  assert.equal(loaded.targets.length, 1);
  assert.match(loaded.error, /invalid or duplicate/);
  assert.equal(loaded.writeBlocked, false);

  const broken = new MemoryStorage({ [key]: "{" });
  const unreadable = core.read(broken);
  assert.equal(unreadable.writeBlocked, true);
  assert.match(unreadable.error, /left unchanged/);
  assert.equal(broken.getItem(key), "{");

  const unavailable = { getItem() { return null; }, setItem() { throw new Error("quota"); } };
  assert.equal(core.read(unavailable).writeBlocked, false);
  assert.equal(core.write(unavailable, loaded.targets), false);
});

test("selected comparison follows selected_metric metadata and rejects failed quality", () => {
  const filters = { occupation: "technical_engineering", period_days: 90 };
  const history = {
    selected_metric: { kind: "occupation", id: "technical_engineering", label: "Technical engineering" },
    metrics: [
      validMetric({ id: "other_role", selected: true, direction: "up", delta_pp: 99 }),
      validMetric({ selected: false }),
    ],
  };
  const comparison = core.resolveComparison(history, filters);
  assert.equal(comparison.state, "stable");
  assert.equal(comparison.metric.id, "technical_engineering");

  const failed = core.resolveComparison({
    ...history,
    metrics: [validMetric({ direction: "up", quality: { passed: false } })],
  }, filters);
  assert.equal(failed.state, "insufficient");

  const unknownStatus = core.resolveComparison({
    ...history,
    metrics: [validMetric({ status: "up", direction: "up", delta_pp: 4 })],
  }, filters);
  assert.equal(unknownStatus.state, "unavailable");

  const noMetricTarget = core.resolveComparison(history, { source: "board-a", q: "platform", period_days: 90 });
  assert.equal(noMetricTarget.state, "coverage_only");
});

test("bounded work isolates target request failures", async () => {
  let active = 0;
  let maximum = 0;
  const results = await core.mapBounded([1, 2, 3, 4, 5], 2, async (value) => {
    active += 1;
    maximum = Math.max(maximum, active);
    await new Promise((resolve) => setTimeout(resolve, 2));
    active -= 1;
    if (value === 3) throw new Error("one target failed");
    return value * 2;
  });
  assert.equal(maximum, 2);
  assert.deepEqual(results.map((item) => item.ok), [true, true, false, true, true]);
  assert.equal(results[4].value, 10);
});

class FakeElement {
  constructor(tagName) {
    this.tagName = tagName.toUpperCase();
    this.children = [];
    this.attributes = {};
    this.dataset = {};
    this.style = {};
    this.handlers = {};
    this.className = "";
    this.hidden = false;
    this._text = "";
  }

  get textContent() {
    return this._text + this.children.map((child) => child.textContent || "").join("");
  }

  set textContent(value) {
    this._text = String(value ?? "");
    this.children = [];
  }

  get lastChild() {
    return this.children[this.children.length - 1] || null;
  }

  get childElementCount() {
    return this.children.length;
  }

  get classList() {
    return { add: (...values) => { this.className = `${this.className} ${values.join(" ")}`.trim(); } };
  }

  querySelector(selector) {
    const className = selector.startsWith(".") ? selector.slice(1) : "";
    return className
      ? findElement(this, (item) => item !== this && item.className.split(/\s+/).includes(className))
      : null;
  }

  append(...nodes) {
    this.children.push(...nodes.filter(Boolean));
  }

  prepend(...nodes) {
    this.children.unshift(...nodes.filter(Boolean));
  }

  replaceChildren(...nodes) {
    this._text = "";
    this.children = nodes.filter(Boolean);
  }

  setAttribute(name, value) {
    this.attributes[name] = String(value);
  }

  removeAttribute(name) {
    delete this.attributes[name];
  }

  addEventListener(name, handler) {
    (this.handlers[name] ||= []).push(handler);
  }

  click() {
    for (const handler of this.handlers.click || []) handler({ preventDefault() {} });
  }

  focus() {}
}

function descendants(node) {
  return [node, ...node.children.flatMap(descendants)];
}

function findElement(root, predicate) {
  return descendants(root).find(predicate) || null;
}

function makeSnapshot(params) {
  const period = Number(params.get("period_days") || 90);
  return {
    summary: { posting_count: 8, company_count: 1, source_count: 1 },
    quality: { state: "ready", stale_source_count: 0, issues: [] },
    data_as_of_utc: "2026-11-01T00:00:00+00:00",
    generated_at_utc: "2026-11-05T00:00:00+00:00",
    sources: [{ key: "board-a", company_name: "Board A", status: "succeeded", stale: false, last_success_at_utc: "2026-11-01T00:00:00+00:00" }],
    filter_options: {
      occupations: [{ id: "technical_engineering", label: "Technical engineering" }],
      specialties: [], skills: [], sources: [{ key: "board-a", company_name: "Board A" }],
    },
    occupations: [{ id: "technical_engineering", label: "Technical engineering", count: 8, share: 1 }],
    specialties: [], skills: [],
    jobs: { items: [], total: 8, page: 1, page_size: 1, total_pages: 8 },
    history: {
      period_days: period,
      status: "provisional",
      reason: null,
      selected_metric: { kind: "occupation", id: "technical_engineering", label: "Technical engineering", denominator: "N_panel" },
      metrics: [validMetric({ period_days: period, status: "provisional" })],
      windows: {
        previous: { start_utc: "2026-09-01T00:00:00+00:00", end_utc: "2026-10-01T00:00:00+00:00", N_panel: 20 },
        current: { start_utc: "2026-10-01T00:00:00+00:00", end_utc: "2026-11-01T00:00:00+00:00", N_panel: 20 },
      },
      quality: { matched_slots: 20, expected_slots: 20 },
    },
  };
}

async function waitFor(predicate, description = "") {
  for (let attempt = 0; attempt < 100; attempt += 1) {
    if (predicate()) return;
    await new Promise((resolve) => setTimeout(resolve, 2));
  }
  assert.fail(`Timed out waiting for the UI to render${description ? `: ${description}` : ""}`);
}

test("DOM flow saves applied filters, changes period, reopens exact slice and removes it", async () => {
  const sourceRoot = path.resolve(__dirname, "../../src/career_os/radar_web");
  const localStorage = new MemoryStorage({ "career-radar.saved.v1": JSON.stringify([{ id: "kept-job" }]) });
  const elements = new Map();
  const nav = ["overview", "explore", "saved", "watchlist", "research", "methodology"].map((section) => {
    const link = new FakeElement("a");
    link.dataset.nav = section;
    return link;
  });
  const document = {
    baseURI: "http://127.0.0.1:8765/radar/",
    createElement: (tag) => new FakeElement(tag),
    getElementById(id) { return elements.get(id) || null; },
    querySelector(selector) { return elements.get(selector) || null; },
    querySelectorAll(selector) { return selector === "[data-nav]" ? nav : []; },
  };
  for (const id of ["app", "breadcrumb", "saved-count", "watchlist-count", "watchlist-storage-status", "refresh-button", "main"]) {
    elements.set(id, new FakeElement("div"));
  }
  const skip = new FakeElement("a");
  elements.set(".skip-link", skip);

  let currentHash = "#/explore?occupation=technical_engineering&page=4&include_inactive=true";
  const listeners = {};
  const requestedPeriods = [];
  let resolveBoardC = null;
  let deferBoardCOnce = true;
  const window = {
    get localStorage() { return localStorage; },
    location: {},
    addEventListener(name, handler) { (listeners[name] ||= []).push(handler); },
    scrollTo() {},
  };
  Object.defineProperty(window.location, "hash", {
    get() { return currentHash; },
    set(value) {
      currentHash = value;
      for (const handler of listeners.hashchange || []) handler();
    },
  });
  const fetch = async (rawUrl) => {
    const url = new URL(rawUrl);
    requestedPeriods.push(Number(url.searchParams.get("period_days") || 90));
    const source = url.searchParams.get("source");
    if (source === "board-b" && url.searchParams.get("page_size") === "1") {
      return { ok: false, status: 503, json: async () => ({ detail: "Board B request failed" }) };
    }
    if (source === "board-c" && url.searchParams.get("page_size") === "1" && deferBoardCOnce) {
      deferBoardCOnce = false;
      return new Promise((resolve) => {
        resolveBoardC = () => resolve({ ok: true, status: 200, json: async () => makeSnapshot(url.searchParams) });
      });
    }
    return { ok: true, status: 200, json: async () => makeSnapshot(url.searchParams) };
  };
  const context = { window, document, fetch, URL, URLSearchParams, Intl, Date, Number, Math, String, Array, Object, Set, JSON, Error, Promise, setTimeout };
  vm.runInNewContext(fs.readFileSync(path.join(sourceRoot, "watchlist.js"), "utf8"), context);
  vm.runInNewContext(fs.readFileSync(path.join(sourceRoot, "app.js"), "utf8"), context);
  const root = elements.get("app");

  await waitFor(() => root.textContent.includes("Matching postings"));
  const watch = findElement(root, (item) => item.tagName === "BUTTON" && item.textContent === "Watch applied filters");
  assert.ok(watch);
  watch.click();
  await waitFor(() => root.textContent.includes("Matching postings") && localStorage.getItem(core.STORAGE_KEY));
  const stored = JSON.parse(localStorage.getItem(core.STORAGE_KEY));
  assert.equal(stored.targets.length, 1);
  assert.deepEqual(stored.targets[0].filters, { occupation: "technical_engineering", period_days: 90 });
  assert.equal(JSON.parse(localStorage.getItem("career-radar.saved.v1"))[0].id, "kept-job");

  window.location.hash = "#/explore?source=board-b";
  await waitFor(() => root.textContent.includes("Matching postings"));
  findElement(root, (item) => item.tagName === "BUTTON" && item.textContent === "Watch applied filters").click();
  await waitFor(() => JSON.parse(localStorage.getItem(core.STORAGE_KEY)).targets.length === 2);

  window.location.hash = "#/watchlist";
  await waitFor(() => root.textContent.includes("Current saved active postings") && root.textContent.includes("Snapshot request failed"));
  assert.match(root.textContent, /8/);
  const period = findElement(root, (item) => item.tagName === "BUTTON" && item.textContent === "30 days");
  period.click();
  await waitFor(() => requestedPeriods.includes(30) && root.textContent.includes("30-day history"));
  const reopen = findElement(root, (item) => item.tagName === "A" && item.textContent === "View filtered postings →"
    && item.href.includes("occupation=technical_engineering"));
  assert.match(reopen.href, /occupation=technical_engineering.*period_days=30/);

  window.location.hash = "#/explore?source=board-c";
  await waitFor(() => root.textContent.includes("Matching postings"));
  findElement(root, (item) => item.tagName === "BUTTON" && item.textContent === "Watch applied filters").click();
  await waitFor(() => JSON.parse(localStorage.getItem(core.STORAGE_KEY)).targets.length === 3);

  window.location.hash = "#/watchlist?period_days=30";
  await waitFor(() => resolveBoardC !== null);
  window.location.hash = "#/saved";
  await waitFor(() => root.textContent.includes("Saved jobs"), root.textContent);
  resolveBoardC();
  await new Promise((resolve) => setTimeout(resolve, 15));
  assert.match(root.textContent, /Saved jobs/);
  assert.doesNotMatch(root.textContent, /Company and role signals/);

  window.location.hash = "#/watchlist?period_days=30";
  await waitFor(() => root.textContent.includes("Current saved active postings") && root.textContent.includes("Snapshot request failed"));

  for (let remaining = 3; remaining > 0; remaining -= 1) {
    const remove = findElement(root, (item) => item.tagName === "BUTTON" && item.textContent === "Remove");
    assert.ok(remove, `expected remove button with ${remaining} targets`);
    remove.click();
    if (remaining === 1) await waitFor(() => root.textContent.includes("Your Watchlist is empty"));
    else await waitFor(() => JSON.parse(localStorage.getItem(core.STORAGE_KEY)).targets.length === remaining - 1
      && root.textContent.includes("Current saved active postings"));
  }
  assert.equal(JSON.parse(localStorage.getItem(core.STORAGE_KEY)).targets.length, 0);
  assert.equal(JSON.parse(localStorage.getItem("career-radar.saved.v1"))[0].id, "kept-job");
});
