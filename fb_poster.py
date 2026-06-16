import os
import sys
import re
import json
import time
import random
import argparse
import http.server
import webbrowser
from playwright.sync_api import sync_playwright
from html.parser import HTMLParser

class FacebookFormatParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.chunks = []
        self.current_styles = set()
        
    def handle_starttag(self, tag, attrs):
        if tag in ('b', 'strong'): self.current_styles.add('b')
        elif tag in ('i', 'em'): self.current_styles.add('i')
        elif tag in ('u',): self.current_styles.add('u')
        elif tag == 'br': self.chunks.append(('\n', self.current_styles.copy()))
        elif tag in ('p', 'div'):
            if self.chunks:
                self.chunks.append(('\n', self.current_styles.copy()))
            
    def handle_endtag(self, tag):
        if tag in ('b', 'strong') and 'b' in self.current_styles: self.current_styles.remove('b')
        elif tag in ('i', 'em') and 'i' in self.current_styles: self.current_styles.remove('i')
        elif tag in ('u',) and 'u' in self.current_styles: self.current_styles.remove('u')
            
    def handle_data(self, data):
        if data:
            self.chunks.append((data, self.current_styles.copy()))

# ANSI escape codes for terminal formatting
DOT_GREEN = "\033[32m●\033[0m"
DOT_BLINK_CYAN = "\033[5;36m●\033[0m"
STYLE_DIM = "\033[2m"
STYLE_RESET = "\033[0m"
CROSS_RED = "\033[31m❌\033[0m"


def acquire_lock(lock_path):
    """
    Check if another instance of fb_poster is running by checking the lock file.
    If it is, print a warning and exit. Otherwise, write the current PID.
    """
    if os.path.exists(lock_path):
        try:
            with open(lock_path, 'r') as f:
                pid = int(f.read().strip())
            
            # Check if process with this PID is still running
            try:
                os.kill(pid, 0)
                # If no exception, process is running
                print(f"\n\033[31m❌ Error: Another instance of fb_poster (PID: {pid}) is currently running.\033[0m")
                print("Please wait for it to finish or stop it before running another command.")
                sys.exit(1)
            except OSError:
                # Process is dead, stale lock file. Delete it.
                try:
                    os.remove(lock_path)
                except Exception:
                    pass
        except Exception:
            # Corrupted lock file or reading error. Safe to delete.
            try:
                os.remove(lock_path)
            except Exception:
                pass

    # Write current PID to lock file
    try:
        with open(lock_path, 'w') as f:
            f.write(str(os.getpid()))
    except Exception as e:
        print(f"Warning: Could not create lock file: {e}")


def release_lock(lock_path):
    """
    Release the concurrency lock by deleting the lock file.
    """
    try:
        if os.path.exists(lock_path):
            with open(lock_path, 'r') as f:
                pid = int(f.read().strip())
            # Only delete the lock file if it was created by this process
            if pid == os.getpid():
                os.remove(lock_path)
    except Exception:
        pass


def clean_group_name(name, fallback_slug):
    # Remove trailing time indicators like .17h, .12m, .2d
    name = re.sub(r'\.[0-9]+[hmd]\s*$', '', name).strip()
    
    # If it is the generic "View group" placeholder, treat it as garbage and fall back to slug
    if name.lower() == "view group":
        return fallback_slug.replace('-', ' ').replace('_', ' ').title()
        
    # If it's a notification text, try to extract the group name
    patterns = [
        r'(?:approved your post in|approved your request to join|approved request to join)\s+(.+)',
        r'commented on your post in\s+(.+)',
        r'posted in\s+(.+)',
        r'shared a post to\s+(.+)',
        r'invited you to join\s+(.+)',
        r'added a post in\s+(.+)',
        r'crossposted to\s+(.+)'
    ]
    for pattern in patterns:
        match = re.search(pattern, name, re.IGNORECASE)
        if match:
            cleaned = match.group(1).strip()
            # Remove leading "Unread" prefix if present
            if cleaned.lower().startswith("unread"):
                cleaned = cleaned[6:].strip()
            return cleaned
            
    # If the name starts with "Unread" (from notification bell), strip it
    if name.lower().startswith("unread") and len(name) > 6:
        stripped = name[6:].strip()
        # If it looks like a notification, recurse
        if any(x in stripped.lower() for x in ["approved", "commented", "posted", "added", "shared", "invited", "post", "group", "crossposted"]):
            return clean_group_name(stripped, fallback_slug)
            
    # If the name still contains obvious notification text we couldn't parse, fallback to slug
    if any(x in name.lower() for x in ["approved your post", "commented on your", "posted in", "shared a post", "crossposted to"]):
        return fallback_slug.replace('-', ' ').replace('_', ' ').title()
        
    return name

def is_better_name(new_name, old_name, slug):
    if old_name == slug and new_name != slug:
        return True
    if new_name == slug and old_name != slug:
        return False
        
    new_is_garbage = any(x in new_name.lower() for x in ["approved", "commented", "posted", "added", "shared", "invited", "crossposted", "unread", "view group"])
    old_is_garbage = any(x in old_name.lower() for x in ["approved", "commented", "posted", "added", "shared", "invited", "crossposted", "unread", "view group"])
    
    if old_is_garbage and not new_is_garbage:
        return True
    if new_is_garbage and not old_is_garbage:
        return False
        
    return len(new_name) < len(old_name)

def to_sans_serif_bold(s):
    result = []
    for c in s:
        o = ord(c)
        if 65 <= o <= 90:  # A-Z
            result.append(chr(o + 120211))
        elif 97 <= o <= 122:  # a-z
            result.append(chr(o + 120205))
        elif 48 <= o <= 57:  # 0-9
            result.append(chr(o + 120764))
        else:
            result.append(c)
    return "".join(result)

def convert_markdown_bold(text):
    if not text:
        return text
    def replace(match):
        inner_text = match.group(1)
        return to_sans_serif_bold(inner_text)
    
    return re.sub(r'\*\*(.*?)\*\*', replace, text)

def load_config(config_path):
    if not os.path.exists(config_path):
        print(f"Error: Config file not found at {config_path}")
        print("Please run the script to initialize it, or create a default config.json.")
        sys.exit(1)
    try:
        with open(config_path, 'r') as f:
            config = json.load(f)
        
        # Clean out any invalid or non-group URLs from the loaded config
        config_changed = False
        if "groups" in config:
            cleaned = []
            for g in config["groups"]:
                url = g.get("url", "")
                parts = url.split('/groups/')
                if len(parts) >= 2:
                    group_slug = parts[1].strip('/').split('?')[0].split('/')[0]
                    if group_slug and re.match(r'^[a-zA-Z0-9\._\-]+$', group_slug):
                        if group_slug not in ['feed', 'joins', 'discover', 'create', 'categories', 'manage', 'search', 'explore', 'chats', 'jobs', 'requests', 'profile']:
                            name = g.get("name", "")
                            cleaned_name = clean_group_name(name, group_slug)
                            if cleaned_name != name:
                                g["name"] = cleaned_name
                                config_changed = True
                            cleaned.append(g)
            config["groups"] = cleaned
            
        if config_changed:
            save_config(config_path, config)
            
        return config
    except Exception as e:
        print(f"Error reading config file: {e}")
        sys.exit(1)

def save_config(config_path, config):
    try:
        with open(config_path, 'w') as f:
            json.dump(config, f, indent=2)
    except Exception as e:
        print(f"Error saving config file: {e}")

def run_setup(session_dir, config_path):
    """
    Launch headed browser to let the user log in manually.
    The session is automatically persisted inside user_data_dir.
    """
    print(f"Launching browser to create/update session in: {session_dir}")
    print("Please log in to your Facebook account in the browser window.")
    
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=session_dir,
            headless=False,
            viewport=None
        )
        page = context.new_page()
        page.goto("https://www.facebook.com")
        
        print("\n*** ACTION REQUIRED ***")
        print("1. Log in to Facebook in the browser window.")
        print("2. Complete any security checks or 2FA.")
        print("3. Leave the browser window open and return to this terminal.")
        print("4. Press ENTER below once you are fully logged in and see your feed.")
        
        input("\nPress ENTER when you are ready to complete setup...")
        
        # Quick validation
        if "facebook.com" in page.url:
            print("Session successfully created/updated!")
            try:
                content = page.content()
                name_match = re.search(r'CurrentUserInitialData.*?"NAME":"([^"]+)"', content)
                if name_match:
                    facebook_account = name_match.group(1)
                    print(f"Logged in as: {facebook_account}")
                    config = load_config(config_path)
                    config["facebook_account"] = facebook_account
                    save_config(config_path, config)
            except Exception as e:
                print(f"Note: Could not extract Facebook account name: {e}")
        else:
            print("Warning: Browser does not seem to be on Facebook. Saving session anyway.")
            
        context.close()
    print("Setup completed successfully.")

