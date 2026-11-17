"use strict";
(function () {
  const $ = (id) => document.getElementById(id);
  const on = (id, event, handler) => $(id)?.addEventListener(event, handler);
  const setText = (id, text) => { const node = $(id); if (node) node.textContent = text; };
  const DISPLAY = document.body.classList.contains("display");
  const SOURCE_LABELS = { system: "system audio", microphone: "the microphone", both: "system audio and the microphone" };
  const state = { session: "idle", documents: [], context: new Map(), turns: 0, events: null };

  // ---------- helpers ----------
  function el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (key === "class") node.className = value;
      else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
      else node.setAttribute(key, value);
    }
    for (const child of children.flat()) {
      if (child == null) continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  async function api(path, options = {}) {
    const res = await fetch(path, { credentials: "same-origin", ...options });
    if (res.status === 401) { window.location.replace("/login"); throw new Error("Session expired."); }
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(body.detail || `Request failed (${res.status}).`);
    return body;
  }

  const json = (method, body) => ({ method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
  const time = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  const sizeLabel = (bytes) => bytes > 1048576 ? `${(bytes / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1024))} KB`;

  function notice(text, kind = "ok") {
    const box = $("docNotice");
    if (!box) return;
    box.textContent = text;
    box.className = `notice ${kind}`;
    clearTimeout(notice.timer);
    notice.timer = setTimeout(() => box.classList.add("hidden"), kind === "error" ? 8000 : 3500);
  }

  // Text with query terms highlighted, built from text nodes only (no innerHTML).
  function highlighted(text, terms) {
    const fold = (s) => s.normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();
    const wanted = new Set((terms || []).map(fold));
    const out = [];
    for (const part of text.split(/(\p{L}[\p{L}\p{N}]*|\p{N}+)/u)) {
      if (part && wanted.has(fold(part))) out.push(el("mark", {}, part)); else if (part) out.push(part);
    }
    return out;
  }

  // Summary with [S1] labels rendered as small citation chips.
  function summaryNodes(text) {
    return text.split(/(\[S\d+\])/).filter(Boolean).map((part) =>
      /^\[S\d+\]$/.test(part) ? el("span", { class: "cite" }, part.slice(1, -1)) : part);
  }

  // ---------- session state ----------
  function applyState(s) {
    state.session = s.state;
    const listening = s.state === "listening";
    document.body.classList.toggle("listening", listening);
    const pill = $("statusPill");
    pill.className = `status-pill ${s.state === "listening" ? "listening" : s.state === "starting" ? "starting" : ""}`;
    setText("statusLabel", listening ? "LISTENING" : s.state === "starting" ? "Starting…" : "Capture off");
    $("startBtn")?.classList.toggle("hidden", listening || s.state === "starting");
    $("stopBtn")?.classList.toggle("hidden", !(listening || s.state === "starting"));
    const select = $("sourceSelect");
    if (select) { select.disabled = listening || s.state === "starting"; if (s.source) select.value = s.source; }
    if (DISPLAY) setText("idleHint", listening ? "" : "Waiting for the PC to start a session.");
    setText("bannerText", `LISTENING · ${SOURCE_LABELS[s.source] || "audio"} is being transcribed on your PC`);
    if (s.status) setText("statusLine", s.status);
  }

  // ---------- transcript ----------
  function addTurn(turn) {
    $("transcriptEmpty")?.remove();
    const box = $("transcript");
    const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
    const who = turn.speaker === "microphone" ? "You" : "Meeting";
    box.append(el("div", { class: "turn" },
      el("div", { class: `who ${turn.speaker === "microphone" ? "you" : ""}` }, who, el("time", {}, time(turn.time))),
      el("p", {}, turn.text)));
    state.turns += 1;
    if (DISPLAY) while (box.querySelectorAll(".turn").length > 3) box.querySelector(".turn").remove();
    setText("turnCount", `${state.turns}`);
    if (nearBottom) box.scrollTop = box.scrollHeight;
  }

  function resetTranscript() {
    state.turns = 0;
    setText("turnCount", "");
    $("transcript").replaceChildren(el("div", { class: "empty", id: "transcriptEmpty" },
      DISPLAY ? "Live captions will appear here." : ["Nothing is being captured. Press ", el("b", {}, "Start session"), " when everyone has agreed."]));
  }

  // ---------- context ----------
  function sourceNode(src) {
    const node = el("div", { class: `source ${src.cited ? "" : "uncited"}` },
      el("div", { class: "meta" },
        el("span", { class: "cite" }, src.label), el("b", {}, src.doc_name), el("span", {}, src.location),
        el("span", { class: "tag" }, src.cited ? "cited" : "related")),
      el("p", { class: "excerpt" }, ...highlighted(src.excerpt, src.matched_terms)));
    if (src.excerpt.length > 260) {
      const more = el("button", { class: "more", onclick: () => {
        node.classList.toggle("open");
        more.textContent = node.classList.contains("open") ? "Show less" : "Show full excerpt";
      } }, "Show full excerpt");
      node.append(more);
    }
    return node;
  }

  function renderCard(item, fresh) {
    const card = el("article", { class: `card ${fresh ? "fresh" : ""}`, "data-id": item.id },
      el("div", { class: "query" }, "About: ", el("b", {}, item.query || "")),
      item.summary ? el("p", { class: "summary" }, ...summaryNodes(item.summary))
                   : el("p", { class: "summary pending" }, item.pending ? "Reading your documents…" : "Relevant passages:"),
      ...(item.flags || []).map((f) => el("div", { class: "flag" }, "⚠ ", f)),
      el("div", { class: "sources" }, ...(item.citations || []).map(sourceNode)));
    return card;
  }

  function upsertContext(item, fresh = true) {
    $("contextEmpty")?.remove();
    const box = $("context");
    const existing = box.querySelector(`[data-id="${item.id}"]`);
    box.querySelectorAll(".card.fresh").forEach((c) => c !== existing && c.classList.remove("fresh"));
    const card = renderCard(item, fresh);
    if (existing) existing.replaceWith(card); else box.prepend(card);
    state.context.set(item.id, item);
    setText("contextCount", `${box.querySelectorAll(".card").length}`);
  }

  function removeContext(ids) {
    for (const id of ids) {
      $("context").querySelector(`[data-id="${id}"]`)?.remove();
      state.context.delete(id);
    }
    const count = $("context").querySelectorAll(".card").length;
    setText("contextCount", count ? `${count}` : "");
    if (!count) resetContext();
  }

  function resetContext() {
    state.context.clear();
    setText("contextCount", "");
    $("context").replaceChildren(el("div", { class: "empty", id: "contextEmpty" },
      el("img", { src: "/static/baya-logo.png", alt: "" }),
      "Relevant passages from your active documents will appear here, with their sources."));
  }

  // ---------- documents ----------
  function renderDocuments(docs) {
    state.documents = docs;
    if (!$("documents")) return;
    const active = docs.filter((d) => d.active).length;
    setText("docCount", docs.length ? `${active} active of ${docs.length}` : "");
    const box = $("documents");
    if (!docs.length) {
      box.replaceChildren(el("div", { class: "empty" }, "No documents yet. Add PDF, DOCX, TXT or Markdown files from this iPad."));
      return;
    }
    box.replaceChildren(...docs.map((doc) => {
      const toggle = el("input", { type: "checkbox", "aria-label": `Use ${doc.name}` });
      toggle.checked = doc.active;
      toggle.addEventListener("change", async () => {
        try { await api(`/api/documents/${doc.id}`, json("PATCH", { active: toggle.checked })); await refreshDocuments(); }
        catch (e) { toggle.checked = !toggle.checked; notice(e.message, "error"); }
      });
      return el("div", { class: `doc ${doc.active ? "" : "inactive"}` },
        el("span", { class: "ext" }, doc.ext.slice(1).toUpperCase()),
        el("div", { class: "name" }, el("b", { title: doc.name }, doc.name),
          el("span", {}, `${doc.chunk_count} passages · ${sizeLabel(doc.size)}${doc.active ? "" : " · inactive"}`)),
        el("label", { class: "switch" }, toggle, el("span")),
        el("button", { class: "icon-btn", "aria-label": `Delete ${doc.name}`, onclick: () => askDelete(doc) }, "🗑"));
    }));
  }

  async function refreshDocuments() { if ($("documents")) renderDocuments(await api("/api/documents")); }

  let pendingDelete = null;
  function askDelete(doc) { pendingDelete = doc; setText("deleteName", doc.name); $("deleteModal").classList.remove("hidden"); }
  on("deleteCancel", "click", () => $("deleteModal").classList.add("hidden"));
  on("deleteOk", "click", async () => {
    $("deleteModal").classList.add("hidden");
    if (!pendingDelete) return;
    try { await api(`/api/documents/${pendingDelete.id}`, { method: "DELETE" }); notice(`Deleted ${pendingDelete.name}.`); await refreshDocuments(); }
    catch (e) { notice(e.message, "error"); }
    pendingDelete = null;
  });

  on("uploadBtn", "click", () => $("fileInput").click());
  on("fileInput", "change", async (event) => {
    const files = [...event.target.files];
    event.target.value = "";
    for (const file of files) {
      setText("uploadHint", `Indexing ${file.name}…`);
      const form = new FormData();
      form.append("file", file);
      try {
        const res = await api("/api/documents", { method: "POST", body: form });
        notice(res.created ? `Added ${res.document.name} (${res.document.chunk_count} passages).` : `${file.name} is already in the library.`);
      } catch (e) { notice(`${file.name}: ${e.message}`, "error"); }
    }
    setText("uploadHint", "PDF, DOCX, TXT, Markdown");
    await refreshDocuments();
  });

  // ---------- session controls ----------
  on("startBtn", "click", () => {
    setText("consentSource", SOURCE_LABELS[$("sourceSelect").value]);
    $("consentCheck").checked = false;
    $("consentOk").disabled = true;
    $("consentModal").classList.remove("hidden");
  });
  on("consentCheck", "change", (e) => { $("consentOk").disabled = !e.target.checked; });
  on("consentCancel", "click", () => $("consentModal").classList.add("hidden"));
  on("consentOk", "click", async () => {
    $("consentModal").classList.add("hidden");
    applyState({ state: "starting", source: $("sourceSelect").value, status: "Starting capture…" });
    try { applyState(await api("/api/session/start", json("POST", { source: $("sourceSelect").value }))); }
    catch (e) { applyState({ state: "idle", status: e.message }); }
  });
  on("stopBtn", "click", async () => {
    try { applyState(await api("/api/session/stop", json("POST"))); } catch (e) { setText("statusLine", e.message); }
  });
  on("logoutBtn", "click", async () => {
    try { await api("/api/logout", json("POST")); } finally { window.location.replace("/login"); }
  });

  // ---------- live events ----------
  function connect() {
    if (state.events) state.events.close();
    const events = new EventSource("/api/events");
    state.events = events;
    events.addEventListener("snapshot", (e) => {
      const s = JSON.parse(e.data);
      setText("modelName", `Local model · ${s.model}`);
      applyState(s);
      resetTranscript();
      s.transcript.forEach(addTurn);
      resetContext();
      s.context.forEach((item) => upsertContext(item, false));
      renderDocuments(s.documents);
    });
    events.addEventListener("state", (e) => applyState(JSON.parse(e.data)));
    events.addEventListener("status", (e) => { setText("statusLine", JSON.parse(e.data).status); });
    events.addEventListener("transcript", (e) => addTurn(JSON.parse(e.data)));
    events.addEventListener("context_partial", (e) => {
      const d = JSON.parse(e.data);
      const current = state.context.get(d.id);
      if (current) { current.summary = d.summary; upsertContext(current); }
    });
    events.addEventListener("context", (e) => upsertContext(JSON.parse(e.data)));
    events.addEventListener("context_discard", (e) => removeContext([JSON.parse(e.data).id]));
    events.addEventListener("context_removed", (e) => removeContext(JSON.parse(e.data).ids));
    events.addEventListener("cleared", () => { resetTranscript(); resetContext(); });
    events.addEventListener("documents", () => refreshDocuments().catch(() => {}));
    events.onerror = async () => {
      setText("statusLine", "Connection to your PC lost. Reconnecting…");
      events.close();
      try { await api("/api/state"); } catch (e) { return; }  // 401 redirects to /login
      setTimeout(connect, 2000);
    };
  }

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") document.querySelectorAll(".modal").forEach((m) => m.classList.add("hidden"));
  });
  connect();
})();
