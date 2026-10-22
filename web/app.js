const state = {
  page: 1,
  pages: 1,
  pageSize: 30,
  query: "",
  authorId: "all",
  account: "all",
  year: "all",
  status: "all",
  items: [],
  missingCount: 0,
  activeJob: null,
  jobDismissed: false,
};

const elements = {
  totalMetric: document.querySelector("#totalMetric"),
  downloadedMetric: document.querySelector("#downloadedMetric"),
  missingMetric: document.querySelector("#missingMetric"),
  rateMetric: document.querySelector("#rateMetric"),
  refreshNote: document.querySelector("#refreshNote"),
  syncButton: document.querySelector("#syncButton"),
  searchInput: document.querySelector("#searchInput"),
  authorFilter: document.querySelector("#authorFilter"),
  accountFilter: document.querySelector("#accountFilter"),
  yearFilter: document.querySelector("#yearFilter"),
  statusButtons: [...document.querySelectorAll("[data-status]")],
  resultCount: document.querySelector("#resultCount"),
  downloadPageButton: document.querySelector("#downloadPageButton"),
  articleRows: document.querySelector("#articleRows"),
  emptyState: document.querySelector("#emptyState"),
  loadingState: document.querySelector("#loadingState"),
  previousButton: document.querySelector("#previousButton"),
  nextButton: document.querySelector("#nextButton"),
  pageLabel: document.querySelector("#pageLabel"),
  jobPanel: document.querySelector("#jobPanel"),
  jobTitle: document.querySelector("#jobTitle"),
  jobCount: document.querySelector("#jobCount"),
  jobProgress: document.querySelector("#jobProgress"),
  jobCurrent: document.querySelector("#jobCurrent"),
  closeJobButton: document.querySelector("#closeJobButton"),
  toast: document.querySelector("#toast"),
};

let searchTimer;
let toastTimer;

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const payload = await response.json();
  if (!response.ok || !payload.success) {
    throw new Error(payload.message || `请求失败（${response.status}）`);
  }
  return payload;
}

function showToast(message, isError = false) {
  clearTimeout(toastTimer);
  elements.toast.textContent = message;
  elements.toast.classList.toggle("error", isError);
  elements.toast.hidden = false;
  toastTimer = setTimeout(() => {
    elements.toast.hidden = true;
  }, 3200);
}

function formatCount(value) {
  return new Intl.NumberFormat("zh-CN").format(value || 0);
}

function formatRefresh(value) {
  if (!value) return "尚未同步";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return `最近同步 ${date.toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  })}`;
}

function setSelectOptions(select, values, firstLabel) {
  const selected = select.value;
  select.replaceChildren();
  const all = document.createElement("option");
  all.value = "all";
  all.textContent = firstLabel;
  select.append(all);
  values.forEach((value) => {
    const option = document.createElement("option");
    option.value = typeof value === "string" ? value : value.value;
    option.textContent = typeof value === "string" ? value : value.label;
    select.append(option);
  });
  if ([...select.options].some((option) => option.value === selected)) {
    select.value = selected;
  }
}

async function loadStats() {
  const { data } = await api(`/api/stats?author_id=${encodeURIComponent(state.authorId)}`);
  elements.totalMetric.textContent = formatCount(data.total);
  elements.downloadedMetric.textContent = formatCount(data.downloaded);
  elements.missingMetric.textContent = formatCount(data.missing);
  state.missingCount = data.missing;
  elements.downloadPageButton.disabled = data.missing === 0;
  const rate = data.total ? Math.round((data.downloaded / data.total) * 100) : 0;
  elements.rateMetric.textContent = `${rate}%`;
  const wechat = data.wechat || {};
  elements.refreshNote.textContent = wechat.available
    ? `微信已同步 · ${formatRefresh(data.last_refresh)}`
    : `微信待授权 · ${formatRefresh(data.last_refresh)}`;
  elements.refreshNote.title = wechat.warning || "本机微信公众号文章同步正常";
  setSelectOptions(
    elements.authorFilter,
    data.authors.map((author) => ({ value: author.id, label: author.name })),
    "全部作者",
  );
  setSelectOptions(elements.accountFilter, Object.keys(data.accounts), "全部公众号");
  setSelectOptions(elements.yearFilter, data.years, "全部年份");
}