def run_fetch_groups(session_dir, config_path):
    """
    Load the Facebook session, navigate to groups/joins, and extract joined groups.
    Updates config.json with newly discovered groups.
    """
    if not os.path.exists(session_dir):
        print(f"Error: Session directory '{session_dir}' does not exist. Run with --setup first.")
        sys.exit(1)
        
    config = load_config(config_path)
    groups = {}
    
    print("Launching browser headlessly to fetch joined groups...")
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=session_dir,
            headless=True
        )
        page = context.new_page()
        
        print("Navigating to Facebook groups list...")
        page.goto("https://www.facebook.com/groups/joins/")
        
        # Wait for redirects and initial load
        page.wait_for_timeout(6000)
        
        # Verify we are logged in
        if "login" in page.url or page.query_selector("input[name='email']"):
            print("Error: Facebook session expired or you are not logged in. Please run setup to log in:")
            print("  venv/bin/python fb_poster.py --setup")
            context.close()
            sys.exit(1)
            
        print("Scrolling page to load all joined groups...")
        last_height = page.evaluate("document.body.scrollHeight")
        no_change_count = 0
        max_scrolls = 25
        reached_bottom = False
        
        for i in range(max_scrolls):
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(random.randint(2000, 4500))
            
            new_height = page.evaluate("document.body.scrollHeight")
            if new_height == last_height:
                no_change_count += 1
                if no_change_count >= 3:
                    print("Reached bottom of the page.")
                    reached_bottom = True
                    break
            else:
                no_change_count = 0
                last_height = new_height
                
            # Scrape links found so far for terminal feedback
            main_area = page.query_selector('div[role="main"]')
            elements = main_area.query_selector_all('a[href*="/groups/"]') if main_area else page.query_selector_all('a[href*="/groups/"]')
            print(f"Scroll {i+1}/{max_scrolls}: Scanned {len(elements)} link candidates...")
            
        # Parse links using JS-evaluated card-based selector (precision matching for members)
        print("Parsing group URLs and names...")
        scraped_groups = page.evaluate("""
            () => {
                const groups = {};
                const joinedButtons = Array.from(document.querySelectorAll('div[role="button"], button')).filter(el => {
                    return el.textContent.trim() === 'Joined';
                });
                
                joinedButtons.forEach(btn => {
                    let parent = btn.parentElement;
                    let foundCard = null;
                    for (let depth = 0; depth < 10; depth++) {
                        if (!parent) break;
                        const groupLinks = Array.from(parent.querySelectorAll('a[href*="/groups/"]'));
                        if (groupLinks.length > 0) {
                            const hasValidGroupLink = groupLinks.some(link => {
                                const href = link.getAttribute('href') || '';
                                const parts = href.split('/groups/');
                                if (parts.length >= 2) {
                                    const slug = parts[1].split('/')[0].split('?')[0];
                                    return slug && !['feed', 'joins', 'discover', 'create', 'categories', 'manage', 'search', 'explore', 'chats', 'jobs', 'requests', 'profile'].includes(slug);
                                }
                                return false;
                            });
                            if (hasValidGroupLink) {
                                foundCard = parent;
                                break;
                            }
                        }
                        parent = parent.parentElement;
                    }
                    
                    if (foundCard) {
                        const links = foundCard.querySelectorAll('a[href*="/groups/"]');
                        links.forEach(link => {
                            const href = link.href;
                            const text = link.innerText.trim();
                            const parts = href.split('/groups/');
                            if (parts.length >= 2) {
                                const slug = parts[1].split('/')[0].split('?')[0];
                                if (slug && !['feed', 'joins', 'discover', 'create', 'categories', 'manage', 'search', 'explore', 'chats', 'jobs', 'requests', 'profile'].includes(slug)) {
                                    const groupUrl = 'https://www.facebook.com/groups/' + slug + '/';
                                    if (text && text !== slug && !/approved|commented|posted|added|shared|invited|crossposted|unread|view group/i.test(text)) {
                                        groups[groupUrl] = text;
                                    } else if (!groups[groupUrl]) {
                                        groups[groupUrl] = text || slug;
                                    }
                                }
                            }
                        });
                    }
                });
                return groups;
            }
        """)

        # Fallback to general selector parsing if JS card parser returned nothing
        if not scraped_groups:
            print("Note: Card-based scraper found no groups. Falling back to general selector...")
            main_area = page.query_selector('div[role="main"]')
            elements = main_area.query_selector_all('a[href*="/groups/"]') if main_area else page.query_selector_all('a[href*="/groups/"]')
            for el in elements:
                try:
                    href = el.get_attribute('href')
                    if not href:
                        continue
                    if href.startswith('/'):
                        href = 'https://www.facebook.com' + href
                    
                    parts = href.split('/groups/')
                    if len(parts) < 2:
                        continue
                    group_part = parts[1].strip('/')
                    group_slug = group_part.split('/')[0]
                    
                    if not group_slug or not re.match(r'^[a-zA-Z0-9\._\-]+$', group_slug):
                        continue
                    if group_slug in ['feed', 'joins', 'discover', 'create', 'categories', 'manage', 'search', 'explore', 'chats', 'jobs', 'requests', 'profile']:
                        continue
                        
                    group_url = f"https://www.facebook.com/groups/{group_slug}/"
                    text = el.inner_text().strip()
                    if text:
                        lines = [l.strip() for l in text.split('\n') if l.strip()]
                        raw_name = lines[0] if lines else group_slug
                        name = clean_group_name(raw_name, group_slug)
                    else:
                        name = group_slug
                        
                    if group_url not in scraped_groups or is_better_name(name, scraped_groups[group_url], group_slug):
                        scraped_groups[group_url] = name
                except Exception:
                    continue

        groups = scraped_groups

        # Extract user profile name
        try:
            content = page.content()
            name_match = re.search(r'CurrentUserInitialData.*?"NAME":"([^"]+)"', content)
            if name_match:
                facebook_account = name_match.group(1)
                config["facebook_account"] = facebook_account
                print(f"Scraped Facebook profile: {facebook_account}")
        except Exception as e:
            print(f"Note: Could not scrape profile name: {e}")
            
        context.close()
        
    print(f"Found {len(groups)} joined groups!")
    
    # Process updates to config.json
    existing_groups = config.get("groups", [])
    existing_by_url = {g["url"]: g for g in existing_groups}
    
    if reached_bottom:
        # We successfully scanned the entire Joins page.
        # Overwrite list to automatically remove groups you have left, preserving their configurations.
        updated_groups = []
        for url, name in groups.items():
            if url in existing_by_url:
                group_item = existing_by_url[url]
                group_item["name"] = name  # Keep name updated
                updated_groups.append(group_item)
            else:
                updated_groups.append({
                    "name": name,
                    "url": url,
                    "enabled": False  # Disabled by default
                })
        config["groups"] = updated_groups
    else:
        # If scroll limit was hit prematurely, only add/update to prevent accidental deletions.
        for url, name in groups.items():
            if url in existing_by_url:
                existing_by_url[url]["name"] = name
            else:
                new_group = {
                    "name": name,
                    "url": url,
                    "enabled": False  # Disabled by default
                }
                existing_groups.append(new_group)
                existing_by_url[url] = new_group
        config["groups"] = existing_groups

    save_config(config_path, config)
    print(f"Groups written to {config_path}.")
    run_manage_groups(config_path)

class GroupManagerHTTPHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Silence standard HTTP logs in the terminal
        pass

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            
            config = load_config(self.server.config_path)
            groups = config.get("groups", [])
            post_text = config.get("post_text", "")
            facebook_account = config.get("facebook_account", "")
            
            html_content = self.server.generate_html(groups, post_text, facebook_account)
            self.wfile.write(html_content.encode("utf-8"))
        elif self.path == "/ping":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok"}).encode("utf-8"))
            self.server.last_ping_time = time.time()
            self.server.has_received_ping = True
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/save":
            content_length = int(self.headers['Content-Length'])
            post_data = self.rfile.read(content_length).decode('utf-8')
            
            try:
                payload = json.loads(post_data)
                enabled_urls = set(payload.get("enabled_urls", []))
                post_text = payload.get("post_text", "").strip()
                post_photos = payload.get("post_photos", False)
                
                config = load_config(self.server.config_path)
                config["post_text"] = post_text
                config["post_photos"] = post_photos
                config["buy_sell_info"] = payload.get("buy_sell_info", {})
                for group in config.get("groups", []):
                    group["enabled"] = group.get("url") in enabled_urls
                    
                save_config(self.server.config_path, config)
                
                self.server.changes_saved = True
                
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"status": "success"}).encode("utf-8"))
                
                print("\nChanges saved successfully.")
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(str(e).encode("utf-8"))
        elif self.path == "/exit":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "success"}).encode("utf-8"))
            self.server.keep_running = False
        else:
            self.send_response(404)
            self.end_headers()

