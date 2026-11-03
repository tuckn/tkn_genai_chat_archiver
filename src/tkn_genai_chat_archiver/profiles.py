"""Built-in acquisition scopes. They select folders, not file extensions."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Profile:
    name: str
    software: str
    default_source_root: str
    include: tuple[str, ...]
    exclude: tuple[str, ...]
    description: str
    # Files the app replaces atomically (write elsewhere, then rename); no settle wait is needed.
    settle_exempt: tuple[str, ...] = ()
    # Several named source folders; relative paths then start with the folder name.
    default_source_roots: tuple[tuple[str, str], ...] = ()
    # Selected only when another selected file lives in the same parent folder.
    companion_include: tuple[str, ...] = ()


PROFILES: dict[str, Profile] = {
    "codex": Profile(
        name="codex",
        software="Codex",
        default_source_root="~/.codex",
        include=(
            "sessions/**",
            "archived_sessions/**",
            "session_index.jsonl",
            ".codex-global-state.json",
            "history.jsonl",
        ),
        exclude=("**/*.ffs_db", "**/*.lock"),
        description="Codex CLI and desktop app rollouts, including files Codex compressed to .jsonl.zst",
        settle_exempt=(".codex-global-state.json",),
    ),
    "claude-code": Profile(
        name="claude-code",
        software="Claude Code",
        default_source_root="~/.claude",
        include=("projects/**", "history.jsonl"),
        exclude=("**/*.lock",),
        description="Claude Code project folders: transcripts, subagents, tool results, titles and memory",
    ),
    "antigravity": Profile(
        name="antigravity",
        software="Antigravity",
        default_source_root="~/.gemini",
        include=(
            "antigravity*/brain/**",
            "antigravity*/conversations/**",
            "antigravity*/conversation_summaries.db",
            "antigravity*/annotations/**",
            "antigravity*/implicit/**",
            "antigravity*/knowledge/**",
            "antigravity*/*.pb",
            "antigravity*/*.pbtxt",
            "antigravity*/history.jsonl",
        ),
        exclude=("**/*.lock", "**/presence/**", "**/*-wal", "**/*-shm", "**/*-journal"),
        description="Antigravity IDE and CLI conversations, including per-conversation SQLite databases",
    ),
    "github-copilot": Profile(
        name="github-copilot",
        software="GitHub Copilot",
        default_source_root="",
        default_source_roots=(("vscode", "%APPDATA%/Code/User"), ("cli", "~/.copilot")),
        include=(
            "vscode*/workspaceStorage/*/chatSessions/**",
            "vscode*/workspaceStorage/*/chatEditingSessions/**",
            "vscode*/workspaceStorage/*/GitHub.copilot-chat/transcripts/**",
            "vscode*/workspaceStorage/*/GitHub.copilot-chat/chat-session-resources/**",
            "vscode*/workspaceStorage/*/GitHub.copilot-chat/debug-logs/**",
            "vscode*/workspaceStorage/*/workspace.json",
            "vscode*/globalStorage/emptyWindowChatSessions/**",
            "vscode*/globalStorage/github.copilot-chat/session-store.db",
            "cli/session-state/**",
            "cli/history-session-state/**",
            "cli/session-store.db",
            "cli/data.db",
            "cli/attachments/**",
            "cli/chats/**",
            "cli/command-history-state.json",
            "cli/sidebar-sessions-state/**",
            "cli/vscode.session.metadata.cache.json",
        ),
        exclude=(
            "**/*.lock",
            "**/.session-operation-locks/**",
            "**/debug-logs/*/models.json",
            "**/*-wal",
            "**/*-shm",
            "**/*-journal",
        ),
        description="GitHub Copilot Chat in VS Code and GitHub Copilot CLI",
        companion_include=("vscode*/workspaceStorage/*/workspace.json",),
    ),
    "vscode-chat": Profile(
        name="vscode-chat",
        software="Visual Studio Code",
        default_source_root="%APPDATA%/Code/User",
        include=(
            "workspaceStorage/*/chatSessions/**",
            "workspaceStorage/*/chatEditingSessions/**",
            "workspaceStorage/*/workspace.json",
            "globalStorage/emptyWindowChatSessions/**",
        ),
        exclude=("**/*.lock",),
        description="VS Code chat sessions only; superseded by github-copilot, kept for existing archives",
    ),
}
