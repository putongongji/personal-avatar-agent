const messages = document.querySelector("#messages");
const form = document.querySelector("#composer");
const input = document.querySelector("#question");
const sendButton = document.querySelector("#send");
const suggestions = document.querySelector("#suggestions");
const conversationList = document.querySelector("#conversationList");
const newConversationButton = document.querySelector("#newConversation");
const userBadge = document.querySelector("#userBadge");
const adminLink = document.querySelector("#adminLink");
const logoutButton = document.querySelector("#logout");
const conversationStorageKey = "personal-avatar-agent:conversation-id";

let conversationId = localStorage.getItem(conversationStorageKey) || "";
let isStreaming = false;
let shouldFollowOutput = true;
let currentUser = null;
let currentAbortController = null;
let stopRequested = false;

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function scrollToLatest(force = false) {
  if (!force && !shouldFollowOutput) return;
  requestAnimationFrame(() => {
    messages.scrollTop = messages.scrollHeight;
  });
}

function updateFollowState() {
  const distance = messages.scrollHeight - messages.scrollTop - messages.clientHeight;
  shouldFollowOutput = distance < 96;
}

function resizeInput() {
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, 180)}px`;
}

function createMessage(role, meta = "") {
  const node = document.createElement("article");
  node.className = `message ${role}`;

  const metaNode = document.createElement("div");
  metaNode.className = "meta";
  metaNode.textContent = meta || (role === "user" ? "你" : "Agent");

  const progressNode = document.createElement("div");
  progressNode.className = "trace";

  const body = document.createElement("div");
  body.className = "answer markdown-body";

  node.append(metaNode);
  if (role === "assistant") node.append(progressNode);
  node.append(body);
  messages.appendChild(node);
  scrollToLatest(true);
  return { node, metaNode, progressNode, body };
}

function addUserMessage(text) {
  const message = createMessage("user", "你");
  message.body.textContent = text;
}

function addAssistantMessage(text, meta = "Agent", sources = []) {
  const message = createMessage("assistant", meta);
  message.body.innerHTML = renderMarkdown(text);
  attachCitationTooltips(message.body, sources);
}

function renderInline(text) {
  return escapeHtml(text)
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/`(.+?)`/g, "<code>$1</code>")
    .replace(/(?:\[S(\d+)\]|\bS(\d+)\b)/g, (_, bracketRef, bareRef) => {
      const ref = bracketRef || bareRef;
      return `<span class="citation" data-source-ref="S${ref}">S${ref}</span>`;
    });
}

function renderMarkdown(markdown) {
  const lines = String(markdown || "").split(/\r?\n/);
  const html = [];
  let inList = false;

  function closeList() {
    if (inList) {
      html.push("</ul>");
      inList = false;
    }
  }

  for (const rawLine of lines) {
    const line = rawLine.trimEnd();
    if (!line.trim()) {
      closeList();
      continue;
    }
    if (/^---+$/.test(line.trim())) {
      closeList();
      html.push("<hr>");
      continue;
    }
    const heading = line.match(/^(#{1,4})\s+(.+)$/);
    if (heading) {
      closeList();
      const level = Math.min(heading[1].length + 1, 4);
      html.push(`<h${level}>${renderInline(heading[2])}</h${level}>`);
      continue;
    }
    const bullet = line.match(/^[-*]\s+(.+)$/);
    if (bullet) {
      if (!inList) {
        html.push("<ul>");
        inList = true;
      }
      html.push(`<li>${renderInline(bullet[1])}</li>`);
      continue;
    }
    const numbered = line.match(/^\d+\.\s+(.+)$/);
    if (numbered) {
      if (!inList) {
        html.push("<ul>");
        inList = true;
      }
      html.push(`<li>${renderInline(numbered[1])}</li>`);
      continue;
    }
    closeList();
    html.push(`<p>${renderInline(line)}</p>`);
  }
  closeList();
  return html.join("");
}

function attachCitationTooltips(node, sources = []) {
  const citations = node.querySelectorAll(".citation");
  citations.forEach((citation) => {
    const match = citation.dataset.sourceRef?.match(/S(\d+)/);
    if (!match) return;
    const source = sources[Number(match[1]) - 1];
    if (!source) return;
    citation.dataset.quote = source.quote || source.excerpt || "";
    citation.dataset.heading = source.heading || "";
    citation.title = source.quote || source.excerpt || "";
  });
}

function renderTrace(node, items, state = "running") {
  if (!items.length) {
    node.innerHTML = "";
    return;
  }
  const current = items.at(-1)?.message || "正在处理";
  const label = state === "done" ? "执行过程" : current;
  const detailsOpen = state !== "done" ? "open" : "";
  node.innerHTML = `
    <details class="trace-panel ${state}" ${detailsOpen}>
      <summary>
        <span class="trace-spinner"></span>
        <span>${label}</span>
        <small>${state === "done" ? "完成" : "进行中"}</small>
      </summary>
      <div class="trace-list">
        ${items
          .map(
            (item, index) => `<div class="trace-item ${state !== "done" && index === items.length - 1 ? "active" : ""}">
              <span>${escapeHtml(item.message)}</span>
            </div>`
          )
          .join("")}
      </div>
    </details>
  `;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function ask(question) {
  const text = question.trim();
  if (!text || isStreaming) return;

  addUserMessage(text);
  input.value = "";
  resizeInput();
  suggestions.innerHTML = "";
  sendButton.disabled = false;
  sendButton.textContent = "停止";
  sendButton.classList.add("stop");
  isStreaming = true;
  stopRequested = false;
  currentAbortController = new AbortController();
  shouldFollowOutput = true;

  const assistant = createMessage("assistant", "Agent");
  let answer = "";
  let displayedAnswer = "";
  let trace = [];
  let latestSources = [];
  let progressChain = Promise.resolve();
  let typeChain = Promise.resolve();

  function refreshAnswer() {
    assistant.body.innerHTML = renderMarkdown(displayedAnswer);
    scrollToLatest();
  }

  function enqueueProgress(message, items = []) {
    progressChain = progressChain.then(async () => {
      if (trace.at(-1)?.message !== message) {
        trace.push({ message, items });
      }
      renderTrace(assistant.progressNode, trace, "running");
      scrollToLatest();
      await sleep(360);
    });
    return progressChain;
  }

  function enqueueText(textChunk) {
    typeChain = typeChain.then(async () => {
      await progressChain;
      for (const char of Array.from(textChunk)) {
        displayedAnswer += char;
        refreshAnswer();
        await sleep(char.trim() ? 8 : 3);
      }
    });
    return typeChain;
  }

  try {
    const response = await fetch("/api/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: text, user_type: "public", conversation_id: conversationId }),
      signal: currentAbortController.signal,
    });
    if (!response.ok || !response.body) {
      const data = await response.json().catch(() => ({}));
      throw new Error(data.detail || "请求失败");
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop() || "";

      for (const line of lines) {
        if (!line.trim()) continue;
        const event = JSON.parse(line);
        if (event.type === "progress") {
          enqueueProgress(event.message, event.items || []);
        }
        if (event.type === "delta") {
          answer += event.text;
          enqueueText(event.text);
        }
        if (event.type === "sources") {
          latestSources = event.sources || [];
        }
        if (event.type === "done") {
          if (event.conversation_id) {
            conversationId = event.conversation_id;
            localStorage.setItem(conversationStorageKey, conversationId);
          }
          await progressChain;
          await typeChain;
          displayedAnswer = answer;
          assistant.metaNode.textContent = `${event.answer_provider} · 质量分 ${Math.round(event.quality_score)}`;
          assistant.body.innerHTML = renderMarkdown(displayedAnswer);
          attachCitationTooltips(assistant.body, event.sources || latestSources);
          renderTrace(assistant.progressNode, trace, "done");
          renderSuggestions(event.suggestions || []);
          await loadConversations();
          scrollToLatest();
        }
      }
    }
  } catch (error) {
    if (error.name === "AbortError" || stopRequested) {
      await progressChain;
      await typeChain;
      if (trace.length) renderTrace(assistant.progressNode, trace, "done");
      assistant.metaNode.textContent = "Agent · 已停止";
      if (!displayedAnswer.trim()) {
        assistant.body.textContent = "已停止回答。";
      }
    } else {
      assistant.progressNode.innerHTML = "";
      assistant.body.textContent = `请求失败：${error.message}`;
    }
  } finally {
    await progressChain;
    await typeChain;
    isStreaming = false;
    stopRequested = false;
    currentAbortController = null;
    sendButton.disabled = false;
    sendButton.textContent = "发送";
    sendButton.classList.remove("stop");
    input.focus();
    scrollToLatest();
  }
}

function stopAnswer() {
  if (!isStreaming || !currentAbortController) return;
  stopRequested = true;
  sendButton.disabled = true;
  sendButton.textContent = "停止中";
  currentAbortController.abort();
}

function renderSuggestions(questions) {
  suggestions.innerHTML = "";
  questions.slice(0, 3).forEach((question) => {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = question;
    button.addEventListener("click", () => ask(question));
    suggestions.appendChild(button);
  });
}

async function loadSuggestions() {
  const response = await fetch("/api/suggested-questions");
  const data = await response.json();
  renderSuggestions(data.questions || []);
}

async function loadMe() {
  const response = await fetch("/api/me");
  const data = await response.json();
  if (!data.user) {
    window.location.href = "/login";
    return null;
  }
  currentUser = data.user;
  userBadge.innerHTML = `<strong>${escapeHtml(currentUser.display_name || "访客")}</strong><span>${escapeHtml(currentUser.email || currentUser.id)}</span>`;
  adminLink.classList.toggle("hidden", currentUser.role !== "admin");
  return currentUser;
}

async function logout() {
  await fetch("/api/auth/logout", { method: "POST" });
  localStorage.removeItem(conversationStorageKey);
  window.location.href = "/login";
}

function startNewConversation() {
  conversationId = "";
  localStorage.removeItem(conversationStorageKey);
  messages.innerHTML = "";
  renderSuggestions([]);
  loadSuggestions();
  markActiveConversation();
  input.focus();
}

function markActiveConversation() {
  conversationList.querySelectorAll(".conversation-item").forEach((button) => {
    button.classList.toggle("active", button.dataset.id === conversationId);
  });
}

async function loadConversations() {
  const response = await fetch("/api/conversations");
  if (response.status === 401) {
    window.location.href = "/login";
    return;
  }
  if (!response.ok) return;
  const data = await response.json();
  const conversations = data.conversations || [];
  conversationList.innerHTML = "";
  if (!conversations.length) {
    conversationList.innerHTML = `<div class="conversation-empty">暂无对话</div>`;
    return;
  }
  conversations.forEach((item) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "conversation-item";
    button.dataset.id = item.id;
    button.innerHTML = `
      <strong>${escapeHtml(item.title || "新的对话")}</strong>
      <span>${escapeHtml(item.updated_at || "")}</span>
    `;
    button.addEventListener("click", () => openConversation(item.id));
    conversationList.appendChild(button);
  });
  markActiveConversation();
}

async function openConversation(id) {
  if (!id || id === conversationId || isStreaming) return;
  conversationId = id;
  localStorage.setItem(conversationStorageKey, conversationId);
  await restoreConversation();
  markActiveConversation();
  input.focus();
}

async function restoreConversation() {
  if (!conversationId) return false;
  const response = await fetch(`/api/conversations/${conversationId}`);
  if (!response.ok) {
    localStorage.removeItem(conversationStorageKey);
    conversationId = "";
    return false;
  }
  const data = await response.json();
  messages.innerHTML = "";
  (data.messages || []).forEach((item) => {
    if (item.role === "user") {
      addUserMessage(item.content || "");
    } else {
      const metadata = item.metadata || {};
      const meta = metadata.answer_provider
        ? `${metadata.answer_provider} · 质量分 ${Math.round(metadata.quality_score || 0)}`
        : "Agent";
      addAssistantMessage(item.content || "", meta, item.sources || []);
    }
  });
  scrollToLatest(true);
  return (data.messages || []).length > 0;
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  if (isStreaming) {
    stopAnswer();
    return;
  }
  ask(input.value);
});

input.addEventListener("keydown", (event) => {
  if (event.key !== "Enter") return;
  if (event.shiftKey) return;
  event.preventDefault();
  if (isStreaming) {
    stopAnswer();
    return;
  }
  ask(input.value);
});

input.addEventListener("input", resizeInput);
messages.addEventListener("scroll", updateFollowState);
newConversationButton.addEventListener("click", startNewConversation);
logoutButton.addEventListener("click", logout);

(async function init() {
  const user = await loadMe();
  if (!user) return;
  await loadConversations();
  const restored = await restoreConversation();
  if (!restored) {
    await loadSuggestions();
  }
  resizeInput();
  input.focus();
})();
