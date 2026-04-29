"""
feibot - A lightweight AI agent framework
"""

__logo__ = "🐈"
_base_version = "0.1.4"


def _get_git_version() -> str:
    """Get version from git: v{base}-dev+{commit_hash}"""
    import subprocess
    import os

    try:
        # Check if inside git repo
        git_dir = os.path.join(os.path.dirname(__file__), "..", ".git")
        if not os.path.exists(git_dir):
            return _base_version

        # Get short commit hash
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            cwd=os.path.dirname(__file__),
        )
        if result.returncode != 0:
            return _base_version

        commit_hash = result.stdout.strip()[:7]
        return f"{_base_version}-dev+{commit_hash}"
    except Exception:
        return _base_version


__version__ = _get_git_version()
