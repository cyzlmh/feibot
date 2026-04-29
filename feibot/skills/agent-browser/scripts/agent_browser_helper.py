#!/usr/bin/env python3
"""
Helper script for agent-browser CLI automation.
Provides a Pythonic interface to common browser automation tasks.
"""

import subprocess
import argparse
import json
import sys
from typing import Optional, List, Dict, Any


class AgentBrowser:
    """Python wrapper for agent-browser CLI."""

    def __init__(self, provider: Optional[str] = None):
        self.provider = provider
        self._check_installation()

    def _check_installation(self):
        """Verify agent-browser is installed."""
        try:
            subprocess.run(
                ["agent-browser", "--version"],
                capture_output=True,
                check=True
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            print("Error: agent-browser is not installed.", file=sys.stderr)
            print("Install with: npm install -g agent-browser", file=sys.stderr)
            sys.exit(1)

    def _run(self, *args: str) -> str:
        """Run an agent-browser command and return output."""
        cmd = ["agent-browser"]
        if self.provider:
            cmd.extend(["-p", self.provider])
        cmd.extend(args)

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"Command failed: {result.stderr}")
        return result.stdout

    def open(self, url: str) -> str:
        """Navigate to a URL."""
        return self._run("open", url)

    def snapshot(self) -> str:
        """Get accessibility tree with element references."""
        return self._run("snapshot")

    def click(self, selector: str, new_tab: bool = False) -> str:
        """Click an element by ref or selector."""
        args = ["click", selector]
        if new_tab:
            args.append("--new-tab")
        return self._run(*args)

    def fill(self, selector: str, text: str) -> str:
        """Clear and fill an input field."""
        return self._run("fill", selector, text)

    def type(self, selector: str, text: str) -> str:
        """Type text into an element."""
        return self._run("type", selector, text)

    def get_text(self, selector: str) -> str:
        """Get text content of an element."""
        return self._run("get", "text", selector)

    def get_html(self, selector: str) -> str:
        """Get innerHTML of an element."""
        return self._run("get", "html", selector)

    def get_title(self) -> str:
        """Get page title."""
        return self._run("get", "title")

    def get_url(self) -> str:
        """Get current URL."""
        return self._run("get", "url")

    def screenshot(self, path: str, full: bool = False, annotate: bool = False) -> str:
        """Take a screenshot."""
        args = ["screenshot", path]
        if full:
            args.append("--full")
        if annotate:
            args.append("--annotate")
        return self._run(*args)

    def pdf(self, path: str) -> str:
        """Save page as PDF."""
        return self._run("pdf", path)

    def wait(self, selector_or_ms: str, for_text: Optional[str] = None,
             for_url: Optional[str] = None, load_state: Optional[str] = None) -> str:
        """Wait for element, time, text, URL, or load state."""
        args = ["wait", selector_or_ms]
        if for_text:
            args.extend(["--text", for_text])
        if for_url:
            args.extend(["--url", for_url])
        if load_state:
            args.extend(["--load", load_state])
        return self._run(*args)

    def eval(self, js: str, base64_output: bool = False) -> str:
        """Execute JavaScript."""
        args = ["eval"]
        if base64_output:
            args.append("-b")
        args.append(js)
        return self._run(*args)

    def find_role(self, role: str, action: str, name: Optional[str] = None,
                  value: Optional[str] = None) -> str:
        """Find element by ARIA role and perform action."""
        args = ["find", "role", role, action]
        if name:
            args.extend(["--name", name])
        if value:
            args.append(value)
        return self._run(*args)

    def find_text(self, text: str, action: str, exact: bool = False) -> str:
        """Find element by text and perform action."""
        args = ["find", "text", text, action]
        if exact:
            args.append("--exact")
        return self._run(*args)

    def scroll(self, direction: str, pixels: int = 100, selector: Optional[str] = None) -> str:
        """Scroll page or element."""
        args = ["scroll", direction, str(pixels)]
        if selector:
            args.extend(["--selector", selector])
        return self._run(*args)

    def close(self) -> str:
        """Close the browser."""
        return self._run("close")


def demo():
    """Demo script showing basic usage."""
    browser = AgentBrowser()

    try:
        # Open a page
        print("Opening example.com...")
        browser.open("example.com")

        # Get snapshot
        print("\nPage snapshot:")
        snapshot = browser.snapshot()
        print(snapshot[:500] + "..." if len(snapshot) > 500 else snapshot)

        # Get title and URL
        print(f"\nTitle: {browser.get_title()}")
        print(f"URL: {browser.get_url()}")

        # Take screenshot
        print("\nTaking screenshot...")
        browser.screenshot("/tmp/demo.png")
        print("Screenshot saved to /tmp/demo.png")

    finally:
        browser.close()
        print("\nBrowser closed.")


def main():
    parser = argparse.ArgumentParser(description="Agent Browser Helper")
    parser.add_argument("--demo", action="store_true", help="Run demo script")
    parser.add_argument("--provider", help="Cloud provider (browserbase/browseruse/kernel)")
    parser.add_argument("command", nargs="?", help="Command to run")
    parser.add_argument("args", nargs="*", help="Command arguments")

    args = parser.parse_args()

    if args.demo:
        demo()
        return

    if args.command:
        browser = AgentBrowser(provider=args.provider)
        result = browser._run(args.command, *args.args)
        print(result)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
