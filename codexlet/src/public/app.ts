interface SessionSummary {
  id: string;
  title: string;
  workspacePath: string;
  createdAt: string;
  updatedAt: string;
  messageCount: number;
}

// Global declarations for CDN-loaded libraries
declare const marked: {
  parse: (text: string, options?: unknown) => string;
  setOptions: (options: Record<string, unknown>) => void;
};

declare const hljs: {
  highlightElement: (element: HTMLElement) => void;
};

interface ChatMessage {
  id: string;
  role: "user" | "assistant" | "tool";
  content: string;
  timestamp: string;
  name?: string;
  args?: Record<string, unknown>;
}

interface SessionDetails {
  id: string;
  title: string;
  workspacePath: string;
  createdAt: string;
  updatedAt: string;
  messages: ChatMessage[];
}

interface TreeNode {
  name: string;
  path: string;
  type: "file" | "directory";
  children?: TreeNode[];
}

// DOM Elements
const welcomeScreen = document.getElementById("welcome-screen") as HTMLDivElement;
const chatView = document.getElementById("chat-view") as HTMLDivElement;
const chatLogEl = document.getElementById("chat-log") as HTMLDivElement;
const chatInputEl = document.getElementById("chat-input") as HTMLTextAreaElement;
const sendBtn = document.getElementById("send-btn") as HTMLButtonElement;
const chatTitleEl = document.getElementById("chat-title") as HTMLSpanElement;
const chatPathEl = document.getElementById("chat-path") as HTMLSpanElement;
const chatListEl = document.getElementById("chat-list") as HTMLUListElement;
const emptyChatsEl = document.getElementById("empty-chats") as HTMLDivElement;
const projectTreeEl = document.getElementById("project-tree") as HTMLDivElement;
const projectToggle = document.getElementById("project-toggle") as HTMLButtonElement;
const projectMenu = document.getElementById("project-menu") as HTMLDivElement;
const currentProjectEl = document.getElementById("current-project") as HTMLSpanElement;
const newTaskBtn = document.getElementById("new-task-btn") as HTMLButtonElement;
const newTaskModal = document.getElementById("new-task-modal") as HTMLDivElement;
const modalClose = document.getElementById("modal-close") as HTMLButtonElement;
const modalCancel = document.getElementById("modal-cancel") as HTMLButtonElement;
const newSessionForm = document.getElementById("new-session-form") as HTMLFormElement;
const workspacePathEl = document.getElementById("workspacePath") as HTMLInputElement;
const sessionTitleEl = document.getElementById("sessionTitle") as HTMLInputElement;
const accessToggle = document.getElementById("access-toggle") as HTMLButtonElement;
const accessDropdown = document.getElementById("access-dropdown") as HTMLDivElement;
const accessMenu = document.getElementById("access-menu") as HTMLDivElement;
const modeToggle = document.getElementById("mode-toggle") as HTMLDivElement;
const toastContainer = document.getElementById("toast-container") as HTMLDivElement;
const attachBtn = document.getElementById("attach-btn") as HTMLButtonElement;

// State
let sessions: SessionSummary[] = [];
let activeSessionId: string | null = null;
let activeSession: SessionDetails | null = null;
let currentAccess = "full";
let currentMode = "swarm";
let isLoading = false;
let projects: { name: string; path: string }[] = [];

// Initialize
(async () => {
  await loadProjects();
  await loadSessions();
  setupEventListeners();
  setupKeyboardShortcuts();
  autoResizeTextarea();
})();

// ===================== API Functions =====================

async function loadSessions(): Promise<void> {
  try {
    const response = await fetch("/api/sessions");
    if (!response.ok) throw new Error("Failed to load sessions");
    const payload = (await response.json()) as { sessions: SessionSummary[] };
    sessions = payload.sessions;
    renderChatList();
  } catch (err) {
    showToast("Failed to load sessions", "error");
    console.error(err);
  }
}

