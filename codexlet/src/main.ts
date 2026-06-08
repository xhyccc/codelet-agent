/**
 * Electron main process for codexlet desktop app.
 *
 * Flow:
 *  1. Set CODEXLET_DATA_DIR to the OS user-data directory (so sessions are
 *     stored outside the app bundle on all platforms).
 *  2. Start the embedded Express server and obtain its port.
 *  3. Create a BrowserWindow that loads the local server URL.
 */

import { app, BrowserWindow, dialog, session, shell } from "electron";
import { startServer } from "./server";

// Prevent multiple instances of the app from running simultaneously.
const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
}

// Point the server at the OS-appropriate user-data directory before
// startServer() is called (it reads the env var lazily at call time).
process.env.CODEXLET_DATA_DIR = app.getPath("userData");

let mainWindow: BrowserWindow | null = null;
let serverPort: number | null = null;

function createWindow(port: number): void {
  mainWindow = new BrowserWindow({
    width: 1280,
    height: 840,
    minWidth: 800,
    minHeight: 600,
    title: "codexlet",
    // Use platform-native traffic-light buttons on macOS.
    titleBarStyle: process.platform === "darwin" ? "hiddenInset" : "default",
    webPreferences: {
      // The renderer is a regular web page served by localhost – no Node
      // integration or preload script needed.
      nodeIntegration: false,
      contextIsolation: true,
    },
  });

  // Open external links in the system browser, not in the app window.
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (!url.startsWith(`http://127.0.0.1:${port}`)) {
      void shell.openExternal(url);
      return { action: "deny" };
    }
    return { action: "allow" };
  });

  void mainWindow.loadURL(`http://127.0.0.1:${port}`);

  mainWindow.on("closed", () => {
    mainWindow = null;
  });
}

app.on("second-instance", () => {
  // Someone tried to run a second instance – focus our window instead.
  if (mainWindow) {
    if (mainWindow.isMinimized()) mainWindow.restore();
    mainWindow.focus();
  }
});

app.whenReady().then(async () => {
  try {
    serverPort = await startServer();

    // Expose the app version to the renderer via a request header.
    const version = app.getVersion();
    session.defaultSession.webRequest.onBeforeSendHeaders(
      { urls: [`http://127.0.0.1:${serverPort}/*`] },
      (
        details: Electron.OnBeforeSendHeadersListenerDetails,
        callback: (headersDetails: Electron.BeforeSendResponse) => void,
      ) => {
        callback({
          requestHeaders: {
            ...details.requestHeaders,
            "X-Codexlet-Version": version,
          },
        });
      },
    );

    createWindow(serverPort);
  } catch (err: unknown) {
    const message = err instanceof Error ? err.message : String(err);
    void dialog.showErrorBox(
      "codexlet – startup error",
      `Failed to start the embedded server:\n\n${message}`,
    );
    app.quit();
  }
});

// Re-open the window when the dock icon is clicked (macOS).
app.on("activate", () => {
  if (BrowserWindow.getAllWindows().length === 0 && serverPort !== null) {
    createWindow(serverPort);
  }
});

// Quit when all windows are closed (except on macOS, where apps stay in the
// dock until the user explicitly quits with Cmd-Q).
app.on("window-all-closed", () => {
  if (process.platform !== "darwin") {
    app.quit();
  }
});

// Restrict navigation: only allow the embedded server URL.
app.on("web-contents-created", (_event, contents) => {
  contents.on("will-navigate", (event, navigationUrl) => {
    const parsed = new URL(navigationUrl);
    if (parsed.hostname !== "127.0.0.1") {
      event.preventDefault();
    }
  });
});
