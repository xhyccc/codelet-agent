# codexlet

TypeScript GUI for `codelet` using CLI machine mode.

## Features

- Create multiple sessions.
- Select a workspace folder for each session.
- Chat per session while preserving the underlying `codelet` session (`--resume`).
- Session metadata and chat history are stored in `codexlet/.codexlet/sessions.json`.

## Run

```bash
cd codexlet
npm install
npm run build
npm start
```

Open http://127.0.0.1:8787.
