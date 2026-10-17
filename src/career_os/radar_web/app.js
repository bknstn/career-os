(() => {
  "use strict";

  const API_BASE = new URL("api/", document.baseURI);
  const STORAGE_KEY = "career-radar.saved.v1";
  const root = document.getElementById("app");
  const breadcrumb = document.getElementById("breadcrumb");
  const savedCount = document.getElementById("saved-count");
  const refreshButton = document.getElementById("refresh-button");
  const skipLink = document.querySelector(".skip-link");
  const dateFormatter = new Intl.DateTimeFormat("ru-RU", {
    dateStyle: "medium",
    timeStyle: "short",
    timeZone: "Europe/Warsaw",
  });
  let renderSequence = 0;
  let savedJobs = readSavedJobs();
  let showAllSources = false;

  const ISSUE_COPY = {
    source_stale: "Некоторые источники давно не проходили успешное полное сканирование.",
    occupation_unknown_above_threshold: "Доля нераспознанных ролей в исходной панели выше порога сопоставимости.",
    occupation_unknown_above_reference: "Доля нераспознанных ролей в исходной панели выше контрольного уровня.",
    description_missing: "У части вакансий нет описания, поэтому распознавание навыков неполное.",
  };
  const HISTORY_COPY = {
    missing_history: "Нет подходящих неизменяемых наблюдений для выбранного периода.",
    panel_not_registered: "Состав источников ещё не зафиксирован для исторического сравнения.",
    panel_source_mismatch: "Исторические данные не совпадают с источниками в текущем составе панели.",
    panel_manifest_unavailable: "Не удалось подтвердить зафиксированный состав источников для сравнения.",
    source_outside_panel: "Выбранный источник не входит в зафиксированную историческую панель.",
    missing_history_tables: "В сохранённых данных пока нет таблиц для сравнения сканов.",
    missing_history_columns: "В сохранённых данных пока нет полей для проверки истории.",
    coverage_below_threshold: "Мало сопоставимых сканов, чтобы сравнение прошло проверку полноты.",
    board_coverage_below_threshold: "Часть источников представлена слишком редко для сопоставления.",
    occupation_unknown_above_threshold: "Нераспознанных ролей слишком много для надёжного сравнения ролей.",
    specialty_unresolved_above_threshold: "Специализации распознаны недостаточно уверенно для сравнения.",
    description_coverage_below_threshold: "Недостаточно описаний для сопоставления упоминаний навыков.",
    category_sample_below_threshold: "Для этой категории пока мало парных наблюдений.",
    zero_denominator: "Невозможно рассчитать долю: в одном из периодов нет объявлений для сравнения.",
    panel_identity_mismatch: "Состав или настройки источников изменились, поэтому периоды несопоставимы.",
    history_present: "Для выбранного окна нет парных успешных сканов.",
    overall_coverage: "Общее покрытие парными сканами ниже порога для сравнения.",
    board_coverage: "Часть источников представлена слишком редко для сопоставления.",
    occupation_unknown_threshold: "Нераспознанных ролей слишком много для надёжного сравнения ролей.",
    specialty_unresolved_threshold: "Специализации распознаны недостаточно уверенно для сравнения.",
    description_coverage_threshold: "Недостаточно описаний для сравнения навыков.",
    sample_threshold: "Для этой метрики пока мало вакансий или компаний в парных окнах.",
    positive_denominator: "Нельзя сравнить долю без объявлений в обоих периодах.",
  };
  const LIMITATION_COPY = {
    "Current results describe saved inventory, not the full external labor market.": "Срез отражает сохранённые объявления, а не весь внешний рынок труда.",
    "Unknown occupations and missing descriptions remain visible in coverage fields.": "Нераспознанные роли и вакансии без описаний остаются видимыми в показателях покрытия.",
    "Historical comparisons are null/insufficient until their own matched-panel, coverage, and sample gates pass.": "Исторические сравнения остаются пустыми, пока не проходят собственные проверки панели, покрытия и размера выборки.",
  };

  function issueMessage(code) {
    return ISSUE_COPY[String(code)] || "Есть ограничения полноты данных; подробности описаны в методологии.";
  }

  function historyReasonMessage(reason) {
    return HISTORY_COPY[String(reason)] || "Недостаточно сопоставимых данных для этого исторического сравнения.";
  }

  function historyStatusLabel(status, direction = "") {
    if (direction === "uncertain" || status === "uncertain") return "Пока неопределённо";
    if (status === "insufficient") return "Недостаточно данных";
    if (status === "available" || status === "provisional" || status === "sufficient") return "Сравнение доступно";
    return "История не подтверждена";
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
        title: String(job.title || "Название не указано"),
        company_name: String(job.company_name || "Компания не указана"),
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
    if (!value) return "дата неизвестна";
    const date = new Date(value);
    return Number.isNaN(date.valueOf()) ? "дата неизвестна" : dateFormatter.format(date);
  }

  function formatNumber(value) {
    if (value === null || value === undefined || !Number.isFinite(Number(value))) return "—";
    return new Intl.NumberFormat("ru-RU").format(Number(value));
  }

  function formatPercent(value) {
    if (value === null || value === undefined || !Number.isFinite(Number(value))) return "—";
    const numeric = Number(value);
    const percentage = Math.abs(numeric) <= 1 ? numeric * 100 : numeric;
    return `${new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 1 }).format(percentage)}%`;
  }

  const LABELS = {
    technical_engineering: "Инженерные роли",
    engineering_management: "Инженерное руководство",
    solutions_architecture: "Архитектура решений",
    nontechnical: "Нетехнические роли",
    unknown: "Не распознано",
    backend_engineering: "Серверная разработка",
    frontend_engineering: "Разработка интерфейсов",
    fullstack_engineering: "Full-stack разработка",
    data_engineering: "Инженерия данных",
    product_engineering: "Продуктовая разработка",
    platform_infrastructure_engineering: "Платформа и инфраструктура",
    machine_learning_engineering: "Инженерия машинного обучения",
    ai_engineering: "Инженерия ИИ",
    ai_product_engineering: "Продуктовая инженерия ИИ",
  };
  const SPECIALTY_STATES = {
    resolved: "Распознана",
    missing: "Не указана в названии",
    unsupported: "Вне текущей таксономии",
    ambiguous: "Неоднозначная",
    not_assessed: "Не оценивалась",
    not_applicable: "Не применяется",
  };
  const JOB_STATES = {
    active: "Активна в сохранённом срезе",
    inactive: "Неактивна в сохранённом срезе",
  };

  function displayLabel(value, fallback = "Не распознано") {
    if (value && typeof value === "object") return String(LABELS[value.id] || value.label || value.id || fallback);
    if (value === null || value === undefined || value === "") return fallback;
    return LABELS[String(value)] || String(value);
  }

  function displayState(value, dictionary) {
    if (!value) return "не указано";
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
      : total ? formatPercent(unknown === undefined ? null : unknown / total) : "доля не указана";
    const parts = [];
    if (unknown !== undefined || share !== undefined) {
      parts.push(`${unknown === undefined ? "число неизвестно" : `${formatNumber(unknown)} ролей не распознано`} (${unknownShare})`);
    }
    if (missingDescription !== undefined || missingShare !== undefined) {
      const descriptionShare = missingShare !== undefined ? formatPercent(missingShare)
        : total ? formatPercent(missingDescription / total) : "доля не указана";
      parts.push(`${missingDescription === undefined ? "число неизвестно" : `${formatNumber(missingDescription)} без описания`} (${descriptionShare})`);
    }
    const totalText = total === undefined ? "в исходной панели" : `из ${formatNumber(total)} вакансий исходной панели`;
    return `Базовая панель: ${parts.join("; ")} ${totalText}; фильтры выдачи не меняют эту оценку.`;
  }

  function shortDate(value) {
    if (!value) return "последний успешный скан неизвестен";
    return `скан: ${formatDate(value)}`;
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
      throw new Error(typeof detail === "string" ? detail : `Сервер ответил с кодом ${response.status}.`);
    }
    if (!payload || typeof payload !== "object") throw new Error("Сервер вернул неожиданный ответ.");
    return payload;
  }

  function setBusy(busy) {
    root.setAttribute("aria-busy", busy ? "true" : "false");
  }

  function loadingView() {
    root.replaceChildren();
    const state = element("div", "loading-state");
    const inner = element("div");
    append(inner, element("div", "loading-mark"), text("span", "", "Загружаем срез рынка…"));
    state.append(inner);
    root.append(state);
  }

  function errorView(error) {
    root.replaceChildren();
    const wrap = element("section", "error-banner");
    const copy = element("div");
    append(copy,
      text("strong", "", "Не удалось загрузить данные"),
      text("p", "", error instanceof Error ? error.message : "Проверьте соединение с локальным приложением."));
    const retry = element("button", "button button-small", "Повторить");
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
    append(copy, text("p", "eyebrow", kicker), text("h1", "", title));
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
    copyBody.append(text("div", "snapshot-title", "Сохранённый срез вакансий"));
    const asOf = data.data_as_of_utc ? formatDate(data.data_as_of_utc) : "дата среза не указана";
    const qualityNote = Array.isArray(quality.issues) && quality.issues.length
      ? [...new Set(quality.issues.map(issueMessage))].join(" · ")
      : "Статус active отражает последнее сохранённое состояние, а не гарантию открытой вакансии.";
    const baseCoverage = baseCoverageDescription(data);
    copyBody.append(text("div", "snapshot-description", `Срез на ${asOf}. ${qualityNote}${baseCoverage ? ` ${baseCoverage}` : ""}`));
    const sources = Array.isArray(data.sources) ? data.sources : [];
    const staleCount = Number(quality.stale_source_count || sources.filter((source) => source.stale).length || 0);
    const meta = text("div", "snapshot-meta", staleCount > 0 ? `${staleCount} источн. требуют внимания` : `Сформировано ${formatDate(data.generated_at_utc)}`);
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
    const link = element("a", "panel-link", "Все вакансии →");
    link.href = "#/explore";
    const view = panel(title, denominatorLabel, link);
    const list = element("div", "distribution-list");
    const rows = Array.isArray(items) ? items.slice(0, 6) : [];
    const maxCount = Math.max(1, ...rows.map((item) => Number(item.count) || 0), Number(unknownCount) || 0);
    for (const item of rows) {
      const label = displayLabel({ id: item.id, label: item.label }, "Без названия");
      const row = element("button", "distribution-row");
      row.type = "button";
      row.setAttribute("aria-label", `Открыть вакансии: ${label}, ${formatNumber(item.count)}`);
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
    const hasUnknownItem = rows.some((item) => /unknown|unclassified|не распозн|неизвест/i.test(`${item.id || ""} ${item.label || ""}`));
    if (hasUnknown && !hasUnknownItem && routeType === "occupation") {
      const row = element("button", "distribution-row unknown-row");
      row.type = "button";
      row.setAttribute("aria-label", `Показать нераспознанные роли: ${formatNumber(unknownCount)}`);
      append(row,
        text("span", "distribution-name", "Не распознано"),
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
    if (!rows.length && !hasUnknown) list.append(text("div", "empty-inline", "Пока нет данных для этой сводки."));
    view.body.append(list);
    view.body.append(text("p", "mini-note", routeType === "skill"
      ? "Доля — вакансии, где навык буквально найден в тексте. Объявления без распознанного текста остаются в знаменателе; это не список требований или оценка спроса."
      : "Метки — результаты правил классификатора. Нераспознанные названия остаются в срезе."));
    view.box.append(view.body);
    return view.box;
  }

  function sourceStatus(source) {
    const status = String(source.status || "never").toLowerCase();
    if (source.stale || ["never", "failed", "no recent result", "no_recent_result"].includes(status) || !source.last_success_at_utc) {
      const label = status === "never" || status.startsWith("no recent") || status === "no_recent_result" ? "нет успешного скана" : "устарел / ошибка";
      return { label, style: "status-pill status-error" };
    }
    if (status === "partial") return { label: "частичный скан", style: "status-pill status-warning" };
    if (status === "succeeded") return { label: "успешный скан", style: "status-pill" };
    return { label: "статус не подтверждён", style: "status-pill status-warning" };
  }

  function sourcesPanel(sources) {
    const items = Array.isArray(sources) ? sources : [];
    const expand = items.length > 7 ? element("button", "panel-link source-toggle", showAllSources ? "Свернуть" : `Показать все ${formatNumber(items.length)}`) : null;
    if (expand) {
      expand.type = "button";
      expand.setAttribute("aria-expanded", showAllSources ? "true" : "false");
      expand.setAttribute("aria-controls", "radar-sources-list");
    }
    const view = panel("Источники", "Свежесть последнего успешного полного скана", expand);
    const list = element("div", "source-list");
    list.id = "radar-sources-list";
    const renderRows = () => {
      list.replaceChildren();
      const visibleItems = showAllSources ? items : items.slice(0, 7);
      for (const source of visibleItems) {
        const row = element("div", "source-row");
        const identity = element("div");
        append(identity,
          text("div", "source-name", source.company_name || source.key || "Источник"),
          text("div", "source-detail", shortDate(source.last_success_at_utc))
        );
        const status = sourceStatus(source);
        append(row, identity, text("span", status.style, status.label));
        list.append(row);
      }
      if (!visibleItems.length) list.append(text("p", "mini-note", "В ответе пока нет списка источников."));
    };
    renderRows();
    if (expand) expand.addEventListener("click", () => {
      showAllSources = !showAllSources;
      expand.textContent = showAllSources ? "Свернуть" : `Показать все ${formatNumber(items.length)}`;
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
    const titleLink = element("a", "", job.title || "Название не указано");
    titleLink.href = detailHref(job);
    title.append(titleLink);
    append(identity, title, text("div", "job-company", job.company_name || "Компания не указана"));
    const actions = element("div", "job-actions");
    if (options.allowSave !== false) {
      const saved = isSaved(job.id);
      const button = element("button", "save-button", saved ? "♥" : "♡");
      button.type = "button";
      button.setAttribute("aria-pressed", saved ? "true" : "false");
      button.setAttribute("aria-label", saved ? "Убрать из сохранённого" : "Сохранить вакансию");
      button.title = saved ? "Убрать из сохранённого" : "Сохранить вакансию";
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
    const seen = text("span", "", `Последний раз замечена ${formatDate(job.last_seen_at_utc)}`);
    append(tail, seen);
    const sourceLink = externalAnchor(job.source_url, "Источник ↗", "");
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
    const title = `Сравнение за ${formatNumber(history && history.period_days || 90)} дней`;
    const tabs = element("div", "period-tabs");
    tabs.setAttribute("role", "group");
    tabs.setAttribute("aria-label", "Период исторического сравнения");
    const selectedPeriod = Number(history && history.period_days || route && route.params.get("period_days") || 90);
    for (const periodDays of [30, 90, 180]) {
      const tab = element("button", "period-tab", `${periodDays} дн.`);
      tab.type = "button";
      tab.setAttribute("aria-pressed", periodDays === selectedPeriod ? "true" : "false");
      tab.addEventListener("click", () => changePeriod(periodDays, route));
      tabs.append(tab);
    }
    const view = panel(title, "Историческое сравнение показывается только при наличии сопоставимых сканов.", tabs);
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
        const metricLabel = `${item.label || displayLabel(item.id || item.level, "Метрика")}${item.previous_count !== null && item.previous_count !== undefined && item.current_count !== null && item.current_count !== undefined ? ` · ${formatNumber(item.previous_count)} → ${formatNumber(item.current_count)}` : ""}`;
        append(card,
          text("div", "history-period", metricLabel),
          text("div", "history-state", item.delta_pp === null || item.delta_pp === undefined
            ? historyStatusLabel(item.status, item.direction)
            : `${Number(item.delta_pp) > 0 ? "+" : ""}${formatNumber(item.delta_pp)} п.п.`),
          text("div", "history-reason", item.reason ? historyReasonMessage(item.reason) : item.direction === "uncertain" ? "Направление пока неопределённо." : `Текущая доля: ${formatPercent(item.current_share)}; предыдущая: ${formatPercent(item.previous_share)}.`)
        );
        cards.append(card);
      }
    } else {
      const card = element("div", "history-card");
      append(card,
        text("div", "history-period", `Окно ${formatNumber(info.period_days || 90)} дней`),
        text("div", "history-state", historyStatusLabel(state, info.direction)),
        text("div", "history-reason", reason)
      );
      cards.append(card);
    }
    view.body.append(cards);
    view.body.append(text("div", "metric-note", "Сравнение строится по парным неизменяемым наблюдениям и проверкам качества. Пустая история не означает нулевой спрос."));
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

  function jobsPanel(items, total, title = "Недавно замеченные вакансии", showAll = true) {
    const link = showAll ? element("a", "panel-link", "Смотреть все →") : null;
    if (link) link.href = "#/explore";
    const view = panel(title, "Карточки отражают сохранённые описания и время последнего наблюдения.", link);
    const list = element("div", "job-list");
    const rows = Array.isArray(items) ? items : [];
    if (rows.length) for (const job of rows) list.append(jobCard(job));
    else list.append(emptyState("⌕", "Вакансий пока нет", "После успешного сбора источников объявления появятся здесь. Неизвестные роли остаются видимыми в общей выдаче."));
    view.body.append(list);
    if (total !== null && total !== undefined) {
      const footer = element("div", "list-footer");
      footer.append(text("span", "mini-note", `Всего в выбранном срезе: ${formatNumber(total)}`));
      view.body.append(footer);
    }
    view.box.append(view.body);
    return view.box;
  }

  function renderOverview(data) {
    const summary = data.summary || {};
    append(root,
      pageHeading("РАДАР РЫНКА", "Роли и навыки в реальных вакансиях", "Исследуйте объявления на собранных страницах компаний. Сводки показывают распознанные упоминания и сохраняют неизвестные случаи.", actionGroup(buttonLink("#/explore", "Все вакансии", "button"), buttonLink("#/explore?occupation=technical_engineering", "Технические роли", "button button-primary"))),
      snapshotBanner(data)
    );
    const stats = element("div", "stat-grid");
    append(stats,
      statCard("Вакансии в срезе", summary.posting_count, "Сохранённые активные объявления", "▤"),
      statCard("Компании", summary.company_count, `${formatNumber(summary.source_count)} источников в панели`, "⌂"),
      statCard("Инженерные роли", summary.technical_engineering_count, "По правилам классификатора", "⌘"),
      statCard("Роль не распознана", summary.unknown_occupation_count, `${formatNumber(summary.missing_description_count)} без описания`, "?"));
    root.append(stats);

    const grid = element("div", "content-grid");
    const left = element("div", "stack");
    const right = element("div", "stack");
    left.append(distributionPanel("Распределение по ролям", data.occupations, summary.posting_count, "occupation", "Доля от всех вакансий с учётом неизвестных", summary.unknown_occupation_count));
    left.append(distributionPanel("Специализации", data.specialties, summary.technical_engineering_count, "specialty", "Доля от технических инженерных вакансий"));
    left.append(distributionPanel("Частые упоминания навыков", data.skills, summary.posting_count, "skill", "Доля вакансий с найденным упоминанием"));
    left.append(jobsPanel((data.jobs && data.jobs.items || []).slice(0, 5), data.jobs && data.jobs.total, "Свежие наблюдения"));
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
      const retained = element("option", "", `Текущий фильтр · ${LABELS[currentValue] || currentValue}`);
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
    queryInput.placeholder = "Название, компания или текст описания";
    queryInput.value = params.get("q") || "";
    queryInput.setAttribute("aria-label", "Поиск по вакансиям");
    const queryLabel = text("label", "", "Поиск");
    queryLabel.htmlFor = queryInput.id;
    append(queryField, queryLabel, queryInput);
    grid.append(queryField);
    const catalogs = data.filter_options || {};
    grid.append(selectField("Роль", "occupation", catalogs.occupations || data.occupations || [], params.get("occupation"), "Все роли"));
    grid.append(selectField("Специализация", "specialty", catalogs.specialties || data.specialties || [], params.get("specialty"), "Все специализации"));
    grid.append(selectField("Навык", "skill", catalogs.skills || data.skills || [], params.get("skill"), "Любой навык"));
    const sourceOptions = catalogs.sources || (data.sources || []).map((source) => ({ key: source.key, company_name: source.company_name }));
    grid.append(selectField("Источник", "source", sourceOptions, params.get("source"), "Все источники"));
    const period = selectField("Окно истории", "period_days", [
      { id: "30", label: "30 дней" }, { id: "90", label: "90 дней" }, { id: "180", label: "180 дней" },
    ], params.get("period_days") || "90", null);
    grid.append(period);
    const submitWrap = element("div", "field filter-submit");
    const submit = element("button", "button button-primary", "Применить фильтры");
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
    const includeLabel = element("label", "check-field", "Включить неактивные объявления");
    includeLabel.prepend(includeInactive);
    const enabledOnly = element("input");
    enabledOnly.type = "checkbox";
    enabledOnly.name = "enabled_only";
    enabledOnly.value = "true";
    enabledOnly.checked = params.get("enabled_only") !== "false";
    const enabledLabel = element("label", "check-field", "Только включённые источники");
    enabledLabel.prepend(enabledOnly);
    append(checks, includeLabel, element("span", "", "　"), enabledLabel);
    const hint = text("span", "filter-status", "Период меняет только историческое сравнение, а не список сохранённых вакансий.");
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
    const headingTitle = isOccupation ? (selected ? displayLabel(selected) : "Вакансии по роли")
      : isSkill ? (selected && selected.label || "Вакансии с упоминанием навыка") : "Поиск по вакансиям";
    const headingCopy = isOccupation
      ? "Результат классификации по правилам Career Radar. Совпадение роли не означает, что она подтверждена работодателем."
      : isSkill ? "Откройте объявления, где термин найден в заголовке или описании. Упоминание не подтверждает обязательность навыка." : "Фильтруйте сохранённые объявления и переходите к тексту вакансии и основаниям распознавания.";
    append(root, pageHeading(isOccupation ? "РОЛЬ" : isSkill ? "НАВЫК" : "ИССЛЕДОВАНИЕ", headingTitle, headingCopy));
    root.append(snapshotBanner(data));
    root.append(buildFilterForm(data, route));

    const split = element("div", "content-grid");
    const mainStack = element("div", "stack");
    const sideStack = element("div", "stack");
    const summary = data.summary || {};
    const jobs = data.jobs || { items: [], total: 0, page: 1, page_size: 25, total_pages: 0 };
    const results = element("section");
    const resultsHeading = element("div", "results-header");
    append(resultsHeading, text("h2", "", "Найденные объявления"), text("span", "results-count", `${formatNumber(jobs.total)} вакансий`));
    results.append(resultsHeading);
    if (Array.isArray(jobs.items) && jobs.items.length) {
      const list = element("div", "panel job-list");
      for (const job of jobs.items) list.append(jobCard(job));
      results.append(list);
      results.append(pagination(jobs));
    } else {
      const none = emptyState("⌕", "Ничего не найдено", "Измените запрос или сбросьте часть фильтров. Неизвестные роли и описания без распознанных навыков не исключаются сами по себе.", buttonLink("#/explore", "Сбросить фильтры", "button"));
      results.append(none);
    }
    mainStack.append(results);
    sideStack.append(distributionPanel("Роли в текущем срезе", data.occupations, summary.posting_count, "occupation", "Доля от отфильтрованных вакансий", summary.unknown_occupation_count));
    sideStack.append(distributionPanel("Специализации", data.specialties, summary.technical_engineering_count, "specialty", "Доля от выбранных инженерных вакансий"));
    sideStack.append(distributionPanel("Упоминания навыков", data.skills, summary.posting_count, "skill", "Доля от отфильтрованных вакансий"));
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
    wrap.append(text("span", "pagination-label", `${formatNumber(start)}–${formatNumber(end)} из ${formatNumber(jobs.total || 0)}`));
    const actions = element("div", "pagination-actions");
    const previous = element("button", "button button-small", "← Назад");
    previous.type = "button";
    previous.disabled = page <= 1;
    previous.addEventListener("click", () => setPage(page - 1));
    const next = element("button", "button button-small", "Дальше →");
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
    const explore = buttonLink("#/explore", "Найти вакансии", "button button-primary");
    actions.append(explore);
    append(root,
      pageHeading("ВАШ СПИСОК", "Сохранённые вакансии", "Список хранится только в localStorage этого браузера. Вы можете вернуться к объявлениям из исследования.", actions),
      snapshotInfoLocal()
    );
    if (!savedJobs.length) {
      root.append(emptyState("♡", "Здесь появятся интересные позиции", "Нажмите на сердечко в карточке вакансии, чтобы добавить её в список для дальнейшего изучения.", buttonLink("#/explore", "Перейти к вакансиям", "button button-primary")));
      return;
    }
    const list = element("section", "panel job-list");
    for (const job of savedJobs) {
      const card = jobCard(job);
      const footer = card.querySelector(".job-tail");
      if (footer) footer.prepend(text("span", "", `В списке с ${formatDate(job.saved_at)}`));
      list.append(card);
    }
    const clear = element("button", "button button-danger button-small", "Очистить список");
    clear.type = "button";
    clear.addEventListener("click", () => {
      savedJobs = [];
      persistSavedJobs();
      renderRoute();
    });
    const tools = element("div", "filter-bottom");
    append(tools, text("span", "filter-status", `${formatNumber(savedJobs.length)} вакансий · только в этом браузере`), clear);
    list.append(tools);
    root.append(list);
  }

  function snapshotInfoLocal() {
    const banner = element("section", "snapshot-banner");
    const copy = element("div", "snapshot-copy");
    const body = element("div");
    append(copy, text("span", "snapshot-symbol", "⌂"), body);
    append(body,
      text("div", "snapshot-title", "Локальные данные пользователя"),
      text("div", "snapshot-description", "Сохранённые вакансии не отправляются на сервер. Их карточки отражают значения в момент сохранения.")
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
    const title = summary.title || "Название не указано";
    updateNav("explore", "Вакансия");
    const back = buttonLink("#/explore", "← К списку", "button");
    const savedButton = element("button", "button", isSaved(summary.id || id) ? "♥ В сохранённом" : "♡ Сохранить");
    savedButton.type = "button";
    savedButton.setAttribute("aria-pressed", isSaved(summary.id || id) ? "true" : "false");
    savedButton.addEventListener("click", () => saveJob({ ...summary, id: summary.id || id }));
    append(root, pageHeading("ВАКАНСИЯ", title, summary.company_name || "Компания не указана", actionGroup(back, savedButton)));

    const linkActions = element("div", "detail-links");
    const source = externalAnchor(summary.source_url, "Открыть источник ↗", "button button-primary");
    const apply = externalAnchor(summary.apply_url, "Откликнуться ↗", "button");
    append(linkActions, source, apply);
    const metaTags = element("div", "job-meta");
    if (summary.occupation) metaTags.append(text("span", "tag tag-accent", displayLabel(summary.occupation)));
    if (summary.specialty) metaTags.append(text("span", "tag tag-blue", displayLabel(summary.specialty)));
    if (summary.status) metaTags.append(text("span", "tag tag-muted", displayState(summary.status, JOB_STATES)));

    const layout = element("div", "detail-layout");
    const main = element("section", "panel");
    const desc = element("section", "detail-section");
    desc.append(text("h2", "", "Описание вакансии"));
    desc.append(text("p", "", description === null ? "Полный текст описания недоступен." : description));
    main.append(desc);
    main.append(evidenceSection("Основание распознавания роли", roleEvidence));
    main.append(evidenceSection("Найденные упоминания навыков", skillEvidence));
    const side = element("aside", "stack detail-side");
    const meta = panel("Сведения о наблюдении", "Сохранённые поля источника");
    const metaBody = meta.body;
    const values = [
      ["Источник", summary.source_key || "не указан"],
      ["Последнее наблюдение", formatDate(summary.last_seen_at_utc)],
      ["Состояние", displayState(summary.status, JOB_STATES)],
      ["Специализация", displayLabel(summary.specialty, "не распознана")],
      ["Статус специализации", displayState(summary.specialty_state, SPECIALTY_STATES)],
      ["Навыки в выдаче", formatNumber(Array.isArray(summary.skills) ? summary.skills.length : 0)],
    ];
    for (const [key, value] of values) {
      const row = element("div", "key-value");
      append(row, text("span", "", key), text("span", "", value));
      metaBody.append(row);
    }
    meta.box.append(metaBody);
    side.append(meta.box);
    const note = panel("Как читать эту карточку", "");
    note.body.append(text("p", "mini-note", "Классификация и навыки — автоматические совпадения правил с сохранённым текстом. Источник может уже изменить или закрыть вакансию."));
    note.box.append(note.body);
    side.append(note.box);
    append(layout, main, side);
    root.append(metaTags, linkActions, layout);
  }

  function evidenceSection(title, evidence) {
    const section = element("section", "detail-section");
    section.append(text("h2", "", title));
    if (!Array.isArray(evidence) || !evidence.length) {
      section.append(text("p", "", "Точные совпадения для этого раздела не возвращены API."));
      return section;
    }
    const list = element("div", "evidence-list");
    for (const item of evidence) {
      const card = element("div", "evidence-item");
      const matched = item.matched_text || item.text || item.skill_label || item.label || item.occupation_label || "Совпадение";
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

  async function renderMethodology(seq) {
    const methodology = await getJson("methodology");
    if (seq !== renderSequence) return;
    root.replaceChildren();
    updateNav("methodology", "Методология");
    root.append(pageHeading("МЕТОДОЛОГИЯ", methodology.title || "Как устроены данные", "Здесь описаны единица наблюдения, правила чтения сводок и границы доступных доказательств."));
    const grid = element("div", "methodology-grid");
    const copyPanel = panel("Что показывает Career Radar", "Версия методологии: " + String(methodology.version || methodology.schema || "не указана"));
    const copy = element("div", "methodology-copy");
    append(copy,
      text("h2", "", "Текущий срез"),
      text("p", "", "Единица учёта — вакансия источника. Объявления берутся из последнего сохранённого состояния включённых источников; active означает сохранённый статус, а не подтверждение, что вакансия всё ещё открыта."),
      text("h3", "", "Роли и специализации"),
      text("p", "", "Метки ролей — результат версионированных правил классификатора. Неизвестные и неоднозначные названия сохраняются в выборке. Доля роли считается от выбранных вакансий; доля специализации — от выбранных инженерных вакансий."),
      text("h3", "", "Упоминания навыков"),
      text("p", "", "Система ищет термины по сохранённым заголовку и описанию. Вакансия учитывает каждый навык один раз. Доля показывает, в какой части выбранных объявлений термин найден. Она не означает, что навык обязателен, востребован рынком или отсутствует, если не распознан."),
      text("h3", "", "История"),
      text("p", "", "Сравнение за 30, 90 или 180 дней требует парных неизменяемых сканов и прохождения проверки качества. Если evidence недостаточно, значения остаются пустыми с объяснением. Нулевая историческая доля не подставляется вместо отсутствующих данных."),
      text("h3", "", "Свежесть и источники"),
      text("p", "", "Дата последнего наблюдения вакансии и дата последнего успешного скана источника — разные вещи. Частичный или неудачный скан не подтверждает полноту обновления. Кнопка «Перечитать срез» заново загружает уже сохранённые данные приложения и не запускает сбор с сайтов компаний."));
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

    const side = panel("Ограничения интерпретации", "Проверьте перед выводами");
    const limits = element("div");
    const knownLimitText = [
      "Это панель собранных страниц компаний, а не весь рынок труда.",
      "Заголовки и описания могут быть неполными, измениться или содержать шум.",
      "Распознавание основано на правилах и может пропустить новые названия и синонимы.",
      "Данные не учитывают число откликов, закрытие вакансии или реальные требования после найма.",
      "Список сохранённых вакансий остаётся только в локальном браузере.",
    ];
    const limitsFromApi = Array.isArray(methodology.limitations) ? methodology.limitations : null;
    for (const [index, item] of (limitsFromApi || knownLimitText).entries()) {
      const row = element("div", "limit-item");
      const raw = typeof item === "string" ? item : item.text || item.description || "";
      const translated = LIMITATION_COPY[raw] || (limitsFromApi ? "Дополнительное ограничение полноты данных; подробности указаны в версии методологии." : raw);
      append(row, text("span", "limit-number", String(index + 1)), text("span", "", translated));
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
        updateNav("saved", "Сохранённое");
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
      const isOverview = page === "overview";
      updateNav(isOverview ? "overview" : "explore", isOverview ? "Обзор" : "Вакансии");
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