class GroupManagerServer(http.server.HTTPServer):
    def __init__(self, server_address, RequestHandlerClass, config_path):
        super().__init__(server_address, RequestHandlerClass)
        self.config_path = config_path
        self.keep_running = True
        self.changes_saved = False
        self.last_ping_time = time.time()
        self.has_received_ping = False
        
    def generate_html(self, groups, post_text, facebook_account=""):
        import html
        import json
        js_post_text = json.dumps(post_text)
        escaped_post_text = html.escape(post_text)
        
        # Check photo status
        config = load_config(self.config_path)
        post_photos = config.get("post_photos", False)
        photos_dir_name = config.get("photos_directory", "pics")
        buy_sell_info = config.get("buy_sell_info", {
            "enabled": False,
            "title": "",
            "price": "",
            "location": "",
            "description": ""
        })
        js_buy_sell_info = json.dumps(buy_sell_info)
        
        photo_count = 0
        base_dir = os.path.dirname(os.path.abspath(__file__))
        pics_path = os.path.join(base_dir, photos_dir_name)
        if os.path.exists(pics_path) and os.path.isdir(pics_path):
            valid_extensions = ('.jpg', '.jpeg', '.png', '.heic', '.webp')
            photo_files = [f for f in os.listdir(pics_path) if f.lower().endswith(valid_extensions)]
            photo_count = len(photo_files)
            
        checked_attr = "checked" if post_photos else ""
        initial_label = f"Photos Active: {photo_count} found" if post_photos else f"Photos Disabled: {photo_count} found"
        
        photo_status_html = f"""
        <label class="photo-toggle-pill" id="photoTogglePill" title="Click to toggle photo attachments">
            <input type="checkbox" id="postPhotosCheckbox" style="display: none;" onchange="updatePhotoPillState(this); checkChanges()" {checked_attr}>
            <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"></rect><circle cx="8.5" cy="8.5" r="1.5"></circle><polyline points="21 15 16 10 5 21"></polyline></svg>
            <span id="photoToggleLabel">{initial_label}</span>
        </label>
        """
            
        if facebook_account:
            badge_html = f"""
            <div style="display: flex; gap: 0.4rem; align-items: center; flex-wrap: wrap;">
                <div class="account-badge">
                    <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"></path><circle cx="12" cy="7" r="4"></circle></svg>
                    <span>{html.escape(facebook_account)}</span>
                </div>
                {photo_status_html}
            </div>
            """
        else:
            badge_html = f"""
            <div style="display: flex; gap: 0.4rem; align-items: center; flex-wrap: wrap;">
                <div class="account-badge disconnected">
                    <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"></path><circle cx="12" cy="7" r="4"></circle></svg>
                    <span>No profile linked (run --fetch-groups)</span>
                </div>
                {photo_status_html}
            </div>
            """
        
        group_rows = ""
        for i, g in enumerate(groups):
            name = g.get("name", "Unnamed Group")
            url = g.get("url", "")
            checked = "checked" if g.get("enabled") else ""
            
            group_rows += f"""
            <div class="group-card" data-index="{i+1}" data-name="{name.lower()}" data-url="{url.lower()}">
                <label class="checkbox-container">
                    <input type="checkbox" class="group-checkbox" data-url="{url}" {checked} onchange="updateCardState(this)">
                    <span class="checkmark"></span>
                </label>
                <div class="group-details">
                    <div class="group-name">{name}</div>
                    <a href="{url}" target="_blank" class="group-url">{url}</a>
                </div>
                <div class="group-status-badge"></div>
            </div>
            """
            
        html_doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Facebook Groups Manager</title>
    <style>
        :root {{
            --bg-color: #e2e8f0;
            --panel-bg: #f8fafc;
            --border-color: #94a3b8;
            --text-color: #1f2937;
            --text-muted: #4b5563;
            --primary: #10b981;
            --primary-hover: #059669;
            --accent: #2563eb;
            --accent-hover: #1d4ed8;
            --card-bg: #ffffff;
            --card-border: #cbd5e1;
            --card-border-active: #10b981;
            --input-bg: #ffffff;
            --input-border: #94a3b8;
        }}

        * {{
            box-sizing: border-box;
            margin: 0;
            padding: 0;
            font-family: Helvetica, Arial, sans-serif;
            scrollbar-width: thin;
            scrollbar-color: rgba(0,0,0,0.15) transparent;
        }}

        *::-webkit-scrollbar {{
            width: 6px;
        }}
        *::-webkit-scrollbar-track {{
            background: transparent;
        }}
        *::-webkit-scrollbar-thumb {{
            background-color: rgba(0, 0, 0, 0.15);
            border-radius: 3px;
        }}

        body {{
            background-color: var(--bg-color);
            color: var(--text-color);
            display: flex;
            justify-content: center;
            align-items: center;
            min-height: 100vh;
            padding: 0.5rem;
            background-image: 
                radial-gradient(at 0% 0%, rgba(59, 130, 246, 0.04) 0px, transparent 50%),
                radial-gradient(at 100% 100%, rgba(16, 185, 129, 0.03) 0px, transparent 50%);
        }}

        .container {{
            width: 100%;
            max-width: 900px;
            background: var(--panel-bg);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 1.25rem;
            box-shadow: 0 10px 15px -3px rgba(0, 0, 0, 0.05), 0 4px 6px -2px rgba(0, 0, 0, 0.02);
            display: flex;
            flex-direction: column;
            max-height: 96vh;
        }}

        header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 0.75rem;
            padding-bottom: 0.75rem;
            border-bottom: 1px solid var(--border-color);
            flex-shrink: 0;
        }}

        h1 {{
            font-size: 1.35rem;
            font-weight: 600;
            color: var(--text-color);
        }}

        .header-left {{
            display: flex;
            flex-direction: column;
            gap: 0.25rem;
        }}

        .account-badge {{
            display: inline-flex;
            align-items: center;
            gap: 0.3rem;
            background-color: #eff6ff;
            color: #1e40af;
            border: 1px solid #bfdbfe;
            font-size: 0.7rem;
            font-weight: 600;
            padding: 0.15rem 0.5rem;
            border-radius: 9999px;
            width: fit-content;
            box-shadow: 0 1px 2px rgba(0, 0, 0, 0.02);
            transition: all 0.2s ease;
        }}

        .account-badge:hover {{
            background-color: #dbeafe;
            border-color: #93c5fd;
            transform: translateY(-1px);
        }}

        .account-badge.disconnected {{
            background-color: #f1f5f9;
            color: #64748b;
            border-color: #cbd5e1;
        }}

        .account-badge svg {{
            flex-shrink: 0;
        }}

        .dashboard-info {{
            background-color: #f0f9ff;
            border: 1px solid #bae6fd;
            border-radius: 8px;
            padding: 0.5rem 2rem 0.5rem 0.75rem;
            margin-bottom: 0.75rem;
            display: flex;
            gap: 0.5rem;
            align-items: flex-start;
            font-size: 0.8rem;
            line-height: 1.4;
            color: #0369a1;
            flex-shrink: 0;
            box-shadow: 0 1px 2px rgba(0, 0, 0, 0.02);
            position: relative;
        }}

        .info-close-btn {{
            position: absolute;
            top: 0.35rem;
            right: 0.5rem;
            background: none;
            border: none;
            color: #0284c7;
            cursor: pointer;
            font-size: 1.15rem;
            font-weight: bold;
            line-height: 1;
            padding: 0.1rem 0.3rem;
            border-radius: 4px;
            transition: all 0.15s ease;
            display: flex;
            align-items: center;
            justify-content: center;
        }}

        .info-close-btn:hover {{
            background-color: #e0f2fe;
            color: #0369a1;
        }}

        .group-status-badge {{
            display: inline-flex;
            align-items: center;
            justify-content: center;
            width: 8px;
            height: 8px;
            background-color: var(--primary);
            border-radius: 50%;
            margin-left: 0.25rem;
            flex-shrink: 0;
            opacity: 0;
            transition: all 0.2s ease;
            pointer-events: none;
        }}

        .group-card.active-card .group-status-badge {{
            opacity: 1;
        }}

        .badge-dot {{
            display: none;
        }}

        .textarea-section {{
            display: flex;
            flex-direction: column;
            gap: 0.25rem;
            margin-bottom: 0.75rem;
            flex-shrink: 0;
        }}

        .textarea-label {{
            font-size: 0.85rem;
            font-weight: 600;
            color: var(--text-color);
        }}

        .post-textarea {{
            width: 100%;
            height: 75px;
            background: var(--input-bg);
            border: 1px solid var(--input-border);
            color: var(--text-color);
            padding: 0.5rem 0.75rem;
            border-radius: 6px;
            outline: none;
            font-size: 0.9rem;
            line-height: 1.4;
            overflow-y: auto;
            transition: all 0.2s ease;
            text-align: left;
        }}

        .post-textarea:focus {{
            outline: none;
            border-color: var(--accent);
            box-shadow: 0 0 0 2px rgba(37, 99, 235, 0.1);
        }}

        .post-textarea:empty::before {{
            content: attr(placeholder);
            color: #94a3b8;
            pointer-events: none;
            display: block;
        }}

        .editor-toolbar {{
            display: none;
            background: #1e293b;
            border-radius: 6px;
            padding: 0.2rem;
            box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1), 0 2px 4px -1px rgba(0, 0, 0, 0.06);
            border: 1px solid #334155;
            gap: 0.15rem;
            position: absolute;
            z-index: 10000;
            opacity: 0;
            transform: translateY(10px) scale(0.95);
            transition: opacity 0.15s ease, transform 0.15s ease;
            pointer-events: none;
        }}

        .editor-toolbar.show {{
            opacity: 1;
            transform: translateY(0) scale(1);
            pointer-events: auto;
        }}

        .editor-toolbar button {{
            background: transparent;
            border: none;
            color: #f8fafc;
            padding: 0.35rem 0.55rem;
            border-radius: 4px;
            cursor: pointer;
            font-size: 0.8rem;
            display: flex;
            align-items: center;
            justify-content: center;
            min-width: 28px;
            height: 28px;
            transition: all 0.1s ease;
        }}

        .editor-toolbar button:hover {{
            background: #334155;
            color: #ffffff;
        }}

        .editor-toolbar button.active {{
            background: #0f172a;
            color: #38bdf8;
        }}

        .controls-row {{
            display: flex;
            gap: 0.5rem;
            align-items: center;
            margin-bottom: 0.75rem;
            flex-shrink: 0;
        }}

        .search-wrapper {{
            position: relative;
            flex-grow: 1;
        }}

        .search-input {{
            width: 100%;
            background: var(--input-bg);
            border: 1px solid var(--input-border);
            color: var(--text-color);
            padding: 0.5rem 0.75rem;
            padding-left: 2.2rem;
            border-radius: 6px;
            outline: none;
            font-size: 0.9rem;
            transition: all 0.2s ease;
        }}

        .search-input:focus {{
            border-color: var(--accent);
            box-shadow: 0 0 0 2px rgba(59, 130, 246, 0.1);
        }}

        .search-icon {{
            position: absolute;
            left: 0.75rem;
            top: 50%;
            transform: translateY(-50%);
            color: var(--text-muted);
            pointer-events: none;
        }}

        .btn {{
            padding: 0.4rem 1rem;
            border: 1px solid var(--border-color);
            border-radius: 9999px;
            font-weight: 700;
            cursor: pointer;
            transition: all 0.2s ease;
            font-size: 0.75rem;
            outline: none;
        }}

        .btn-secondary {{
            background: #ffffff;
            color: var(--text-color);
        }}

        .btn-secondary:hover {{
            background: #f8fafc;
            border-color: #cbd5e1;
        }}

        .btn-primary {{
            background: var(--accent);
            color: #ffffff;
            border: none;
            box-shadow: 0 1px 2px 0 rgba(0, 0, 0, 0.05);
        }}

        .btn-primary:hover {{
            background: var(--accent-hover);
        }}

        .btn:disabled {{
            opacity: 0.4;
            cursor: not-allowed;
            pointer-events: none;
        }}

        .groups-list {{
            flex-grow: 1;
            overflow-y: auto;
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
            gap: 0.35rem;
            padding-right: 0.4rem;
            margin-bottom: 0.75rem;
            align-content: start;
        }}

        .group-card {{
            display: flex;
            align-items: center;
            background: var(--card-bg);
            border: 1px solid var(--card-border);
            padding: 0.3rem 0.5rem;
            border-radius: 6px;
            transition: all 0.15s ease;
            box-shadow: 0 1px 2px 0 rgba(0, 0, 0, 0.02);
            position: relative;
            overflow: hidden;
            min-height: 38px;
        }}

        .group-card:hover {{
            background: #f8fafc;
            border-color: #cbd5e1;
        }}

        .group-card.active-card {{
            border-color: var(--card-border-active);
            background: rgba(16, 185, 129, 0.03);
        }}

        .photo-toggle-pill {{
            display: inline-flex;
            align-items: center;
            gap: 0.3rem;
            font-size: 0.7rem;
            font-weight: 600;
            padding: 0.15rem 0.5rem;
            border-radius: 9999px;
            cursor: pointer;
            transition: all 0.15s ease;
            user-select: none;
            border: 1px solid;
            box-shadow: 0 1px 2px rgba(0, 0, 0, 0.02);
            height: fit-content;
        }}

        .photo-toggle-pill:has(input:checked) {{
            background-color: #f0fdf4;
            color: #166534;
            border-color: #bbf7d0;
        }}
        .photo-toggle-pill:has(input:checked):hover {{
            background-color: #dcfce7;
            border-color: #86efac;
        }}

        .photo-toggle-pill:not(:has(input:checked)) {{
            background-color: #f1f5f9;
            color: #64748b;
            border-color: #cbd5e1;
        }}
        .photo-toggle-pill:not(:has(input:checked)):hover {{
            background-color: #e2e8f0;
            border-color: #cbd5e1;
        }}

        .photo-toggle-pill svg {{
            flex-shrink: 0;
        }}

        /* Checkbox customization */
        .checkbox-container {{
            position: relative;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            width: 18px;
            height: 18px;
            margin-right: 0.45rem;
            flex-shrink: 0;
        }}

        .checkbox-container input {{
            position: absolute;
            opacity: 0;
            cursor: pointer;
            height: 0;
            width: 0;
        }}

        .checkmark {{
            position: absolute;
            top: 0;
            left: 0;
            height: 18px;
            width: 18px;
            background-color: var(--input-bg);
            border: 1px solid var(--input-border);
            border-radius: 4px;
            transition: all 0.2s ease;
        }}

        .checkbox-container:hover input ~ .checkmark {{
            border-color: var(--primary);
        }}

        .checkbox-container input:checked ~ .checkmark {{
            background-color: var(--primary);
            border-color: var(--primary);
        }}

        .checkmark:after {{
            content: "";
            position: absolute;
            display: none;
        }}

        .checkbox-container input:checked ~ .checkmark:after {{
            display: block;
        }}

        .checkbox-container .checkmark:after {{
            left: 5px;
            top: 2px;
            width: 4px;
            height: 8px;
            border: solid white;
            border-width: 0 2px 2px 0;
            transform: rotate(45deg);
        }}

        .group-details {{
            display: flex;
            flex-direction: column;
            gap: 0.05rem;
            overflow: hidden;
            flex-grow: 1;
        }}

        .group-name {{
            font-size: 0.82rem;
            font-weight: 700;
            color: var(--text-color);
            text-overflow: ellipsis;
            white-space: nowrap;
            overflow: hidden;
        }}

        .group-url {{
            font-size: 0.68rem;
            color: var(--text-muted);
            text-decoration: none;
            transition: color 0.2s ease;
            text-overflow: ellipsis;
            white-space: nowrap;
            overflow: hidden;
        }}

        .group-url:hover {{
            color: var(--accent);
            text-decoration: underline;
        }}

        footer {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-shrink: 0;
            padding-top: 1.5rem;
            border-top: 1px solid var(--border-color);
        }}

        .counter-group {{
            display: flex;
            gap: 0.75rem;
            align-items: center;
        }}

        .counter-separator {{
            color: var(--border-color);
            font-size: 0.9rem;
        }}

        .counter {{
            font-size: 0.9rem;
            color: var(--text-muted);
        }}

        .counter span {{
            color: var(--primary);
            font-weight: 600;
        }}

        /* Toast notification */
        .toast {{
            position: fixed;
            bottom: 2rem;
            left: 50%;
            transform: translateX(-50%) translateY(100px);
            background: rgba(16, 185, 129, 0.95);
            color: #fff;
            padding: 0.75rem 1.5rem;
            border-radius: 8px;
            font-weight: 500;
            box-shadow: 0 10px 15px -3px rgba(0,0,0,0.1);
            transition: transform 0.3s cubic-bezier(0.175, 0.885, 0.32, 1.275);
            z-index: 1000;
        }}

        .toast.show {{
            transform: translateX(-50%) translateY(0);
        }}

        /* Media queries for short viewports */
        @media (max-height: 750px) {{
            .container {{
                padding: 0.75rem;
                max-height: 98vh;
            }}
            header {{
                margin-bottom: 0.5rem;
                padding-bottom: 0.5rem;
            }}
            h1 {{
                font-size: 1.15rem;
            }}
            .dashboard-info {{
                margin-bottom: 0.5rem;
                padding: 0.4rem 1.75rem 0.4rem 0.6rem;
                font-size: 0.75rem;
            }}
            .textarea-section {{
                margin-bottom: 0.5rem;
            }}
            .post-textarea {{
                height: 48px;
                font-size: 0.85rem;
            }}
            .controls-row {{
                margin-bottom: 0.5rem;
            }}
            .groups-list {{
                margin-bottom: 0.5rem;
                gap: 0.25rem;
            }}
            .group-card {{
                padding: 0.25rem 0.4rem;
                min-height: 34px;
            }}
            footer {{
                padding-top: 0.75rem;
            }}
        }}

        @media (max-height: 600px) {{
            .container {{
                padding: 0.5rem;
            }}
            .dashboard-info {{
                display: none;
            }}
            .post-textarea {{
                height: 40px;
                font-size: 0.8rem;
            }}
            header {{
                margin-bottom: 0.35rem;
                padding-bottom: 0.35rem;
            }}
            .controls-row {{
                margin-bottom: 0.35rem;
            }}
            .groups-list {{
                margin-bottom: 0.35rem;
            }}
            footer {{
                padding-top: 0.5rem;
            }}
        }}
        /* Modal dialog styles */
        .buy-sell-modal {{
            border: 1px solid var(--card-border);
            border-radius: 12px;
            padding: 1.25rem;
            max-width: 500px;
            width: 90%;
            background: var(--panel-bg);
            box-shadow: 0 20px 25px -5px rgba(0, 0, 0, 0.1), 0 10px 10px -5px rgba(0, 0, 0, 0.04);
            position: fixed;
            top: 50%;
            left: 50%;
            transform: translate(-50%, -50%);
            margin: 0;
            outline: none;
            overflow: hidden;
        }}

        .buy-sell-modal::backdrop {{
            background-color: rgba(15, 23, 42, 0.3);
            backdrop-filter: blur(4px);
        }}

        .modal-input {{
            width: 100%;
            background: var(--input-bg);
            border: 1px solid var(--input-border);
            color: var(--text-color);
            padding: 0.45rem 0.6rem;
            border-radius: 6px;
            outline: none;
            font-size: 0.85rem;
            transition: all 0.2s ease;
        }}

        .modal-input:focus {{
            border-color: var(--accent);
            box-shadow: 0 0 0 2px rgba(37, 99, 235, 0.1);
        }}

        .modal-textarea {{
            width: 100%;
            height: 90px;
            background: var(--input-bg);
            border: 1px solid var(--input-border);
            color: var(--text-color);
            padding: 0.45rem 0.6rem;
            border-radius: 6px;
            outline: none;
            font-size: 0.85rem;
            line-height: 1.4;
            resize: none;
            transition: all 0.2s ease;
        }}

        .modal-checkbox-row {{
            display: flex;
            align-items: center;
            gap: 0.5rem;
            cursor: pointer;
            user-select: none;
            font-size: 0.85rem;
            font-weight: 600;
            color: var(--text-color);
            margin-bottom: 0.25rem;
            padding: 0.2rem 0;
        }}

        .modal-checkbox-row input[type="checkbox"] {{
            width: 16px;
            height: 16px;
            accent-color: var(--primary);
            cursor: pointer;
            margin: 0;
        }}
    </style>
