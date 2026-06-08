import express from "express";
import rateLimit from "express-rate-limit";
import path from "node:path";
import fs from "node:fs";
import net from "node:net";
import { spawn } from "node:child_process";
import { randomUUID } from "node:crypto";

interface ChatMessage {
  id: string;
  role: "user" | "assistant" | "tool";
  content: string;
  timestamp: string;
  name?: string;
  args?: Record<string, unknown>;
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

function runCodeletPromptStream(
  session: CodexletSession,
  prompt: string,
  onEvent: (event: { type: string; data: unknown }) => void,
): Promise<{ final: string; codeletSessionId?: string }> {
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
    let finalBuffer = "";
    let inFinal = false;

    const timer = setTimeout(() => {
      timedOut = true;
      child.kill("SIGTERM");
    }, CODELET_TIMEOUT_MS);

    child.stdout.on("data", (chunk: Buffer) => {
      const text = chunk.toString();
      stdout += text;

      // Stream tool calls in real-time
      const toolRegex = /<tool\s+name="([^"]+)">(.*?)<\/tool>/gs;
      let match;
      while ((match = toolRegex.exec(stdout)) !== null) {
        const name = decodeEntities(match[1] || "");
        const body = decodeEntities(match[2] || "");
        let args: Record<string, unknown> = {};
        try {
          args = JSON.parse(body);
        } catch {
          args = { raw: body };
        }
        onEvent({ type: "tool", data: { name, args } });
        // Remove the matched tool from stdout to avoid re-matching
        stdout = stdout.slice(0, match.index) + stdout.slice(match.index + match[0].length);
      }

      // Stream final answer chunks
      const finalOpenIdx = stdout.indexOf("<final>");
      if (finalOpenIdx !== -1) {
        const afterOpen = stdout.slice(finalOpenIdx + 7);
        const finalCloseIdx = afterOpen.indexOf("</final>");
        if (finalCloseIdx !== -1) {
          // Complete final tag found
          const content = afterOpen.slice(0, finalCloseIdx);
          onEvent({ type: "chunk", data: { content: decodeEntities(content), done: false } });
        } else {
          // Streaming partial final content
          onEvent({ type: "chunk", data: { content: decodeEntities(afterOpen), done: false } });
        }
      }
    });

    child.stderr.on("data", (chunk: Buffer) => {
      stderr += chunk.toString();
      onEvent({ type: "stderr", data: { text: chunk.toString() } });
    });

    child.on("close", (code) => {
      clearTimeout(timer);
      if (timedOut) {
        onEvent({ type: "error", data: { message: `Request timed out after ${CODELET_TIMEOUT_MS} ms.` } });
        resolve({ final: `Request timed out after ${CODELET_TIMEOUT_MS} ms.` });
        return;
      }
      if (code !== 0 && !stdout.includes("<final>")) {
        const errorMsg = stderr.trim() || `codelet exited with status ${code ?? -1}.`;
        onEvent({ type: "error", data: { message: errorMsg } });
        resolve({ final: errorMsg });
        return;
      }
      const final = parseFinalFromMachineOutput(stdout);
      onEvent({ type: "chunk", data: { content: "", done: true } });
      onEvent({ type: "final", data: { content: final } });
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
      const msg = `Failed to start codelet: ${String(error.message)}`;
      onEvent({ type: "error", data: { message: msg } });
      resolve({ final: msg });
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

  // Streaming chat endpoint using Server-Sent Events
  app.post("/api/sessions/:sessionId/chat/stream", async (req, res) => {
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

    // Set up SSE headers
    res.setHeader("Content-Type", "text/event-stream");
    res.setHeader("Cache-Control", "no-cache");
    res.setHeader("Connection", "keep-alive");
    res.setHeader("X-Accel-Buffering", "no");

    const now = new Date().toISOString();
    const userMessage: ChatMessage = {
      id: randomUUID(),
      role: "user",
      content,
      timestamp: now,
    };
    session.messages.push(userMessage);

    const assistantId = randomUUID();
    let assistantContent = "";
    const toolEvents: { name: string; args: Record<string, unknown> }[] = [];

    const sendEvent = (event: { type: string; data: unknown }) => {
      res.write(`event: ${event.type}\n`);
      res.write(`data: ${JSON.stringify(event.data)}\n\n`);
    };

    try {
      const result = await runCodeletPromptStream(session, content, (event) => {
        if (event.type === "tool") {
          const toolData = event.data as { name: string; args: Record<string, unknown> };
          toolEvents.push(toolData);
          sendEvent({ type: "tool", data: toolData });
        } else if (event.type === "chunk") {
          const chunkData = event.data as { content: string; done: boolean };
          if (chunkData.content) {
            assistantContent += chunkData.content;
            sendEvent({ type: "chunk", data: { content: chunkData.content, full: assistantContent } });
          }
        } else if (event.type === "final") {
          const finalData = event.data as { content: string };
          assistantContent = finalData.content;
          sendEvent({ type: "final", data: { content: finalData.content } });
        } else if (event.type === "error") {
          const errorData = event.data as { message: string };
          sendEvent({ type: "error", data: errorData });
        } else if (event.type === "stderr") {
          sendEvent(event);
        }
      });

      // Save the assistant message
      const assistantMessage: ChatMessage = {
        id: assistantId,
        role: "assistant",
        content: assistantContent || result.final,
        timestamp: new Date().toISOString(),
      };
      session.messages.push(assistantMessage);
      session.updatedAt = assistantMessage.timestamp;
      if (result.codeletSessionId) {
        session.codeletSessionId = result.codeletSessionId;
      }

      // Also save any tool messages
      for (const tool of toolEvents) {
        const toolMessage: ChatMessage = {
          id: randomUUID(),
          role: "tool" as const,
          content: `Executed ${tool.name}`,
          timestamp: new Date().toISOString(),
          name: tool.name,
          args: tool.args,
        };
        session.messages.push(toolMessage);
      }

      saveStore(dataDir, store);

      sendEvent({ type: "done", data: { sessionId: session.id, messageId: assistantId } });
    } catch (err) {
      sendEvent({ type: "error", data: { message: String(err) } });
    } finally {
      res.end();
    }
  });

  // List unique projects from sessions
  app.get("/api/projects", (_req, res) => {
    const seen = new Set<string>();
    const projects: { name: string; path: string }[] = [];
    for (const session of store.sessions) {
      if (!seen.has(session.workspacePath)) {
        seen.add(session.workspacePath);
        projects.push({
          name: path.basename(session.workspacePath),
          path: session.workspacePath,
        });
      }
    }
    res.json({ projects });
  });

  // Get workspace file tree
  app.get("/api/workspace/tree", (req, res) => {
    const workspacePath = String(req.query.path ?? "").trim();
    if (!workspacePath) {
      res.status(400).json({ error: "path query parameter is required." });
      return;
    }
    const resolved = path.resolve(workspacePath);
    if (!fs.existsSync(resolved) || !fs.statSync(resolved).isDirectory()) {
      res.status(400).json({ error: "path must be an existing directory." });
      return;
    }

    interface TreeNode {
      name: string;
      path: string;
      type: "file" | "directory";
      children?: TreeNode[];
    }

    function buildTree(dirPath: string): TreeNode {
      const name = path.basename(dirPath);
      const node: TreeNode = {
        name,
        path: dirPath,
        type: "directory",
        children: [],
      };

      try {
        const entries = fs.readdirSync(dirPath, { withFileTypes: true });
        // Skip hidden dirs and common non-project dirs
        const skipDirs = new Set([
          "node_modules", ".git", "__pycache__", ".pytest_cache",
          ".venv", "venv", "dist", "build", ".next", ".turbo",
          "coverage", ".coverage", ".tox", ".eggs",
        ]);
        for (const entry of entries) {
          if (entry.name.startsWith(".") && skipDirs.has(entry.name)) continue;
          if (entry.name.startsWith(".") && entry.isDirectory()) continue;
          const childPath = path.join(dirPath, entry.name);
          if (entry.isDirectory()) {
            if (!skipDirs.has(entry.name)) {
              node.children!.push(buildTree(childPath));
            }
          } else {
            node.children!.push({
              name: entry.name,
              path: childPath,
              type: "file",
            });
          }
        }
        // Sort: directories first, then files, alphabetically
        node.children!.sort((a, b) => {
          if (a.type === b.type) return a.name.localeCompare(b.name);
          return a.type === "directory" ? -1 : 1;
        });
      } catch {
        // Permission denied or other error - return empty children
      }

      return node;
    }

    try {
      const tree = buildTree(resolved);
      res.json({ tree });
    } catch (err) {
      res.status(500).json({ error: `Failed to build tree: ${String(err)}` });
    }
  });

  // List skills from workspace .codelet/skills directory
  app.get("/api/skills", (req, res) => {
    const workspacePath = String(req.query.path ?? "").trim();
    if (!workspacePath) {
      res.status(400).json({ error: "path query parameter is required." });
      return;
    }
    const skillsDir = path.join(path.resolve(workspacePath), ".codelet", "skills");
    const skills: { name: string; description: string }[] = [];
    if (fs.existsSync(skillsDir) && fs.statSync(skillsDir).isDirectory()) {
      try {
        const entries = fs.readdirSync(skillsDir, { withFileTypes: true });
        for (const entry of entries) {
          if (entry.isDirectory()) {
            const skillFile = path.join(skillsDir, entry.name, "SKILL.md");
            let description = "";
            if (fs.existsSync(skillFile)) {
              try {
                const content = fs.readFileSync(skillFile, "utf-8");
                // Extract first line as description
                const firstLine = content.split("\n")[0]?.replace(/^#\s*/, "").trim();
                description = firstLine || entry.name;
              } catch {
                description = entry.name;
              }
            }
            skills.push({ name: entry.name, description });
          }
        }
      } catch {
        // Ignore errors
      }
    }
    res.json({ skills });
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
