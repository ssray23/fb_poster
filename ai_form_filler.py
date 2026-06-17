"""
AI-Powered Form Filler for Facebook Buy/Sell Listings.

Uses Groq (Llama 3.3 70B) to dynamically analyze form fields and map them to
config values, replacing brittle hardcoded CSS selectors.
"""

import json
import time
import os

from groq import Groq

# ---------------------------------------------------------------------------
# 1. DOM Snapshot Extractor
# ---------------------------------------------------------------------------

def extract_form_snapshot(page):
    """
    Scan the visible page for form elements and build a structured inventory.
    Returns a list of dicts, each describing one interactive element.
    
    We extract: inputs, textareas, contenteditable divs, selects, and
    clickable elements that look like dropdowns or expand buttons.
    """
    snapshot = page.evaluate("""() => {
        const results = [];
        
        // Helper: build a human-readable locator hint for an element
        function getLocatorHint(el) {
            // Check for a parent <label>
            const label = el.closest('label');
            if (label) {
                const labelText = label.textContent.trim().split('\\n')[0].substring(0, 60);
                const tag = el.tagName.toLowerCase();
                const type = el.type ? `[type="${el.type}"]` : '';
                return `label:has-text("${labelText}") ${tag}${type}`;
            }
            
            // Check aria-label
            if (el.getAttribute('aria-label')) {
                const tag = el.tagName.toLowerCase();
                return `${tag}[aria-label="${el.getAttribute('aria-label')}"]`;
            }
            
            // Check placeholder
            if (el.getAttribute('placeholder')) {
                const tag = el.tagName.toLowerCase();
                return `${tag}[placeholder="${el.getAttribute('placeholder')}"]`;
            }
            
            // Fallback: use tag + index
            return null;
        }
        
        // Helper: check if element is visible
        function isVisible(el) {
            const rect = el.getBoundingClientRect();
            if (rect.width === 0 && rect.height === 0) return false;
            const style = window.getComputedStyle(el);
            if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
            return true;
        }
        
        // Helper: check if element is inside a comment/reply area
        function isCommentArea(el) {
            const attrs = ['aria-label', 'placeholder', 'aria-placeholder', 'data-testid'];
            for (const attr of attrs) {
                const val = (el.getAttribute(attr) || '').toLowerCase();
                if (val.includes('comment') || val.includes('reply') || val.includes('write a public')) {
                    return true;
                }
            }
            // Walk up 3 levels checking for comment containers
            let parent = el.parentElement;
            for (let i = 0; i < 3 && parent; i++) {
                const testid = (parent.getAttribute('data-testid') || '').toLowerCase();
                if (testid.includes('comment') || testid.includes('reply')) return true;
                parent = parent.parentElement;
            }
            return false;
        }
        
        // Scan inputs
        const inputs = document.querySelectorAll('input:not([type="hidden"]):not([type="file"]):not([type="checkbox"]):not([type="radio"])');
        for (const el of inputs) {
            if (!isVisible(el) || isCommentArea(el)) continue;
            results.push({
                tag: 'input',
                type: el.type || 'text',
                locator_hint: getLocatorHint(el),
                aria_label: el.getAttribute('aria-label') || '',
                placeholder: el.getAttribute('placeholder') || '',
                label_text: el.closest('label') ? el.closest('label').textContent.trim().split('\\n')[0].substring(0, 60) : '',
                current_value: el.value || '',
                is_required: el.required || el.getAttribute('aria-required') === 'true'
            });
        }
        
        // Scan textareas
        const textareas = document.querySelectorAll('textarea');
        for (const el of textareas) {
            if (!isVisible(el) || isCommentArea(el)) continue;
            results.push({
                tag: 'textarea',
                type: 'textarea',
                locator_hint: getLocatorHint(el),
                aria_label: el.getAttribute('aria-label') || '',
                placeholder: el.getAttribute('placeholder') || '',
                label_text: el.closest('label') ? el.closest('label').textContent.trim().split('\\n')[0].substring(0, 60) : '',
                current_value: el.value || '',
                is_required: el.required || el.getAttribute('aria-required') === 'true'
            });
        }
        
        // Scan contenteditable divs
        const editables = document.querySelectorAll('div[contenteditable="true"]');
        for (const el of editables) {
            if (!isVisible(el) || isCommentArea(el)) continue;
            results.push({
                tag: 'div[contenteditable]',
                type: 'contenteditable',
                locator_hint: getLocatorHint(el),
                aria_label: el.getAttribute('aria-label') || '',
                placeholder: el.getAttribute('placeholder') || el.getAttribute('aria-placeholder') || '',
                label_text: el.closest('label') ? el.closest('label').textContent.trim().split('\\n')[0].substring(0, 60) : '',
                current_value: el.textContent.trim().substring(0, 100),
                is_required: el.getAttribute('aria-required') === 'true'
            });
        }
        
        // Scan for dropdown-like elements (Condition, Category, etc.)
        // Facebook renders these as custom divs with role="combobox" or clickable labels
        const comboboxes = document.querySelectorAll('[role="combobox"], [role="listbox"]');
        for (const el of comboboxes) {
            if (!isVisible(el)) continue;
            results.push({
                tag: 'combobox',
                type: 'dropdown',
                locator_hint: getLocatorHint(el),
                aria_label: el.getAttribute('aria-label') || '',
                placeholder: '',
                label_text: el.closest('label') ? el.closest('label').textContent.trim().split('\\n')[0].substring(0, 60) : '',
                current_value: el.textContent.trim().substring(0, 60),
                is_required: el.getAttribute('aria-required') === 'true'
            });
        }
        
        // Scan for expand/accordion buttons ("More details", etc.)
        const expandButtons = [];
        const allSpans = document.querySelectorAll('span');
        for (const span of allSpans) {
            const text = span.textContent.trim();
            if (!isVisible(span)) continue;
            if (text === 'More details' || text === 'Show more' || text === 'Additional details') {
                expandButtons.push(text);
            }
        }
        
        // Scan for listing type options ("Item for sale", "Vehicle", etc.)
        const listingTypes = [];
        const allTexts = document.querySelectorAll('span, div[role="radio"], div[role="option"]');
        for (const el of allTexts) {
            const text = el.textContent.trim();
            if (!isVisible(el)) continue;
            if (['Item for sale', 'Vehicle for sale', 'Home for sale or rent', 'Home for sale', 'Home for rent'].includes(text)) {
                listingTypes.push(text);
            }
        }
        
        return {
            fields: results,
            expand_buttons: expandButtons,
            listing_types: listingTypes,
            page_title: document.title
        };
    }""")
    
    return snapshot


