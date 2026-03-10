# GitHHug

Daily project idea incubator. Scrapes what's trending across GitHub, HuggingFace, and arXiv, then uses LLMs to generate 10 actionable project ideas with full descriptions and implementation handoffs.

Optionally analyzes your existing projects and suggests improvements based on what's trending.

## What it does

Every run:

1. **Scrapes** trending repos (GitHub), models + papers (HuggingFace), and AI papers (arXiv)
2. **Generates** 10 project ideas via LLM (OpenAI, Gemini, or Anthropic — automatic fallback)
3. **Writes** structured output:
   - `IDEAS.md` — summary table of all 10 ideas
   - `EXISTING-PROJECT-IMPROVEMENTS.md` — improvement suggestions for your projects (if configured)
   - Per-idea `DESCRIPTION.md` — concept, audience, tech stack, monetization, timing
   - Per-idea `HANDOFF.md` — directory structure, deps, build order, time-to-MVP
4. **Emails** a digest (optional, via Gmail SMTP)

Output lands in `ideas/YYYY-MM-DD/` — one run per day, idempotent by default.

## Example output

```
ideas/2026-03-08/
  IDEAS.md
  EXISTING-PROJECT-IMPROVEMENTS.md
  swarm-trader/
    DESCRIPTION.md
    HANDOFF.md
  sonic-memory/
    DESCRIPTION.md
    HANDOFF.md
  doc-forge/
    ...
```

Each `DESCRIPTION.md` includes concept, target audience, tech stack, key features, monetization angle, and why-now timing. Each `HANDOFF.md` has a phased implementation plan with directory structure and dependencies.

## Setup

```bash
# Clone
git clone https://github.com/evilander/githhug.git
cd githhug

# Install
pip install -e ".[all]"
# Or just the providers you use:
pip install -e ".[openai]"

# Configure
cp .env.example .env
# Edit .env with at least one LLM API key
```

### Provider priority

GitHHug tries providers in order until one succeeds:

| Priority | Provider | Model | Env var |
|----------|----------|-------|---------|
| 1 | OpenAI | gpt-5.4 | `OPENAI_API_KEY` |
| 2 | Gemini | gemini-3.1-pro-preview | `GEMINI_API_KEY` |
| 3 | Anthropic | claude-sonnet-4-6 | `ANTHROPIC_API_KEY` |

You only need one key. Set whichever provider(s) you have access to.

### Project inventory (optional)

To get improvement suggestions for your existing projects, create a `projects.yaml`:

```bash
cp projects.yaml.example projects.yaml
# Edit with your projects
```

Format:

```yaml
- name: My Web App
  path: ~/projects/my-web-app
  description: Next.js SaaS with Stripe billing and user auth

- name: CLI Tool
  path: ~/projects/cli-tool
  description: Python CLI for batch image processing
```

### Email digest (optional)

Set these in `.env` to receive daily email summaries:

```
GMAIL_USER=you@gmail.com
GMAIL_PASSWORD=your_app_password
EMAIL_TO=you@gmail.com
```

Use a [Gmail App Password](https://support.google.com/accounts/answer/185833), not your real password.

## Usage

```bash
# Generate today's ideas
python daily_run.py

# Or via the installed entry point
githhug

# Dry run — fetch trending data only, no LLM call
githhug --dry

# Skip email
githhug --no-email

# Force regenerate (even if today's output exists)
githhug --force

# Use a custom projects config
githhug --config /path/to/projects.yaml
```

### Automation

Run daily via cron, Task Scheduler, or systemd timer:

```bash
# cron (Linux/macOS) — run at 7am daily
0 7 * * * cd /path/to/githhug && python daily_run.py

# Task Scheduler (Windows) — create a basic task pointing to:
# python B:\path\to\githhug\daily_run.py
```

## Requirements

- Python 3.11+
- At least one LLM API key (OpenAI, Gemini, or Anthropic)

## License

MIT
