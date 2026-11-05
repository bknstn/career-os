(function attachWatchlistCore(global) {
  "use strict";

  const STORAGE_KEY = "career-radar.watchlist.v1";
  const VERSION = 1;
  const MAX_TARGETS = 20;
  const PERIODS = new Set([30, 90, 180]);
  const COMPARABLE_STATUSES = new Set(["provisional", "available", "sufficient"]);
  const FILTER_KEYS = ["q", "occupation", "specialty", "skill", "source"];

  function cleanText(value, maximum) {
    if (typeof value !== "string") return null;
    const cleaned = value.trim();
    if (!cleaned || cleaned.length > maximum || /[\u0000-\u001f\u007f]/.test(cleaned)) return null;
    return cleaned;
  }

  function normalizeFilters(input) {
    if (!input || typeof input !== "object" || Array.isArray(input)) return null;
    const filters = {};
    for (const key of FILTER_KEYS) {
      if (input[key] === undefined || input[key] === null || input[key] === "") continue;
      const value = cleanText(input[key], key === "q" ? 200 : 160);
      if (!value) return null;
      filters[key] = value;
    }
    if (!FILTER_KEYS.some((key) => filters[key])) return null;
    const period = Number(input.period_days ?? 90);
    if (!Number.isInteger(period) || !PERIODS.has(period)) return null;
    filters.period_days = period;
    return filters;
  }

  function filterKey(filters) {
    const normalized = normalizeFilters(filters);
    if (!normalized) return null;
    const identity = { ...normalized };
    delete identity.period_days;
    if (identity.q) identity.q = identity.q.toLocaleLowerCase("en");
    return JSON.stringify(identity);
  }

  function normalizeTarget(record) {
    if (!record || typeof record !== "object" || Array.isArray(record)) return null;
    const filters = normalizeFilters(record.filters);
    const label = cleanText(record.label, 120);
    return filters && label ? { filters, label } : null;
  }

  function deduplicate(targets) {
    const result = [];
    const seen = new Set();
    for (const item of Array.isArray(targets) ? targets : []) {
      const target = normalizeTarget(item);
      if (!target) continue;
      const key = filterKey(target.filters);
      if (seen.has(key)) continue;
      seen.add(key);
      result.push(target);
      if (result.length >= MAX_TARGETS) break;
    }
    return result;
  }

  function read(storage, key = STORAGE_KEY) {
    if (!storage || typeof storage.getItem !== "function") {
      return { targets: [], error: "Browser storage is unavailable.", writeBlocked: true };
    }
    let serialized;
    try {
      serialized = storage.getItem(key);
    } catch {
      return { targets: [], error: "Browser storage could not be read.", writeBlocked: true };
    }
    if (serialized === null || serialized === "") {
      return { targets: [], error: null, writeBlocked: false };
    }
    let stored;
    try {
      stored = JSON.parse(serialized);
    } catch {
      return { targets: [], error: "Watchlist data could not be read and was left unchanged.", writeBlocked: true };
    }
    if (!stored || typeof stored !== "object" || stored.version !== VERSION || !Array.isArray(stored.targets)) {
      return { targets: [], error: "Watchlist data has an unsupported format and was left unchanged.", writeBlocked: true };
    }
    const valid = stored.targets.map(normalizeTarget).filter(Boolean);
    const targets = deduplicate(valid);
    const removed = stored.targets.length - valid.length;
    const deduped = valid.length - targets.length;
    return {
      targets,
      error: removed || deduped
        ? "Some invalid or duplicate Watchlist entries were skipped."
        : null,
      writeBlocked: false,
    };
  }

  function write(storage, targets, key = STORAGE_KEY) {
    if (!storage || typeof storage.setItem !== "function") return false;
    try {
      storage.setItem(key, JSON.stringify({ version: VERSION, targets: deduplicate(targets) }));
      return true;
    } catch {
      return false;
    }
  }

  function add(targets, target) {
    const normalized = normalizeTarget(target);
    if (!normalized) return { targets: deduplicate(targets), added: false, reason: "invalid" };
    const current = deduplicate(targets);
    const key = filterKey(normalized.filters);
    if (current.some((item) => filterKey(item.filters) === key)) {
      return { targets: current, added: false, reason: "duplicate" };
    }
    if (current.length >= MAX_TARGETS) {
      return { targets: current, added: false, reason: "limit" };
    }
    return { targets: [normalized, ...current], added: true, reason: null };
  }

  function remove(targets, filters) {
    const key = filterKey(filters);
    if (!key) return deduplicate(targets);
    return deduplicate(targets).filter((item) => filterKey(item.filters) !== key);
  }

  async function mapBounded(items, limit, worker) {
    const values = Array.from(items || []);
    const results = new Array(values.length);
    const concurrency = Math.max(1, Math.min(values.length || 1, Math.floor(Number(limit) || 1)));
    let next = 0;
    await Promise.all(Array.from({ length: concurrency }, async () => {
      while (next < values.length) {
        const index = next;
        next += 1;
        try {
          results[index] = { ok: true, value: await worker(values[index], index) };
        } catch (error) {
          results[index] = { ok: false, error };
        }
      }
    }));
    return results;
  }

  function numeric(value) {
    return typeof value === "number" && Number.isFinite(value);
  }

  function resolveComparison(history, filters) {
    const normalized = normalizeFilters(filters);
    if (!normalized || !normalized.occupation && !normalized.specialty && !normalized.skill) {
      return { state: "coverage_only", metric: null, reason: null };
    }
    const metadata = history && history.selected_metric;
    if (!metadata || typeof metadata.kind !== "string" || typeof metadata.id !== "string") {
      return { state: "unavailable", metric: null, reason: history && history.reason || "missing_history" };
    }
    const metric = (Array.isArray(history.metrics) ? history.metrics : []).find((item) =>
      item && item.level === metadata.kind && item.id === metadata.id
    );
    if (!metric) {
      return { state: "unavailable", metric: null, reason: history && history.reason || "missing_history" };
    }
    const valid = metric.quality && metric.quality.passed === true
      && numeric(metric.delta_pp)
      && numeric(metric.previous_share)
      && numeric(metric.current_share)
      && numeric(metric.previous_denominator) && metric.previous_denominator > 0
      && numeric(metric.current_denominator) && metric.current_denominator > 0
      && numeric(metric.previous_count) && numeric(metric.current_count)
      && ["up", "down", "stable"].includes(metric.direction)
      && COMPARABLE_STATUSES.has(String(metric.status || "").toLowerCase());
    return {
      state: valid ? metric.direction
        : metric.direction === "uncertain" || metric.status === "uncertain" ? "uncertain"
          : metric.status === "insufficient" || !metric.quality || metric.quality.passed !== true ? "insufficient"
            : "unavailable",
      metric,
      reason: valid ? null : metric.reason || history.reason || "sample_threshold",
    };
  }

  const api = Object.freeze({
    STORAGE_KEY,
    VERSION,
    MAX_TARGETS,
    normalizeFilters,
    filterKey,
    normalizeTarget,
    deduplicate,
    read,
    write,
    add,
    remove,
    mapBounded,
    resolveComparison,
  });

  if (typeof module === "object" && module.exports) module.exports = api;
  if (global) global.CareerRadarWatchlistCore = api;
})(typeof window === "undefined" ? globalThis : window);
