"use strict";
const $ = (id) => document.getElementById(id);
const kinds = ["international", "domestic"];
const secretNames = ["webhook", "dingtalk_secret", "goldapi_key", "juhe_key"];
const secretId = (name) => name.replaceAll("_", "-");
let currentState;
let toastTimer;
let dirty = false;
const kindLabel = (kind) => kind === "international" ? "国际金价早报" : "国内金价观察";

function toast(message, error = false) {
  clearTimeout(toastTimer);
  $("toast").textContent = message;
  $("toast").className = "toast" + (error ? " error" : "");
  toastTimer = setTimeout(() => $("toast").classList.add("hidden"), error ? 9000 : 4000);
}

async function api(path, method = "GET", body) {
  const response = await fetch(new URL(path, window.location.origin), {
    method,
    headers: {"Content-Type": "application/json", "X-Gold-Watch": "1"},
    body: body === undefined ? undefined : JSON.stringify(body),
    credentials: "same-origin",
  });
  const result = await response.json();
  if (!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : "请求失败，请检查配置或服务状态");
  return result;
}

function formatDate(value, short = false) {
  if (!value) return "—";
  return new Date(value).toLocaleString("zh-CN", {
    timeZone: "Asia/Shanghai", hour12: false,
    ...(short ? {month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit"} : {}),
  });
}

function drawQuote(kind, data) {
  if (!data) return;
  const q = data.quote;
  $("price-" + kind).textContent = Number(q.price).toLocaleString("en-US", {minimumFractionDigits: 2, maximumFractionDigits: 2});
  $("quote-time-" + kind).textContent = formatDate(q.quote_time, true);
  $("source-" + kind).textContent = q.source;
  $("status-" + kind).textContent = data.status + (q.warnings.length ? " · " + q.warnings.join("；") : "");
  if (kind === "international") {
    const conversion = data.conversion;
    $("conversion-international").classList.toggle("hidden", !conversion);
    if (conversion) {
      $("cny-price-international").textContent = conversion.available
        ? `折合人民币约 ${conversion.price} 元/克` : "人民币换算暂不可用";
      $("exchange-rate-international").textContent = conversion.available
        ? `1 美元 ≈ ${conversion.rate} 元人民币 · 更新于 ${formatDate(conversion.updated_at, true)}（北京时间）` +
          (conversion.notes.length ? " · " + conversion.notes.join("；") : "")
        : conversion.text;
      $("exchange-source-international").classList.toggle("hidden", !conversion.available);
      if (conversion.available) {
        $("exchange-source-international").textContent = `汇率来源：${conversion.source} ↗`;
        $("exchange-source-international").href = conversion.source_url;
      }
    }
  }
  const target = Number(currentState?.settings[kind].target_price);
  const targetNode = $("target-gap-" + kind);
  targetNode.classList.toggle("hidden", !target);
  if (target) {
    const gap = Number(q.price) - target;
    targetNode.textContent = `观察目标 ${target.toFixed(2)} ${q.unit} · ` + (gap > 0
      ? `距观察目标还差 ${gap.toFixed(2)}，约 ${(gap / target * 100).toFixed(2)}%（以目标价为基准）`
      : gap === 0 ? "当前参考价已达到目标"
        : `当前参考价已低于目标 ${(-gap).toFixed(2)}，约 ${(-gap / target * 100).toFixed(2)}%（以目标价为基准）`);
  }
}

function svgNode(name, attributes, text) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", name);
  for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, value);
  if (text !== undefined) node.textContent = text;
  return node;
}

