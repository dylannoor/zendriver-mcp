# Zendriver MCP - LLM Instructions

## How to Use These Tools Effectively

This guide helps LLMs (like Claude) use Zendriver MCP for browser automation.

---

## IMPORTANT: Tool Usage Order

**Always follow this sequence:**

1. **Start browser first**: Call `start_browser` before any other tool
2. **Navigate to page**: Call `navigate` with the target URL
3. **Wait for load**: Use `wait` (1-2 seconds) after navigation
4. **Then interact**: Now you can click, type, screenshot, etc.
5. **Stop when done**: Call `stop_browser` to clean up

### Example Flow:
```
1. start_browser()
2. navigate("https://example.com")
3. wait(2)
4. screenshot()  # or other actions
5. stop_browser()
```

---

## Finding Elements - Best Practices

### Interaction Tree (Fastest Overview)
`get_interaction_tree()` returns a compact JSON list of interactive
elements (max 150 by default, raise `limit` for more), each with a
numeric `id`. Those ids double as selectors: `click(selector="12")`,
`type_text(text="hi", selector="7")`.

### CSS Selectors

| Pattern | Example | When to Use |
|---------|---------|-------------|
| By ID | `#login-button` | When element has unique ID |
| By class | `.submit-btn` | When element has distinctive class |
| By tag + class | `button.primary` | When multiple elements share class |
| By attribute | `input[type="email"]` | For form inputs |
| By data attribute | `[data-testid="submit"]` | For testing-friendly sites |
| Nested | `form#login input[type="password"]` | When you need specificity |

### Finding by Text (When Selectors Fail)
```
click(text="Sign In")
find_element(text="Welcome")
```

### Accessibility Tree (Stable uids)
```
get_accessibility_snapshot()   # tree with uids like "ax_1b2c"
click_by_uid(uid="ax_1b2c")    # deterministic click
describe_uid(uid="ax_1b2c")    # inspect before clicking
```

### Strategy When Element Not Found:
1. First try: `find_element(selector="...")` to check if it exists
2. If not found: `get_interaction_tree()` or `get_text_content()` to see the page
3. Try broader: `find_all_elements(selector="button")`, `find_buttons()`, `find_inputs()`

---

## Common Tasks - Step by Step

### Login to a Website
```
1. start_browser()
2. navigate("https://site.com/login")
3. wait(2)
4. type_text(text="username", selector="input[name='username']")
5. type_text(text="password", selector="input[name='password']")
6. click(selector="button[type='submit']")
7. wait(3)
8. screenshot()  # verify login worked
```

### Fill a Form
```
1. Use fill_form with JSON:
   fill_form('{"#name": "John", "#email": "john@test.com", "#message": "Hello"}')

2. Or fill individually:
   type_text(text="John", selector="#name")
   type_text(text="john@test.com", selector="#email")
```

### Scrape Data (Paginated)
`get_content` and `get_text_content` return 10,000 chars per call.
The first line reports the slice and total, e.g.
`[chars 0-10000 of 54321] (next: offset=10000)` - pass that offset to
continue, or raise `max_chars` when you really need more at once.
```
1. navigate to page
2. get_text_content()                      # visible text, first 10k chars
3. get_text_content(offset=10000)          # next slice if needed
4. get_content(max_chars=30000)            # full HTML, bigger slice
5. OR execute_js('[...document.querySelectorAll(".item")].map(e => e.innerText)')
```

### Structured Extraction
```
get_links(filter_text="pdf")              # text -> href, deduplicated
extract_table(selector="#prices", format="markdown")
extract_structured_data()                 # JSON-LD, OpenGraph, meta tags
get_network_response_body(url_pattern="/api/products")   # raw API response
download_file(url="https://site.com/report.pdf", save_path="~/report.pdf")
```
`download_file` is an in-page fetch, so navigate to the file's site first.

### Take Screenshots
```
screenshot()                              # returned image, downscaled to max 1024px wide
screenshot(save_path="output.png")        # also save to disk at full resolution
screenshot(full_resolution=True)          # return without downscaling (costs more tokens)
screenshot_element(selector="#chart")     # just one element
```

