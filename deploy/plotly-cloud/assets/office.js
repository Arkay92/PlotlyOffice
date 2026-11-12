(() => {
  const KEY_STORAGE = "plotlyoffice.key";
  const NAME_STORAGE = "plotlyoffice.name";
  const CLIENT_STORAGE = "plotlyoffice.client";
  const SUITE_URLS_STORAGE = "plotlyoffice.suite_urls";
  const VIEW_MODE_STORAGE = "plotlyoffice.view_mode";

  const SUITE_LABELS = {
    word: { name: "PlotlyWord", short: "Word", icon: "W" },
    sheets: { name: "PlotlySheets", short: "Sheets", icon: "S" },
    slides: { name: "PlotlySlides", short: "Slides", icon: "P" },
  };

  const state = {
    key: localStorage.getItem(KEY_STORAGE) || "",
    name: sessionStorage.getItem(NAME_STORAGE) || localStorage.getItem(NAME_STORAGE) || "Workspace User",
    color: "#176b5a",
    clientId: sessionStorage.getItem(CLIENT_STORAGE) || (crypto.randomUUID ? crypto.randomUUID() : String(Math.random()).slice(2)),
    shareToken: "",
    view: "folder",
    folderId: null,
    role: "owner",
    breadcrumbs: [],
    items: [],
    peers: [],
    query: "",
    typeFilter: "all",
    sortBy: "name",
    layoutMode: localStorage.getItem(VIEW_MODE_STORAGE) || "grid",
    suiteUrls: {},
    defaultSuiteUrls: {},
    newMenuOpen: false,
    drawer: null, // { mode: 'comments'|'activity', itemId, title }
    drawerItems: [],
    commentText: "",
    toast: null,
    dragging: false,
  };
  sessionStorage.setItem(CLIENT_STORAGE, state.clientId);

  function topWin() {
    try {
      return window.top && window.top.location ? window.top : window;
    } catch {
      return window;
    }
  }

  function parseLocation() {
    const loc = topWin().location;
    const params = new URLSearchParams(loc.search);
    const hashParams = new URLSearchParams((loc.hash || "").replace(/^#/, ""));
    state.folderId = params.get("folder") || null;
    state.shareToken = hashParams.get("share") || "";
    const itemFocus = params.get("item");
    if (itemFocus && !state.folderId) {
      state.pendingItemFocus = itemFocus;
    }
  }

  function syncUrl() {
    const win = topWin();
    const url = new URL(win.location.href);
    if (state.folderId) {
      url.searchParams.set("folder", state.folderId);
    } else {
      url.searchParams.delete("folder");
    }
    url.searchParams.delete("item");
    if (state.shareToken) {
      url.hash = "share=" + encodeURIComponent(state.shareToken);
    } else {
      url.hash = "";
    }
    try {
      win.history.replaceState({}, "", url.toString());
    } catch {
      // ignore cross-origin frame restrictions
    }
  }

  async function api(path, opts = {}) {
    const headers = { ...(opts.headers || {}) };
    if (state.key) headers["Authorization"] = "Bearer " + state.key;
    if (state.shareToken) headers["X-Document-Key"] = state.shareToken;
    if (opts.body && !(opts.body instanceof FormData) && !headers["Content-Type"]) {
      headers["Content-Type"] = "application/json";
    }
    const res = await fetch(path, { ...opts, headers });
    const text = await res.text();
    let data = {};
    try {
      data = text ? JSON.parse(text) : {};
    } catch {
      data = { error: text };
    }
    if (!res.ok) {
      throw new Error(data.error || `Request failed (${res.status})`);
    }
    return data;
  }

  function showToast(msg, isError = false) {
    state.toast = { msg, isError };
    renderToast();
    clearTimeout(showToast._t);
    showToast._t = setTimeout(() => {
      state.toast = null;
      renderToast();
    }, 4200);
  }

  function esc(str) {
    return String(str ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function fmtBytes(n) {
    if (!n) return "—";
    if (n < 1024) return n + " B";
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
    return (n / (1024 * 1024)).toFixed(1) + " MB";
  }

  function fmtDate(ts) {
    if (!ts) return "";
    return new Date(ts * 1000).toLocaleDateString(undefined, {
      month: "short",
      day: "numeric",
      year: "numeric",
    });
  }

  function initials(name) {
    return (name || "U")
      .split(/\s+/)
      .slice(0, 2)
      .map((s) => s[0]?.toUpperCase() || "")
      .join("") || "U";
  }

  function itemIcon(item) {
    if (item.kind === "folder") return { cls: "icon-folder", label: "📁" };
    if (item.suite_app === "word") return { cls: "icon-word", label: "W" };
    if (item.suite_app === "sheets") return { cls: "icon-sheets", label: "S" };
    if (item.suite_app === "slides") return { cls: "icon-slides", label: "P" };
    const ext = (item.ext || "").toUpperCase().slice(0, 4) || "FILE";
    return { cls: "icon-other", label: ext };
  }

  function visibleItems() {
    let list = [...state.items];
    if (state.query.trim()) {
      const q = state.query.trim().toLowerCase();
      list = list.filter((i) => i.name.toLowerCase().includes(q));
    }
    if (state.typeFilter !== "all") {
      if (state.typeFilter === "other") {
        list = list.filter((i) => i.kind === "folder" || !i.suite_app);
      } else {
        list = list.filter((i) => i.kind === "folder" || i.suite_app === state.typeFilter);
      }
    }
    list.sort((a, b) => {
      if (a.kind !== b.kind) return a.kind === "folder" ? -1 : 1;
      if (state.sortBy === "updated") return b.updated - a.updated;
      if (state.sortBy === "size") return b.size_bytes - a.size_bytes;
      return a.name.localeCompare(b.name, undefined, { sensitivity: "base" });
    });
    return list;
  }

  async function ensureSession() {
    const res = await api("/api/session", {
      method: "POST",
      body: JSON.stringify({ name: state.name }),
    });
    if (res.valid === false) {
      state.key = "";
      localStorage.removeItem(KEY_STORAGE);
      return ensureSession();
    }
    if (res.key) {
      state.key = res.key;
      localStorage.setItem(KEY_STORAGE, res.key);
    }
    if (res.name) {
      state.name = res.name;
      sessionStorage.setItem(NAME_STORAGE, res.name);
      localStorage.setItem(NAME_STORAGE, res.name);
    }
    if (res.color) state.color = res.color;
  }

  async function loadSuiteConfig() {
    const cfg = await api("/api/suite/config");
    state.defaultSuiteUrls = cfg.urls || {};
    let saved = {};
    try {
      saved = JSON.parse(localStorage.getItem(SUITE_URLS_STORAGE) || "{}");
    } catch {
      saved = {};
    }
    state.suiteUrls = { ...state.defaultSuiteUrls, ...saved };
  }

  async function loadItems() {
    const params = new URLSearchParams();
    if (state.folderId) {
      params.set("parent_id", state.folderId);
    } else {
      params.set("view", state.view);
    }
    if (state.query.trim() && !state.folderId) {
      params.set("q", state.query.trim());
    }
    const data = await api("/api/items?" + params.toString());
    state.items = data.items || [];
    state.breadcrumbs = data.breadcrumbs || [];
    state.role = data.role || "owner";
    syncUrl();
    render();
  }

  async function openFolder(folderId) {
    state.view = "folder";
    state.folderId = folderId || null;
    state.peers = [];
    if (!folderId && state.shareToken) {
      state.shareToken = "";
    }
    await loadItems();
    await heartbeat();
  }

  async function heartbeat() {
    const targetId = state.folderId;
    if (!targetId) {
      state.peers = [];
      return;
    }
    try {
      const peers = await api(`/api/items/${encodeURIComponent(targetId)}/presence`, {
        method: "POST",
        body: JSON.stringify({ client: state.clientId, cursor: { view: state.layoutMode } }),
      });
      state.peers = peers || [];
      renderTopbarPeers();
    } catch {
      // ignore transient presence errors
    }
  }

  async function uploadFiles(fileList) {
    const files = Array.from(fileList || []);
    if (!files.length) return;
    const form = new FormData();
    if (state.folderId) form.append("parent_id", state.folderId);
    for (const f of files) form.append("files", f);
    try {
      const res = await api("/api/upload", { method: "POST", body: form });
      showToast(`Uploaded ${res.items.length} file${res.items.length > 1 ? "s" : ""}.`);
      await loadItems();
    } catch (err) {
      showToast(err.message, true);
    }
  }

  async function createFolderPrompt() {
    state.newMenuOpen = false;
    const name = window.prompt("Folder name", "New Folder");
    if (!name || !name.trim()) return;
    try {
      await api("/api/folders", {
        method: "POST",
        body: JSON.stringify({ name: name.trim(), parent_id: state.folderId }),
      });
      showToast("Folder created.");
      await loadItems();
    } catch (err) {
      showToast(err.message, true);
    }
  }

  async function createBlankDoc(appId) {
    state.newMenuOpen = false;
    const label = SUITE_LABELS[appId]?.name || appId;
    const title = window.prompt(`New ${label} title`, `Untitled ${SUITE_LABELS[appId]?.short || "File"}`);
    if (!title || !title.trim()) return;
    try {
      const item = await api("/api/files/blank", {
        method: "POST",
        body: JSON.stringify({ app_id: appId, title: title.trim(), parent_id: state.folderId }),
      });
      showToast(`Created ${item.name}.`);
      await loadItems();
    } catch (err) {
      showToast(err.message, true);
    }
  }

  async function openInSuite(item, forceNew = false) {
    const appMeta = SUITE_LABELS[item.suite_app];
    if (!appMeta) return;
    try {
      showToast(`Opening ${item.name} in ${appMeta.name}…`);
      const res = await api(`/api/items/${encodeURIComponent(item.id)}/open-suite`, {
        method: "POST",
        body: JSON.stringify({ suite_urls: state.suiteUrls, force_new: forceNew }),
      });
      window.open(res.url, "_blank", "noopener");
      await loadItems();
    } catch (err) {
      showToast(err.message, true);
    }
  }

  async function renameItem(item) {
    const next = window.prompt("Rename item", item.name);
    if (!next || !next.trim() || next.trim() === item.name) return;
    try {
      await api(`/api/items/${encodeURIComponent(item.id)}`, {
        method: "PATCH",
        body: JSON.stringify({ name: next.trim() }),
      });
      showToast("Renamed.");
      await loadItems();
    } catch (err) {
      showToast(err.message, true);
    }
  }

  async function toggleStar(item) {
    try {
      await api(`/api/items/${encodeURIComponent(item.id)}`, {
        method: "PATCH",
        body: JSON.stringify({ starred: !item.starred }),
      });
      await loadItems();
    } catch (err) {
      showToast(err.message, true);
    }
  }

  async function trashOrRestore(item, trashed) {
    try {
      await api(`/api/items/${encodeURIComponent(item.id)}`, {
        method: "PATCH",
        body: JSON.stringify({ trashed }),
      });
      showToast(trashed ? "Moved to Trash." : "Restored.");
      await loadItems();
    } catch (err) {
      showToast(err.message, true);
    }
  }

  async function deleteForever(item) {
    if (!window.confirm(`Permanently delete "${item.name}"? This cannot be undone.`)) return;
    try {
      await api(`/api/items/${encodeURIComponent(item.id)}?permanent=true`, { method: "DELETE" });
      showToast("Deleted permanently.");
      await loadItems();
    } catch (err) {
      showToast(err.message, true);
    }
  }

  async function downloadFile(item) {
    try {
      const headers = {};
      if (state.key) headers["Authorization"] = "Bearer " + state.key;
      if (state.shareToken) headers["X-Document-Key"] = state.shareToken;
      const res = await fetch(`/api/items/${encodeURIComponent(item.id)}/download`, { headers });
      if (!res.ok) throw new Error("Download failed.");
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = item.name;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (err) {
      showToast(err.message, true);
    }
  }

  async function openPreview(item) {
    try {
      const data = await api(`/api/items/${encodeURIComponent(item.id)}/preview`);
      const dlg = document.getElementById("preview-dialog");
      const body = document.getElementById("preview-body");
      document.getElementById("preview-title").textContent = item.name;
      if (data.kind === "text") {
        body.innerHTML = `<pre class="preview-text">${esc(data.text)}</pre>` +
          (data.truncated ? `<p style="color:var(--muted)">Showing first 128 KB.</p>` : "");
      } else if (data.kind === "image" || data.kind === "pdf") {
        const headers = {};
        if (state.key) headers["Authorization"] = "Bearer " + state.key;
        if (state.shareToken) headers["X-Document-Key"] = state.shareToken;
        const r = await fetch(`/api/items/${encodeURIComponent(item.id)}/download?inline=1`, { headers });
        const blobUrl = URL.createObjectURL(await r.blob());
        if (data.kind === "image") {
          body.innerHTML = `<img class="preview-image" src="${blobUrl}" alt="${esc(item.name)}">`;
        } else {
          body.innerHTML = `<iframe src="${blobUrl}" style="width:100%;height:60vh;border:0;border-radius:10px;"></iframe>`;
        }
      } else {
        body.innerHTML = `<p>Inline preview is not available for <strong>.${esc(item.ext || "file")}</strong>. You can download it${item.suite_app ? ` or open it in ${SUITE_LABELS[item.suite_app].name}` : ""}.</p>`;
      }
      dlg.showModal();
    } catch (err) {
      showToast(err.message, true);
    }
  }

  async function openShareDialog(item) {
    if (!item) {
      showToast("Open or select a folder or file to share.", true);
      return;
    }
    const dlg = document.getElementById("share-dialog");
    const listEl = document.getElementById("share-links-list");
    document.getElementById("share-item-title").textContent = item.name;
    dlg.dataset.itemId = item.id;
    dlg.dataset.itemKind = item.kind;

    async function refreshLinks() {
      try {
        const links = await api(`/api/items/${encodeURIComponent(item.id)}/shares`);
        if (!links.length) {
          listEl.innerHTML = `<p style="color:var(--muted);margin:0;">No active share links yet.</p>`;
          return;
        }
        const origin = topWin().location.origin;
        listEl.innerHTML = links
          .map((l) => {
            const targetUrl =
              item.kind === "folder"
                ? `${origin}/?folder=${encodeURIComponent(item.id)}#share=${encodeURIComponent(l.token)}`
                : `${origin}/?folder=${encodeURIComponent(item.parent_id || item.id)}#share=${encodeURIComponent(l.token)}`;
            return `
              <div class="share-link-row" data-link-id="${esc(l.id)}">
                <div class="share-link-info">
                  <div class="share-link-label">${esc(l.label)}</div>
                  <div style="font-size:11px;color:var(--muted)">${l.role === "edit" ? "Can edit" : "Can view"}</div>
                </div>
                <select class="select-clean share-role-select">
                  <option value="view" ${l.role === "view" ? "selected" : ""}>Can view</option>
                  <option value="edit" ${l.role === "edit" ? "selected" : ""}>Can edit</option>
                </select>
                <button class="btn btn-sm copy-share-btn" data-url="${esc(targetUrl)}">Copy link</button>
                <button class="btn btn-sm btn-danger revoke-share-btn" title="Revoke">×</button>
              </div>`;
          })
          .join("");

        listEl.querySelectorAll(".copy-share-btn").forEach((btn) => {
          btn.onclick = () => {
            navigator.clipboard?.writeText(btn.dataset.url);
            showToast("Share link copied to clipboard.");
          };
        });
        listEl.querySelectorAll(".share-role-select").forEach((sel) => {
          sel.onchange = async () => {
            const id = sel.closest(".share-link-row").dataset.linkId;
            await api(`/api/items/${encodeURIComponent(item.id)}/shares/${encodeURIComponent(id)}`, {
              method: "PUT",
              body: JSON.stringify({ role: sel.value }),
            });
            showToast("Permission updated.");
          };
        });
        listEl.querySelectorAll(".revoke-share-btn").forEach((btn) => {
          btn.onclick = async () => {
            const id = btn.closest(".share-link-row").dataset.linkId;
            await api(`/api/items/${encodeURIComponent(item.id)}/shares/${encodeURIComponent(id)}`, {
              method: "DELETE",
            });
            await refreshLinks();
          };
        });
      } catch (err) {
        listEl.innerHTML = `<p style="color:var(--danger);margin:0;">${esc(err.message)}</p>`;
      }
    }

    dlg.querySelectorAll(".create-share-btn").forEach((btn) => {
      btn.onclick = async () => {
        try {
          await api(`/api/items/${encodeURIComponent(item.id)}/shares`, {
            method: "POST",
            body: JSON.stringify({ role: btn.dataset.role, label: `Link (${btn.dataset.role})` }),
          });
          await refreshLinks();
        } catch (err) {
          showToast(err.message, true);
        }
      };
    });

    dlg.onclose = () => {
      loadItems().catch(() => {});
    };

    await refreshLinks();
    dlg.showModal();
  }

  async function openDrawer(mode, item) {
    if (!item) {
      showToast("Open a folder or select an item first.", true);
      return;
    }
    state.drawer = { mode, itemId: item.id, title: item.name };
    await refreshDrawer();
  }

  async function refreshDrawer() {
    if (!state.drawer) return;
    try {
      const endpoint =
        state.drawer.mode === "comments"
          ? `/api/items/${encodeURIComponent(state.drawer.itemId)}/comments`
          : `/api/items/${encodeURIComponent(state.drawer.itemId)}/activity`;
      state.drawerItems = await api(endpoint);
      render();
    } catch (err) {
      showToast(err.message, true);
    }
  }

  function renderTopbarPeers() {
    const el = document.getElementById("peer-stack");
    if (!el) return;
    el.innerHTML = state.peers
      .slice(0, 5)
      .map(
        (p) =>
          `<div class="peer-avatar" style="background:${esc(p.color)}" title="${esc(p.name)}">${esc(
            initials(p.name)
          )}</div>`
      )
      .join("");
  }

  function renderToast() {
    let el = document.getElementById("office-toast");
    if (!state.toast) {
      if (el) el.remove();
      return;
    }
    if (!el) {
      el = document.createElement("div");
      el.id = "office-toast";
      document.body.appendChild(el);
    }
    el.className = "toast" + (state.toast.isError ? " error" : "");
    el.textContent = state.toast.msg;
  }

  function render() {
    const root = document.getElementById("app");
    const items = visibleItems();
    const canEdit = state.role === "owner" || state.role === "edit";
    const currentFolder = state.breadcrumbs[state.breadcrumbs.length - 1] || null;
    const viewTitles = {
      folder: currentFolder ? currentFolder.name : "My Files",
      recent: "Recent Files",
      starred: "Starred",
      shared: "Shared Links",
      trash: "Trash",
    };
    const heading = state.folderId ? currentFolder?.name || "Folder" : viewTitles[state.view] || "My Files";

    root.innerHTML = `
      <aside class="sidebar">
        <div class="brand">
          <div class="brand-mark">O</div>
          <div>
            <div class="brand-title">PlotlyOffice<span>.</span></div>
            <div class="brand-eyebrow">ALL-IN-ONE WORKSPACE</div>
          </div>
        </div>

        <div class="sidebar-actions">
          <button class="btn-new" id="btn-new" ${!canEdit ? "disabled" : ""}>+ New</button>
          <button class="btn-upload" id="btn-upload" ${!canEdit || state.view === "trash" ? "disabled" : ""}>↥ Upload</button>
          <input type="file" id="file-input" multiple hidden>
          ${
            state.newMenuOpen
              ? `<div class="new-menu" id="new-menu">
                  <button data-new="folder">📁 New Folder</button>
                  <div class="menu-sep"></div>
                  <button data-new="word">📝 New Word Document</button>
                  <button data-new="sheets">📊 New Spreadsheet</button>
                  <button data-new="slides">📽️ New Presentation</button>
                </div>`
              : ""
          }
        </div>

        <div class="sidebar-section-label">
          <span>Your Drive</span>
          <span class="pill-private">${state.shareToken ? "SHARED" : "PRIVATE"}</span>
        </div>

        <nav class="nav-list">
          <button class="nav-item ${!state.folderId && state.view === "folder" ? "active" : ""}" data-view="folder">📁 My Files</button>
          <button class="nav-item ${!state.folderId && state.view === "recent" ? "active" : ""}" data-view="recent">🕒 Recent</button>
          <button class="nav-item ${!state.folderId && state.view === "starred" ? "active" : ""}" data-view="starred">⭐ Starred</button>
          <button class="nav-item ${!state.folderId && state.view === "shared" ? "active" : ""}" data-view="shared">🔗 Shared</button>
          <button class="nav-item ${!state.folderId && state.view === "trash" ? "active" : ""}" data-view="trash">🗑️ Trash</button>
        </nav>

        <div class="sidebar-section-label" style="margin-top:14px;">
          <span>Office Suite</span>
        </div>
        <div class="suite-links">
          <a class="suite-link" href="${esc(state.suiteUrls.word || "#")}" target="_blank" rel="noopener">
            <span><span class="suite-dot" style="background:#60a5fa"></span>PlotlyWord</span>
            <span>↗</span>
          </a>
          <a class="suite-link" href="${esc(state.suiteUrls.sheets || "#")}" target="_blank" rel="noopener">
            <span><span class="suite-dot" style="background:#34d399"></span>PlotlySheets</span>
            <span>↗</span>
          </a>
          <a class="suite-link" href="${esc(state.suiteUrls.slides || "#")}" target="_blank" rel="noopener">
            <span><span class="suite-dot" style="background:#fbbf24"></span>PlotlySlides</span>
            <span>↗</span>
          </a>
        </div>

        <div class="sidebar-spacer"></div>

        <button class="profile-btn" id="btn-profile">
          <span class="online-dot"></span>
          <div style="min-width:0;flex:1;">
            <div style="font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${esc(state.name)}</div>
            <div style="font-size:11px;color:var(--sidebar-muted);">Workspace active</div>
          </div>
        </button>
      </aside>

      <main class="main" id="drop-zone">
        ${
          state.dragging
            ? `<div class="drop-overlay"><div class="drop-overlay-card">Drop files to upload to ${esc(heading)}</div></div>`
            : ""
        }
        <header class="topbar">
          <div class="topbar-left">
            <div class="breadcrumbs">
              ${
                !state.shareToken
                  ? `<button class="crumb-btn ${!state.folderId ? "current" : ""}" data-crumb="">My Files</button>`
                  : `<span class="crumb-btn" style="cursor:default;">Shared</span>`
              }
              ${state.breadcrumbs
                .map(
                  (c, idx) =>
                    `<span class="crumb-sep">/</span>
                     <button class="crumb-btn ${idx === state.breadcrumbs.length - 1 ? "current" : ""}" data-crumb="${esc(c.id)}">${esc(c.name)}</button>`
                )
                .join("")}
            </div>
            <div class="search-box">
              <span class="search-icon">🔍</span>
              <input id="search-input" type="search" placeholder="Search files &amp; folders…" value="${esc(state.query)}">
            </div>
          </div>
          <div class="topbar-right">
            <div class="peer-stack" id="peer-stack"></div>
            ${
              currentFolder
                ? `<button class="btn btn-sm" id="btn-folder-comments">💬 Comments</button>
                   <button class="btn btn-sm" id="btn-folder-activity">📜 Activity</button>
                   ${state.role === "owner" ? `<button class="btn btn-sm btn-primary" id="btn-folder-share">🔗 Share Folder</button>` : ""}`
                : ""
            }
            <button class="btn btn-sm" id="btn-suite-settings" title="Configure PlotlyWord, PlotlySheets &amp; PlotlySlides URLs">⚙ Suite</button>
          </div>
        </header>

        <div class="toolbar">
          <div class="toolbar-title-wrap">
            <h1 class="view-heading">${esc(heading)}</h1>
            ${state.folderId ? `<span class="role-badge">${esc(state.role)}</span>` : ""}
          </div>
          <div class="toolbar-controls">
            <div class="filter-chips">
              ${["all", "word", "sheets", "slides", "other"]
                .map(
                  (t) =>
                    `<button class="chip ${state.typeFilter === t ? "active" : ""}" data-filter="${t}">${
                      t === "all" ? "All" : t === "other" ? "Other" : SUITE_LABELS[t].short
                    }</button>`
                )
                .join("")}
            </div>
            <select class="select-clean" id="sort-select">
              <option value="name" ${state.sortBy === "name" ? "selected" : ""}>Sort: Name</option>
              <option value="updated" ${state.sortBy === "updated" ? "selected" : ""}>Sort: Modified</option>
              <option value="size" ${state.sortBy === "size" ? "selected" : ""}>Sort: Size</option>
            </select>
            <div class="view-toggle">
              <button class="${state.layoutMode === "grid" ? "active" : ""}" data-layout="grid" title="Grid view">☷</button>
              <button class="${state.layoutMode === "list" ? "active" : ""}" data-layout="list" title="List view">☰</button>
            </div>
          </div>
        </div>

        <div class="content-scroll">
          ${renderItems(items, canEdit)}
        </div>

        ${renderDrawer()}
      </main>

      <dialog id="share-dialog">
        <div class="dialog-head">
          <div>
            <div class="dialog-eyebrow">COLLABORATION</div>
            <h3 class="dialog-title" id="share-item-title">Share</h3>
          </div>
          <button class="btn btn-sm" onclick="document.getElementById('share-dialog').close()">✕</button>
        </div>
        <div class="dialog-body">
          <div class="share-create-grid">
            <button class="share-create-card create-share-btn" data-role="edit">
              <strong>+ Can edit</strong>
              <div style="font-size:11.5px;color:var(--muted);margin-top:2px;">Upload, create folders &amp; edit in suite</div>
            </button>
            <button class="share-create-card create-share-btn" data-role="view">
              <strong>+ Can view</strong>
              <div style="font-size:11.5px;color:var(--muted);margin-top:2px;">Browse, preview, download &amp; view</div>
            </button>
          </div>
          <div id="share-links-list" style="display:flex;flex-direction:column;gap:8px;"></div>
        </div>
      </dialog>

      <dialog id="suite-dialog">
        <div class="dialog-head">
          <div>
            <div class="dialog-eyebrow">INTEGRATIONS</div>
            <h3 class="dialog-title">Office Suite URLs</h3>
          </div>
          <button class="btn btn-sm" onclick="document.getElementById('suite-dialog').close()">✕</button>
        </div>
        <div class="dialog-body">
          <p style="margin:0;color:var(--muted);">Point PlotlyOffice to where your sibling apps are running.</p>
          <div>
            <label class="field-label">PlotlyWord URL (.docx, .txt, .md, .html)</label>
            <input class="input-full" id="url-word" value="${esc(state.suiteUrls.word || "")}">
          </div>
          <div>
            <label class="field-label">PlotlySheets URL (.xlsx, .csv, .ods)</label>
            <input class="input-full" id="url-sheets" value="${esc(state.suiteUrls.sheets || "")}">
          </div>
          <div>
            <label class="field-label">PlotlySlides URL (.pptx)</label>
            <input class="input-full" id="url-slides" value="${esc(state.suiteUrls.slides || "")}">
          </div>
          <div style="display:flex;justify-content:flex-end;gap:8px;">
            <button class="btn" id="btn-suite-reset">Reset Defaults</button>
            <button class="btn btn-primary" id="btn-suite-save">Save URLs</button>
          </div>
        </div>
      </dialog>

      <dialog id="preview-dialog" class="dialog-wide">
        <div class="dialog-head">
          <div>
            <div class="dialog-eyebrow">FILE PREVIEW</div>
            <h3 class="dialog-title" id="preview-title">Preview</h3>
          </div>
          <button class="btn btn-sm" onclick="document.getElementById('preview-dialog').close()">✕</button>
        </div>
        <div class="dialog-body" id="preview-body"></div>
      </dialog>

      <dialog id="profile-dialog">
        <div class="dialog-head">
          <div>
            <div class="dialog-eyebrow">WORKSPACE IDENTITY</div>
            <h3 class="dialog-title">Your Display Name</h3>
          </div>
          <button class="btn btn-sm" onclick="document.getElementById('profile-dialog').close()">✕</button>
        </div>
        <div class="dialog-body">
          <label class="field-label">Display name shown to collaborators</label>
          <input class="input-full" id="profile-name-input" value="${esc(state.name)}">
          <div style="display:flex;justify-content:flex-end;">
            <button class="btn btn-primary" id="btn-profile-save">Update Name</button>
          </div>
        </div>
      </dialog>
    `;

    bindEvents(items);
    renderTopbarPeers();
  }

  function renderItems(items, canEdit) {
    if (!items.length) {
      return `
        <div class="empty-state">
          <h3>${state.view === "trash" ? "Trash is empty" : "This folder is empty"}</h3>
          <p>${
            state.view === "trash"
              ? "Deleted files and folders will appear here."
              : "Drag and drop files here, upload from your computer, or create a new folder or Office document."
          }</p>
          ${
            state.view !== "trash" && canEdit
              ? `<div style="display:flex;gap:10px;justify-content:center;">
                  <button class="btn btn-primary" id="empty-upload">↥ Upload Files</button>
                  <button class="btn" id="empty-folder">📁 New Folder</button>
                 </div>`
              : ""
          }
        </div>
      `;
    }

    if (state.layoutMode === "list") {
      return `
        <table class="list-table">
          <thead>
            <tr>
              <th>Name</th>
              <th>Suite App</th>
              <th>Modified</th>
              <th>Size</th>
              <th style="text-align:right;">Actions</th>
            </tr>
          </thead>
          <tbody>
            ${items
              .map((item) => {
                const ic = itemIcon(item);
                const suite = SUITE_LABELS[item.suite_app];
                return `
                  <tr data-id="${esc(item.id)}">
                    <td>
                      <div class="list-name-cell">
                        <div class="item-icon ${ic.cls}">${ic.label}</div>
                        <span class="item-name open-item">${esc(item.name)}</span>
                        ${item.shared ? `<span class="shared-pill">Shared</span>` : ""}
                      </div>
                    </td>
                    <td>${suite ? `<span class="suite-pill pill-${item.suite_app}">${suite.name}</span>` : item.kind === "folder" ? "Folder" : esc(item.ext.toUpperCase() || "File")}</td>
                    <td>${fmtDate(item.updated)}</td>
                    <td>${item.kind === "folder" ? "—" : fmtBytes(item.size_bytes)}</td>
                    <td>
                      <div class="list-actions-cell">
                        ${suite && !item.trashed ? `<button class="btn btn-sm btn-primary suite-open-btn">Open in ${suite.short}</button>` : ""}
                        ${item.kind === "file" && !item.trashed ? `<button class="btn btn-sm preview-btn">Preview</button>` : ""}
                        ${item.kind === "file" ? `<button class="btn btn-sm download-btn">↓</button>` : ""}
                        ${item.role === "owner" && !item.trashed ? `<button class="btn btn-sm share-btn">Share</button>` : ""}
                        ${canEdit && !item.trashed ? `<button class="btn btn-sm rename-btn" title="Rename">✎</button>` : ""}
                        ${canEdit && !item.trashed ? `<button class="btn btn-sm trash-btn" title="Move to Trash">🗑</button>` : ""}
                        ${item.trashed ? `<button class="btn btn-sm restore-btn">Restore</button><button class="btn btn-sm btn-danger delete-btn">Delete</button>` : ""}
                      </div>
                    </td>
                  </tr>`;
              })
              .join("")}
          </tbody>
        </table>
      `;
    }

    return `
      <div class="grid-view">
        ${items
          .map((item) => {
            const ic = itemIcon(item);
            const suite = SUITE_LABELS[item.suite_app];
            return `
              <div class="item-card" data-id="${esc(item.id)}">
                <div class="card-top">
                  <div class="item-icon ${ic.cls}">${ic.label}</div>
                  <div class="card-badges">
                    ${suite ? `<span class="suite-pill pill-${item.suite_app}">${suite.short}</span>` : ""}
                    ${item.shared ? `<span class="shared-pill">Shared</span>` : ""}
                    ${canEdit && !item.trashed ? `<button class="icon-btn star-btn ${item.starred ? "starred" : ""}" title="Star">★</button>` : ""}
                  </div>
                </div>
                <div class="item-name open-item">${esc(item.name)}</div>
                <div class="item-meta">
                  <span>${item.kind === "folder" ? "Folder" : fmtBytes(item.size_bytes)}</span>
                  <span>${fmtDate(item.updated)}</span>
                </div>
                <div class="card-actions">
                  ${
                    item.trashed
                      ? `<button class="btn btn-sm restore-btn" style="flex:1;justify-content:center;">Restore</button>
                         <button class="btn btn-sm btn-danger delete-btn">Delete</button>`
                      : `
                        ${
                          item.kind === "folder"
                            ? `<button class="btn btn-sm open-item" style="flex:1;justify-content:center;">Open Folder</button>`
                            : suite
                            ? `<button class="btn btn-sm btn-primary btn-open-suite suite-open-btn">Open in ${suite.short}</button>`
                            : `<button class="btn btn-sm preview-btn" style="flex:1;justify-content:center;">Preview</button>`
                        }
                        ${item.kind === "file" ? `<button class="icon-btn download-btn" title="Download">↓</button>` : ""}
                        ${item.role === "owner" ? `<button class="icon-btn share-btn" title="Share">🔗</button>` : ""}
                        <button class="icon-btn comments-btn" title="Comments">💬</button>
                        ${canEdit ? `<button class="icon-btn rename-btn" title="Rename">✎</button>` : ""}
                        ${canEdit ? `<button class="icon-btn trash-btn" title="Trash">🗑</button>` : ""}
                      `
                  }
                </div>
              </div>
            `;
          })
          .join("")}
      </div>
    `;
  }

  function renderDrawer() {
    if (!state.drawer) return "";
    const isComments = state.drawer.mode === "comments";
    return `
      <aside class="drawer">
        <div class="drawer-head">
          <div>
            <div class="dialog-eyebrow">${isComments ? "COMMENTS" : "ACTIVITY"}</div>
            <h3>${esc(state.drawer.title)}</h3>
          </div>
          <button class="btn btn-sm" id="close-drawer">✕</button>
        </div>
        <div class="drawer-body">
          ${
            !state.drawerItems.length
              ? `<p style="color:var(--muted);">No ${isComments ? "comments" : "activity"} yet.</p>`
              : state.drawerItems
                  .map((entry) =>
                    isComments
                      ? `<div class="comment-card ${entry.resolved ? "resolved" : ""}" data-cid="${esc(entry.id)}">
                          <div class="comment-head">
                            <span class="comment-author">
                              <span style="width:8px;height:8px;border-radius:50%;background:${esc(entry.author_color)};display:inline-block;"></span>
                              ${esc(entry.author_name)}
                            </span>
                            <span>${fmtDate(entry.created)}</span>
                          </div>
                          <div>${esc(entry.body)}</div>
                        </div>`
                      : `<div class="comment-card">
                          <div class="comment-head">
                            <span class="comment-author">${esc(entry.actor_name)}</span>
                            <span>${fmtDate(entry.created)}</span>
                          </div>
                          <div><strong>${esc(entry.action)}</strong> ${esc(entry.detail)}</div>
                        </div>`
                  )
                  .join("")
          }
        </div>
        ${
          isComments
            ? `<form class="drawer-foot" id="comment-form">
                <input id="comment-input" placeholder="Add a comment…" value="${esc(state.commentText)}">
                <button class="btn btn-primary btn-sm" type="submit">Send</button>
               </form>`
            : ""
        }
      </aside>
    `;
  }

  function bindEvents(items) {
    const byId = Object.fromEntries(items.map((i) => [i.id, i]));

    document.getElementById("btn-new")?.addEventListener("click", (e) => {
      e.stopPropagation();
      state.newMenuOpen = !state.newMenuOpen;
      render();
    });

    document.getElementById("new-menu")?.addEventListener("click", (e) => {
      const btn = e.target.closest("button[data-new]");
      if (!btn) return;
      const what = btn.dataset.new;
      if (what === "folder") createFolderPrompt();
      else createBlankDoc(what);
    });

    const fileInput = document.getElementById("file-input");
    document.getElementById("btn-upload")?.addEventListener("click", () => fileInput?.click());
    document.getElementById("empty-upload")?.addEventListener("click", () => fileInput?.click());
    document.getElementById("empty-folder")?.addEventListener("click", createFolderPrompt);
    fileInput?.addEventListener("change", () => uploadFiles(fileInput.files));

    document.querySelectorAll(".nav-item[data-view]").forEach((btn) => {
      btn.addEventListener("click", () => {
        state.folderId = null;
        state.shareToken = "";
        state.view = btn.dataset.view;
        loadItems();
      });
    });

    document.querySelectorAll(".crumb-btn[data-crumb]").forEach((btn) => {
      btn.addEventListener("click", () => openFolder(btn.dataset.crumb || null));
    });

    const searchInput = document.getElementById("search-input");
    searchInput?.addEventListener("input", (e) => {
      state.query = e.target.value;
      clearTimeout(bindEvents._st);
      bindEvents._st = setTimeout(() => {
        if (!state.folderId) loadItems();
        else render();
      }, 180);
    });

    document.querySelectorAll(".chip[data-filter]").forEach((btn) => {
      btn.addEventListener("click", () => {
        state.typeFilter = btn.dataset.filter;
        render();
      });
    });

    document.getElementById("sort-select")?.addEventListener("change", (e) => {
      state.sortBy = e.target.value;
      render();
    });

    document.querySelectorAll(".view-toggle button[data-layout]").forEach((btn) => {
      btn.addEventListener("click", () => {
        state.layoutMode = btn.dataset.layout;
        localStorage.setItem(VIEW_MODE_STORAGE, state.layoutMode);
        render();
      });
    });

    // Item actions
    document.querySelectorAll("[data-id]").forEach((el) => {
      const item = byId[el.dataset.id];
      if (!item) return;
      el.querySelectorAll(".open-item").forEach((b) =>
        b.addEventListener("click", () => {
          if (item.kind === "folder") openFolder(item.id);
          else if (item.suite_app) openInSuite(item);
          else openPreview(item);
        })
      );
      el.querySelector(".suite-open-btn")?.addEventListener("click", () => openInSuite(item));
      el.querySelector(".preview-btn")?.addEventListener("click", () => openPreview(item));
      el.querySelector(".download-btn")?.addEventListener("click", () => downloadFile(item));
      el.querySelector(".share-btn")?.addEventListener("click", () => openShareDialog(item));
      el.querySelector(".comments-btn")?.addEventListener("click", () => openDrawer("comments", item));
      el.querySelector(".rename-btn")?.addEventListener("click", () => renameItem(item));
      el.querySelector(".star-btn")?.addEventListener("click", () => toggleStar(item));
      el.querySelector(".trash-btn")?.addEventListener("click", () => trashOrRestore(item, true));
      el.querySelector(".restore-btn")?.addEventListener("click", () => trashOrRestore(item, false));
      el.querySelector(".delete-btn")?.addEventListener("click", () => deleteForever(item));
    });

    // Topbar folder actions
    const currentFolder = state.breadcrumbs[state.breadcrumbs.length - 1];
    document.getElementById("btn-folder-share")?.addEventListener("click", () => openShareDialog(currentFolder));
    document.getElementById("btn-folder-comments")?.addEventListener("click", () => openDrawer("comments", currentFolder));
    document.getElementById("btn-folder-activity")?.addEventListener("click", () => openDrawer("activity", currentFolder));

    // Suite settings
    document.getElementById("btn-suite-settings")?.addEventListener("click", () => {
      document.getElementById("suite-dialog").showModal();
    });
    document.getElementById("btn-suite-save")?.addEventListener("click", () => {
      const next = {
        word: document.getElementById("url-word").value.trim().replace(/\/+$/, ""),
        sheets: document.getElementById("url-sheets").value.trim().replace(/\/+$/, ""),
        slides: document.getElementById("url-slides").value.trim().replace(/\/+$/, ""),
      };
      state.suiteUrls = { ...state.defaultSuiteUrls, ...next };
      localStorage.setItem(SUITE_URLS_STORAGE, JSON.stringify(state.suiteUrls));
      document.getElementById("suite-dialog").close();
      showToast("Suite URLs saved.");
      render();
    });
    document.getElementById("btn-suite-reset")?.addEventListener("click", () => {
      localStorage.removeItem(SUITE_URLS_STORAGE);
      state.suiteUrls = { ...state.defaultSuiteUrls };
      document.getElementById("suite-dialog").close();
      showToast("Reset to default suite URLs.");
      render();
    });

    // Profile dialog
    document.getElementById("btn-profile")?.addEventListener("click", () => {
      document.getElementById("profile-dialog").showModal();
    });
    document.getElementById("btn-profile-save")?.addEventListener("click", async () => {
      const val = document.getElementById("profile-name-input").value.trim();
      if (!val) return;
      try {
        const res = await api("/api/session", { method: "POST", body: JSON.stringify({ name: val }) });
        state.name = res.name;
        sessionStorage.setItem(NAME_STORAGE, res.name);
        localStorage.setItem(NAME_STORAGE, res.name);
        document.getElementById("profile-dialog").close();
        showToast("Display name updated.");
        render();
      } catch (err) {
        showToast(err.message, true);
      }
    });

    // Drawer
    document.getElementById("close-drawer")?.addEventListener("click", () => {
      state.drawer = null;
      render();
    });
    document.getElementById("comment-form")?.addEventListener("submit", async (e) => {
      e.preventDefault();
      const input = document.getElementById("comment-input");
      const text = input.value.trim();
      if (!text) return;
      try {
        await api(`/api/items/${encodeURIComponent(state.drawer.itemId)}/comments`, {
          method: "POST",
          body: JSON.stringify({ body: text }),
        });
        state.commentText = "";
        await refreshDrawer();
      } catch (err) {
        showToast(err.message, true);
      }
    });

    // Drag and drop upload
    const dz = document.getElementById("drop-zone");
    if (dz) {
      dz.ondragover = (e) => {
        e.preventDefault();
        if (!state.dragging) {
          state.dragging = true;
          render();
        }
      };
      dz.ondragleave = (e) => {
        if (e.target === dz) {
          state.dragging = false;
          render();
        }
      };
      dz.ondrop = (e) => {
        e.preventDefault();
        state.dragging = false;
        render();
        if (e.dataTransfer?.files?.length) {
          uploadFiles(e.dataTransfer.files);
        }
      };
    }
  }

  document.addEventListener("click", () => {
    if (state.newMenuOpen) {
      state.newMenuOpen = false;
      render();
    }
  });

  async function boot() {
    parseLocation();
    await ensureSession();
    await loadSuiteConfig();
    await loadItems();
    await heartbeat();
    setInterval(async () => {
      if (!state.folderId) return;
      heartbeat();
      const hasOpenDialog = document.querySelector("dialog[open]");
      const activeTag = document.activeElement?.tagName;
      if (hasOpenDialog || activeTag === "INPUT" || activeTag === "TEXTAREA" || activeTag === "SELECT") {
        return;
      }
      try {
        const params = new URLSearchParams({ parent_id: state.folderId });
        const data = await api("/api/items?" + params.toString());
        if (JSON.stringify(data.items) !== JSON.stringify(state.items)) {
          state.items = data.items || [];
          state.breadcrumbs = data.breadcrumbs || [];
          state.role = data.role || "owner";
          render();
        }
      } catch {
        // ignore transient poll errors
      }
    }, 2500);
  }

  boot().catch((err) => {
    document.getElementById("app").innerHTML = `<div class="empty-state"><h3>Unable to load PlotlyOffice</h3><p>${esc(err.message)}</p></div>`;
  });
})();