# ---------------------------------------------------------------------------
# 2. Gemini AI Analyzer
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a form-filling assistant for Facebook Marketplace / Buy & Sell group listing forms.

You will receive:
1. A JSON snapshot of visible form fields on the page (inputs, textareas, dropdowns, etc.)
2. The values to fill from the user's config

Your job is to:
- Map each config value to the correct form field
- Return a structured JSON action plan

RULES:
- Match fields by examining their label_text, aria_label, placeholder, and type
- "title" config value maps to fields labeled "Title", "What are you selling?", or similar
- "price" config value maps to fields labeled "Price" or similar
- "location" config value maps to fields labeled "Location" or similar (these need autocomplete handling)
- "description" config value maps to fields labeled "Description", "Describe your item", or similar
- If a "Condition" dropdown exists, include a click_then_select action for it
- If expand_buttons are present (like "More details"), include them in pre_actions
- If listing_types are present (like "Item for sale"), include the most appropriate one in pre_actions
- Only map fields that have matching config values — skip unknown fields
- Use the locator_hint from the field as the locator value. If locator_hint is null, construct one from the field attributes.

RESPONSE FORMAT (strict JSON, no markdown):
{
  "pre_actions": [
    {"action": "click_text", "text": "Item for sale", "reason": "Select listing type"},
    {"action": "click_text", "text": "More details", "reason": "Expand hidden fields"}
  ],
  "field_mappings": [
    {
      "locator": "label:has-text(\\"Title\\") input",
      "action": "fill",
      "value": "the value to fill",
      "field_name": "title"
    },
    {
      "locator": "label:has-text(\\"Price\\") input",
      "action": "fill",
      "value": "1600",
      "field_name": "price"
    },
    {
      "locator": "label:has-text(\\"Condition\\")",
      "action": "click_then_select",
      "option_text": "New",
      "field_name": "condition"
    },
    {
      "locator": "label:has-text(\\"Location\\") input",
      "action": "fill_with_autocomplete",
      "value": "Ealing",
      "field_name": "location"
    },
    {
      "locator": "label:has-text(\\"Description\\") textarea",
      "action": "fill",
      "value": "the description text",
      "field_name": "description"
    }
  ]
}

