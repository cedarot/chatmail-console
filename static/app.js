const csrf = document.body.dataset.csrf;
const state = { usersPage: 1, usersQuery: {} };

function textCell(value) {
  const cell = document.createElement("td");
  cell.textContent = value === null || value === undefined || value === "" ? "Unavailable" : String(value);
  return cell;
}

function row(values) {
  const tr = document.createElement("tr");
  values.forEach((value) => tr.appendChild(textCell(value)));
  return tr;
}

function renderTable(id, items, values) {
  const body = document.getElementById(id);
  body.replaceChildren();
  if (!items.length) {
    const tr = document.createElement("tr");
    const td = textCell("No events in the selected window");
    td.colSpan = values.length;
    td.className = "empty";
    tr.appendChild(td);
    body.appendChild(tr);
    return;
  }
  items.forEach((item) => body.appendChild(row(values(item))));
}

async function api(path, options = {}) {
  const response = await fetch(path, { credentials: "same-origin", ...options });
  if (response.status === 401) { window.location.href = "/login"; throw new Error("Authentication required"); }
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error?.message || "Request failed");
  return payload;
}

function paramsFromForm(form, extra = {}) {
  const params = new URLSearchParams(extra);
  new FormData(form).forEach((value, key) => { if (value) params.set(key, value); });
  return params;
}

function stamp(value) { return value ? value.replace("T", " ").replace("Z", " UTC") : "Unavailable"; }

function setSourceBadge(id, status) {
  const badge = document.getElementById(id);
  badge.textContent = status?.state === "healthy" ? "Available" : "Unavailable";
  badge.className = `badge ${status?.state === "healthy" ? "ok" : "bad"}`;
}

async function loadSummary() {
  const summary = await api("/api/summary");
  document.getElementById("user-count").textContent = summary.userCount === null ? "—" : summary.userCount;
  document.getElementById("user-source").textContent = summary.sources.users.message;
  document.getElementById("login-count").textContent = summary.loginCount === null ? "—" : summary.loginCount;
  document.getElementById("access-count").textContent = summary.accessCount === null ? "—" : summary.accessCount;
  setSourceBadge("users-status", summary.sources.users);
  setSourceBadge("logins-status", summary.sources.logins);
  setSourceBadge("access-status", summary.sources.access);
  document.getElementById("last-updated").textContent = `Updated ${stamp(summary.observedAt)}`;
}

async function loadUsers() {
  const query = new URLSearchParams({ page: state.usersPage, pageSize: 25 });
  if (state.usersQuery) query.set("query", state.usersQuery);
  const payload = await api(`/api/users?${query}`);
  setSourceBadge("users-status", payload.sourceStatus);
  renderTable("users-table", payload.items, (item) => [item.userId, item.label]);
  document.getElementById("users-page").textContent = `Page ${payload.page}`;
  document.getElementById("users-prev").disabled = payload.page <= 1;
  document.getElementById("users-next").disabled = !payload.hasMore;
}

async function loadLogins() {
  const query = paramsFromForm(document.getElementById("logins-filter"));
  const payload = await api(`/api/logins?${query}`);
  setSourceBadge("logins-status", payload.sourceStatus);
  renderTable("logins-table", payload.items, (item) => [stamp(item.timestamp), item.userId, item.ip, item.device]);
}

async function loadAccess() {
  const query = paramsFromForm(document.getElementById("access-filter"));
  const payload = await api(`/api/access?${query}`);
  setSourceBadge("access-status", payload.sourceStatus);
  renderTable("access-table", payload.items, (item) => [stamp(item.timestamp), item.ip, item.device, item.method, item.path, item.status, item.responseBytes]);
}

async function refresh() {
  const status = document.getElementById("global-status");
  status.textContent = "Refreshing dashboard…";
  status.className = "status";
  try { await Promise.all([loadSummary(), loadUsers(), loadLogins(), loadAccess()]); status.textContent = "Dashboard loaded"; }
  catch (error) { status.textContent = error.message; status.className = "status error"; }
}

document.getElementById("refresh").addEventListener("click", refresh);
document.getElementById("logout").addEventListener("click", async () => {
  await fetch("/auth/logout", { method: "POST", headers: { "X-CSRF-Token": csrf }, credentials: "same-origin" });
  window.location.href = "/login";
});
document.getElementById("users-filter").addEventListener("submit", (event) => { event.preventDefault(); state.usersQuery = new FormData(event.currentTarget).get("query") || ""; state.usersPage = 1; loadUsers(); });
document.getElementById("users-prev").addEventListener("click", () => { if (state.usersPage > 1) { state.usersPage -= 1; loadUsers(); } });
document.getElementById("users-next").addEventListener("click", () => { state.usersPage += 1; loadUsers(); });
document.getElementById("logins-filter").addEventListener("submit", (event) => { event.preventDefault(); loadLogins(); });
document.getElementById("access-filter").addEventListener("submit", (event) => { event.preventDefault(); loadAccess(); });
refresh();