function drawTrend(kind, trend) {
  const root = $("trend-" + kind);
  root.replaceChildren();
  const samples = trend?.points.filter(point => point.price !== null) || [];
  const summary = document.createElement("p");
  summary.className = "trend-summary";
  summary.textContent = trend?.summary || "近7日：暂无采集记录";
  root.append(summary);
  if (samples.length) {
    const meta = document.createElement("p");
    meta.className = "subtle trend-meta";
    meta.textContent = `${trend.source} · ${trend.unit} · 日采样范围 ${Number(trend.low).toFixed(2)}–${Number(trend.high).toFixed(2)}`;
    const readout = document.createElement("p");
    readout.className = "trend-readout";
    readout.setAttribute("aria-live", "polite");
    const showPoint = point => {
      readout.textContent = `${formatDate(point.quote_time, true)} · ${Number(point.price).toFixed(2)} ${trend.unit}`;
    };
    showPoint(samples[samples.length - 1]);
    const chartLabel = kind === "international" ? "国际金价" : "国内金价";
    const svg = svgNode("svg", {viewBox: "0 0 640 260", role: "img", "aria-label": `${chartLabel}最近7天价格采样折线图`, class: "trend-chart"});
    svg.append(svgNode("title", {}, "每天最后一条采集报价；缺失日期留空。悬停或聚焦数据点查看详情。"));
    const low = Number(trend.low), high = Number(trend.high);
    const padding = Math.max((high - low) * 0.15, high * 0.0005, 0.01);
    const min = low - padding, max = high + padding;
    const x = index => 94 + index * 86;
    const y = price => 202 - (Number(price) - min) / (max - min) * 174;
    for (const value of [max, (min + max) / 2, min]) {
      const position = y(value);
      svg.append(svgNode("line", {x1: 94, y1: position, x2: 610, y2: position, class: "trend-gridline"}),
        svgNode("text", {x: 82, y: position + 5, "text-anchor": "end", class: "trend-axis"}, value.toFixed(2)));
    }
    let segment = [];
    const flush = () => {
      if (segment.length > 1) svg.append(svgNode("polyline", {points: segment.join(" "), class: "trend-line"}));
      segment = [];
    };
    trend.points.forEach((point, index) => {
      svg.append(svgNode("text", {x: x(index), y: 240, "text-anchor": "middle", class: "trend-axis"}, point.date.slice(5)));
      if (point.price === null) { flush(); return; }
      segment.push(`${x(index)},${y(point.price)}`);
    });
    flush();
    trend.points.forEach((point, index) => {
      if (point.price === null) return;
      const dot = svgNode("circle", {cx: x(index), cy: y(point.price), r: 6, tabindex: 0, class: "trend-point",
        "aria-label": `${point.date} ${Number(point.price).toFixed(2)} ${trend.unit}`});
      dot.append(svgNode("title", {}, `${point.date} · ${Number(point.price).toFixed(2)} ${trend.unit}`));
      dot.addEventListener("mouseenter", () => showPoint(point));
      dot.addEventListener("focus", () => showPoint(point));
      dot.addEventListener("click", () => showPoint(point));
      svg.append(dot);
    });
    root.append(meta, svg, readout);
  } else {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    empty.textContent = "最近7天暂无同来源报价。刷新预览或推送后开始积累。";
    root.append(empty);
  }
  const note = document.createElement("p");
  note.className = "subtle";
  note.textContent = trend?.note || "每天取最后一条采集报价，缺失日期留空。";
  root.append(note);
  if (trend?.points.length) {
    const details = document.createElement("details");
    details.className = "trend-data";
    const label = document.createElement("summary");
    label.textContent = "查看每日采样数据";
    const table = document.createElement("table");
    const caption = document.createElement("caption");
    caption.textContent = `近7日 · ${trend.unit || "暂无报价"} · 北京时间`;
    table.append(caption);
    const heading = document.createElement("tr");
    for (const text of ["日期", "价格", "上游更新"]) {
      const th = document.createElement("th"); th.scope = "col"; th.textContent = text; heading.append(th);
    }
    const head = document.createElement("thead"); head.append(heading); table.append(head);
    const body = document.createElement("tbody");
    for (const point of trend.points) {
      const row = document.createElement("tr");
      for (const text of [point.date.slice(5), point.price === null ? "无记录" : Number(point.price).toFixed(2),
        point.quote_time ? formatDate(point.quote_time, true) : "—"]) {
        const cell = document.createElement("td"); cell.textContent = text; row.append(cell);
      }
      body.append(row);
    }
    table.append(body); details.append(label, table); root.append(details);
  }
}