function buildQuery() {
  const params = new URLSearchParams({
    page: String(state.page),
    page_size: String(state.pageSize),
    q: state.query,
    author_id: state.authorId,
    account: state.account,
    year: state.year,
    status: state.status,
  });
  return params.toString();
}

function actionButton(item) {
  const container = document.createElement("div");
  container.className = "row-actions";

  const original = document.createElement("a");
  original.className = "icon-button";
  original.href = item.url;
  original.target = "_blank";
  original.rel = "noopener noreferrer";
  original.title = "打开微信原文";
  original.setAttribute("aria-label", "打开微信原文");
  original.textContent = "↗";
  container.append(original);

  if (item.status === "downloaded") {
    const local = document.createElement("a");
    local.className = "text-button icon-button";
    local.href = `/api/articles/${encodeURIComponent(item.id)}/file`;
    local.title = "下载本地文本";
    local.setAttribute("aria-label", "下载本地文本");
    local.textContent = "↓";
    container.append(local);
  } else if (item.status === "missing") {
    const download = document.createElement("button");
    download.className = "text-button download-action";
    download.type = "button";
    download.textContent = "下载";
    download.addEventListener("click", async () => {
      download.disabled = true;
      try {
        await startDownload([item.id]);
      } catch (error) {
        download.disabled = false;
        showToast(`下载任务创建失败：${error.message}`, true);
      }
    });
    container.append(download);
  }
  return container;
}

function renderRows(items) {
  elements.articleRows.replaceChildren();
  items.forEach((item) => {
    const row = document.createElement("tr");

    const date = document.createElement("td");
    date.className = "date-cell";
    const dateText = document.createElement("span");
    dateText.className = "date-text";
    dateText.textContent = item.date || "日期未知";
    date.append(dateText);

    const article = document.createElement("td");
    article.className = "article-cell";
    const title = document.createElement("a");
    title.className = "article-title";
    title.href = item.url;
    title.target = "_blank";
    title.rel = "noopener noreferrer";
    title.textContent = item.title;
    title.title = item.title;
    article.append(title);
    if (item.summary) {
      const summary = document.createElement("span");
      summary.className = "article-summary";
      summary.textContent = item.summary;
      article.append(summary);
    }

    const account = document.createElement("td");
    account.className = "account-cell";
    const tag = document.createElement("span");
    tag.className = "account-tag";
    tag.textContent = item.account;
    account.append(tag);

    const author = document.createElement("td");
    author.className = "author-cell";
    author.textContent = item.author_name;

    const status = document.createElement("td");
    status.className = "status-cell";
    const badge = document.createElement("span");
    badge.className = `status status-${item.status}`;
    const statusLabels = {
      downloaded: "已归档",
      missing: "待下载",
      paid: "付费文章",
      failed: "下载失败",
    };
    badge.textContent = statusLabels[item.status] || item.status;
    status.append(badge);

    const actions = document.createElement("td");
    actions.className = "action-cell";
    actions.append(actionButton(item));

    row.append(date, article, author, account, status, actions);
    elements.articleRows.append(row);
  });
}

async function loadArticles() {
  elements.loadingState.hidden = false;
  elements.emptyState.hidden = true;
  try {
    const { data } = await api(`/api/articles?${buildQuery()}`);
    state.items = data.items;
    state.pages = data.pages;
    elements.resultCount.textContent = formatCount(data.total);
    elements.pageLabel.textContent = `第 ${data.page} / ${data.pages} 页`;
    elements.previousButton.disabled = data.page <= 1;
    elements.nextButton.disabled = data.page >= data.pages;
    elements.downloadPageButton.disabled = state.missingCount === 0;
    renderRows(data.items);
    elements.emptyState.hidden = data.items.length !== 0;
  } catch (error) {
    elements.articleRows.replaceChildren();
    elements.emptyState.hidden = false;
    showToast(`文章目录读取失败：${error.message}`, true);
  } finally {
    elements.loadingState.hidden = true;
  }
}

async function refreshAll() {
  await Promise.all([loadStats(), loadArticles()]);
}

