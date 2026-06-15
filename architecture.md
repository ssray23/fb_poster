# Facebook Group Poster - Technical Architecture

This document describes the architectural layout, components, data flows, and technical details of the Facebook Group Poster solution.

---

## Architecture Overview

The application is structured as a modular Python utility that bridges headless browser automation, file-based configuration, and a lightweight local control web server.

```text
                    +-----------------------+
                    |   User (CLI / Cron)   |
                    +-----------+-----------+
                                |
                   [fb_poster.lock] (Process Lock)
                                |
                                v
                    +-----------+-----------+
                    |    CLI Entry Point    |<==================+
                    +-----+-----------+-----+                   |
                          |           |                         |
             +------------+           +-------------+           |
             | (--setup / --fetch-    | (--manage)  |           |
             |  groups / --post /     |             |           |
             |  --test)               |             |           |
             v                        v             |           |
     +-------+-------+        +-------+-------+     |           |
     |  Playwright   |        |  Local HTTP   |     |           |
     |  Automation   |        |  Web Server   |     |           |
     |    Engine     |        |  (Port 8080)  |     |           |
     +-------+-------+        +-------+-------+     |           |
             |                        |             |           |
             | (Loads/Saves           | (Serves     |           |
             |  Cookies &             |  Dashboard) |           |
             |  State)                v             |           |
     +-------+-------+        +-------+-------+     |           |
     |  fb_session/  |        |  Browser UI   |     |           |
     | (Chrome Profile)       |  (Manage Page)|     |           |
     +---------------+        +-------+-------+     |           |
                                      |             |           |
                                      | (POST       |           |
                                      |  Updates)   |           |
                                      v             |           |
                              +-------+-------+     |           |
                              |  config.json  |-----+ (Updates) |
                              +---------------+                 |
                                                                |
                              +---------------------------------+
```

---

## Core Components

### 1. Process Concurrency Lock (`fb_poster.lock`)
* **Purpose:** Prevents race conditions and write conflicts caused by running multiple browser actions in parallel (e.g. running `--manage` while `--post` is running).
* **Mechanism:**
  - On launch, the script checks for `fb_poster.lock`.
  - If present, it reads the PID and checks if a process with that PID is active (using Unix `os.kill(pid, 0)`). If active, it gracefully blocks execution with an error.
  - If no active process is found (stale lock), it deletes the lock file, writes its own PID, and continues.
  - Releases and deletes the lock file via a `finally` cleanup block when exiting.

### 2. Playwright Automation Engine
* **Purpose:** Handles manual login session caching, programmatically fetches member groups, and executes automated posts.
* **Session Persistence:** Configured as a persistent context (`p.chromium.launch_persistent_context`), saving cookies, LocalStorage, and browser parameters in the `./fb_session/` directory.

### 3. State & Configuration Layer (`config.json`)
* **Purpose:** Acts as a centralized data repository.
* **Parameters:** Includes message body, group definitions (name, URL, enabled status), rate limiting delay ranges, profile metadata, and photo upload variables.

### 4. Interactive Group Manager Dashboard
* **Purpose:** Local HTTP control panel served from the CLI.
* **Implementation:** Starts a Python native server (`http.server.HTTPServer`) binding to `localhost:8080`.
* **Heartbeat Ping:** Uses a regular heartbeat mechanism (`/ping` request every second) from the browser page. If the user closes the browser tab, the pings stop, and the server automatically shuts down and releases the process lock.

---

## How Headless Mode Works

Headless browser execution is the default mode for scanning groups (`--fetch-groups`) and posting drafts/messages (`--post`, `--test`).

### 1. Concept
In headless mode, Playwright launches a Chromium browser engine without a graphical user interface (GUI). 
- It does **not** draw windows, borders, or viewports on the desktop screen.
- Instead, the browser engine allocates a virtual, headless rendering viewport in memory.

### 2. Underlying Mechanism
- **DOM & Javascript Engine:** The browser still parses HTML, downloads CSS, constructs the DOM tree, executes JavaScript, and resolves cookies and storage identical to a standard visual browser.
- **Virtual Viewport:** Playwright assigns a virtual resolution/viewport (e.g., 1280x800) in headless memory. Layout calculations, CSS animations, and DOM rendering occur exactly as if a screen existed.
- **Interactions:** Keyboard inputs (`page.keyboard.type()`) and mouse clicks (`page.locator().click()`) are simulated programmatically by dispatching high-level input events directly into Chromium’s rendering pipeline.
- **Screenshots:** Since the viewport is rendered in memory, you can capture full visual representations of pages using `page.screenshot()`.

### 3. Why Headless Mode is Used Here
- **Resources:** Consumes significantly less CPU and RAM since it bypasses window manager composition and GPU screen drawing.
- **Background Execution:** Allows you to continue using your computer without browser windows popping up and stealing cursor focus while the script types.
- **Server Readiness:** Allows the script to run seamlessly on headless environments like remote Linux servers, Docker containers, or scheduled backgrounds (macOS Cron).
