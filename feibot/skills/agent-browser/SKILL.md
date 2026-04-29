---
name: agent-browser
description: Browser automation CLI wrapper for AI agents. Use when the user needs to automate web browser tasks like navigating websites, taking screenshots, filling forms, clicking elements, extracting page content, or performing web scraping. Supports headless browser operations via agent-browser CLI.
---

# Agent Browser Skill

This skill provides browser automation capabilities using the `agent-browser` CLI tool from Vercel Labs.

## Prerequisites

The `agent-browser` CLI must be installed:

```bash
# Global installation (recommended)
npm install -g agent-browser
agent-browser install  # Download Chromium

# Or via Homebrew on macOS
brew install agent-browser
agent-browser install
```

## Usage

Use the `exec` tool to run `agent-browser` commands. Common workflows:

### Basic Navigation

```bash
# Open a URL
agent-browser open example.com

# Take a screenshot
agent-browser screenshot page.png

# Get page snapshot (accessibility tree with refs)
agent-browser snapshot
```

### Interacting with Elements

```bash
# Click an element (using snapshot ref like @e2)
agent-browser click @e2

# Fill a form field
agent-browser fill @e3 "test@example.com"

# Type text
agent-browser type @e4 "search query"

# Get element text
agent-browser get text @e1
```

### Using Traditional Selectors

```bash
# Click by CSS selector
agent-browser click "#submit"

# Fill by selector
agent-browser fill "#email" "test@example.com"
```

### Semantic Locators (Find)

```bash
# Find by role and click
agent-browser find role button click --name "Submit"

# Find by text and click
agent-browser find text "Sign In" click

# Find by label and fill
agent-browser find label "Email" fill "test@test.com"
```

### Screenshots and PDFs

```bash
# Screenshot current viewport
agent-browser screenshot screenshot.png

# Full page screenshot
agent-browser screenshot --full fullpage.png

# Annotated screenshot with element labels
agent-browser screenshot --annotate annotated.png

# Save as PDF
agent-browser pdf output.pdf
```

### Waiting

```bash
# Wait for element
agent-browser wait "#loading"

# Wait for time (milliseconds)
agent-browser wait 1000

# Wait for text to appear
agent-browser wait --text "Welcome"

# Wait for network idle
agent-browser wait --load networkidle
```

### JavaScript Evaluation

```bash
# Run JavaScript
agent-browser eval "document.title"

# Get data as base64
agent-browser eval -b "JSON.stringify(document.body.innerText)"
```

### Session Management

```bash
# Close browser when done
agent-browser close
```

## Python Helper Script

For complex automation workflows, use the provided helper script:

```bash
python scripts/agent_browser_helper.py --help
```

## Best Practices

1. **Always close the browser** when done: `agent-browser close`
2. **Use snapshot** to get element references for reliable interactions
3. **Use semantic locators** (`find role`, `find text`) for more robust automation
4. **Add waits** after navigation or before interacting with dynamic content
5. **Use annotated screenshots** to debug element selection issues

## Cloud Browser Providers (Optional)

For environments without local browser support:

```bash
# Browserbase
export BROWSERBASE_API_KEY="your-api-key"
export BROWSERBASE_PROJECT_ID="your-project-id"
agent-browser -p browserbase open https://example.com

# Browser Use
export BROWSER_USE_API_KEY="your-api-key"
agent-browser -p browseruse open https://example.com

# Kernel
export KERNEL_API_KEY="your-api-key"
agent-browser -p kernel open https://example.com
```
