import express from "express";
import rateLimit from "express-rate-limit";
import path from "node:path";
import fs from "node:fs";
import { spawn } from "node:child_process";
import { randomUUID } from "node:crypto";

interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  timestamp: string;
}

interface CodexletSession {
  id: string;
  title: string;
  workspacePath: string;
  createdAt: string;
  updatedAt: string;
  codeletSessionId?: string;
  messages: ChatMessage[];
}

interface SessionStoreShape {
  sessions: CodexletSession[];
}

const app = express();
app.use(express.json({ limit: "1mb" }));

const ROOT_DIR = path.resolve(__dirname, "..");
const DATA_DIR = path.join(ROOT_DIR, ".codexlet");
const DATA_FILE = path.join(DATA_DIR, "sessions.json");
const PUBLIC_DIR = path.join(__dirname, "public");
const PORT = Number(process.env.PORT ?? 8787);
const CODELET_TIMEOUT_MS = Number(process.env.CODEXLET_TIMEOUT_MS ?? 180_000);
const API_RATE_LIMIT_WINDOW_MS = Number(process.env.CODEXLET_RATE_WINDOW_MS ?? 60_000);
const API_RATE_LIMIT_MAX = Number(process.env.CODEXLET_RATE_MAX ?? 60);

function ensureStore(): SessionStoreShape {
  if (!fs.existsSync(DATA_DIR)) {
    fs.mkdirSync(DATA_DIR, { recursive: true });
  }
  if (!fs.existsSync(DATA_FILE)) {
    const initial: SessionStoreShape = { sessions: [] };
    fs.writeFileSync(DATA_FILE, JSON.stringify(initial, null, 2), "utf-8");
    return initial;
  }
  try {
    const parsed = JSON.parse(fs.readFileSync(DATA_FILE, "utf-8")) as SessionStoreShape;
    if (!Array.isArray(parsed.sessions)) {
      throw new Error("invalid store shape");
    }
    return parsed;
  } catch {
    const fallback: SessionStoreShape = { sessions: [] };
    fs.writeFileSync(DATA_FILE, JSON.stringify(fallback, null, 2), "utf-8");
    return fallback;
  }
}

let store = ensureStore();
const apiRateLimiter = createRateLimiter(API_RATE_LIMIT_MAX, API_RATE_LIMIT_WINDOW_MS);
app.use("/api", apiRateLimiter);

function saveStore(): void {
  fs.writeFileSync(DATA_FILE, JSON.stringify(store, null, 2), "utf-8");
}

function listCodeletSessionFiles(workspacePath: string): string[] {
  const sessionDir = path.join(workspacePath, ".codelet", "sessions");
  if (!fs.existsSync(sessionDir)) {
    return [];
  }
  const files = fs.readdirSync(sessionDir)
    .filter((file) => file.endsWith(".json"))
    .map((file) => file.replace(/\.json$/, ""));
  files.sort();
  return files;
}

function decodeEntities(value: string): string {
  return value.replace(/&(lt|gt|amp|quot|#x27);/g, (entity) => {
    switch (entity) {
      case "&lt;":
        return "<";
      case "&gt;":
        return ">";
      case "&amp;":
        return "&";
      case "&quot;":
        return "\"";
      case "&#x27;":
        return "'";
      default:
        return entity;
    }
  });
}

function createRateLimiter(maxRequests: number, windowMs: number) {
  return rateLimit({
    windowMs,
    limit: maxRequests,
    standardHeaders: true,
    legacyHeaders: false,
    message: { error: "Too many requests. Please retry shortly." },
  });
}

function parseFinalFromMachineOutput(stdout: string): string {
  const matches = [...stdout.matchAll(/<final>([\s\S]*?)<\/final>/g)];
  const latest = matches.at(-1);
  if (!latest) {
    return "No <final> response was returned by codelet.";
  }
  const body = latest[1] ?? "";
  return decodeEntities(body.trim());
}