async function syncInventory() {
  elements.syncButton.disabled = true;
  elements.syncButton.firstElementChild.classList.add("spin");
  try {
    const payload = await api("/api/sync", { method: "POST", body: "{}" });
    state.page = 1;
    await refreshAll();
    showToast(
      `${payload.message}，共 ${formatCount(payload.data.total)} 篇`,
      Boolean(payload.data.wechat?.warning),
    );
  } catch (error) {
    showToast(`文章目录同步失败：${error.message}`, true);
  } finally {
    elements.syncButton.disabled = false;
    elements.syncButton.firstElementChild.classList.remove("spin");
  }
}

async function startDownload(ids) {
  const payload = await api("/api/downloads", {
    method: "POST",
    body: JSON.stringify({ ids }),
  });
  state.activeJob = payload.data.id;
  state.jobDismissed = false;
  elements.jobPanel.hidden = false;
  renderJob(payload.data);
  pollJob(payload.data.id);
  showToast(payload.message);
}

function renderJob(job) {
  const percentage = job.total ? Math.round((job.completed / job.total) * 100) : 0;
  elements.jobTitle.textContent = job.status === "finished"
    ? (job.failed ? "下载完成，存在失败项" : "下载完成")
    : "正在下载文章";
  elements.jobCount.textContent = `${job.completed} / ${job.total}，成功 ${job.succeeded}，付费 ${job.paid || 0}，失败 ${job.failed}`;
  elements.jobProgress.style.width = `${percentage}%`;
  elements.jobCurrent.textContent = job.current || (job.errors?.at(-1)?.error ?? "任务已结束");
}

async function pollJob(jobId) {
  try {
    const { data } = await api(`/api/jobs/${encodeURIComponent(jobId)}`);
    if (!state.jobDismissed) {
      elements.jobPanel.hidden = false;
    }
    renderJob(data);
    if (data.status === "finished") {
      state.activeJob = null;
      await refreshAll();
      showToast(
        `下载完成：成功 ${data.succeeded} 篇，付费 ${data.paid || 0} 篇，失败 ${data.failed} 篇`,
        data.failed > 0,
      );
      return;
    }
    setTimeout(() => pollJob(jobId), 1000);
  } catch (error) {
    showToast(`下载进度读取失败：${error.message}`, true);
  }
}

elements.searchInput.addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    state.query = elements.searchInput.value.trim();
    state.page = 1;
    loadArticles();
  }, 280);
});

elements.accountFilter.addEventListener("change", () => {
  state.account = elements.accountFilter.value;
  state.page = 1;
  loadArticles();
});

elements.authorFilter.addEventListener("change", async () => {
  state.authorId = elements.authorFilter.value;
  state.account = "all";
  state.page = 1;
  await refreshAll();
});

elements.yearFilter.addEventListener("change", () => {
  state.year = elements.yearFilter.value;
  state.page = 1;
  loadArticles();
});

elements.statusButtons.forEach((button) => {
  button.addEventListener("click", () => {
    elements.statusButtons.forEach((current) => current.classList.remove("active"));
    button.classList.add("active");
    state.status = button.dataset.status;
    state.page = 1;
    loadArticles();
  });
});

elements.previousButton.addEventListener("click", () => {
  if (state.page <= 1) return;
  state.page -= 1;
  loadArticles();
  window.scrollTo({ top: 180, behavior: "smooth" });
});

elements.nextButton.addEventListener("click", () => {
  if (state.page >= state.pages) return;
  state.page += 1;
  loadArticles();
  window.scrollTo({ top: 180, behavior: "smooth" });
});

elements.downloadPageButton.addEventListener("click", () => {
  api("/api/downloads", {
    method: "POST",
    body: JSON.stringify({
      ids: [],
      all_missing: true,
      author_id: state.authorId,
    }),
  }).then((payload) => {
    state.activeJob = payload.data.id;
    state.jobDismissed = false;
    elements.jobPanel.hidden = false;
    renderJob(payload.data);
    pollJob(payload.data.id);
    showToast(payload.message);
  }).catch((error) => {
    showToast(`批量下载任务创建失败：${error.message}`, true);
  });
});

elements.syncButton.addEventListener("click", syncInventory);
elements.closeJobButton.addEventListener("click", () => {
  state.jobDismissed = true;
  elements.jobPanel.hidden = true;
});

refreshAll().catch((error) => {
  showToast(`页面初始化失败：${error.message}`, true);
});
