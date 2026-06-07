import express from "express";
import rateLimit from "express-rate-limit";
import path from "node:path";
import fs from "node:fs";
import net from "node:net";
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

// On Windows, "python3" may not exist; fall back to "python".
// Override with CODEXLET_PYTHON env var.
const PYTHON_CMD = process.env.CODEXLET_PYTHON ?? (process.platform === "win32" ? "python" : "python3");

const PUBLIC_DIR = path.join(__dirname, "public");
const CODELET_TIMEOUT_MS = Number(process.env.CODEXLET_TIMEOUT_MS ?? 180_000);
const API_RATE_LIMIT_WINDOW_MS = Number(process.env.CODEXLET_RATE_WINDOW_MS ?? 60_000);
const API_RATE_LIMIT_MAX = Number(process.env.CODEXLET_RATE_MAX ?? 60);

// When running as a packaged Electron app, CODEXLET_DATA_DIR is set by the
// main process to app.getPath("userData"). In dev mode it falls back to
// a .codexlet folder next to the project root.
function resolveDataDir(): string {
  if (process.env.CODEXLET_DATA_DIR) {
    return process.env.CODEXLET_DATA_DIR;
  }
  const ROOT_DIR = path.resolve(__dirname, "..");
  return path.join(ROOT_DIR, ".codexlet");
}

/** Find a free TCP port. */
function getFreePort(preferred: number): Promise<number> {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.listen(preferred, "127.0.0.1", () => {
      const addr = server.address();
      const port = typeof addr === "object" && addr ? addr.port : preferred;
      server.close(() => resolve(port));
    });
    server.on("error", () => {
      // Preferred port busy – let OS pick one.
      const fallback = net.createServer();
      fallback.listen(0, "127.0.0.1", () => {
        const addr2 = fallback.address();
        const port2 = typeof addr2 === "object" && addr2 ? addr2.port : 0;
        fallback.close(() => {
          if (port2 === 0) {
            reject(new Error("Could not find a free port"));
          } else {
            resolve(port2);
          }
        });
      });
    });
  });
}

function ensureStore(dataDir: string): SessionStoreShape {
  const dataFile = path.join(dataDir, "sessions.json");
  if (!fs.existsSync(dataDir)) {
    fs.mkdirSync(dataDir, { recursive: true });
  }
  if (!fs.existsSync(dataFile)) {
    const initial: SessionStoreShape = { sessions: [] };
    fs.writeFileSync(dataFile, JSON.stringify(initial, null, 2), "utf-8");
    return initial;
  }
  try {
    const parsed = JSON.parse(fs.readFileSync(dataFile, "utf-8")) as SessionStoreShape;
    if (!Array.isArray(parsed.sessions)) {
      throw new Error("invalid store shape");
    }
    return parsed;
  } catch {
    const fallback: SessionStoreShape = { sessions: [] };
    fs.writeFileSync(dataFile, JSON.stringify(fallback, null, 2), "utf-8");
    return fallback;
  }
}

function saveStore(dataDir: string, store: SessionStoreShape): void {
  const dataFile = path.join(dataDir, "sessions.json");
  fs.writeFileSync(dataFile, JSON.stringify(store, null, 2), "utf-8");
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

    const child = spawn(PYTHON_CMD, args, {
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

function findSession(sessions: CodexletSession[], sessionId: string): CodexletSession | undefined {
  return sessions.find((session) => session.id === sessionId);
}

/**
 * Build and start the Express server.
 *
 * @param preferredPort  Port to try first (defaults to PORT env or 8787).
 *                       If the port is busy, a random free port is used.
 * @returns              The port the server is actually listening on.
 */
export async function startServer(preferredPort?: number): Promise<number> {
  const dataDir = resolveDataDir();
  let store = ensureStore(dataDir);

  const app = express();
  app.use(express.json({ limit: "1mb" }));

  const apiRateLimiter = createRateLimiter(API_RATE_LIMIT_MAX, API_RATE_LIMIT_WINDOW_MS);
  app.use("/api", apiRateLimiter);

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
    saveStore(dataDir, store);
    res.status(201).json({ session });
  });

  app.get("/api/sessions/:sessionId", (req, res) => {
    const session = findSession(store.sessions, req.params.sessionId ?? "");
    if (!session) {
      res.status(404).json({ error: "Session not found." });
      return;
    }
    res.json({ session });
  });

  app.post("/api/sessions/:sessionId/chat", async (req, res) => {
    const session = findSession(store.sessions, req.params.sessionId ?? "");
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

    saveStore(dataDir, store);

    res.json({
      session,
      message: assistantMessage,
    });
  });

  app.use(express.static(PUBLIC_DIR));
  app.get(/.*/, (_req, res) => {
    res.sendFile(path.join(PUBLIC_DIR, "index.html"));
  });

  const defaultPort = Number(process.env.PORT ?? preferredPort ?? 8787);
  const port = await getFreePort(defaultPort);

  return new Promise((resolve, reject) => {
    const httpServer = app.listen(port, "127.0.0.1", () => {
      // eslint-disable-next-line no-console
      console.log(`codexlet listening on http://127.0.0.1:${port}`);
      resolve(port);
    });
    httpServer.on("error", reject);
  });
}

// Allow running as a standalone Node.js script (npm start / npm run dev).
// When imported by the Electron main process this block is skipped.
if (require.main === module) {
  startServer().catch((err: unknown) => {
    console.error("Failed to start server:", err);
    process.exit(1);
  });
}
