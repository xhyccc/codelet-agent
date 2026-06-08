# Codexlet GUI Redesign Plan

## Goal
Transform the current basic dark-themed codexlet GUI into a runnable, elegant, powerful GUI for agentic AI that matches the target UI design (light theme, sidebar navigation, welcome screen, rich input controls).

## Target UI Elements (from image)
- **Light theme** with clean whites and subtle grays
- **Left sidebar**:
  - Top tabs: Work / Chat
  - "New Task" button with keyboard shortcut (⌘K)
  - Navigation: Skills, Scheduled Tasks, WebBridge
  - Project section with folder tree
  - Chats section
  - User profile at bottom
- **Main area**:
  - Welcome screen: "Let's take something off your plate" with mascot icon
  - "Beta Preview" badge
  - Large text input area
  - Bottom toolbar: + attachments, "Full access" dropdown, "Agent" / "Agent Swarm" toggle, send button
  - Project selector dropdown

## Architecture
- Electron main process (`main.ts`) - minimal changes needed
- Express server (`server.ts`) - add APIs for file tree, skills, projects
- Frontend (`index.html`, `styles.css`, `app.ts`) - complete rebuild

## Iterations

### Iteration 1: HTML Structure
Rebuild `index.html` with the target UI layout:
- Sidebar with all sections
- Main area with welcome screen and chat view
- Input area with all controls

### Iteration 2: CSS Styling
Complete light-theme CSS overhaul:
- Clean color palette (whites, grays, subtle blues)
- Proper spacing and typography
- Smooth transitions and hover states

### Iteration 3: Frontend Logic (app.ts)
- Sidebar navigation state
- Work/Chat tab switching
- Project tree rendering
- Session management improvements
- Input controls (Agent/Swarm toggle, Full access dropdown)

### Iteration 4: Server APIs (server.ts)
- `GET /api/workspace/tree` - file tree for project section
- `GET /api/skills` - list available skills
- `GET /api/projects` - list recent projects
- Enhance session APIs

### Iteration 5: Welcome Screen & Input Controls
- Mascot/icon for welcome screen
- Beta Preview badge
- Full access permission dropdown
- Agent/Agent Swarm toggle
- Project selector
- Attachment button

### Iteration 6: Markdown & Code Rendering
- Integrate marked.js for markdown
- Code syntax highlighting with highlight.js
- Copy code buttons
- Proper message formatting

### Iteration 7: Streaming Support
- Server-Sent Events for streaming codelet output
- Real-time tool execution display
- Typing indicators

### Iteration 8: Keyboard Shortcuts & Animations
- ⌘K for New Task
- ⌘Enter to send
- Escape to cancel
- Smooth page transitions
- Loading animations

### Iteration 9: Polish
- Empty states for all sections
- Error handling with toast notifications
- Loading skeletons
- Responsive design improvements

### Iteration 10: Testing & Verification
- Build verification
- Runtime testing
- Bug fixes
- Final polish

## Key Design Decisions
- Keep using vanilla TS (no framework) for simplicity and fast iteration
- Light theme to match target UI
- Leverage codelet CLI `--machine` mode for all agent operations
- Session persistence via existing JSON store
