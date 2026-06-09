# Desktop GUI Plan for Codelet

## Goal
Build a real desktop GUI for codelet that aligns with OpenAI Codex's desktop app UX — native window, system tray, global shortcuts, real-time streaming, tool cards, session management.

## Architecture Decision: Electron + React

**Why Electron + React (not web UI, not TUI):**
- OpenAI Codex uses this exact stack — aligns inch-to-inch
- Real native window with traffic lights, menu bar, system tray
- Global keyboard shortcuts (⌘K, ⌘Enter, ⌘Shift+N)
- Native file dialogs for workspace selection
- Auto-updater, code signing, proper app bundle
- Previous web UI failed because it was "just a web page" — Electron makes it a real app

**Why not Tauri:** Tauri is lighter but the React/Node ecosystem for terminal-like UIs is weaker than Ink, and we already proved Ink doesn't work well for this use case.

**Why not native Python GUI (tkinter/PyQt):** Would require rewriting all UI logic in Python, losing the React component model that makes complex UIs manageable.

---

## Phase 1: Foundation (Week 1)

### 1.1 Electron Shell
- [ ] Set up Electron main process (`src/main/main.ts`)
  - Window creation with proper macOS titleBarStyle (`hiddenInset`)
  - System tray icon + context menu
  - Global shortcut registration (⌘K, ⌘Shift+N, ⌘Enter)
  - Menu bar (File, Edit, View, Window, Help)
  - Deep link handling (`codelet://` protocol)
- [ ] IPC bridge (`src/main/ipc.ts`)
  - `ipcMain.handle('codelet:run', ...)` — spawn Python process
  - `ipcMain.handle('codelet:stream', ...)` — SSE-like streaming via IPC
  - `ipcMain.handle('session:list', ...)` — read `.codexlet/sessions.json`
  - `ipcMain.handle('session:create', ...)` — write session store
  - `ipcMain.handle('workspace:select', ...)` — native file dialog
  - `ipcMain.handle('git:status', ...)` — exec git commands
- [ ] Preload script (`src/main/preload.ts`)
  - Expose `window.codeletAPI` to renderer (contextIsolation: true)
  - Type-safe IPC channel definitions

