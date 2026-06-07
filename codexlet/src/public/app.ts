interface SessionSummary {
  id: string;
  title: string;
  workspacePath: string;
  createdAt: string;
  updatedAt: string;
  messageCount: number;
}

interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  timestamp: string;
}

interface SessionDetails {
  id: string;
  title: string;
  workspacePath: string;
  createdAt: string;
  updatedAt: string;
  messages: ChatMessage[];
}

const sessionListEl = document.getElementById("session-list") as HTMLUListElement;
const newSessionForm = document.getElementById("new-session-form") as HTMLFormElement;
const sessionHeaderEl = document.getElementById("session-header") as HTMLDivElement;
const chatLogEl = document.getElementById("chat-log") as HTMLDivElement;
const chatFormEl = document.getElementById("chat-form") as HTMLFormElement;
const chatInputEl = document.getElementById("chat-input") as HTMLTextAreaElement;
const workspacePathEl = document.getElementById("workspacePath") as HTMLInputElement;
const sessionTitleEl = document.getElementById("sessionTitle") as HTMLInputElement;

let sessions: SessionSummary[] = [];
let activeSessionId: string | null = null;
let activeSession: SessionDetails | null = null;

function escapeHtml(text: string): string {
  return text
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#x27;");
}

function renderSessions(): void {
  sessionListEl.innerHTML = "";
  sessions.forEach((session) => {
    const li = document.createElement("li");
    const button = document.createElement("button");
    button.type = "button";
    if (session.id === activeSessionId) {
      button.classList.add("active");
    }
    button.innerHTML = `${escapeHtml(session.title)}<br><small>${escapeHtml(session.workspacePath)}</small>`;
    button.addEventListener("click", () => selectSession(session.id));
    li.appendChild(button);
    sessionListEl.appendChild(li);
  });
}

function renderChat(): void {
  if (!activeSession) {
    sessionHeaderEl.textContent = "Select or create a session.";
    chatLogEl.innerHTML = "";
    chatFormEl.style.display = "none";
    return;
  }

  sessionHeaderEl.textContent = `${activeSession.title}  ·  ${activeSession.workspacePath}`;
  chatFormEl.style.display = "grid";
  chatLogEl.innerHTML = activeSession.messages
    .map(
      (message) =>
        `<div class="msg ${message.role}"><div class="meta">${message.role} · ${new Date(message.timestamp).toLocaleString()}</div>${escapeHtml(message.content)}</div>`,
    )
    .join("");
  chatLogEl.scrollTop = chatLogEl.scrollHeight;
}

async function loadSessions(): Promise<void> {
  const response = await fetch("/api/sessions");
  const payload = (await response.json()) as { sessions: SessionSummary[] };
  sessions = payload.sessions;
  renderSessions();
}

async function selectSession(sessionId: string): Promise<void> {
  activeSessionId = sessionId;
  renderSessions();
  const response = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}`);
  if (!response.ok) {
    alert("Failed to load session.");
    return;
  }
  const payload = (await response.json()) as { session: SessionDetails };
  activeSession = payload.session;
  renderChat();
}

newSessionForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const response = await fetch("/api/sessions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      title: sessionTitleEl.value.trim(),
      workspacePath: workspacePathEl.value.trim(),
    }),
  });
  const payload = await response.json();
  if (!response.ok) {
    alert(payload.error ?? "Failed to create session.");
    return;
  }
  workspacePathEl.value = "";
  sessionTitleEl.value = "";
  await loadSessions();
  await selectSession(payload.session.id);
});

chatFormEl.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!activeSessionId) {
    return;
  }

  const message = chatInputEl.value.trim();
  if (!message) {
    return;
  }

  chatInputEl.value = "";
  chatInputEl.disabled = true;

  try {
    const response = await fetch(`/api/sessions/${encodeURIComponent(activeSessionId)}/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message }),
    });
    const payload = await response.json();
    if (!response.ok) {
      alert(payload.error ?? "Chat request failed.");
      return;
    }
    activeSession = payload.session;
    await loadSessions();
    renderChat();
  } finally {
    chatInputEl.disabled = false;
    chatInputEl.focus();
  }
});

(async () => {
  await loadSessions();
  renderChat();
})();