</head>
<body>
    <div class="container">
        <header>
            <div class="header-left">
                <h1>Facebook Groups Manager</h1>
                {badge_html}
            </div>
            <div style="display: flex; gap: 0.5rem;">
                <button class="btn btn-secondary" onclick="closeManager()">Close Manager</button>
                <button class="btn btn-primary" id="saveButton" onclick="saveChanges()" disabled>Save Changes</button>
            </div>
        </header>

        <div class="dashboard-info" id="dashboardInfoBanner">
            <div class="info-icon">
                <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"></circle><line x1="12" y1="16" x2="12" y2="12"></line><line x1="12" y1="8" x2="12.01" y2="8"></line></svg>
            </div>
            <div class="info-content">
                <strong>About this Dashboard:</strong> This manager configures the message and selects the target Facebook groups for automated posting. <span class="highlight-text">Checked groups are enabled for posting</span>; unchecked groups will be skipped. Once changes are saved, you can run the automation script in the terminal (<code>python fb_poster.py --post</code>) to post to the selected groups.
            </div>
            <button class="info-close-btn" onclick="dismissInfo()" title="Dismiss info banner">&times;</button>
        </div>

        <div class="textarea-section">
            <div style="display: flex; justify-content: space-between; align-items: center;">
                <div style="display: flex; align-items: center; gap: 0.5rem;">
                    <label class="textarea-label" for="postEditor">Post Content</label>
                    <button class="btn btn-secondary" style="font-size: 0.7rem; padding: 0.15rem 0.4rem; display: inline-flex; align-items: center; gap: 0.2rem; border-radius: 4px;" id="buySellConfigBtn" onclick="openBuySellModal()">
                        <svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="9" cy="21" r="1"></circle><circle cx="20" cy="21" r="1"></circle><path d="M1 1h4l2.68 13.39a2 2 0 0 0 2 1.61h9.72a2 2 0 0 0 2-1.61L23 6H6"></path></svg>
                        <span>Buy/Sell Listing Details</span>
                        <span id="buySellStatusDot" style="width: 6px; height: 6px; background-color: var(--primary); border-radius: 50%; display: none;"></span>
                    </button>
                </div>
                <div class="char-counter" id="charCounter" style="font-size: 0.8rem; font-weight: 600; color: var(--text-muted);">
                    Characters: <span id="charCount" style="color: var(--accent);">0</span>
                </div>
            </div>
            <div id="postEditor" contenteditable="true" class="post-textarea" placeholder="Type the message you want to post to Facebook groups..." oninput="updateCharCounter(); checkChanges()"></div>
            <div id="editorToolbar" class="editor-toolbar">
                <button id="btnBold" onclick="format('bold')" title="Bold (Ctrl+B)"><b>B</b></button>
                <button id="btnItalic" onclick="format('italic')" title="Italic (Ctrl+I)"><i>I</i></button>
                <button id="btnUnderline" onclick="format('underline')" title="Underline (Ctrl+U)"><u>U</u></button>
            </div>
            <div id="formattingNote" style="font-size: 0.68rem; color: var(--text-muted); margin-top: 0.35rem; display: flex; align-items: center; gap: 0.25rem; opacity: 0.9;">
                <span class="note-icon" style="color: var(--primary); display: inline-flex;">
                    <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"></circle><line x1="12" y1="16" x2="12" y2="12"></line><line x1="12" y1="8" x2="12.01" y2="8"></line></svg>
                </span>
                <span id="noteText">Under 130 characters and no newlines will apply gradient formatting, otherwise plain text.</span>
            </div>
        </div>

        <div class="controls-row">
            <div class="search-wrapper">
                <svg class="search-icon" xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"></circle><line x1="21" y1="21" x2="16.65" y2="16.65"></line></svg>
                <input type="text" class="search-input" id="searchBox" placeholder="Search groups by name or URL..." oninput="filterGroups()">
            </div>
            <button class="btn btn-secondary" onclick="toggleAll(true)">Select All</button>
            <button class="btn btn-secondary" onclick="toggleAll(false)">Select None</button>
        </div>

        <div class="groups-list" id="groupsList">
            {group_rows}
        </div>

        <footer>
            <div class="counter-group">
                <div class="counter">
                    Selected: <span id="selectedCount">0</span> / <span id="totalCount">{len(groups)}</span> groups
                </div>
                <div class="counter-separator" id="counterSeparator" style="display: none;">|</div>
                <div class="counter" id="searchCounter" style="display: none;">
                    Showing: <span id="visibleCount">{len(groups)}</span> / <span id="totalCount2">{len(groups)}</span> groups
                </div>
            </div>
            <div style="font-size: 0.8rem; color: var(--text-muted);">
                Close browser tab and Ctrl+C in terminal when finished.
            </div>
        </footer>
    </div>

    <div class="toast" id="toast">Changes saved successfully!</div>

    <dialog id="buySellModal" class="buy-sell-modal" closedby="any" aria-labelledby="buySellModalTitle">
        <div class="modal-content">
            <h2 id="buySellModalTitle" style="font-size: 1.1rem; font-weight: 600; margin-bottom: 0.75rem; color: var(--text-color); display: flex; align-items: center; gap: 0.4rem;">
                <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="9" cy="21" r="1"></circle><circle cx="20" cy="21" r="1"></circle><path d="M1 1h4l2.68 13.39a2 2 0 0 0 2 1.61h9.72a2 2 0 0 0 2-1.61L23 6H6"></path></svg>
                Buy & Sell Listing Details
            </h2>
            
            <div style="display: flex; flex-direction: column; gap: 0.75rem;">
                <label class="modal-checkbox-row">
                    <input type="checkbox" id="buySellEnabled" onchange="checkChanges()">
                    <span>Enable Buy/Sell form for Buy & Sell groups</span>
                </label>
                
                <div class="form-group" style="display: flex; flex-direction: column; gap: 0.25rem;">
                    <label for="buySellTitle" style="font-size: 0.8rem; font-weight: 600; color: var(--text-muted);">What are you selling? (Title)</label>
                    <input type="text" id="buySellTitle" class="modal-input" placeholder="e.g. Rooms/Flat available in Ealing" oninput="checkChanges()">
                </div>
                
                <div style="display: flex; gap: 0.5rem;">
                    <div class="form-group" style="display: flex; flex-direction: column; gap: 0.25rem; flex: 1;">
                        <label for="buySellPrice" style="font-size: 0.8rem; font-weight: 600; color: var(--text-muted);">Price (£)</label>
                        <input type="text" id="buySellPrice" class="modal-input" placeholder="e.g. 700" oninput="checkChanges()">
                    </div>
                    <div class="form-group" style="display: flex; flex-direction: column; gap: 0.25rem; flex: 2;">
                        <label for="buySellLocation" style="font-size: 0.8rem; font-weight: 600; color: var(--text-muted);">Location</label>
                        <input type="text" id="buySellLocation" class="modal-input" placeholder="e.g. Ealing, London" oninput="checkChanges()">
                    </div>
                </div>
                
                <div class="form-group" style="display: flex; flex-direction: column; gap: 0.25rem;">
                    <label for="buySellDesc" style="font-size: 0.8rem; font-weight: 600; color: var(--text-muted);">Description</label>
                    <textarea id="buySellDesc" class="modal-textarea" placeholder="Describe the item you are selling..." oninput="checkChanges()"></textarea>
                </div>
            </div>
            
            <div style="display: flex; justify-content: flex-end; gap: 0.5rem; margin-top: 1rem; padding-top: 0.75rem; border-top: 1px solid var(--card-border);">
                <button class="btn btn-secondary" onclick="closeBuySellModal()">Cancel</button>
                <button class="btn btn-primary" onclick="applyBuySellDetails()">Apply Details</button>
            </div>
        </div>
    </dialog>

    <script>
        let rawPostText = {js_post_text};
        let initialPostText = "";
        let initialPostPhotos = false;
        let initialCheckboxStates = [];
        let initialEditorHTML = "";
        let rawBuySellInfo = {js_buy_sell_info};
        let currentBuySellInfo = {{ ...rawBuySellInfo }};
        let initialBuySellInfo = {{ ...rawBuySellInfo }};

        function toSansSerifBoldJS(str) {{
            let result = '';
            for (let i = 0; i < str.length; i++) {{
                let code = str.charCodeAt(i);
                if (code >= 65 && code <= 90) {{ // A-Z
                    result += String.fromCodePoint(code + 120211);
                }} else if (code >= 97 && code <= 122) {{ // a-z
                    result += String.fromCodePoint(code + 120205);
                }} else if (code >= 48 && code <= 57) {{ // 0-9
                    result += String.fromCodePoint(code + 120764);
                }} else {{
                    result += str[i];
                }}
            }}
            return result;
        }}

        function toSansSerifItalicJS(str) {{
            let result = '';
            for (let i = 0; i < str.length; i++) {{
                let code = str.charCodeAt(i);
                if (code >= 65 && code <= 90) {{ // A-Z
                    result += String.fromCodePoint(code + 120263);
                }} else if (code >= 97 && code <= 122) {{ // a-z
                    result += String.fromCodePoint(code + 120257);
                }} else {{
                    result += str[i];
                }}
            }}
            return result;
        }}

        function toUnderlinedJS(str) {{
            let result = '';
            for (let i = 0; i < str.length; i++) {{
                const code = str.charCodeAt(i);
                if (code >= 0xD800 && code <= 0xDBFF) {{ // High surrogate
                    result += str[i] + str[i+1] + '\\u0332';
                    i++;
                }} else {{
                    result += str[i] + '\\u0332';
                }}
            }}
            return result;
        }}

        function unicodeToHTML(text) {{
            let html = '';
            let inBold = false;
            let inItalic = false;
            let inUnderline = false;
            
            for (let i = 0; i < text.length; i++) {{
                let char = text[i];
                let code = text.codePointAt(i);
                
                let isBold = false;
                let isItalic = false;
                let standardChar = null;
                
                if (code >= 120276 && code <= 120301) {{
                    isBold = true;
                    standardChar = String.fromCodePoint(code - 120211);
                    i++;
                }} else if (code >= 120302 && code <= 120327) {{
                    isBold = true;
                    standardChar = String.fromCodePoint(code - 120205);
                    i++;
                }} else if (code >= 120812 && code <= 120821) {{
                    isBold = true;
                    standardChar = String.fromCodePoint(code - 120764);
                    i++;
                }} else if (code >= 120328 && code <= 120353) {{
                    isItalic = true;
                    standardChar = String.fromCodePoint(code - 120263);
                    i++;
                }} else if (code >= 120354 && code <= 120379) {{
                    isItalic = true;
                    standardChar = String.fromCodePoint(code - 120257);
                    i++;
                }}
                
                let isUnderline = false;
                let nextCharIdx = i + 1;
                if (nextCharIdx < text.length && text[nextCharIdx] === '\\u0332') {{
                    isUnderline = true;
                    i++;
                }}
                
                let finalChar = standardChar !== null ? standardChar : char;
                
                if (finalChar === '&') finalChar = '&amp;';
                else if (finalChar === '<') finalChar = '&lt;';
                else if (finalChar === '>') finalChar = '&gt;';
                else if (finalChar === '\\n') finalChar = '<br>';
                
                if (inBold && !isBold) {{ html += '</b>'; inBold = false; }}
                if (inItalic && !isItalic) {{ html += '</i>'; inItalic = false; }}
                if (inUnderline && !isUnderline) {{ html += '</u>'; inUnderline = false; }}
                
                if (!inUnderline && isUnderline) {{ html += '<u>'; inUnderline = true; }}
                if (!inItalic && isItalic) {{ html += '<i>'; inItalic = true; }}
                if (!inBold && isBold) {{ html += '<b>'; inBold = true; }}
                
                html += finalChar;
            }}
            
            if (inBold) html += '</b>';
            if (inItalic) html += '</i>';
            if (inUnderline) html += '</u>';
            
            return html;
        }}

        function parseEditorHTML(node) {{
            let result = '';
            node.childNodes.forEach(child => {{
                if (child.nodeType === Node.TEXT_NODE) {{
                    result += child.textContent;
                }} else if (child.nodeType === Node.ELEMENT_NODE) {{
                    let innerText = parseEditorHTML(child);
                    const tag = child.tagName.toLowerCase();
                    if (innerText !== '') {{
                        if (tag === 'b' || tag === 'strong' || child.style.fontWeight === 'bold') {{
                            innerText = '<b>' + innerText + '</b>';
                        }}
                        if (tag === 'i' || tag === 'em' || child.style.fontStyle === 'italic') {{
                            innerText = '<i>' + innerText + '</i>';
                        }}
                        if (tag === 'u' || child.style.textDecoration === 'underline') {{
                            innerText = '<u>' + innerText + '</u>';
                        }}
                    }}
                    if (tag === 'br') {{
                        innerText = '\\n';
                    }}
                    if (tag === 'div' || tag === 'p') {{
                        innerText = '\\n' + innerText;
                    }}
                    result += innerText;
                }}
            }});
            return result;
        }}

        function getSelectionState() {{
            const state = {{ bold: false, italic: false, underline: false }};
            try {{
                state.bold = document.queryCommandState('bold');
                state.italic = document.queryCommandState('italic');
                state.underline = document.queryCommandState('underline');
            }} catch (e) {{}}
            return state;
        }}

        function updateToolbarButtonStates() {{
            const state = getSelectionState();
            const btnB = document.getElementById('btnBold');
            const btnI = document.getElementById('btnItalic');
            const btnU = document.getElementById('btnUnderline');
            if (btnB) btnB.classList.toggle('active', state.bold);
            if (btnI) btnI.classList.toggle('active', state.italic);
            if (btnU) btnU.classList.toggle('active', state.underline);
        }}

        function format(command) {{
            document.execCommand(command, false, null);
            updateToolbarButtonStates();
            document.getElementById('postEditor').focus();
            checkChanges();
            updateCharCounter();
        }}

        function handleSelection() {{
            const selection = window.getSelection();
            const toolbar = document.getElementById('editorToolbar');
            const editor = document.getElementById('postEditor');
            
            if (selection && !selection.isCollapsed && selection.rangeCount > 0) {{
                const range = selection.getRangeAt(0);
                if (editor.contains(range.commonAncestorContainer)) {{
                    const rects = range.getClientRects();
                    if (rects.length > 0) {{
                        const rect = rects[0];
                        toolbar.style.display = 'flex';
                        setTimeout(() => toolbar.classList.add('show'), 10);
                        
                        const scrollX = window.pageXOffset || document.documentElement.scrollLeft;
                        const scrollY = window.pageYOffset || document.documentElement.scrollTop;
                        
                        const left = rect.left + scrollX + (rect.width / 2) - (toolbar.offsetWidth / 2);
                        const top = rect.top + scrollY - toolbar.offsetHeight - 8;
                        
                        toolbar.style.left = left + 'px';
                        toolbar.style.top = top + 'px';
                        
                        updateToolbarButtonStates();
                        return;
                    }}
                }}
            }}
            
            if (toolbar) {{
                toolbar.classList.remove('show');
                setTimeout(() => {{
                    if (!toolbar.classList.contains('show')) {{
                        toolbar.style.display = 'none';
                    }}
                }}, 150);
            }}
        }}

        document.addEventListener('selectionchange', handleSelection);

        function dismissInfo() {{
            const banner = document.getElementById('dashboardInfoBanner');
            if (banner) {{
                banner.style.display = 'none';
                localStorage.setItem('fbPoster_infoDismissed', 'true');
            }}
        }}

        function checkInfoBanner() {{
            if (localStorage.getItem('fbPoster_infoDismissed') === 'true') {{
                const banner = document.getElementById('dashboardInfoBanner');
                if (banner) {{
                    banner.style.display = 'none';
                }}
            }}
        }}

        function initOriginalState() {{
            checkInfoBanner();
            const editor = document.getElementById('postEditor');
            const isHTML = /<(b|i|u|br|strong|em)[^>]*>/i.test(rawPostText);
            editor.innerHTML = isHTML ? rawPostText.replace(/\\r?\\n/g, '<br>') : unicodeToHTML(rawPostText);
            initialPostText = rawPostText;
            initialEditorHTML = editor.innerHTML;
            initialPostPhotos = document.getElementById('postPhotosCheckbox').checked;
            const checkboxes = document.querySelectorAll('.group-checkbox');
            initialCheckboxStates = Array.from(checkboxes).map(cb => ({{
                url: cb.getAttribute('data-url'),
                checked: cb.checked
            }}));

            // Ensure paste and drop events immediately trigger change detection
            editor.addEventListener('paste', () => {{
                setTimeout(() => {{
                    updateCharCounter();
                    checkChanges();
                }}, 0);
            }});
            editor.addEventListener('drop', () => {{
                setTimeout(() => {{
                    updateCharCounter();
                    checkChanges();
                }}, 0);
            }});

            // Populate Buy/Sell fields
            document.getElementById('buySellEnabled').checked = currentBuySellInfo.enabled || false;
            document.getElementById('buySellTitle').value = currentBuySellInfo.title || "";
            document.getElementById('buySellPrice').value = currentBuySellInfo.price || "";
            document.getElementById('buySellLocation').value = currentBuySellInfo.location || "";
            document.getElementById('buySellDesc').value = currentBuySellInfo.description || "";
            
            // Configure fallback light-dismiss for the dialog
            const modal = document.getElementById('buySellModal');
            if (modal && !('closedBy' in HTMLDialogElement.prototype)) {{
                modal.addEventListener('click', (event) => {{
                    if (event.target !== modal) return;
                    const rect = modal.getBoundingClientRect();
                    const isDialogContent = (
                        rect.top <= event.clientY &&
                        event.clientY <= rect.top + rect.height &&
                        rect.left <= event.clientX &&
                        event.clientX <= rect.left + rect.width
                    );
                    if (isDialogContent) return;
                    modal.close();
                }});
            }}
            
            updateBuySellStatusDot();
            updateCounters();
            updateCharCounter();
            checkChanges();
        }}

        function updateBuySellStatusDot() {{
            const dot = document.getElementById('buySellStatusDot');
            if (dot) {{
                dot.style.display = currentBuySellInfo.enabled ? 'inline-block' : 'none';
            }}
        }}

        function openBuySellModal() {{
            // Load current state into form fields
            document.getElementById('buySellEnabled').checked = currentBuySellInfo.enabled || false;
            document.getElementById('buySellTitle').value = currentBuySellInfo.title || "";
            document.getElementById('buySellPrice').value = currentBuySellInfo.price || "";
            document.getElementById('buySellLocation').value = currentBuySellInfo.location || "";
            document.getElementById('buySellDesc').value = currentBuySellInfo.description || "";
            
            const modal = document.getElementById('buySellModal');
            if (modal) {{
                modal.showModal();
            }}
        }}

        function closeBuySellModal() {{
            const modal = document.getElementById('buySellModal');
            if (modal) {{
                modal.close();
            }}
        }}

        function applyBuySellDetails() {{
            // Read values from form
            currentBuySellInfo.enabled = document.getElementById('buySellEnabled').checked;
            currentBuySellInfo.title = document.getElementById('buySellTitle').value.trim();
            currentBuySellInfo.price = document.getElementById('buySellPrice').value.trim();
            currentBuySellInfo.location = document.getElementById('buySellLocation').value.trim();
            currentBuySellInfo.description = document.getElementById('buySellDesc').value.trim();
            
            updateBuySellStatusDot();
            closeBuySellModal();
            checkChanges();
        }}

        function updateCharCounter() {{
            const editor = document.getElementById('postEditor');
            const countSpan = document.getElementById('charCount');
            const noteText = document.getElementById('noteText');
            const noteIcon = document.querySelector('.note-icon');
            
            const plainText = editor.innerText || editor.textContent || "";
            const len = plainText.length;
            countSpan.textContent = len;
            
            const hasNewlines = plainText.includes(String.fromCharCode(10));
            const isTooLong = len > 130;
            
            if (isTooLong || hasNewlines) {{
                noteText.style.color = '#b45309';
                noteIcon.style.color = '#b45309';
                countSpan.style.color = '#b45309';
                
                let warning = "Note: ";
                if (isTooLong && hasNewlines) {{
                    warning += "Text is over 130 characters and contains newlines (will post as plain text).";
                }} else if (isTooLong) {{
                    warning += "Text is over 130 characters (will post as plain text).";
                }} else {{
                    warning += "Text contains newlines (will post as plain text).";
                }}
                noteText.textContent = warning;
            }} else {{
                noteText.style.color = 'var(--text-muted)';
                noteIcon.style.color = 'var(--primary)';
                countSpan.style.color = 'var(--primary)';
                noteText.textContent = "Under 130 characters and no newlines will apply gradient formatting, otherwise plain text.";
            }}
        }}

        function checkChanges() {{
            const editor = document.getElementById('postEditor');
            const currentPostText = parseEditorHTML(editor);
            const currentPostPhotos = document.getElementById('postPhotosCheckbox').checked;
            const checkboxes = document.querySelectorAll('.group-checkbox');
            
            let buySellChanged = (currentBuySellInfo.enabled !== initialBuySellInfo.enabled) ||
                                 (currentBuySellInfo.title !== initialBuySellInfo.title) ||
                                 (currentBuySellInfo.price !== initialBuySellInfo.price) ||
                                 (currentBuySellInfo.location !== initialBuySellInfo.location) ||
                                 (currentBuySellInfo.description !== initialBuySellInfo.description);
            
            let hasChanges = currentPostText !== initialPostText || currentPostPhotos !== initialPostPhotos || editor.innerHTML !== initialEditorHTML || buySellChanged;
            
            if (!hasChanges) {{
                for (let i = 0; i < checkboxes.length; i++) {{
                    const cb = checkboxes[i];
                    const url = cb.getAttribute('data-url');
                    const initialVal = initialCheckboxStates.find(item => item.url === url);
                    if (initialVal && cb.checked !== initialVal.checked) {{
                        hasChanges = true;
                        break;
                    }}
                }}
            }}
            
            const saveBtn = document.getElementById('saveButton');
            if (hasChanges) {{
                saveBtn.disabled = false;
            }} else {{
                saveBtn.disabled = true;
            }}
        }}

        function updateCounters() {{
            const checkboxes = document.querySelectorAll('.group-checkbox');
            const checkedCount = Array.from(checkboxes).filter(cb => cb.checked).length;
            document.getElementById('selectedCount').textContent = checkedCount;
            
            checkboxes.forEach(cb => {{
                const card = cb.closest('.group-card');
                if (cb.checked) {{
                    card.classList.add('active-card');
                }} else {{
                    card.classList.remove('active-card');
                }}
            }});
        }}

        function updateCardState(checkbox) {{
            const card = checkbox.closest('.group-card');
            if (checkbox.checked) {{
                card.classList.add('active-card');
            }} else {{
                card.classList.remove('active-card');
            }}
            updateCounters();
            checkChanges();
        }}

        function updatePhotoPillState(checkbox) {{
            const label = document.getElementById('photoToggleLabel');
            const statusText = checkbox.checked ? "Active" : "Disabled";
            label.textContent = "Photos " + statusText + ": {photo_count} found";
        }}

        function filterGroups() {{
            const query = document.getElementById('searchBox').value.toLowerCase().trim();
            const cards = document.querySelectorAll('.group-card');
            let visibleCount = 0;
            
            cards.forEach(card => {{
                const name = card.getAttribute('data-name');
                const url = card.getAttribute('data-url');
                if (name.includes(query) || url.includes(query)) {{
                    card.style.display = 'flex';
                    visibleCount++;
                }} else {{
                    card.style.display = 'none';
                }}
            }});
            
            const searchCounter = document.getElementById('searchCounter');
            const separator = document.getElementById('counterSeparator');
            
            if (query !== "") {{
                searchCounter.style.display = 'block';
                separator.style.display = 'block';
                document.getElementById('visibleCount').textContent = visibleCount;
            }} else {{
                searchCounter.style.display = 'none';
                separator.style.display = 'none';
            }}
        }}

        function toggleAll(status) {{
            const cards = document.querySelectorAll('.group-card');
            cards.forEach(card => {{
                if (card.style.display !== 'none') {{
                    const checkbox = card.querySelector('.group-checkbox');
                    checkbox.checked = status;
                    updateCardState(checkbox);
                }}
            }});
        }}

        function saveChanges() {{
            const checkboxes = document.querySelectorAll('.group-checkbox');
            const enabledUrls = Array.from(checkboxes)
                .filter(cb => cb.checked)
                .map(cb => cb.getAttribute('data-url'));
            const editor = document.getElementById('postEditor');
            const postText = parseEditorHTML(editor);
            const postPhotos = document.getElementById('postPhotosCheckbox').checked;

            fetch('/save', {{
                method: 'POST',
                headers: {{
                    'Content-Type': 'application/json'
                }},
                body: JSON.stringify({{ 
                    enabled_urls: enabledUrls,
                    post_text: postText,
                    post_photos: postPhotos,
                    buy_sell_info: currentBuySellInfo
                }})
            }})
            .then(res => {{
                if (res.ok) {{
                    const toast = document.getElementById('toast');
                    toast.classList.add('show');
                    setTimeout(() => {{
                        toast.classList.remove('show');
                    }}, 2500);
                    
                    // Update baselines in-place without re-rendering the editor.
                    rawPostText = postText;
                    initialPostText = postText;
                    initialEditorHTML = editor.innerHTML;
                    initialPostPhotos = document.getElementById('postPhotosCheckbox').checked;
                    initialBuySellInfo = {{ ...currentBuySellInfo }};
                    const cbs = document.querySelectorAll('.group-checkbox');
                    initialCheckboxStates = Array.from(cbs).map(cb => ({{
                        url: cb.getAttribute('data-url'),
                        checked: cb.checked
                    }}));
                    checkChanges();
                }} else {{
                    alert('Error saving changes. Check terminal.');
                }}
            }})
            .catch(err => {{
                console.error(err);
                alert('Connection to local server lost.');
            }});
        }}

        function closeManager() {{
            const saveBtn = document.getElementById('saveButton');
            if (!saveBtn.disabled) {{
                if (!confirm("You have unsaved changes. Are you sure you want to close without saving?")) {{
                    return;
                }}
            }}
            fetch('/exit', {{ method: 'POST' }})
            .then(() => {{
                window.close();
                setTimeout(() => {{
                    document.body.innerHTML = `
                        <div style="font-family: Helvetica, Arial, sans-serif; text-align: center; margin-top: 100px; padding: 2rem;">
                            <h1 style="color: #1f2937; margin-bottom: 20px; font-weight: 600;">Manager Closed</h1>
                            <p style="color: #4b5563; font-size: 1.1rem; line-height: 1.5;">You can now close this browser tab and return to your terminal.</p>
                            <p style="color: #9ca3af; font-size: 0.9rem; margin-top: 1rem;">(Your browser's security settings prevented this tab from closing automatically)</p>
                        </div>
                    `;
                }}, 200);
            }})
            .catch(err => console.error(err));
        }}

        // Heartbeat ping to keep server alive. If browser tab is closed, pings stop and server exits.
        setInterval(() => {{
            fetch('/ping').catch(() => {{}});
        }}, 1000);

        // Initialize state tracker
        initOriginalState();
    </script>