### 1.2 React Renderer Foundation
- [ ] Set up Vite + React + TypeScript build (`src/renderer/`)
- [ ] Theme system (light/dark, matches Codex's color palette)
  - CSS variables for all colors
  - System theme detection + manual toggle
- [ ] Layout shell
  - Sidebar (collapsible, 260px default)
  - Main chat area
  - Input bar (bottom)
  - Status bar (bottom)
  - Plan panel (collapsible, top)

### 1.3 State Management
- [ ] Zustand store for global state
  - `sessions: Session[]`
  - `activeSessionId: string | null`
  - `currentMode: 'plan' | 'build'`
  - `currentAccess: 'full' | 'read' | 'ask'`
  - `isStreaming: boolean`
  - `streamingContent: string`
  - `toolCalls: ToolCall[]`
  - `pendingApproval: ApprovalRequest | null`

---

## Phase 2: Core Chat Experience (Week 2)

### 2.1 Message Rendering
- [ ] User message bubbles (right-aligned, gradient avatar)
- [ ] Assistant message bubbles (left-aligned, 🤖 avatar)
- [ ] Markdown rendering (marked.js) with:
  - Syntax highlighting (highlight.js)
  - Code block copy buttons
  - Tables, lists, blockquotes
- [ ] Streaming text animation
  - Token-by-token appearance (not full re-render)
  - Blinking cursor while streaming
  - Smooth scroll to bottom

### 2.2 Tool Call Cards
- [ ] `write_file` card — file icon, path, code preview (first 10 lines), expand button
- [ ] `patch_file` card — diff viewer with line numbers, green/red highlighting
- [ ] `bash` card — terminal icon, command, spinner while running, output preview
- [ ] `read_file` card — file icon, path, content preview
- [ ] Generic tool card — collapsible JSON args viewer
- [ ] Status badges: "Running" (spinner) → "Done" (✓) → "Error" (✗)

### 2.3 Input Area
- [ ] Auto-resizing textarea (1-5 rows)
- [ ] Mode toggle (Plan / Build) — visual pill switch
- [ ] Access dropdown (Full / Read Only / Ask Before)
- [ ] Attach button (file picker)
- [ ] Send button (paper plane icon)
- [ ] Keyboard: Enter to send, Shift+Enter for newline

### 2.4 Approval Modal
- [ ] Overlay with backdrop blur
- [ ] Tool name + args display
- [ ] Diff preview for write_file / patch_file
- [ ] Buttons: Deny, Edit, Accept, Accept for Session
- [ ] Keyboard shortcuts: Y (accept), N (deny), A (accept session), Esc (cancel)
- [ ] Risk badge (⚠️ Risky Command) for destructive tools

---

## Phase 3: Session & Project Management (Week 3)

### 3.1 Sidebar
- [ ] Session list with:
  - Title, timestamp, message count
  - Active indicator (highlighted row)
  - Context menu: Rename, Fork, Archive, Delete
  - Drag-to-reorder
- [ ] Project tree (file explorer)
  - Collapsible directories
  - File icons by extension
  - Click to open file (send read_file tool)
- [ ] Navigation tabs: Work / Chat
- [ ] New Task button (⌘K shortcut)

### 3.2 Session CRUD
- [ ] Create session — native folder picker + title input
- [ ] Rename — inline edit or modal
- [ ] Fork — duplicate session with shared codelet session ID
- [ ] Archive — prefix title with [Archived]
- [ ] Delete — confirmation dialog
- [ ] Resume — load `.codexlet/sessions/*.json` into UI state

### 3.3 Welcome Screen
- [ ] Mascot icon (🤖) with animation
- [ ] "Let's take something off your plate" tagline
- [ ] Beta badge
- [ ] Auto-hide when session selected

---

## Phase 4: Advanced Features (Week 4)

### 4.1 Plan / Build Mode
- [ ] Plan mode: agent outputs `<plan>` tags, UI extracts and shows plan panel
- [ ] Plan panel: markdown rendering, step checklist
- [ ] Approve / Reject buttons on plan panel
- [ ] Build mode: direct execution, no plan panel
- [ ] Mode persistence in localStorage

### 4.2 Cost & Usage Tracking
- [ ] Status bar shows: cost ($0.0042), duration (12s), tokens (1,234)
- [ ] Cost breakdown per model call
- [ ] Budget warning when approaching limit
- [ ] Session-level cost aggregation

### 4.3 Narrative View
- [ ] Toggle between Chat (bubbles) and Narrative (continuous scroll)
- [ ] Narrative view: semantic grouping of user intent + agent actions
- [ ] View preference in localStorage

### 4.4 Background Tasks
- [ ] Tasks panel (sidebar toggle)
- [ ] Task list: ID, status (running/done/failed), description
- [ ] Auto-refresh every 5 seconds
- [ ] Task output preview

### 4.5 Skills Browser
- [ ] Skills list with search/filter
- [ ] Skill detail panel: description, effort, whenToUse, body
- [ ] Auto-discover from `.codelet/skills/`

---

## Phase 5: Polish & Integration (Week 5)

### 5.1 System Integration
- [ ] macOS: Dock icon bounce on notification, Touch Bar support
- [ ] Windows: Taskbar progress indicator, jump list
- [ ] Linux: AppIndicator, desktop file
- [ ] Auto-updater (electron-updater)
- [ ] Code signing (macOS notarization, Windows cert)

### 5.2 Keyboard Shortcuts
| Shortcut | Action |
|---|---|
| ⌘K | New Task |
| ⌘Enter | Send message |
| ⌘Shift+N | New Task (alternative) |
| ⌘B | Toggle sidebar |
| ⌘/ | Focus input |
| ⌘1-9 | Switch session |
| Esc | Close modal / cancel |
| Y | Approve (in modal) |
| N | Deny (in modal) |
| A | Accept for session (in modal) |

### 5.3 Settings Panel
- [ ] Model selector dropdown (GPT-4o, o3, GPT-4o Mini, ...)
- [ ] Provider selector (OpenAI, Kimi, GLM, ...)
- [ ] API key input (masked)
- [ ] Theme selector (Light / Dark / System)
- [ ] Approval policy selector
- [ ] Sandbox policy selector
- [ ] Max steps / max tokens sliders

### 5.4 Error Handling
- [ ] Toast notifications (success / error / info)
- [ ] Connection error retry
- [ ] Session corruption recovery
- [ ] Graceful degradation when Python not found

---

## Phase 6: Testing & Release (Week 6)

### 6.1 Testing
- [ ] E2E tests with Playwright (Electron support)
- [ ] Unit tests for IPC handlers
- [ ] Unit tests for React components (Vitest + React Testing Library)
- [ ] Manual test matrix: macOS / Windows / Linux

### 6.2 Packaging
- [ ] macOS: DMG + ZIP (x64 + arm64 universal)
- [ ] Windows: NSIS installer + portable EXE
- [ ] Linux: AppImage + DEB
- [ ] Homebrew formula
- [ ] Chocolatey package

### 6.3 Documentation
- [ ] User guide (screenshots, GIFs)
- [ ] Keyboard shortcut reference
- [ ] Troubleshooting FAQ
- [ ] Contributing guide

---

## Tech Stack

| Layer | Technology |
|---|---|
| Shell | Electron 33+ |
| Renderer | React 18 + Vite |
| State | Zustand |
| Styling | CSS Modules + CSS Variables |
| Markdown | marked.js + highlight.js |
| Icons | Lucide React |
| Testing | Vitest + React Testing Library + Playwright |
| Packaging | electron-builder |
| TypeScript | Strict mode |

---

## File Structure

```
codelet-gui/
├── src/
│   ├── main/                    # Electron main process
│   │   ├── main.ts              # Entry point
│   │   ├── ipc.ts               # IPC handlers
│   │   ├── preload.ts           # Preload script
│   │   ├── menu.ts              # Menu bar
│   │   ├── tray.ts              # System tray
│   │   └── window.ts            # Window management
│   ├── renderer/                # React app
│   │   ├── main.tsx             # React entry
│   │   ├── App.tsx              # Root component
│   │   ├── store/               # Zustand stores
│   │   ├── components/          # React components
│   │   │   ├── Sidebar.tsx
│   │   │   ├── Chat.tsx
│   │   │   ├── MessageBubble.tsx
│   │   │   ├── ToolCard.tsx
│   │   │   ├── InputBar.tsx
│   │   │   ├── StatusBar.tsx
│   │   │   ├── PlanPanel.tsx
│   │   │   ├── ApprovalModal.tsx
│   │   │   ├── NewTaskModal.tsx
│   │   │   ├── SettingsPanel.tsx
│   │   │   └── WelcomeScreen.tsx
│   │   ├── hooks/               # Custom hooks
│   │   ├── lib/                 # Utilities
│   │   └── styles/              # Global CSS + themes
│   └── shared/                  # Shared types
│       └── types.ts
├── assets/                      # Icons, images
├── build/                       # Build scripts
├── electron.vite.config.ts      # Vite config for Electron
├── package.json
├── tsconfig.json
└── README.md
```

---

## Milestones

| Week | Deliverable |
|---|---|
| Week 1 | Electron shell + IPC bridge + React foundation |
| Week 2 | Chat UI with streaming + tool cards + approval modal |
| Week 3 | Sidebar + session management + project tree |
| Week 4 | Plan/Build mode + cost tracking + narrative view + tasks |
| Week 5 | System integration + keyboard shortcuts + settings |
| Week 6 | Testing + packaging + documentation + release |

---

## Risks & Mitigations

| Risk | Mitigation |
|---|---|
| Electron bundle size (150MB+) | Use Vite tree-shaking, lazy load components, consider Tauri for v2 |
| Python process spawning latency | Pre-warm Python process, show loading indicator |
| Cross-platform UI inconsistencies | Test on all platforms weekly, use platform-specific CSS |
| Security (arbitrary code execution) | Keep contextIsolation, validate all IPC inputs, use preload script |
| Performance with large sessions | Virtualize message list, paginate history |

---

## Success Criteria

1. **Functional parity with Codex CLI**: All codelet features accessible via GUI
2. **Real-time streaming**: Token-by-token output without full re-renders
3. **Tool cards**: Visual, expandable, with diff viewer
4. **Session management**: CRUD + resume + fork
5. **Native feel**: Traffic lights, menu bar, system tray, global shortcuts
6. **Cross-platform**: macOS, Windows, Linux packages
7. **Auto-updater**: Users get updates without manual download