function hydrate(settings) {
  for (const kind of kinds) {
    const schedule = settings[kind];
    $(kind + "-enabled").checked = schedule.enabled;
    $(kind + "-time").value = schedule.time;
    $(kind + "-target").value = schedule.target_price ?? "";
    $(kind + "-title").value = schedule.title;
    $(kind + "-template").value = schedule.template;
    $(kind + "-note").value = schedule.note;
    $(kind + "-provider").value = settings[kind + "_provider"];
  }
  $("catchup-minutes").value = settings.catchup_minutes;
  $("domestic-premium").value = settings.domestic_premium;
  $("allow-fallback").checked = settings.allow_fallback;
  for (const name of secretNames) {
    $(secretId(name)).value = "";
    $(name + "-status").textContent = settings[name + "_configured"] ? "已配置 · 留空保留原值" : "尚未配置";
    if ($("clear-" + name)) $("clear-" + name).checked = false;
  }
  dirty = false;
  $("save-settings").textContent = "保存配置";
}

function drawHistory(history) {
  const root = $("history-list");
  root.replaceChildren();
  if (!history.length) {
    const p = document.createElement("p");
    p.className = "empty-state";
    p.textContent = "暂无推送记录。你可以先预览，再手动推送第一份简报。";
    root.append(p);
  }
  const statuses = {success: "发送成功", failed: "发送失败", uncertain: "结果不确定", collecting: "采集中", sending: "发送中", cancelled: "已取消"};
  for (const row of history) {
    const article = document.createElement("article");
    article.className = "history-row";
    const top = document.createElement("div");
    top.className = "history-top";
    const title = document.createElement("strong");
    title.textContent = row.title || kindLabel(row.kind);
    const status = document.createElement("span");
    status.className = "history-status " + row.status;
    status.textContent = statuses[row.status] || row.status;
    const time = document.createElement("time");
    time.textContent = formatDate(row.created_at);
    top.append(title, status, time);
    const detail = document.createElement("p");
    detail.className = "history-detail";
    detail.textContent = (row.slot.startsWith("manual:") ? "手动推送" : "定时推送") + (row.detail ? " · " + row.detail : "");
    article.append(top, detail);
    if (row.message) {
      const details = document.createElement("details");
      const summary = document.createElement("summary");
      summary.textContent = "查看消息内容";
      const pre = document.createElement("pre");
      pre.textContent = row.message;
      const batch = document.createElement("p");
      batch.className = "subtle";
      batch.textContent = "发送批次：" + row.slot;
      details.append(summary, pre, batch);
      article.append(details);
    }
    root.append(article);
  }
}

async function loadState(fillForm = false) {
  currentState = await api("/api/state");
  const s = currentState.settings;
  for (const kind of kinds) {
    drawQuote(kind, currentState.latest[kind]);
    drawTrend(kind, currentState.trends?.[kind]);
    $("time-" + kind).textContent = s[kind].time;
    $("next-" + kind).textContent = currentState.next_runs[kind] ? formatDate(currentState.next_runs[kind], true) : "已暂停";
  }
  $("schedule-label").textContent = s.scheduler_enabled ? "定时推送已开启" : "定时推送已暂停";
  $("schedule-description").textContent = s.scheduler_enabled
    ? kinds.map((kind) => `${s[kind].time} ${kindLabel(kind)}${s[kind].enabled ? "" : "（已停用）"}`).join("  /  ")
    : "配置钉钉机器人后开启，每天收到两份金价简报。";
  $("master-toggle").textContent = s.scheduler_enabled ? "暂停定时推送" : "开启定时推送";
  $("server-clock").textContent = "北京时间 · " + formatDate(currentState.server_time, true);
  $("health-label").textContent = currentState.scheduler.error
    ? "最近任务异常 · " + currentState.scheduler.error
    : "服务已连接 · 配置与记录已持久化";
  drawHistory(currentState.history);
  if (fillForm) hydrate(s);
}