</body>
</html>
"""
        return html_doc

def run_manage_groups(config_path):
    """
    Launch a local HTTP server and open the browser interface to select groups.
    """
    port = 8080
    max_tries = 10
    server = None
    for i in range(max_tries):
        try:
            server = GroupManagerServer(("localhost", port), GroupManagerHTTPHandler, config_path)
            break
        except Exception:
            port += 1
            
    if not server:
        print("Error: Could not start local web server for groups manager.")
        return
        
    url = f"http://localhost:{port}/"
    print(f"\nStarting local Groups Manager web app at: {url}")
    print("Opening browser tab automatically...")
    print("Close the browser tab or press Ctrl+C in this terminal to exit.")
    
    # Open browser tab
    webbrowser.open(url)
    
    server.timeout = 0.5
    try:
        while server.keep_running:
            server.handle_request()
            # If the web app has loaded and started pinging, and pings stop for > 4 seconds,
            # auto-exit since the user closed the browser tab.
            if server.has_received_ping and (time.time() - server.last_ping_time > 4.0):
                print("\nBrowser tab closed. Exiting groups manager...")
                break
    except KeyboardInterrupt:
        pass
    finally:
        if getattr(server, "changes_saved", False):
            print("Exited groups manager. Changes were saved successfully.")
        else:
            print("Exited groups manager. No changes were saved.")

def get_terminal_width():
    try:
        return os.get_terminal_size().columns
    except Exception:
        return 80

class GroupLogger:
    def __init__(self, idx, total, name, url):
        self.idx = idx
        self.total = total
        self.name = name
        self.url = url
        self.lines_printed = 0
        
        term_width = get_terminal_width()
        full_text = f" [{idx}/{total}] Group: {name} ({url})"
        if len(full_text) + 2 > term_width:
            name_text = f" [{idx}/{total}] Group: {name}"
            if len(name_text) + 2 > term_width:
                safe_prefix = f" [{idx}/{total}] Group: "
                max_name_len = max(15, term_width - len(safe_prefix) - 5)
                truncated_name = name[:max_name_len].strip() + "..."
                self.header_text = f"{safe_prefix}{truncated_name}"
            else:
                self.header_text = name_text
        else:
            self.header_text = full_text
            
        # Print the header with blinking dot
        print(f"\n{DOT_BLINK_CYAN}{self.header_text}")

    def log_substep_start(self, text):
        print(f"  {text}...", end="", flush=True)

    def log_substep_done(self, text, status="Done"):
        print(f"\r  {STYLE_DIM}{text}... {status}{STYLE_RESET}\033[K")
        self.lines_printed += 1

    def log_line(self, text):
        print(text)
        self.lines_printed += 1

    def finish(self, success=True, message=None, is_skipped=False):
        if is_skipped:
            dot = DOT_GREEN
            header_style = STYLE_DIM
        elif success:
            dot = DOT_GREEN
            header_style = STYLE_DIM
        else:
            dot = CROSS_RED
            header_style = STYLE_DIM

        # Calculate lines to go up: self.lines_printed + 1 (since cursor is below the last printed line)
        up_count = self.lines_printed + 1
        
        # ANSI sequence: Go up, carriage return, write updated header, reset style, go back down, carriage return
        sys.stdout.write(f"\033[{up_count}A\r{dot}{header_style}{self.header_text}{STYLE_RESET}\033[K\033[{up_count}B\r")
        sys.stdout.flush()

        if message:
            self.log_line(message)

def run_post(session_dir, config_path, test_mode=False):
    """
    Loads session, gets active groups from config, and posts the text.
    """
    if not os.path.exists(session_dir):
        print(f"Error: Session directory '{session_dir}' does not exist. Run with --setup first.")
        sys.exit(1)
        
    config = load_config(config_path)
    buy_sell_info = config.get("buy_sell_info", {})
    buy_sell_enabled = buy_sell_info.get("enabled", False)
    post_text = config.get("post_text", "").strip()
    if not post_text:
        print("Error: 'post_text' is empty or missing in config.json.")
        sys.exit(1)
        
    post_text = convert_markdown_bold(post_text)
        
    enabled_groups = [g for g in config.get("groups", []) if g.get("enabled")]
    if not enabled_groups:
        print("No enabled groups to post to in config.json. Set 'enabled': true for target groups.")
        sys.exit(0)
        
    delay_range = config.get("delay_between_posts_range", [300, 900])
    
    use_gradient = config.get("use_gradient_background", False)
    gradient_name = config.get("gradient_name", "Gradient, purple magenta")
    
    post_photos = config.get("post_photos", False)
    photos_dir_name = config.get("photos_directory", "pics")
    image_paths = []
    
    if post_photos:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        pics_path = os.path.join(base_dir, photos_dir_name)
        if os.path.exists(pics_path) and os.path.isdir(pics_path):
            valid_extensions = ('.jpg', '.jpeg', '.png', '.heic', '.webp')
            for f in os.listdir(pics_path):
                if f.lower().endswith(valid_extensions):
                    image_paths.append(os.path.abspath(os.path.join(pics_path, f)))
            image_paths.sort()
            
    print(f"Starting post run. Total target groups: {len(enabled_groups)}")
    if post_photos:
        if image_paths:
            print(f"Photo upload is enabled. Found {len(image_paths)} photos to upload.")
            # Attaching photos overrides gradient backgrounds
            if use_gradient:
                print("Note: Photo upload will override gradient backgrounds for this run.")
        else:
            print(f"Warning: Photo upload is enabled, but no valid images were found in directory: {photos_dir_name}")
    if use_gradient:
        reasons = []
        if "\n" in post_text:
            reasons.append("contains newlines/line breaks")
        if len(post_text) > 130:
            reasons.append(f"is {len(post_text)} characters (typically needs to be under 130)")
        if reasons:
            print(f"\033[33mWarning: Gradient background is enabled, but your post text {' and '.join(reasons)}. Facebook will likely strip formatting and fallback to plain text.\033[0m")
            
    if test_mode:
        print("[TEST MODE] Enabled. Will type text but ESCAPE out instead of posting.")
        
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=session_dir,
            headless=True
        )
        page = context.new_page()
        
        for idx, group in enumerate(enabled_groups):
            name = group.get("name", "Unnamed Group")
            url = group.get("url")
            
            logger = GroupLogger(idx + 1, len(enabled_groups), name, url)
            
            # Skip if we posted to this group in the last 1 hour (only in live mode)
            last_posted = group.get("last_posted_at")
            if last_posted and not test_mode:
                time_passed = time.time() - last_posted
                if time_passed < 3600:
                    remaining_mins = int((3600 - time_passed) / 60)
                    logger.finish(
                        is_skipped=True,
                        message=f"  {STYLE_DIM}Info: Already posted within the last 1 hour ({remaining_mins} mins remaining). Skipping.{STYLE_RESET}"
                    )
                    logger.log_line("")
                    continue
            
            try:
                logger.log_substep_start("Navigating to group page")
                page.goto(url)
                page.wait_for_timeout(random.randint(5000, 8000))
                logger.log_substep_done("Navigating to group page", "Done")
                
                if "login" in page.url or page.query_selector("input[name='email']"):
                    logger.finish(
                        success=False,
                        message=f"  {CROSS_RED} Error: Session expired. Run setup to log in again:\n    venv/bin/python fb_poster.py --setup"
                    )
                    break
                    
                # Check if membership is pending or we are not a member of private group
                is_pending = False
                is_not_member = False
                try:
                    pending_text = page.locator('text="Your membership is pending"')
                    cancel_req_btn = page.locator('button:has-text("Cancel Request"), div[role="button"]:has-text("Cancel Request")')
                    if pending_text.count() > 0 or cancel_req_btn.count() > 0:
                        is_pending = True
                        
                    join_btn = page.locator('button:has-text("Join Group"), button:has-text("Join group"), div[role="button"]:has-text("Join Group"), div[role="button"]:has-text("Join group")')
                    is_private = page.locator('text="This group is private"').count() > 0 or page.locator('text="Private group"').count() > 0
                    if join_btn.count() > 0 and is_private:
                        is_not_member = True
                except Exception:
                    pass
                    
                if is_pending:
                    logger.finish(
                        is_skipped=True,
                        message=f"  {STYLE_DIM}Info: Membership request is pending. Skipping.{STYLE_RESET}"
                    )
                    logger.log_line("")
                    if idx < len(enabled_groups) - 1:
                        sleep_time = random.randint(2, 5) if test_mode else random.randint(delay_range[0], delay_range[1])
                        print(f"  Sleeping for {sleep_time}s to maintain safe posting frequency...", end="", flush=True)
                        time.sleep(sleep_time)
                        print(f"\r  {STYLE_DIM}Sleeping for {sleep_time}s to maintain safe posting frequency... Done{STYLE_RESET}\033[K")
                    print("")
                    continue
                    
                if is_not_member:
                    logger.finish(
                        is_skipped=True,
                        message=f"  {STYLE_DIM}Info: Not a member of this private group. Skipping.{STYLE_RESET}"
                    )
                    logger.log_line("")
                    if idx < len(enabled_groups) - 1:
                        sleep_time = random.randint(2, 5) if test_mode else random.randint(delay_range[0], delay_range[1])
                        print(f"  Sleeping for {sleep_time}s to maintain safe posting frequency...", end="", flush=True)
                        time.sleep(sleep_time)
                        print(f"\r  {STYLE_DIM}Sleeping for {sleep_time}s to maintain safe posting frequency... Done{STYLE_RESET}\033[K")
                    print("")
                    continue

                # Check if it's a Buy & Sell group
                is_sell_group = False
                sell_button = None
                buy_sell_tab = page.locator('a[role="tab"]:has-text("Buy and sell"), a[role="tab"]:has-text("Buy & sell"), a[role="tab"]:has-text("Buy and Sell")')
                
                try:
                    if buy_sell_tab.count() > 0:
                        is_sell_group = True
                except Exception:
                    pass
                    
                if not is_sell_group:
                    for indicator in ["Sell Something", "What are you selling?", "Create listing", "Sell item"]:
                        try:
                            loc = page.get_by_text(indicator, exact=False)
                            if loc.count() > 0:
                                for i in range(loc.count()):
                                    if loc.nth(i).is_visible():
                                        is_sell_group = True
                                        sell_button = loc.nth(i)
                                        break
                            if is_sell_group:
                                break
                        except Exception:
                            continue
                            
                # If we want to use the Buy/Sell form and it's a Buy/Sell group
                if is_sell_group and buy_sell_enabled:
                    logger.log_substep_start("Filling Buy/Sell form")
                    try:
                        # Open the Buy/Sell form
                        if sell_button:
                            sell_button.click(timeout=5000)
                        else:
                            buy_sell_tab.first.click()
                            page.wait_for_timeout(3000)
                            # Find the "Sell Something" button
                            for indicator in ["Sell Something", "What are you selling?", "Create listing", "Sell item"]:
                                loc = page.get_by_text(indicator, exact=False)
                                if loc.count() > 0:
                                    for i in range(loc.count()):
                                        if loc.nth(i).is_visible():
                                            sell_button = loc.nth(i)
                                            break
                                if sell_button:
                                    break
                            if sell_button:
                                sell_button.click(timeout=5000)
                            else:
                                raise Exception("Sell button not found after clicking tab")
                                
                        page.wait_for_timeout(4000)
                        
                        # If "Choose listing type" is visible, select "Item for sale"
                        item_for_sale = page.get_by_text("Item for sale", exact=False)
                        if item_for_sale.count() > 0 and item_for_sale.first.is_visible():
                            item_for_sale.first.click()
                            page.wait_for_timeout(4000)
                            
                        # Click "More details" to expand if Description/Location are not already visible
                        desc_label = page.locator('label:has-text("Description")')
                        loc_label = page.locator('label:has-text("Location")')
                        if desc_label.count() == 0 or loc_label.count() == 0:
                            more_details = page.get_by_text("More details", exact=False)
                            if more_details.count() > 0:
                                for i in range(more_details.count()):
                                    if more_details.nth(i).is_visible():
                                        more_details.nth(i).scroll_into_view_if_needed()
                                        more_details.nth(i).click()
                                        page.wait_for_timeout(2000)
                                        break
                                        
                        # Find and fill Title
                        title_input = page.locator('label:has-text("Title") input')
                        if title_input.count() == 0:
                            title_input = page.locator('input[placeholder="What are you selling?"], input[aria-label="What are you selling?"], input[placeholder="Title"], input[aria-label="Title"]')
                        if title_input.count() > 0:
                            title_input.first.fill(buy_sell_info.get("title", ""))
                            page.wait_for_timeout(1000)
                            
                        # Find and fill Price
                        price_input = page.locator('label:has-text("Price") input')
                        if price_input.count() == 0:
                            price_input = page.locator('input[placeholder="Price"], input[aria-label="Price"]')
                        if price_input.count() > 0:
                            price_input.first.fill(buy_sell_info.get("price", ""))
                            page.wait_for_timeout(1000)
                            
                        # Find and fill Location
                        location_input = page.locator('label:has-text("Location") input, label:has-text("Location") textarea')
                        if location_input.count() == 0:
                            location_input = page.locator('input[placeholder="Location"], input[aria-label="Location"]')
                        if location_input.count() > 0:
                            location_input.first.fill(buy_sell_info.get("location", ""))
                            page.wait_for_timeout(2500)
                            page.keyboard.press("ArrowDown")
                            page.wait_for_timeout(1000)
                            page.keyboard.press("Enter")
                            page.wait_for_timeout(1000)
                            
                        # Find and fill Description
                        desc_input = page.locator('label:has-text("Description") textarea, label:has-text("Description") input, label:has-text("Description") div[contenteditable="true"]')
                        if desc_input.count() == 0:
                            desc_input = page.locator(
                                'textarea[placeholder*="Describe" i], textarea[placeholder*="Description" i], '
                                'textarea[aria-label*="Description" i], textarea[aria-label*="Describe" i], '
                                'div[contenteditable="true"][aria-label*="Description" i], div[contenteditable="true"][aria-label*="Describe" i], '
                                'div[contenteditable="true"][placeholder*="Description" i], div[contenteditable="true"][placeholder*="Describe" i], '
                                'div[contenteditable="true"][aria-placeholder*="Description" i], div[contenteditable="true"][aria-placeholder*="Describe" i]'
                            )
                        
                        target_desc = None
                        if desc_input.count() > 0:
                            for idx_desc in range(desc_input.count()):
                                item = desc_input.nth(idx_desc)
                                if title_input.count() > 0 and item.element_handle() == title_input.first.element_handle():
                                    continue
                                target_desc = item
                                break
                                
                        # Ultimate fallback: find generic textareas or divs, but explicitly ignore comment fields
                        if not target_desc:
                            candidates = page.locator('textarea, div[contenteditable="true"]')
                            candidate_count = candidates.count()
                            for idx_c in range(candidate_count):
                                cand = candidates.nth(idx_c)
                                try:
                                    aria_label = cand.get_attribute("aria-label") or ""
                                    placeholder = cand.get_attribute("placeholder") or ""
                                    aria_placeholder = cand.get_attribute("aria-placeholder") or ""
                                    
                                    # Skip comment boxes, replies, and already used inputs
                                    is_comment = any(
                                        "comment" in text.lower() or "reply" in text.lower() or "write a public" in text.lower()
                                        for text in [aria_label, placeholder, aria_placeholder]
                                    )
                                    
                                    is_already_used = False
                                    if title_input.count() > 0 and cand.element_handle() == title_input.first.element_handle():
                                        is_already_used = True
                                    if price_input.count() > 0 and cand.element_handle() == price_input.first.element_handle():
                                        is_already_used = True
                                    if location_input.count() > 0 and cand.element_handle() == location_input.first.element_handle():
                                        is_already_used = True
                                        
                                    if not is_comment and not is_already_used:
                                        target_desc = cand
                                        break
                                except Exception:
                                    pass

                        if target_desc:
                            target_desc.focus()
                            target_desc.click()
                            target_desc.fill(buy_sell_info.get("description", ""))
                            page.wait_for_timeout(1000)
                                
                        # Handle photo upload if enabled and photos are available
                        uploaded_photos = False
                        if post_photos and image_paths:
                            file_inputs = page.locator('input[type="file"]')
                            if file_inputs.count() > 0:
                                target_input = file_inputs.first
                                try:
                                    is_multiple = target_input.evaluate('el => el.multiple')
                                except Exception:
                                    is_multiple = True  # Default fallback
                                    
                                if is_multiple:
                                    target_input.set_input_files(image_paths)
                                else:
                                    target_input.set_input_files(image_paths[:1])
                                page.wait_for_timeout(7000)
                                uploaded_photos = True
                                
                        # Submit form
                        next_btn = page.locator('button:has-text("Next"), div[role="button"]:has-text("Next")')
                        post_btn = page.locator('button:has-text("Post"), div[role="button"]:has-text("Post"), button:has-text("Publish"), div[role="button"]:has-text("Publish")')
                        
                        if test_mode:
                            logger.log_substep_done("Filling Buy/Sell form", "Test Mode (Not Posting)")
                            page.keyboard.press("Escape")
                            page.wait_for_timeout(1000)
                            page.keyboard.press("Escape")
                            logger.finish(success=True)
                        else:
                            if next_btn.count() > 0:
                                next_btn.first.click()
                                page.wait_for_timeout(3000)
                                post_btn = page.locator('button:has-text("Post"), div[role="button"]:has-text("Post"), button:has-text("Publish"), div[role="button"]:has-text("Publish")')
                                
                            if post_btn.count() > 0:
                                post_btn.first.click()
                                page.wait_for_timeout(8000) # Wait for post to submit
                                # Update last posted timestamp and save config
                                group["last_posted_at"] = time.time()
                                save_config(config_path, config)
                                logger.log_substep_done("Filling Buy/Sell form", "Posted successfully")
                                logger.finish(success=True)
                            else:
                                raise Exception("Post/Publish button not found")
                            
                    except Exception as e:
                        logger.log_substep_done("Filling Buy/Sell form", f"Failed: {e}")
                        logger.finish(success=False)
                        
                    # Handle sleep before continuing to next group
                    if idx < len(enabled_groups) - 1:
                        sleep_time = random.randint(2, 5) if test_mode else random.randint(delay_range[0], delay_range[1])
                        print(f"  Sleeping for {sleep_time}s to maintain safe posting frequency...", end="", flush=True)
                        time.sleep(sleep_time)
                        print(f"\r  {STYLE_DIM}Sleeping for {sleep_time}s to maintain safe posting frequency... Done{STYLE_RESET}\033[K")
                    print("")
                    continue

                # Try to switch to "Discussion" tab to ensure composer is visible
                discussion_tab = page.locator('a[role="tab"]:has-text("Discussion")')
                if discussion_tab.count() > 0:
                    try:
                        logger.log_substep_start("Switching to Discussion tab")
                        discussion_tab.first.click(timeout=3000)
                        page.wait_for_timeout(4000)
                        logger.log_substep_done("Switching to Discussion tab", "Done")
                    except Exception:
                        logger.log_substep_done("Switching to Discussion tab", "Failed")
                        
                # Click composer button
                logger.log_substep_start("Opening composer")
                composer_clicked = False
                composer_label = ""
                for label in ["Write something...", "Create a public post...", "Create a post...", "Write a post..."]:
                    try:
                        loc = page.get_by_text(label, exact=False)
                        if loc.count() > 0:
                            loc.first.scroll_into_view_if_needed()
                            loc.first.click(timeout=3000)
                            composer_clicked = True
                            composer_label = f"Done ({label})"
                            break
                    except Exception:
                        continue
                        
                if not composer_clicked:
                    # Alternative selector
                    try:
                        loc = page.get_by_role("button", name=re.compile(r"Write something|Create.*post", re.IGNORECASE))
                        if loc.count() > 0:
                            loc.first.click(timeout=3000)
                            composer_clicked = True
                            composer_label = "Done (role button)"
                    except Exception:
                        pass
                        
                if not composer_clicked:
                    # Final fallback selectors
                    try:
                        loc = page.locator('span:has-text("Write something...")')
                        if loc.count() > 0:
                            loc.first.click(timeout=3000)
                            composer_clicked = True
                            composer_label = "Done (fallback span)"
                    except Exception:
                        pass
                        
                if not composer_clicked:
                    # Check if it's a Buy & Sell group requiring a listing form
                    is_sell_group = False
                    
                    # 1. Check tabs
                    try:
                        discussion_tab = page.locator('a[role="tab"]:has-text("Discussion")')
                        buy_sell_tab = page.locator('a[role="tab"]:has-text("Buy and sell"), a[role="tab"]:has-text("Buy & sell"), a[role="tab"]:has-text("Buy and Sell")')
                        if discussion_tab.count() == 0 and buy_sell_tab.count() > 0:
                            is_sell_group = True
                    except Exception:
                        pass
                        
                    # 2. Check page indicators
                    if not is_sell_group:
                        for indicator in ["Sell Something", "What are you selling?", "Create listing", "Sell item"]:
                            try:
                                if page.get_by_text(indicator, exact=False).count() > 0:
                                    is_sell_group = True
                                    break
                            except Exception:
                                continue
                            
                    if is_sell_group:
                        logger.log_substep_done("Opening composer", "Sell form required (skipped)")
                        logger.finish(
                            is_skipped=True,
                            message=f"  {STYLE_DIM}Info: Buy & Sell group requires listing form. Skipped.{STYLE_RESET}"
                        )
                    else:
                        logger.log_substep_done("Opening composer", "Failed (not found)")
                        logger.finish(success=False, message=None)
                    continue
                else:
                    logger.log_substep_done("Opening composer", composer_label)
                    
                page.wait_for_timeout(3000)
                
                # Locate contenteditable textbox (prefer inside the active dialog modal)
                dialog = page.locator('div[role="dialog"]')
                
                # Upload photos if enabled and available
                uploaded_photos = False
                if post_photos and image_paths:
                    if dialog.count() > 0:
                        file_input = dialog.locator('input[type="file"]')
                        if file_input.count() > 0:
                            try:
                                logger.log_substep_start(f"Uploading {len(image_paths)} photos")
                                file_input.first.set_input_files(image_paths)
                                page.wait_for_timeout(7000) # Wait for photos to upload/process
                                logger.log_substep_done(f"Uploading {len(image_paths)} photos", "Done")
                                uploaded_photos = True
                            except Exception as e:
                                logger.log_substep_done(f"Uploading {len(image_paths)} photos", f"Failed: {e}")
                        else:
                            logger.log_line("  Info: Photo input element not found in composer dialog.")
                    else:
                        logger.log_line("  Info: Dialog modal not found for photo upload.")
                
                target_textbox = None
                if dialog.count() > 0:
                    tb_candidates = dialog.locator('div[contenteditable="true"]')
                    if tb_candidates.count() == 0:
                        tb_candidates = dialog.get_by_role("textbox")
                else:
                    tb_candidates = page.locator('div[contenteditable="true"]')
                    if tb_candidates.count() == 0:
                        tb_candidates = page.get_by_role("textbox")
                        
                if tb_candidates.count() > 0:
                    for idx_tb in range(tb_candidates.count()):
                        cand = tb_candidates.nth(idx_tb)
                        try:
                            aria_label = cand.get_attribute("aria-label") or ""
                            placeholder = cand.get_attribute("placeholder") or ""
                            aria_placeholder = cand.get_attribute("aria-placeholder") or ""
                            
                            is_comment = any(
                                "comment" in text.lower() or "reply" in text.lower() or "write a public" in text.lower()
                                for text in [aria_label, placeholder, aria_placeholder]
                            )
                            if not is_comment:
                                target_textbox = cand
                                break
                        except Exception:
                            pass
                    
                if not target_textbox:
                    logger.log_line(f"  {CROSS_RED} Could not locate the input text box. Skipping.")
                    page.keyboard.press("Escape")
                    logger.finish(success=False)
                    continue
                    
                logger.log_substep_start("Typing message")
                target_textbox.focus()
                target_textbox.click()
                
                # Human typing behavior with native rich text formatting
                parser = FacebookFormatParser()
                parser.feed(post_text)
                
                is_mac = page.evaluate("navigator.userAgent.includes('Mac OS')")
                modifier = "Meta" if is_mac else "Control"
                
                current_active_styles = set()
                
                for text_chunk, styles in parser.chunks:
                    # Turn on missing styles
                    for style in styles - current_active_styles:
                        page.keyboard.press(f"{modifier}+{style}")
                        
                    # Turn off extra styles
                    for style in current_active_styles - styles:
                        page.keyboard.press(f"{modifier}+{style}")
                        
                    current_active_styles = styles
                    page.keyboard.type(text_chunk, delay=random.randint(50, 150))
                    
                # Turn off remaining styles
                for style in current_active_styles:
                    page.keyboard.press(f"{modifier}+{style}")
                    
                page.wait_for_timeout(random.randint(2000, 4000))
                logger.log_substep_done("Typing message", "Done")
                
                # Apply background if requested and available (and no photos were uploaded)
                if use_gradient and not uploaded_photos:
                    bg_trigger = page.locator('div[aria-label="Show Background Options"]')
                    if bg_trigger.count() > 0:
                        try:
                            logger.log_substep_start("Applying gradient background")
                            bg_trigger.first.click(timeout=3000)
                            page.wait_for_timeout(2000)
                            
                            # Determine gradient name to apply
                            selected_gradient = None
                            if isinstance(gradient_name, list) and len(gradient_name) > 0:
                                selected_gradient = random.choice(gradient_name)
                            elif str(gradient_name).lower() in ["random", "any"]:
                                # Dynamically scan options in the DOM
                                options_loc = page.locator('div[aria-label*="Gradient"], div[aria-label*="gradient"]')
                                valid_options = []
                                count = options_loc.count()
                                for idx_opt in range(count):
                                    label = options_loc.nth(idx_opt).get_attribute("aria-label") or ""
                                    if label and not any(w in label for w in ["Options", "options", "Show", "Hide"]):
                                        valid_options.append(label)
                                if valid_options:
                                    selected_gradient = random.choice(valid_options)
                                else:
                                    # Fallback list of common Facebook gradient labels if DOM scraping fails
                                    fallback_list = [
                                        "Gradient, purple magenta",
                                        "Gradient, blue purple",
                                        "Gradient, red orange",
                                        "Gradient, orange yellow",
                                        "Gradient, green blue",
                                        "Gradient, blue teal",
                                        "Gradient, pink purple"
                                    ]
                                    selected_gradient = random.choice(fallback_list)
                            else:
                                selected_gradient = gradient_name
                                
                            target_bg = page.locator(f'div[aria-label*="{selected_gradient}"]').first
                            if target_bg.count() > 0:
                                target_bg.click(timeout=3000)
                                page.wait_for_timeout(2000)
                                logger.log_substep_done("Applying gradient background", f"Applied ({selected_gradient})")
                            else:
                                logger.log_substep_done("Applying gradient background", f"Gradient '{selected_gradient}' not found")
                        except Exception as e:
                            logger.log_substep_done("Applying gradient background", f"Failed: {e}")
                    else:
                        logger.log_line("  Info: 'Show Background Options' button not found.")
                
                if test_mode:
                    logger.log_substep_start("[TEST MODE] Closing composer")
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(2000)
                    # Handle discard changes warning modal if it appears
                    discard_btn = page.get_by_role("button", name=re.compile(r"Discard|Leave", re.IGNORECASE))
                    if discard_btn.count() > 0:
                        discard_btn.first.click()
                    logger.log_substep_done("[TEST MODE] Closing composer", "Discarded changes")
                    
                    logger.finish(
                        success=True,
                        message=f"  {STYLE_DIM}Status: [TEST MODE] Verified successfully!{STYLE_RESET}"
                    )
                else:
                    logger.log_substep_start("Publishing post")
                    posted = False
                    # Search inside compose modal first
                    dialog = page.locator('div[role="dialog"]')
                    
                    for btn_label in ["Post", "Publish", "Submit"]:
                        try:
                            if dialog.count() > 0:
                                btn = dialog.get_by_role("button", name=btn_label, exact=True)
                                if btn.count() > 0 and btn.first.is_enabled():
                                    btn.first.click(timeout=3000)
                                    posted = True
                                    break
                                    
                            btn = page.get_by_role("button", name=btn_label, exact=True)
                            if btn.count() > 0 and btn.first.is_enabled():
                                btn.first.click(timeout=3000)
                                posted = True
                                break
                        except Exception:
                            continue
                            
                    if not posted:
                        logger.log_substep_done("Publishing post", "Failed to find 'Post' button")
                        page.keyboard.press("Escape")
                        logger.finish(success=False)
                        continue
                        
                    logger.log_substep_done("Publishing post", "Submitted")
                    
                    # Wait for Facebook to process the post (especially important for photos)
                    try:
                        if dialog.count() > 0:
                            dialog.first.wait_for(state="hidden", timeout=20000)
                        page.wait_for_timeout(5000)
                    except Exception:
                        page.wait_for_timeout(15000)
                    
                    logger.finish(
                        success=True,
                        message=f"  {STYLE_DIM}Status: Post submitted successfully!{STYLE_RESET}"
                    )
                    
                    # Update last posted timestamp and save config
                    group["last_posted_at"] = time.time()
                    save_config(config_path, config)
                    
            except Exception as e:
                logger.finish(
                    success=False,
                    message=f"  {CROSS_RED} Error occurred: {e}"
                )
                
            if idx < len(enabled_groups) - 1:
                sleep_time = random.randint(2, 5) if test_mode else random.randint(delay_range[0], delay_range[1])
                print(f"  Sleeping for {sleep_time}s to maintain safe posting frequency...", end="", flush=True)
                time.sleep(sleep_time)
                print(f"\r  {STYLE_DIM}Sleeping for {sleep_time}s to maintain safe posting frequency... Done{STYLE_RESET}\033[K")
            print("")
                
        context.close()
    print("Done!")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Facebook Group Automated Poster")
    parser.add_argument("--setup", action="store_true", help="Launch browser for manual Facebook login and session save")
    parser.add_argument("--fetch-groups", action="store_true", help="Fetch all joined Facebook groups and update config")
    parser.add_argument("--manage", action="store_true", help="Manage enabled/disabled status of groups interactively")
    parser.add_argument("--post", action="store_true", help="Run automated posting to enabled groups")
    parser.add_argument("--test", action="store_true", help="Dry run for posting: types message but does not click submit")
    
    args = parser.parse_args()
    
    base_dir = os.path.dirname(os.path.abspath(__file__))
    session_dir = os.path.join(base_dir, "fb_session")
    config_path = os.path.join(base_dir, "config.json")
    lock_path = os.path.join(base_dir, "fb_poster.lock")
    
    # We only lock for active commands (not when printing help)
    has_active_cmd = args.setup or args.fetch_groups or args.manage or args.post or args.test
    
    if has_active_cmd:
        acquire_lock(lock_path)
        
    try:
        if args.setup:
            run_setup(session_dir, config_path)
        elif args.fetch_groups:
            run_fetch_groups(session_dir, config_path)
        elif args.manage:
            run_fetch_groups(session_dir, config_path)
        elif args.post or args.test:
            run_post(session_dir, config_path, test_mode=args.test)
        else:
            parser.print_help()
    except KeyboardInterrupt:
        print("\n\nExiting. Script terminated by user (Ctrl+C).")
        sys.exit(0)
    finally:
        if has_active_cmd:
            release_lock(lock_path)

