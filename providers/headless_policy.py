"""What a headless Claude Code run (claude -p) may do. One place, so it can be reviewed and tested.

Every run starts in the repo with --restricted (file tools confined to the working directory,
user/project/local settings files ignored, code-running tools removed unless named), no MCP
servers, --permission-mode dontAsk (anything not allowed below is denied, never prompted), an
explicit --tools list and --allowedTools/--disallowedTools rules.

CEILING lists everything any role may ever be given. Each role gets a subset. Nothing here
reaches outside C:\\Hive-Mint: headless runs never touch the Desktop report files (plain Python
writes the daily report) and never push, install or delete.
"""

from __future__ import annotations

from dataclasses import dataclass

CLI = ".venv/Scripts/python -m app.cli"
CLI_WIN = r".venv\Scripts\python -m app.cli"
PYTEST = ".venv/Scripts/python -m pytest"
PYTEST_WIN = r".venv\Scripts\python -m pytest"

# Built-in tools a role may have (--tools).
CEILING_TOOLS = frozenset({"WebSearch", "WebFetch", "Read", "Glob", "Grep", "Write", "Edit", "Bash"})

# Allow rules a role may have (--allowedTools). Paths are relative to the repo (the working directory).
CEILING_ALLOW = frozenset({
    "WebSearch", "WebFetch", "Read", "Glob", "Grep",
    "Edit(./research/**)", "Write(./research/**)",
    "Edit(./products/**)", "Write(./products/**)",
    f"Bash({CLI} *)", f"Bash({CLI_WIN} *)",
    f"Bash({PYTEST} *)", f"Bash({PYTEST_WIN} *)",
})

# Denied for every role, even if an allow rule would match (deny wins).
DENY = (
    "Bash(git *)", "Bash(pip *)", "Bash(python -m pip *)", "Bash(npm *)", "Bash(npx *)", "Bash(winget *)",
    "Bash(choco *)", "Bash(curl *)", "Bash(wget *)", "Bash(rm *)", "Bash(del *)", "Bash(rmdir *)",
    "Bash(Remove-Item *)", "Bash(powershell *)", "Bash(pwsh *)", "Bash(cmd *)", "Bash(*app.cli shift*)",
    "Bash(*approve-rework*)", "Bash(*review-import*)", "PowerShell",
    "Edit(./.claude/**)", "Write(./.claude/**)", "Edit(./.git/**)", "Write(./.git/**)", "Edit(./.env*)",
    "Write(./.env*)", "Edit(./data/**)", "Write(./data/**)", "Edit(./app/**)", "Write(./app/**)",
    "Edit(./agents/**)", "Write(./agents/**)", "Edit(./providers/**)", "Write(./providers/**)",
    "Edit(./config/**)", "Write(./config/**)", "Edit(./tools/**)", "Write(./tools/**)", "Edit(./tests/**)",
    "Write(./tests/**)",
)


@dataclass(frozen=True)
class Role:
    name: str
    tools: tuple[str, ...]
    allow: tuple[str, ...]

    def __post_init__(self) -> None:
        if not set(self.tools) <= CEILING_TOOLS:
            raise ValueError(f"{self.name}: tools outside the ceiling: {set(self.tools) - CEILING_TOOLS}")
        if not set(self.allow) <= CEILING_ALLOW:
            raise ValueError(f"{self.name}: allow rules outside the ceiling: {set(self.allow) - CEILING_ALLOW}")


RESEARCH = Role("research", tools=("WebSearch", "WebFetch"), allow=("WebSearch", "WebFetch"))

# The spec writer drafts a spec file under products/drafts/ and checks it with the project CLI
# (python -m app.cli spec-check <file>), which builds it and runs Testing, before it answers.
SPEC = Role("spec", tools=("Read", "Glob", "Grep", "Write", "Edit", "Bash"),
            allow=("Read", "Glob", "Grep", "Edit(./products/**)", "Write(./products/**)",
                   f"Bash({CLI} *)", f"Bash({CLI_WIN} *)"))

# The Red Team reads the packet, chart PNGs and PDFs, and writes verdict.json; Python imports it.
RED_TEAM = Role("red_team", tools=("Read", "Glob", "Grep", "Write"),
                allow=("Read", "Glob", "Grep", "Write(./research/**)", "Edit(./research/**)"))

# SEO reads the build's files and writes nothing: Python validates the listing and writes the package.
SEO = Role("seo", tools=("Read", "Glob", "Grep"), allow=("Read", "Glob", "Grep"))

# The owner's desk opens the pictures and files and answers with a verdict; it writes nothing.
OWNER_DESK = Role("owner_desk", tools=("Read", "Glob", "Grep"), allow=("Read", "Glob", "Grep"))

ROLES = {r.name: r for r in (RESEARCH, SPEC, RED_TEAM, SEO, OWNER_DESK)}

# Environment variables that would make claude bill pay-per-use instead of the subscription.
BILLING_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
               "CLAUDE_CODE_USE_FOUNDRY", "ANTHROPIC_BASE_URL")

# claude auth status --json must show exactly this before any run.
REQUIRED_AUTH = {"loggedIn": True, "authMethod": "claude.ai", "subscriptionType": "max", "apiProvider": "firstParty"}