IMPORTANT: Return ONLY the JSON object. No explanations, no markdown code fences."""


def analyze_form_with_ai(form_snapshot, buy_sell_info, api_key):
    """
    Send the form snapshot + config values to Groq (Llama 3.3) and get back
    a structured action plan.
    
    Returns a dict with 'pre_actions' and 'field_mappings' keys, or None on failure.
    """
    client = Groq(api_key=api_key)
    
    user_message = f"""Here is the form snapshot from the Facebook Buy/Sell listing page:

FORM FIELDS:
{json.dumps(form_snapshot['fields'], indent=2)}

EXPAND BUTTONS VISIBLE: {json.dumps(form_snapshot.get('expand_buttons', []))}
LISTING TYPE OPTIONS VISIBLE: {json.dumps(form_snapshot.get('listing_types', []))}

CONFIG VALUES TO FILL:
- title: {json.dumps(buy_sell_info.get('title', ''))}
- price: {json.dumps(buy_sell_info.get('price', ''))}
- location: {json.dumps(buy_sell_info.get('location', ''))}
- description: {json.dumps(buy_sell_info.get('description', ''))}

Analyze the fields and return a JSON action plan to fill this form."""

    models_to_try = [
        "llama-3.3-70b-versatile",
        "llama-3.1-8b-instant",
        "mixtral-8x7b-32768"
    ]
    
    response = None
    last_error = None
    
    for model_name in models_to_try:
        try:
            response = client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_message}
                ],
                response_format={"type": "json_object"},
                temperature=0.1
            )
            break # Success!
        except Exception as e:
            last_error = e
            if "429" in str(e) or "rate_limit" in str(e).lower():
                continue
            else:
                raise e # If it's a different error, fail normally
                
    if not response:
        raise last_error

    text = response.choices[0].message.content.strip()
    
    action_plan = json.loads(text)
    return action_plan


# ---------------------------------------------------------------------------
# 3. Action Executor
# ---------------------------------------------------------------------------

def execute_form_actions(page, action_plan, image_paths, logger=None):
    """
    Execute the AI-generated action plan using Playwright.
    
    Handles:
    - click_text: Click a visible text element (for listing types, expand buttons)
    - fill: Fill an input/textarea with a value
    - fill_with_autocomplete: Fill and handle autocomplete dropdown (Location)
    - click_then_select: Click to open dropdown, then select an option (Condition)
    """
    
    def _log(msg):
        if logger:
            logger.log_line(f"  {msg}")
    
    # Execute pre-actions (listing type selection, expand buttons)
    for pre_action in action_plan.get("pre_actions", []):
        action = pre_action.get("action")
        if action == "click_text":
            text = pre_action.get("text", "")
            reason = pre_action.get("reason", "")
            try:
                loc = page.get_by_text(text, exact=False)
                if loc.count() > 0:
                    for i in range(loc.count()):
                        if loc.nth(i).is_visible():
                            loc.nth(i).scroll_into_view_if_needed()
                            loc.nth(i).click(timeout=5000)
                            _log(f"AI: Clicked '{text}' ({reason})")
                            page.wait_for_timeout(3000)
                            break
                else:
                    _log(f"AI: '{text}' not found on page, skipping")
            except Exception as e:
                _log(f"AI: Failed to click '{text}': {e}")
    
    # Execute field mappings
    for mapping in action_plan.get("field_mappings", []):
        locator_str = mapping.get("locator", "")
        action = mapping.get("action", "")
        field_name = mapping.get("field_name", "unknown")
        
        try:
            if action == "fill":
                value = mapping.get("value", "")
                el = page.locator(locator_str)
                if el.count() > 0:
                    target = el.first
                    target.scroll_into_view_if_needed()
                    target.fill(value)
                    page.wait_for_timeout(1000)
                    _log(f"AI: Filled '{field_name}' via {locator_str}")
                else:
                    _log(f"AI: Locator '{locator_str}' not found for '{field_name}'")
                    # Fallback: try aria-label or placeholder from the mapping
                    _try_fallback_fill(page, mapping, _log)
                    
            elif action == "fill_with_autocomplete":
                value = mapping.get("value", "")
                el = page.locator(locator_str)
                if el.count() > 0:
                    target = el.first
                    target.scroll_into_view_if_needed()
                    target.fill(value)
                    page.wait_for_timeout(2500)
                    page.keyboard.press("ArrowDown")
                    page.wait_for_timeout(1000)
                    page.keyboard.press("Enter")
                    page.wait_for_timeout(1000)
                    _log(f"AI: Filled '{field_name}' with autocomplete via {locator_str}")
                else:
                    _log(f"AI: Locator '{locator_str}' not found for '{field_name}'")
                    _try_fallback_fill(page, mapping, _log, autocomplete=True)
                    
            elif action == "click_then_select":
                option_text = mapping.get("option_text", "New")
                el = page.locator(locator_str)
                if el.count() > 0 and el.first.is_visible():
                    el.first.click()
                    page.wait_for_timeout(1500)
                    # Look for the option in the dropdown
                    option = page.locator(f'div[role="option"]:has-text("{option_text}")')
                    if option.count() > 0:
                        option.first.click()
                        page.wait_for_timeout(1000)
                        _log(f"AI: Selected '{option_text}' for '{field_name}'")
                    else:
                        # Try clicking any first option as fallback
                        any_option = page.locator('div[role="option"]')
                        if any_option.count() > 0:
                            any_option.first.click()
                            page.wait_for_timeout(1000)
                            _log(f"AI: Selected first option for '{field_name}' ('{option_text}' not found)")
                        else:
                            _log(f"AI: No options found for '{field_name}'")
                else:
                    _log(f"AI: Locator '{locator_str}' not found for '{field_name}'")
                    _try_fallback_dropdown(page, mapping, _log)
                    
        except Exception as e:
            _log(f"AI: Error on '{field_name}': {e}")
    
    # Upload photos using the smart file-input selector
    uploaded_photos = False
    if image_paths:
        file_inputs = page.locator('input[type="file"]')
        if file_inputs.count() > 0:
            target_input = None
            for i in range(file_inputs.count()):
                item = file_inputs.nth(i)
                try:
                    accept = item.get_attribute("accept") or ""
                    is_multiple = item.evaluate("el => el.multiple")
                    if "image" in accept and is_multiple:
                        target_input = item
                        break
                except Exception:
                    pass
            if not target_input:
                target_input = file_inputs.first
                
            try:
                is_multiple = target_input.evaluate('el => el.multiple')
            except Exception:
                is_multiple = True
                
            if is_multiple:
                target_input.set_input_files(image_paths)
            else:
                target_input.set_input_files(image_paths[:1])
            page.wait_for_timeout(7000)
            uploaded_photos = True
            _log(f"AI: Uploaded {len(image_paths)} photo(s)")
    
    return uploaded_photos


def _try_fallback_fill(page, mapping, _log, autocomplete=False):
    """
    When the AI's primary locator fails, try alternative strategies
    based on field_name.
    """
    field_name = mapping.get("field_name", "")
    value = mapping.get("value", "")
    
    fallback_selectors = {
        "title": [
            'label:has-text("Title") input',
            'input[placeholder="What are you selling?"]',
            'input[aria-label="What are you selling?"]',
            'input[placeholder="Title"]',
            'input[aria-label="Title"]',
        ],
        "price": [
            'label:has-text("Price") input',
            'input[placeholder="Price"]',
            'input[aria-label="Price"]',
        ],
        "location": [
            'label:has-text("Location") input',
            'label:has-text("Location") textarea',
            'input[placeholder="Location"]',
            'input[aria-label="Location"]',
        ],
        "description": [
            'label:has-text("Description") textarea',
            'label:has-text("Description") div[contenteditable="true"]',
            'textarea[placeholder*="Describe" i]',
            'textarea[aria-label*="Description" i]',
            'div[contenteditable="true"][aria-label*="Description" i]',
        ],
    }
    
    selectors = fallback_selectors.get(field_name, [])
    for selector in selectors:
        try:
            el = page.locator(selector)
            if el.count() > 0 and el.first.is_visible():
                if field_name == "description":
                    el.first.focus()
                    el.first.click()
                el.first.fill(value)
                page.wait_for_timeout(1000)
                if autocomplete:
                    page.wait_for_timeout(1500)
                    page.keyboard.press("ArrowDown")
                    page.wait_for_timeout(1000)
                    page.keyboard.press("Enter")
                    page.wait_for_timeout(1000)
                _log(f"AI: Fallback filled '{field_name}' via {selector}")
                return True
        except Exception:
            continue
    
    _log(f"AI: All fallbacks failed for '{field_name}'")
    return False


def _try_fallback_dropdown(page, mapping, _log):
    field_name = mapping.get("field_name", "")
    option_text = mapping.get("option_text", "New")
    
    fallback_selectors = {
        "condition": [
            'label:has-text("Condition")',
            'div:has-text("Condition") > div[role="button"]',
            'div:has-text("Condition") > div > div[role="button"]',
            'div[aria-label="Condition"]',
        ]
    }
    
    selectors = fallback_selectors.get(field_name, [])
    for selector in selectors:
        try:
            el = page.locator(selector)
            if el.count() > 0 and el.first.is_visible():
                el.first.click()
                page.wait_for_timeout(1500)
                
                option = page.locator(f'div[role="option"]:has-text("{option_text}")')
                if option.count() > 0:
                    option.first.click()
                    page.wait_for_timeout(1000)
                    _log(f"AI: Fallback selected '{option_text}' for '{field_name}' via {selector}")
                    return True
                
                any_option = page.locator('div[role="option"]')
                if any_option.count() > 0:
                    any_option.first.click()
                    page.wait_for_timeout(1000)
                    _log(f"AI: Fallback selected first option for '{field_name}' via {selector}")
                    return True
        except Exception:
            continue
            
    _log(f"AI: All dropdown fallbacks failed for '{field_name}'")
    return False


# ---------------------------------------------------------------------------
# 4. Top-Level Orchestrator
# ---------------------------------------------------------------------------

def fill_buy_sell_form_with_ai(page, buy_sell_info, image_paths, api_key, logger=None):
    """
    Top-level function to fill a Buy/Sell listing form using AI.
    
    This function:
    1. Extracts a DOM snapshot of the form
    2. Sends it to Gemini Flash for analysis
    3. Executes the returned action plan
    
    Returns a dict with:
      - success: bool
      - uploaded_photos: bool
      - error: str or None
    """
    
    def _log(msg):
        if logger:
            logger.log_line(f"  {msg}")
    
    try:
        # Step 1: Extract initial DOM snapshot
        _log("AI: Scanning form fields...")
        snapshot = extract_form_snapshot(page)
        field_count = len(snapshot.get("fields", []))
        _log(f"AI: Found {field_count} form fields")
        
        # Step 1.5: If form isn't open yet, click listing type and re-scan
        if field_count < 3 and snapshot.get("listing_types"):
            _log("AI: Form not fully visible. Selecting listing type first...")
            target_type = None
            for lt in snapshot["listing_types"]:
                if "item" in lt.lower() or "sale" in lt.lower():
                    target_type = lt
                    break
            if not target_type:
                target_type = snapshot["listing_types"][0]
                
            try:
                loc = page.get_by_text(target_type, exact=True)
                if loc.count() > 0:
                    for i in range(loc.count()):
                        if loc.nth(i).is_visible():
                            loc.nth(i).scroll_into_view_if_needed()
                            loc.nth(i).click()
                            _log(f"AI: Clicked listing type '{target_type}'")
                            page.wait_for_timeout(3000)
                            
                            # Re-take snapshot now that form is open
                            snapshot = extract_form_snapshot(page)
                            field_count = len(snapshot.get("fields", []))
                            _log(f"AI: Re-scanned and found {field_count} form fields")
                            break
            except Exception as e:
                _log(f"AI: Failed to click listing type: {e}")
        
        if field_count == 0:
            return {"success": False, "uploaded_photos": False, "error": "No form fields found on page"}
        
        # Step 2: Get AI analysis
        _log("AI: Analyzing form with Groq Llama 3.3...")
        action_plan = analyze_form_with_ai(snapshot, buy_sell_info, api_key)
        
        if not action_plan:
            return {"success": False, "uploaded_photos": False, "error": "AI returned empty action plan"}
        
        mapping_count = len(action_plan.get("field_mappings", []))
        pre_count = len(action_plan.get("pre_actions", []))
        _log(f"AI: Plan has {pre_count} pre-actions and {mapping_count} field mappings")
        
        # Step 3: Execute the action plan
        uploaded_photos = execute_form_actions(page, action_plan, image_paths, logger)
        
        return {"success": True, "uploaded_photos": uploaded_photos, "error": None}
        
    except json.JSONDecodeError as e:
        _log(f"AI: Failed to parse AI response as JSON: {e}")
        return {"success": False, "uploaded_photos": False, "error": f"JSON parse error: {e}"}
    except Exception as e:
        _log(f"AI: Error during AI form filling: {e}")
        return {"success": False, "uploaded_photos": False, "error": str(e)}