function runCodeletPrompt(session: CodexletSession, prompt: string): Promise<{ final: string; codeletSessionId?: string }> {
  return new Promise((resolve) => {
    const before = new Set(listCodeletSessionFiles(session.workspacePath));
    const args = ["-m", "codelet", "--machine", "--cwd", session.workspacePath];
    if (session.codeletSessionId) {
      args.push("--resume", session.codeletSessionId);
    }
    args.push(prompt);

    const child = spawn("python3", args, {
      cwd: session.workspacePath,
      env: process.env,
    });

    let stdout = "";
    let stderr = "";
    let timedOut = false;

    const timer = setTimeout(() => {
      timedOut = true;
      child.kill("SIGTERM");
    }, CODELET_TIMEOUT_MS);

    child.stdout.on("data", (chunk) => {
      stdout += chunk.toString();
    });
    child.stderr.on("data", (chunk) => {
      stderr += chunk.toString();
    });

    child.on("close", (code) => {
      clearTimeout(timer);
      if (timedOut) {
        resolve({ final: `Request timed out after ${CODELET_TIMEOUT_MS} ms.` });
        return;
      }
      if (code !== 0 && !stdout.includes("<final>")) {
        resolve({ final: stderr.trim() || `codelet exited with status ${code ?? -1}.` });
        return;
      }
      const final = parseFinalFromMachineOutput(stdout);
      const after = listCodeletSessionFiles(session.workspacePath);
      let codeletSessionId = session.codeletSessionId;
      if (!codeletSessionId) {
        const created = after.find((id) => !before.has(id));
        codeletSessionId = created ?? after[after.length - 1];
      }
      if (codeletSessionId) {
        resolve({ final, codeletSessionId });
      } else {
        resolve({ final });
      }
    });

    child.on("error", (error) => {
      clearTimeout(timer);
      resolve({ final: `Failed to start codelet: ${String(error.message)}` });
    });
  });
}

function findSession(sessionId: string): CodexletSession | undefined {
  return store.sessions.find((session) => session.id === sessionId);
}

app.get("/api/sessions", (_req, res) => {
  const sessions = store.sessions
    .slice()
    .sort((a, b) => b.updatedAt.localeCompare(a.updatedAt))
    .map((session) => ({
      id: session.id,
      title: session.title,
      workspacePath: session.workspacePath,
      createdAt: session.createdAt,
      updatedAt: session.updatedAt,
      messageCount: session.messages.length,
    }));
  res.json({ sessions });
});

app.post("/api/sessions", (req, res) => {
  const title = String(req.body?.title ?? "").trim();
  const workspacePathInput = String(req.body?.workspacePath ?? "").trim();
  const workspacePath = path.resolve(workspacePathInput);

  if (!workspacePathInput) {
    res.status(400).json({ error: "workspacePath is required." });
    return;
  }
  if (!fs.existsSync(workspacePath) || !fs.statSync(workspacePath).isDirectory()) {
    res.status(400).json({ error: "workspacePath must be an existing directory." });
    return;
  }

  const now = new Date().toISOString();
  const session: CodexletSession = {
    id: randomUUID(),
    title: title || path.basename(workspacePath),
    workspacePath,
    createdAt: now,
    updatedAt: now,
    messages: [],
  };
  store.sessions.push(session);
  saveStore();
  res.status(201).json({ session });
});

app.get("/api/sessions/:sessionId", (req, res) => {
  const session = findSession(req.params.sessionId);
  if (!session) {
    res.status(404).json({ error: "Session not found." });
    return;
  }
  res.json({ session });
});

app.post("/api/sessions/:sessionId/chat", async (req, res) => {
  const session = findSession(req.params.sessionId);
  if (!session) {
    res.status(404).json({ error: "Session not found." });
    return;
  }

  const content = String(req.body?.message ?? "").trim();
  if (!content) {
    res.status(400).json({ error: "message is required." });
    return;
  }

  const now = new Date().toISOString();
  const userMessage: ChatMessage = {
    id: randomUUID(),
    role: "user",
    content,
    timestamp: now,
  };
  session.messages.push(userMessage);

  const result = await runCodeletPrompt(session, content);
  const assistantMessage: ChatMessage = {
    id: randomUUID(),
    role: "assistant",
    content: result.final,
    timestamp: new Date().toISOString(),
  };
  session.messages.push(assistantMessage);
  session.updatedAt = assistantMessage.timestamp;
  if (result.codeletSessionId) {
    session.codeletSessionId = result.codeletSessionId;
  }

  saveStore();

  res.json({
    session,
    message: assistantMessage,
  });
});

app.use(express.static(PUBLIC_DIR));
app.get(/.*/, (_req, res) => {
  res.sendFile(path.join(PUBLIC_DIR, "index.html"));
});

app.listen(PORT, () => {
  // eslint-disable-next-line no-console
  console.log(`codexlet listening on http://127.0.0.1:${PORT}`);
});