### Shadow DOM (Custom Elements)
```
describe_shadow(selector="nes-button")    # dump nested shadow tree
click_shadow(selector="nes-button")       # click the real inner control
human_click(text="Verstuur")              # shadow-aware humanlike click
```

---

## Security Testing

```
1. start_browser()
2. navigate("https://target-site.com")
3. wait(2)
4. run_security_audit()  # headers, forms, mixed content, exposed data
```

---

## Handling Dynamic Content

### Wait for Elements
```
wait_for_element(selector="#loaded-content", timeout=10)
wait_for_text(text="Order confirmed")     # case-insensitive, sees shadow DOM
wait_for_network()                        # wait until network goes idle
wait_for_request(url_pattern="/api/")     # wait for a specific call to complete
```

### Handle SPAs (Single Page Apps)
```
1. navigate to page
2. wait(3)  # SPAs need more time
3. scroll(direction="down", pixels=500)  # trigger lazy loading
4. wait(1)
5. Now interact with content
```

### Infinite Scroll Pages
```
for i in range(5):
    scroll(direction="down", pixels=1000)
    wait(1)
get_content()  # now has more content
```

### Popups and Overlays in the Way
```
hide_element(selector=".cookie-banner")   # display:none
remove_element(selector="#newsletter-modal")
highlight_element(selector="#target")     # outline it, then screenshot() to check
```

---

## Record and Replay

```
1. start_recording()
2. navigate(...), click(...), type_text(...), wait_for_text(...)
3. stop_recording()                       # returns the steps as JSON
4. replay_action_sequence(sequence="[...]")   # stops at the first failing step
```
Record `wait_for_element` / `wait_for_text` between steps so the replay
waits the same way. Numeric ids only replay if `get_interaction_tree`
is part of the recording.

---

## Multi-Tab Operations

```
1. start_browser()
2. navigate("https://site1.com")    # first tab
3. new_tab("https://site2.com")     # second tab
4. list_tabs()                      # see all tabs + ids
5. switch_tab(tab_id="...")         # go back
6. close_tab(tab_id="...")          # close one
```

---

## JavaScript Execution

Do NOT use `return` statements - the script is wrapped automatically
and the last expression is the result.

### Simple Queries
```
execute_js('document.title')
execute_js('document.querySelectorAll("a").length')
```

### Extract Data
```
execute_js('''
    Array.from(document.querySelectorAll('.product')).map(p => ({
        name: p.querySelector('.name')?.innerText,
        price: p.querySelector('.price')?.innerText
    }))
''')
```

---

## Troubleshooting

### Element Not Found
1. Check if page loaded: `get_page_info()`
2. See page structure: `get_interaction_tree()` or `get_text_content()`
3. Try finding all similar: `find_all_elements(selector="button")`
4. Wait longer: `wait(3)` then retry
5. Try text search: `find_element(text="Button Text")`

### Page Not Loading
1. Check URL: `get_page_info()`
2. Take screenshot: `screenshot()`
3. Check for errors: `get_console_logs()`
4. Cloudflare wall? `is_cloudflare_challenge_present()` then `bypass_cloudflare()`
5. Try reload: `reload_page()`

### Clicks Not Working
1. Scroll to element: `scroll_to_element(selector="...")`
2. Wait after scroll: `wait(0.5)`
3. Custom element? `describe_shadow(...)` then `click_shadow(...)`
4. Try humanlike: `human_click(selector="...")`
5. Last resort by coordinates: `mouse_click(x, y)`

---

## Best Practices

1. **Always start/stop browser** - Don't leave browsers running
2. **Use waits after navigation** - Pages need time to load
3. **Prefer get_interaction_tree over get_content** - Far fewer tokens for interaction work
4. **Take screenshots for verification** - Visual confirmation helps
5. **Be specific with selectors** - Avoid ambiguous matches
6. **Use headless for speed** - `start_browser(headless=True)` when no visual needed
7. **Clean up storage if needed** - `clear_storage()` between tests
