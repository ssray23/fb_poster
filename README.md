# Facebook Group Poster

A robust and refined tool to automate posting text messages (supporting gradient backgrounds) to a selected list of Facebook Groups you are a member of. Since Meta deprecated the official Groups API, this script uses **Playwright** browser automation. It maintains a persistent browser session so you only have to log in once, and simulates human typing and interaction behavior.

## Key Features

- **Default Headless Execution**: The browser runs invisibly in the background automatically—no special flags needed.
- **Photo Upload Integration**: Automatically scans a configured directory (defaults to `./pics/`) for images (JPG, PNG, HEIC, WEBP) and uploads them to the composer. Attaching photos automatically bypasses and overrides gradient backgrounds (as Facebook does not support both).
- **Unified Manage Flow**: Running `python fb_poster.py --manage` automatically runs the scraper (`--fetch-groups`) first to sync any newly joined groups from Facebook before opening the interactive manager.
- **Web-Based Groups Manager Dashboard (`--manage`)**: A modern, interactive web interface served locally (Helvetica styling, light theme) that allows you to:
  - Edit your post content directly in a top textarea.
  - Enable, disable, or toggle groups using interactive checkboxes.
  - View a profile badge indicating the logged-in Facebook member (e.g., `Shashank Roy`).
  - View a status badge indicating if photo upload is active and how many photos are found.
  - Search groups in real-time by name or URL, with a live "Showing X / Y" search counter.
  - View live select counters showing how many groups are currently selected.
  - Dynamic save validation (the "Save Changes" button enables only when unsaved changes exist).
- **Dynamic Logging Interface**: Real-time console logs featuring status indicators:
  - Blinking Cyan Dot (`●`) for the group currently in progress.
  - Solid Green Dot (`●`) and dimmed lines for successfully completed groups.
  - Red Cross (`❌`) for errors.
  - Custom dimmed sub-steps and sleep countdowns.
- **Robust Safeguards**:
  - **1-Hour Duplicate Prevention**: Skips any group posted to within the last hour to prevent spamming.
  - **Smart Buy & Sell Group Detection**: Detects and skips groups requiring multi-step listing forms (checks tab layouts and page indicators) and logs them cleanly.
  - **Session Expiration Guard**: Automatically halts if authentication expires, preventing repeated failures.
  - **Concurrency Process Lock**: Prevents conflicting concurrent runs of the script (such as editing groups via `--manage` while `--post` is active) using a PID file lock. Automatically recovers stale locks if a process dies.
  - **Clean Cancellations**: Intercepts `Ctrl+C` globally to exit gracefully without tracebacks.

---

## Installation

This project is set up with a Python virtual environment to manage dependencies locally.

1. Ensure Python 3 is installed.
2. Activate the virtual environment:
   ```bash
   source venv/bin/activate
   ```
3. (If needed on a new machine) Install dependencies and browser binaries:
   ```bash
   pip install -r requirements.txt
   playwright install chromium
   ```

---

## Usage Instructions

### Step 1: Initial Login Setup
Launch the browser in headed mode to log in to Facebook, save your session, and link your profile:
```bash
./venv/bin/python fb_poster.py --setup
```
- A browser window will open. Log in manually.
- Complete any Multi-Factor Authentication (2FA) or captcha security checks.
- Once you see your Facebook Feed, return to your terminal and press **ENTER**.
- This creates a persistent session inside `./fb_session/` and links your account name (saved inside `config.json` as `"facebook_account"`).

### Step 2: Configure Photo Posting (Optional)
To attach photos to your posts automatically:
1. Place your listing image files (e.g. `.jpg`, `.jpeg`, `.png`, `.heic`, `.webp`) inside the `./pics/` directory.
2. In `config.json`, verify that `"post_photos"` is set to `true` (it is enabled by default) and `"photos_directory"` is set to `"pics"`.
*Note: Because Facebook does not support both, attaching photos automatically overrides and bypasses gradient backgrounds.*

### Step 3: Sync & Manage Groups
Sync your Facebook groups, configure active groups, and edit post text using the local web dashboard:
```bash
./venv/bin/python fb_poster.py --manage
```
- **Automatic Syncing**: This command will headlessly navigate to Facebook first, scan your joined groups list, add any new groups to `config.json` (leaving them disabled by default), start the local web server, and open the dashboard in your default browser (`http://localhost:8080/`).
- Toggle individual groups or use the **Select All** / **Select None** buttons.
- Filter groups in real-time using the search box.
- Write or edit your message in the text area at the top.
- Click **Save Changes** to write changes directly to `config.json`.
- Close the browser tab and press **Ctrl+C** in your terminal to exit.

### Step 4: Dry-Run / Test Your Posting (Recommended)
Before posting live, run the script in test mode:
```bash
./venv/bin/python fb_poster.py --test
```
- This will run the posting sequence headlessly. It types your message in the composer and applies any gradient styling, then discards the draft without publishing so you can verify the script runs flawlessly.
- Buy & Sell groups are automatically detected and skipped with descriptive log entries.

### Step 5: Post Live
Run the script to post for real:
```bash
./venv/bin/python fb_poster.py --post
```
- This runs headlessly and posts to all enabled groups, applying randomized delays between groups to mimic human behavior.

---

## Scheduling on macOS

You can automate this script to run periodically using standard macOS **Cron**.

1. Open your user crontab editor:
   ```bash
   crontab -e
   ```
2. Add a line to schedule the poster. For example, to run the script **every 3 days at 9:00 AM**:
   ```cron
   0 9 */3 * * "/Users/suddharay/Library/Mobile Documents/com~apple~CloudDocs/My Projects/FB Poster/venv/bin/python" "/Users/suddharay/Library/Mobile Documents/com~apple~CloudDocs/My Projects/FB Poster/fb_poster.py" --post >> "/Users/suddharay/Library/Mobile Documents/com~apple~CloudDocs/My Projects/FB Poster/fb_poster.log" 2>&1
   ```
   *Note: Because iCloud folders on macOS contain spaces, the paths in crontab must be quoted.*

3. Save and close the editor. Your script will now run automatically in the background on the schedule.