async function busy(button, action) {
  const before = button.textContent;
  button.disabled = true;
  button.textContent = "处理中…";
  try { await action(); } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; button.textContent = before; }
}

for (const item of document.querySelectorAll("[data-page]")) {
  item.addEventListener("click", () => {
    for (const page of document.querySelectorAll(".page")) page.classList.add("hidden");
    $("page-" + item.dataset.page).classList.remove("hidden");
    for (const nav of document.querySelectorAll("[data-page]")) nav.classList.toggle("active", nav === item);
    $("breadcrumb").textContent = "工作台 / " + item.textContent.slice(1).trim();
  });
}

for (const button of document.querySelectorAll("[data-preview]")) {
  button.addEventListener("click", () => busy(button, async () => {
    if (dirty) toast("当前预览使用已保存配置，编辑中的修改请先保存。");
    const kind = button.dataset.preview;
    const result = await api("/api/preview/" + kind, "POST");
    await loadState();
    drawQuote(kind, result);
    $("preview-title").textContent = result.title + " · 实际消息内容";
    $("message-preview").textContent = result.message;
    toast("行情已更新，预览未发送到钉钉。");
  }));
}

for (const button of document.querySelectorAll("[data-send]")) {
  button.addEventListener("click", () => busy(button, async () => {
    try {
      const result = await api("/api/send/" + button.dataset.send, "POST");
      toast(result.detail);
    } finally { await loadState(); }
  }));
}

$("master-toggle").addEventListener("click", () => {
  const button = $("master-toggle");
  busy(button, async () => {
    const enabled = !currentState.settings.scheduler_enabled;
    await api("/api/settings", "PUT", {scheduler_enabled: enabled});
    await loadState();
    toast(enabled ? "定时推送已开启" : "定时推送已暂停");
  }).then(() => {
    button.textContent = currentState?.settings.scheduler_enabled ? "暂停定时推送" : "开启定时推送";
  });
});

for (const id of ["refresh-state", "refresh-history"]) {
  $(id).addEventListener("click", () => busy($(id), () => loadState()));
}

$("settings-form").addEventListener("input", () => {
  dirty = true;
  $("save-settings").textContent = "保存配置 · 有修改";
});

$("settings-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const button = $("save-settings");
  busy(button, async () => {
    const body = {};
    for (const kind of kinds) {
      body[kind] = {
        enabled: $(kind + "-enabled").checked, time: $(kind + "-time").value,
        target_price: $(kind + "-target").value || null, title: $(kind + "-title").value,
        template: $(kind + "-template").value, note: $(kind + "-note").value,
      };
      body[kind + "_provider"] = $(kind + "-provider").value;
    }
    body.catchup_minutes = Number($("catchup-minutes").value);
    body.domestic_premium = $("domestic-premium").value || "0";
    body.allow_fallback = $("allow-fallback").checked;
    body.clear_secrets = [];
    for (const name of secretNames) {
      body[name] = $(secretId(name)).value;
      if ($("clear-" + name)?.checked) body.clear_secrets.push(name);
    }
    await api("/api/settings", "PUT", body);
    await loadState(true);
    toast("配置已保存，定时任务立即使用新配置。");
  }).then(() => { button.textContent = dirty ? "保存配置 · 有修改" : "保存配置"; });
});

window.addEventListener("beforeunload", (event) => {
  if (dirty) { event.preventDefault(); event.returnValue = ""; }
});

loadState(true).catch((error) => toast(error.message, true));
setInterval(() => loadState().catch(() => { $("health-label").textContent = "服务连接中断，请检查网络"; }), 30000);
