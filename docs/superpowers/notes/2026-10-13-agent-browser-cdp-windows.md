# agent-browser CDP 实战记录（Windows）

> 源出 AGENTS.md 同名节，2026-11-20 原文迁出；AGENTS 只留红线指针。2026-10-13 以 agent-browser 0.39.0 于 Windows 验证。

Verified 2026-10-13 with agent-browser 0.39.0 on Windows:

- `agent-browser --cdp <port>` works fine when a Chrome is already listening on that port (`open`, `snapshot`, `click "@eN"`, `read`, `tab list` all OK).
- **Never run `agent-browser --cdp <port> close`** on an externally launched Chrome — it kills the external Chrome process tree. To stop, kill Chrome directly (`Stop-Process`).
- If no browser is listening on the CDP port, commands fail with a misleading `Operation timed out. The page may still be loading...` and can leave a hung `agent-browser-win32-x64.exe` process. Before retrying: check the port with `netstat -ano | findstr :<port>` and clean up leftover `agent-browser*` processes.
- To launch a CDP Chrome manually: `& "<chrome.exe>" --remote-debugging-port=9222 --user-data-dir="<dir>" about:blank` (run in background).