async function selectSession(sessionId: string): Promise<void> {
  activeSessionId = sessionId;
  renderChatList();

  try {
    const response = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}`);
    if (!response.ok) throw new Error("Failed to load session");
    const payload = (await response.json()) as { session: SessionDetails };
    activeSession = payload.session;
    showChatView();
    renderChat();
    updateProjectSelector(activeSession.workspacePath);
  } catch (err) {
    showToast("Failed to load session", "error");
    console.error(err);
  }
}

async function createSession(title: string, workspacePath: string): Promise<void> {
  try {
    const response = await fetch("/api/sessions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: title.trim(), workspacePath: workspacePath.trim() }),
    });
    const payload = await response.json();
    if (!response.ok) {
      showToast(payload.error || "Failed to create session", "error");
      return;
    }
    await loadSessions();
    await selectSession(payload.session.id);
    closeModal();
    showToast("Task created successfully", "success");
  } catch (err) {
    showToast("Failed to create session", "error");
    console.error(err);
  }
}

async function sendMessage(message: string): Promise<void> {
  if (!activeSessionId || !message.trim()) return;

  setLoading(true);

  // Optimistically add user message
  const userMessage: ChatMessage = {
    id: crypto.randomUUID(),
    role: "user",
    content: message.trim(),
    timestamp: new Date().toISOString(),
  };
  if (activeSession) {
    activeSession.messages.push(userMessage);
    renderChat();
  }

  // Create a placeholder assistant message for streaming
  const assistantMessageId = crypto.randomUUID();
  const assistantMessage: ChatMessage = {
    id: assistantMessageId,
    role: "assistant",
    content: "",
    timestamp: new Date().toISOString(),
  };
  if (activeSession) {
    activeSession.messages.push(assistantMessage);
    renderChat();
  }

  try {
    const response = await fetch(`/api/sessions/${encodeURIComponent(activeSessionId)}/chat/stream`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: message.trim() }),
    });

    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      showToast(payload.error || "Chat request failed", "error");
      // Remove the placeholder assistant message
      if (activeSession) {
        activeSession.messages = activeSession.messages.filter((m) => m.id !== assistantMessageId);
        renderChat();
      }
      setLoading(false);
      return;
    }

    const reader = response.body?.getReader();
    if (!reader) {
      showToast("Failed to read response stream", "error");
      setLoading(false);
      return;
    }

    const decoder = new TextDecoder();
    let buffer = "";
    let toolMessages: ChatMessage[] = [];

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop() || "";

      let currentEvent: { type: string; data: string } | null = null;

      for (const line of lines) {
        if (line.startsWith("event: ")) {
          currentEvent = { type: line.slice(7), data: "" };
        } else if (line.startsWith("data: ") && currentEvent) {
          currentEvent.data = line.slice(6);
          try {
            const eventData = JSON.parse(currentEvent.data);
            handleStreamEvent(currentEvent.type, eventData, assistantMessageId, toolMessages);
          } catch {
            // Ignore parse errors for malformed events
          }
          currentEvent = null;
        }
      }
    }

    // Final decode
    if (buffer) {
      const lines = buffer.split("\n");
      let currentEvent: { type: string; data: string } | null = null;
      for (const line of lines) {
        if (line.startsWith("event: ")) {
          currentEvent = { type: line.slice(7), data: "" };
        } else if (line.startsWith("data: ") && currentEvent) {
          currentEvent.data = line.slice(6);
          try {
            const eventData = JSON.parse(currentEvent.data);
            handleStreamEvent(currentEvent.type, eventData, assistantMessageId, toolMessages);
          } catch {
            // Ignore
          }
        }
      }
    }

    await loadSessions();
    renderChat();
  } catch (err) {
    showToast("Failed to send message", "error");
    console.error(err);
    // Remove the placeholder assistant message on error
    if (activeSession) {
      activeSession.messages = activeSession.messages.filter((m) => m.id !== assistantMessageId);
      renderChat();
    }
  } finally {
    setLoading(false);
  }
}

function handleStreamEvent(
  type: string,
  data: unknown,
  assistantMessageId: string,
  toolMessages: ChatMessage[],
): void {
  if (!activeSession) return;

  if (type === "tool") {
    const toolData = data as { name: string; args: Record<string, unknown> };
    const toolMsg: ChatMessage = {
      id: crypto.randomUUID(),
      role: "tool",
      content: `Executed ${toolData.name}`,
      timestamp: new Date().toISOString(),
      name: toolData.name,
      args: toolData.args,
    };
    toolMessages.push(toolMsg);
    activeSession.messages.push(toolMsg);
    renderChat();
  } else if (type === "chunk") {
    const chunkData = data as { content: string; full?: string };
    const assistantMsg = activeSession.messages.find((m) => m.id === assistantMessageId);
    if (assistantMsg) {
      assistantMsg.content = chunkData.full || chunkData.content;
      renderChat();
    }
  } else if (type === "final") {
    const finalData = data as { content: string };
    const assistantMsg = activeSession.messages.find((m) => m.id === assistantMessageId);
    if (assistantMsg) {
      assistantMsg.content = finalData.content;
      renderChat();
    }
  } else if (type === "error") {
    const errorData = data as { message: string };
    showToast(errorData.message, "error");
  } else if (type === "done") {
    // Session complete, will reload in finally block
  }
}

async function loadProjects(): Promise<void> {
  try {
    const response = await fetch("/api/projects");
    if (!response.ok) return;
    const payload = (await response.json()) as { projects: { name: string; path: string }[] };
    projects = payload.projects || [];
    renderProjectMenu();
  } catch {
    // Projects endpoint is optional
    projects = [];
  }
}

async function loadWorkspaceTree(workspacePath: string): Promise<void> {
  // Show loading state
  projectTreeEl.innerHTML = `
    <div style="padding: 16px; text-align: center;">
      <div class="spinner" style="margin: 0 auto 8px;"></div>
      <div style="font-size: 12px; color: var(--text-muted);">Loading files...</div>
    </div>
  `;

  try {
    const response = await fetch(`/api/workspace/tree?path=${encodeURIComponent(workspacePath)}`);
    if (!response.ok) {
      projectTreeEl.innerHTML = `
        <div class="empty-state">
          <div class="empty-state-icon">&#128194;</div>
          <div class="empty-state-title">No files found</div>
          <div class="empty-state-desc">Could not load the project files.</div>
        </div>
      `;
      return;
    }
    const payload = (await response.json()) as { tree: TreeNode };
    if (payload.tree) {
      renderProjectTree(payload.tree);
    } else {
      projectTreeEl.innerHTML = `
        <div class="empty-state">
          <div class="empty-state-icon">&#128194;</div>
          <div class="empty-state-title">Empty project</div>
          <div class="empty-state-desc">This workspace has no files yet.</div>
        </div>
      `;
    }
  } catch {
    projectTreeEl.innerHTML = `
      <div class="empty-state">
        <div class="empty-state-icon">&#128683;</div>
        <div class="empty-state-title">Error loading files</div>
        <div class="empty-state-desc">Could not connect to the server.</div>
      </div>
    `;
  }
}

// ===================== Rendering =====================

function renderChatList(): void {
  chatListEl.innerHTML = "";
  if (sessions.length === 0) {
    emptyChatsEl.style.display = "block";
    return;
  }
  emptyChatsEl.style.display = "none";

  sessions.forEach((session) => {
    const li = document.createElement("li");
    const button = document.createElement("button");
    button.type = "button";
    if (session.id === activeSessionId) {
      button.classList.add("active");
    }
    button.innerHTML = `
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"></path></svg>
      <span style="overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">${escapeHtml(session.title)}</span>
    `;
    button.addEventListener("click", () => selectSession(session.id));
    li.appendChild(button);
    chatListEl.appendChild(li);
  });
}

function renderChat(): void {
  if (!activeSession) {
    showWelcomeScreen();
    return;
  }

  chatTitleEl.textContent = activeSession.title;
  chatPathEl.textContent = activeSession.workspacePath;
  chatLogEl.innerHTML = "";

  if (activeSession.messages.length === 0) {
    chatLogEl.innerHTML = `
      <div class="empty-state">
        <div class="empty-state-icon">&#129302;</div>
        <div class="empty-state-title">Start a conversation</div>
        <div class="empty-state-desc">Ask me to write code, debug issues, or explain concepts. I'm here to help you build.</div>
      </div>
    `;
    return;
  }

  activeSession.messages.forEach((message) => {
    const msgEl = createMessageElement(message);
    chatLogEl.appendChild(msgEl);
  });

  // Add typing indicator if loading
  if (isLoading) {
    const typingEl = document.createElement("div");
    typingEl.className = "msg assistant";
    typingEl.innerHTML = `
      <div class="msg-avatar">&#129302;</div>
      <div class="msg-content">
        <div class="msg-header">
          <span class="msg-author">codexlet</span>
        </div>
        <div class="typing-indicator">
          <span></span><span></span><span></span>
        </div>
      </div>
    `;
    chatLogEl.appendChild(typingEl);
  }

  scrollToBottom();
}

function createMessageElement(message: ChatMessage): HTMLElement {
  const div = document.createElement("div");
  div.className = `msg ${message.role}`;

  const avatar = message.role === "user" ? "HX" : "&#129302;";
  const author = message.role === "user" ? "You" : "codexlet";
  const time = new Date(message.timestamp).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

  let content = message.content;

  // Parse markdown for assistant messages
  if (message.role === "assistant" && typeof marked !== "undefined") {
    content = marked.parse(content) as string;
  } else {
    content = escapeHtml(content);
    // Convert URLs to links
    content = content.replace(
      /(https?:\/\/[^\s<]+)/g,
      '<a href="$1" target="_blank" rel="noopener" style="color: var(--accent); text-decoration: none;">$1</a>'
    );
    // Convert newlines to breaks for non-markdown
    content = content.replace(/\n/g, "<br>");
  }

  // Tool messages get special rendering
  if (message.role === "tool") {
    div.innerHTML = `
      <div class="msg-avatar">&#128295;</div>
      <div class="msg-content">
        <div class="msg-header">
          <span class="msg-author">Tool: ${escapeHtml(message.name || "unknown")}</span>
          <span class="msg-time">${time}</span>
        </div>
        <div class="tool-panel">
          <div class="tool-panel-header">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="16 18 22 12 16 6"></polyline><polyline points="8 6 2 12 8 18"></polyline></svg>
            Executed ${escapeHtml(message.name || "tool")}
          </div>
          <div class="tool-panel-content">${escapeHtml(message.content)}</div>
        </div>
      </div>
    `;
    return div;
  }

  div.innerHTML = `
    <div class="msg-avatar">${avatar}</div>
    <div class="msg-content">
      <div class="msg-header">
        <span class="msg-author">${author}</span>
        <span class="msg-time">${time}</span>
      </div>
      <div class="msg-body">${content}</div>
    </div>
  `;

  // Add copy buttons to code blocks
  if (message.role === "assistant") {
    requestAnimationFrame(() => {
      div.querySelectorAll("pre").forEach((pre) => {
        const wrapper = document.createElement("div");
        wrapper.className = "code-block-wrapper";
        pre.parentNode?.insertBefore(wrapper, pre);
        wrapper.appendChild(pre);

        const copyBtn = document.createElement("button");
        copyBtn.className = "copy-code-btn";
        copyBtn.textContent = "Copy";
        copyBtn.addEventListener("click", () => {
          const code = pre.querySelector("code")?.textContent || pre.textContent || "";
          navigator.clipboard.writeText(code).then(() => {
            copyBtn.textContent = "Copied!";
            setTimeout(() => (copyBtn.textContent = "Copy"), 2000);
          });
        });
        wrapper.appendChild(copyBtn);
      });

      // Apply syntax highlighting
      if (typeof hljs !== "undefined") {
        div.querySelectorAll("pre code").forEach((block) => {
          hljs.highlightElement(block as HTMLElement);
        });
      }
    });
  }

  return div;
}

function renderProjectTree(node: TreeNode, depth = 0, parentExpanded = true): void {
  if (depth === 0) {
    projectTreeEl.innerHTML = "";
  }

  if (!parentExpanded && depth > 0) return;

  const div = document.createElement("div");
  div.className = `tree-item ${node.type}`;
  div.style.paddingLeft = `${8 + depth * 16}px`;

  // Get file extension for icon styling
  if (node.type === "file") {
    const ext = node.name.split(".").pop()?.toLowerCase() || "";
    div.dataset.ext = ext;
  }

  const icon = node.type === "directory" 
    ? (node.children && node.children.length > 0 ? "&#128193;" : "&#128194;") 
    : getFileIcon(node.name);
  div.innerHTML = `<span class="tree-icon">${icon}</span> ${escapeHtml(node.name)}`;

  if (node.type === "directory") {
    div.addEventListener("click", (e) => {
      e.stopPropagation();
      const isExpanded = div.classList.toggle("expanded");
      // Re-render the entire tree to show/hide children
      if (activeSession) {
        loadWorkspaceTree(activeSession.workspacePath);
      }
    });
  }

  projectTreeEl.appendChild(div);

  if (node.children) {
    const isExpanded = div.classList.contains("expanded") || depth === 0;
    node.children.forEach((child) => renderProjectTree(child, depth + 1, isExpanded));
  }
}

function getFileIcon(filename: string): string {
  const ext = filename.split(".").pop()?.toLowerCase() || "";
  const iconMap: Record<string, string> = {
    ts: "&#128187;", tsx: "&#128187;",
    js: "&#128187;", jsx: "&#128187;",
    py: "&#128013;",
    html: "&#127760;",
    css: "&#127912;",
    json: "&#128203;",
    md: "&#128221;",
    yml: "&#9881;", yaml: "&#9881;",
    sh: "&#128425;", bash: "&#128425;",
    dockerfile: "&#128674;",
    gitignore: "&#128065;",
    env: "&#128272;",
    sql: "&#128451;",
    csv: "&#128209;",
    xml: "&#128220;",
    svg: "&#127912;",
    png: "&#127912;", jpg: "&#127912;", jpeg: "&#127912;", gif: "&#127912;",
    pdf: "&#128196;",
    zip: "&#128230;", tar: "&#128230;", gz: "&#128230;",
  };
  return iconMap[ext] || "&#128196;";
}

function renderProjectMenu(): void {
  projectMenu.innerHTML = "";
  if (projects.length === 0) {
    const item = document.createElement("div");
    item.className = "project-menu-item";
    item.textContent = "No projects found";
    projectMenu.appendChild(item);
    return;
  }

  projects.forEach((project) => {
    const item = document.createElement("div");
    item.className = "project-menu-item";
    if (currentProjectEl.textContent === project.name) {
      item.classList.add("active");
    }
    item.innerHTML = `
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"></path></svg>
      ${escapeHtml(project.name)}
    `;
    item.addEventListener("click", () => {
      currentProjectEl.textContent = project.name;
      projectMenu.classList.remove("open");
      loadWorkspaceTree(project.path);
    });
    projectMenu.appendChild(item);
  });
}

// ===================== UI State =====================

function showWelcomeScreen(): void {
  welcomeScreen.style.display = "flex";
  chatView.style.display = "none";
  chatTitleEl.textContent = "Select or create a session";
  chatPathEl.textContent = "";
}

function showChatView(): void {
  welcomeScreen.style.display = "none";
  chatView.style.display = "flex";
}

function setLoading(loading: boolean): void {
  isLoading = loading;
  sendBtn.disabled = loading;
  chatInputEl.disabled = loading;
  if (activeSession) {
    renderChat();
  }
}

function updateProjectSelector(workspacePath: string): void {
  const name = workspacePath.split("/").pop() || workspacePath;
  currentProjectEl.textContent = name;
  loadWorkspaceTree(workspacePath);
}

function scrollToBottom(): void {
  chatLogEl.scrollTop = chatLogEl.scrollHeight;
}

function openModal(): void {
  newTaskModal.style.display = "flex";
  // Auto-detect current directory if available
  if (activeSession) {
    workspacePathEl.value = activeSession.workspacePath;
    sessionTitleEl.focus();
  } else {
    workspacePathEl.value = "";
    workspacePathEl.focus();
  }
}

function closeModal(): void {
  newTaskModal.style.display = "none";
  workspacePathEl.value = "";
  sessionTitleEl.value = "";
}

function showToast(message: string, type: "success" | "error" | "info" = "info"): void {
  const toast = document.createElement("div");
  toast.className = `toast ${type}`;
  toast.textContent = message;
  toastContainer.appendChild(toast);

  setTimeout(() => {
    toast.style.opacity = "0";
    toast.style.transform = "translateX(20px)";
    toast.style.transition = "all 0.3s ease";
    setTimeout(() => toast.remove(), 300);
  }, 3000);
}

// ===================== Event Listeners =====================

function setupEventListeners(): void {
  // Send button
  sendBtn.addEventListener("click", () => {
    const message = chatInputEl.value.trim();
    if (message) {
      chatInputEl.value = "";
      autoResizeTextarea();
      sendMessage(message);
    }
  });

  // Chat input
  chatInputEl.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      const message = chatInputEl.value.trim();
      if (message && !isLoading) {
        chatInputEl.value = "";
        autoResizeTextarea();
        sendMessage(message);
      }
    }
  });

  chatInputEl.addEventListener("input", autoResizeTextarea);

  // New task
  newTaskBtn.addEventListener("click", openModal);
  modalClose.addEventListener("click", closeModal);
  modalCancel.addEventListener("click", closeModal);
  newTaskModal.addEventListener("click", (e) => {
    if (e.target === newTaskModal) closeModal();
  });

  // New session form
  newSessionForm.addEventListener("submit", (e) => {
    e.preventDefault();
    const title = sessionTitleEl.value;
    const path = workspacePathEl.value;
    if (path.trim()) {
      createSession(title, path);
    }
  });

  // Access dropdown
  accessToggle.addEventListener("click", (e) => {
    e.stopPropagation();
    accessDropdown.classList.toggle("open");
  });

  accessMenu.querySelectorAll(".dropdown-item").forEach((item) => {
    item.addEventListener("click", () => {
      const value = (item as HTMLElement).dataset.value || "full";
      currentAccess = value;
      accessMenu.querySelectorAll(".dropdown-item").forEach((i) => i.classList.remove("active"));
      item.classList.add("active");
      const title = item.querySelector(".item-title")?.textContent || "Full access";
      accessToggle.innerHTML = `
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"></rect><path d="M7 11V7a5 5 0 0 1 10 0v4"></path></svg>
        ${title}
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="6 9 12 15 18 9"></polyline></svg>
      `;
      accessDropdown.classList.remove("open");
    });
  });

  // Mode toggle
  modeToggle.querySelectorAll(".mode-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const mode = (btn as HTMLElement).dataset.mode || "agent";
      currentMode = mode;
      modeToggle.querySelectorAll(".mode-btn").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
    });
  });

  // Project selector
  projectToggle.addEventListener("click", (e) => {
    e.stopPropagation();
    projectMenu.classList.toggle("open");
  });

  // Attach button
  attachBtn.addEventListener("click", () => {
    showToast("File attachments coming soon", "info");
  });

  // Sidebar toggle (collapse/expand)
  const sidebarToggle = document.querySelector(".sidebar-toggle") as HTMLButtonElement;
  const sidebar = document.querySelector(".sidebar") as HTMLElement;
  if (sidebarToggle && sidebar) {
    sidebarToggle.addEventListener("click", () => {
      sidebar.classList.toggle("collapsed");
      const isCollapsed = sidebar.classList.contains("collapsed");
      sidebarToggle.innerHTML = isCollapsed ? "&#9776;" : "&#9776;";
      showToast(isCollapsed ? "Sidebar collapsed" : "Sidebar expanded", "info");
    });
  }

  // Settings button
  const settingsBtn = document.getElementById("settings-btn") as HTMLButtonElement;
  if (settingsBtn) {
    settingsBtn.addEventListener("click", () => {
      showToast("Settings panel coming soon", "info");
    });
  }

  // Close dropdowns on outside click
  document.addEventListener("click", () => {
    accessDropdown.classList.remove("open");
    projectMenu.classList.remove("open");
  });

  // Sidebar tabs
  document.querySelectorAll(".sidebar-tabs .tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".sidebar-tabs .tab-btn").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      const tab = (btn as HTMLElement).dataset.tab;
      showToast(`Switched to ${tab} tab`, "info");
    });
  });

  // Nav items
  document.querySelectorAll(".nav-item").forEach((item) => {
    item.addEventListener("click", () => {
      document.querySelectorAll(".nav-item").forEach((i) => i.classList.remove("active"));
      item.classList.add("active");
      const view = (item as HTMLElement).dataset.view;
      showToast(`${view} view coming soon`, "info");
    });
  });
}

function setupKeyboardShortcuts(): void {
  document.addEventListener("keydown", (e) => {
    // Cmd/Ctrl + K for new task
    if ((e.metaKey || e.ctrlKey) && e.key === "k") {
      e.preventDefault();
      openModal();
    }

    // Cmd/Ctrl + Enter to send
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
      e.preventDefault();
      if (!isLoading && chatInputEl.value.trim()) {
        const message = chatInputEl.value.trim();
        chatInputEl.value = "";
        autoResizeTextarea();
        sendMessage(message);
      }
    }

    // Cmd/Ctrl + B to toggle sidebar
    if ((e.metaKey || e.ctrlKey) && e.key === "b") {
      e.preventDefault();
      const sidebar = document.querySelector(".sidebar") as HTMLElement;
      if (sidebar) {
        sidebar.classList.toggle("collapsed");
      }
    }

    // Cmd/Ctrl + Shift + N for new task (alternative)
    if ((e.metaKey || e.ctrlKey) && e.shiftKey && e.key === "N") {
      e.preventDefault();
      openModal();
    }

    // Escape to close modal or cancel
    if (e.key === "Escape") {
      if (newTaskModal.style.display === "flex") {
        closeModal();
      }
    }

    // Cmd/Ctrl + / to focus input
    if ((e.metaKey || e.ctrlKey) && e.key === "/") {
      e.preventDefault();
      chatInputEl.focus();
    }
  });
}

function autoResizeTextarea(): void {
  chatInputEl.style.height = "auto";
  chatInputEl.style.height = Math.min(chatInputEl.scrollHeight, 200) + "px";
}

// ===================== Utilities =====================

function escapeHtml(text: string): string {
  return text
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#x27;");
}

// Configure marked
if (typeof marked !== "undefined") {
  marked.setOptions({
    breaks: true,
    gfm: true,
    headerIds: false,
    mangle: false,
  });
}
