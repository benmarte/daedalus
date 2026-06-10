"""
Default configuration values for all projects.

These defaults are inherited by every project unless overridden.
"""

DEFAULTS = {
    "model": {
        "provider": "anthropic",       # anthropic, openai, google, custom
        "name": "claude-sonnet-4-6",
        "temperature": 0.2,
        "max_tokens": 4096,
    },
    "delivery": {
        "channel": "slack",            # slack:C0B7PS0BTT7, discord:999888777, etc.
    },
    "cron": {
        "schedule": "every 60m",             # every 60m, every 2h, 0 9 * * *, ISO timestamp
    },
    "issues": {
        "filters": {
            "state": "open",           # open, closed, all
            "limit": 20,
            "labels": [],
            "exclude_labels": [],
            "title_prefixes": [],
            "exclude_title_prefixes": [],
            "search_terms": [],
            "assignees": [None],
        },
        "skip_patterns": ["duplicate", "wontfix"],
        "thresholds": {
            "max_body_chars": 800,
            "enhancement_body_chars": 300,
        },
        "retry": {
            "max_attempts": 2,
            "backoff_seconds": 300,
        },
        "processing": {
            "max_issues_per_run": 20,
            "sequential": True,
            "branch_prefix": "fix",
            "commit_prefix": "fix:",
            "large_issue_label": "backlog",
        },
        "dedup": {
            "enabled": True,
            "strategy": "number",       # number (GitHub issue # + repo), content (title + body hash)
            "similarity_threshold": 0.8,
        },
        "priority": {
            "enabled": True,
            "order": "desc",            # desc (high first), asc, none
            "label_map": {
                "priority: critical": 1,
                "priority: high": 2,
                "priority: medium": 3,
                "priority: low": 4,
            },
        },
    },
    "lifecycle": {
        "kanban": {
            "enabled": True,
            "idempotency_prefix": "github-issue-",
        },
        "auto_close": True,
        "manual_close": False,
        "phases": [
            {"name": "Triage", "order": 0, "auto_advance": False},
            {"name": "Spec", "order": 1, "auto_advance": True},
            {"name": "Plan", "order": 2, "auto_advance": True},
            {"name": "Build", "order": 3, "auto_advance": False},
            {"name": "Test", "order": 4, "auto_advance": False},
            {"name": "Review", "order": 5, "auto_advance": False},
            {"name": "Simplify", "order": 6, "auto_advance": True},
            {"name": "Ship", "order": 7, "auto_advance": False},
        ],
    },
    "execution": {
        "phase_timeout_seconds": 600,   # Max seconds per phase (0 = no limit)
        "phase_retry_count": 2,         # Retries per phase before failing
        "phase_retry_delay_seconds": 30,  # Delay between phase retries
        "issue_timeout_seconds": 3600,  # Max seconds per issue across all phases
    },
    "vcs": {
        "target_branch": "dev",
        "branch_prefix": "fix",
        "commit_prefix": "fix:",
        "pr_title_prefix": "fix:",
        "pr_labels": [],
        "track_pr_status": True,        # Poll PR status after creation
    },
    "sources": {
        "github": {
            "enabled": True,
            "token_env": "GH_TOKEN",
        },
        "gitlab": {
            "enabled": False,
        },
        "jira": {
            "enabled": False,
        },
        "linear": {
            "enabled": False,
        },
    },
    "webhook": {
        "enabled": False,
        "secret": "",                   # HMAC secret for GitHub webhooks
        "auto_trigger": True,           # Trigger lifecycle on push/PR events
    },
    "notification": {
        "deliver": "slack",
        "channels": {
            "default": "slack",
            "on_failure": "slack",
        },
    },
    "platform": {
        "name": "github",
    },
    "stats": {
        "enabled": True,
        "output_dir": ".hermes/daedalus-stats/",
    },
    "tech_stack": "auto",
}
