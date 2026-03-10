"""GitHHug — Daily project idea incubator.

Scrapes trending repos/models/papers from GitHub, HuggingFace, and arXiv,
then uses LLMs to generate actionable project ideas with descriptions and
implementation handoffs. Optionally suggests improvements for your existing projects.

Usage:
  python daily_run.py              # Generate today's ideas
  python daily_run.py --dry        # Fetch trending only, no LLM call
  python daily_run.py --no-email   # Skip email digest
  python daily_run.py --force      # Regenerate even if today's output exists
"""

import argparse
import json
import os
import re
import smtplib
import sys
import textwrap
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

# Force UTF-8 on Windows
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import requests
import yaml
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

GITHHUG_ROOT = Path(__file__).parent
IDEAS_DIR = GITHHUG_ROOT / "ideas"
IDEAS_DIR.mkdir(exist_ok=True)

# Provider priority: OpenAI (gpt-5.4) > Gemini (3.1-pro-preview) > Anthropic (fallback)
PROVIDERS = [
    {
        "name": "openai",
        "model": "gpt-5.4",
        "env_key": "OPENAI_API_KEY",
        "max_tokens": 16000,
    },
    {
        "name": "gemini",
        "model": "gemini-3.1-pro-preview",
        "env_key": "GEMINI_API_KEY",
        "max_tokens": 16000,
    },
    {
        "name": "anthropic",
        "model": "claude-sonnet-4-6",
        "env_key": "ANTHROPIC_API_KEY",
        "max_tokens": 16000,
    },
]

def load_projects(config_path: Path | None = None) -> list[tuple[str, str, str]]:
    """Load project inventory from a YAML config file.

    Returns list of (name, path, description) tuples.
    If no config file exists, returns an empty list (ideas will still
    be generated, but improvement suggestions will be skipped).
    """
    if config_path is None:
        config_path = GITHHUG_ROOT / "projects.yaml"

    if not config_path.exists():
        return []

    with open(config_path, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    if not isinstance(data, list):
        return []

    return [
        (p.get("name", ""), p.get("path", ""), p.get("description", ""))
        for p in data
        if isinstance(p, dict) and p.get("name")
    ]

# ---------------------------------------------------------------------------
# Trending fetchers
# ---------------------------------------------------------------------------

def fetch_github_trending() -> list[dict]:
    """Scrape GitHub trending page for top repos."""
    repos = []
    try:
        headers = {"Accept": "text/html", "User-Agent": "GitHHug/1.0"}
        resp = requests.get("https://github.com/trending", headers=headers, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        for article in soup.select("article.Box-row")[:20]:
            name_el = article.select_one("h2 a")
            desc_el = article.select_one("p")
            stars_el = article.select_one("span.d-inline-block.float-sm-right")
            if name_el:
                name = name_el.get_text(strip=True).replace("\n", "").replace(" ", "")
                repos.append({
                    "name": name,
                    "description": desc_el.get_text(strip=True) if desc_el else "",
                    "stars_today": stars_el.get_text(strip=True) if stars_el else "",
                    "url": f"https://github.com/{name}",
                })
    except Exception as e:
        print(f"  [warn] GitHub trending fetch failed: {e}")
    return repos


def fetch_huggingface_trending_models() -> list[dict]:
    """Fetch trending models from HuggingFace API."""
    models = []
    try:
        # Try trendingScore sort first, fall back to likes
        for sort_key in ["trendingScore", "likes"]:
            resp = requests.get(
                "https://huggingface.co/api/models",
                params={"sort": sort_key, "direction": "-1", "limit": 15},
                timeout=15,
            )
            if resp.status_code == 200:
                for m in resp.json():
                    models.append({
                        "id": m.get("modelId", "") or m.get("id", ""),
                        "downloads": m.get("downloads", 0),
                        "likes": m.get("likes", 0),
                        "pipeline_tag": m.get("pipeline_tag", ""),
                    })
                break
    except Exception as e:
        print(f"  [warn] HuggingFace models fetch failed: {e}")
    return models


def fetch_huggingface_trending_papers() -> list[dict]:
    """Fetch trending papers from HuggingFace daily papers."""
    papers = []
    try:
        resp = requests.get(
            "https://huggingface.co/api/daily_papers",
            params={"limit": 15},
            timeout=15,
        )
        resp.raise_for_status()
        for p in resp.json():
            paper = p.get("paper", {})
            papers.append({
                "title": paper.get("title", ""),
                "summary": (paper.get("summary", "") or "")[:300],
                "upvotes": p.get("numUpvotes", 0),
            })
    except Exception as e:
        print(f"  [warn] HuggingFace papers fetch failed: {e}")
    return papers


def fetch_arxiv_ai() -> list[dict]:
    """Fetch recent AI papers from arXiv RSS."""
    papers = []
    try:
        import feedparser
        feed = feedparser.parse("https://rss.arxiv.org/rss/cs.AI")
        for entry in feed.entries[:15]:
            papers.append({
                "title": entry.get("title", "").replace("\n", " "),
                "summary": (entry.get("summary", "") or "")[:300],
                "link": entry.get("link", ""),
            })
    except Exception as e:
        print(f"  [warn] arXiv fetch failed: {e}")
    return papers


# ---------------------------------------------------------------------------
# LLM generation
# ---------------------------------------------------------------------------

def build_prompt(trending: dict, today: str, projects: list[tuple[str, str, str]]) -> str:
    """Build the generation prompt from trending data + project inventory."""

    gh_section = "\n".join(
        f"- **{r['name']}**: {r['description']} ({r['stars_today']})"
        for r in trending["github"]
    ) or "No data fetched."

    hf_models_section = "\n".join(
        f"- **{m['id']}**: {m['pipeline_tag']}, {m['downloads']:,} downloads, {m['likes']} likes"
        for m in trending["hf_models"]
    ) or "No data fetched."

    hf_papers_section = "\n".join(
        f"- **{p['title']}** ({p['upvotes']} upvotes): {p['summary']}"
        for p in trending["hf_papers"]
    ) or "No data fetched."

    arxiv_section = "\n".join(
        f"- **{p['title']}**: {p['summary']}"
        for p in trending["arxiv"]
    ) or "No data fetched."

    projects_section = "\n".join(
        f"- **{name}**: {desc}"
        for name, _, desc in projects
    ) if projects else "No projects configured."

    improvements_block = ""
    if projects:
        improvements_block = textwrap.dedent(f"""\
        ## Document 2: EXISTING-PROJECT-IMPROVEMENTS.md
        For each of the user's existing projects, suggest 2-4 improvements inspired by today's trending.
        Use tables with columns: Improvement, Source, Effort (Quick win/Medium/Major refactor), Priority.
        End with a "Quick Wins Summary" section.

        """)

    projects_block = ""
    if projects:
        projects_block = f"""\
    ### Your Existing Projects
    {projects_section}

    ---
"""

    improvements_json = ""
    if projects:
        improvements_json = '"improvements_md": "full markdown content for EXISTING-PROJECT-IMPROVEMENTS.md",'

    return textwrap.dedent(f"""\
    You are GitHHug, a daily project idea incubator.

    Today is {today}. Generate project ideas based on today's trending data.

    ## Document 1: IDEAS.md
    Generate exactly 10 new project ideas inspired by today's trending repos, models, and papers.
    Each idea should be:
    - Practical and buildable by one developer
    - Interesting enough to sustain motivation
    - A mix of money-making, creative, practical, and innovative vibes

    Format as a markdown file with:
    - "Sources Scanned" section listing what you found
    - A table: #, Name, One-liner, Vibe
    - Footer noting what each project folder contains

    {improvements_block}## Document {"3" if projects else "2"}: Per-idea DESCRIPTION.md
    For each of the 10 ideas, generate a detailed description with:
    - What it is, who it's for
    - Tech stack recommendation
    - Key features (5-8 bullet points)
    - Monetization angle (if applicable)
    - Why now (what trending thing makes this timely)

    ## Document {"4" if projects else "3"}: Per-idea HANDOFF.md
    For each idea, a getting-started guide with:
    - Directory structure
    - Key dependencies
    - Implementation order (what to build first)
    - Estimated time to MVP

    ---

    ## TODAY'S TRENDING DATA

    ### GitHub Trending
    {gh_section}

    ### HuggingFace Trending Models
    {hf_models_section}

    ### HuggingFace Trending Papers
    {hf_papers_section}

    ### arXiv AI (recent)
    {arxiv_section}

    ---

    {projects_block}
    ## OUTPUT FORMAT
    Return valid JSON with this structure:
    {{
      "ideas_md": "full markdown content for IDEAS.md",
      {improvements_json}
      "ideas": [
        {{
          "slug": "kebab-case-name",
          "name": "Display Name",
          "description_md": "full DESCRIPTION.md content",
          "handoff_md": "full HANDOFF.md content"
        }}
      ]
    }}

    Return ONLY the JSON, no markdown fences, no commentary.
    """)


def _call_openai(prompt: str, provider: dict) -> str:
    import openai
    client = openai.OpenAI(api_key=os.environ[provider["env_key"]])
    resp = client.chat.completions.create(
        model=provider["model"],
        max_completion_tokens=provider["max_tokens"],
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
    )
    return resp.choices[0].message.content


def _call_gemini(prompt: str, provider: dict) -> str:
    import google.generativeai as genai
    genai.configure(api_key=os.environ[provider["env_key"]])
    model = genai.GenerativeModel(provider["model"])
    resp = model.generate_content(
        prompt,
        generation_config=genai.GenerationConfig(
            max_output_tokens=provider["max_tokens"],
            response_mime_type="application/json",
        ),
    )
    return resp.text


def _call_anthropic(prompt: str, provider: dict) -> str:
    import anthropic
    client = anthropic.Anthropic(api_key=os.environ[provider["env_key"]])
    resp = client.messages.create(
        model=provider["model"],
        max_tokens=provider["max_tokens"],
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text


_CALLERS = {
    "openai": _call_openai,
    "gemini": _call_gemini,
    "anthropic": _call_anthropic,
}


def generate_ideas(trending: dict, today: str, projects: list[tuple[str, str, str]]) -> dict | None:
    """Try each provider in priority order until one succeeds."""
    prompt = build_prompt(trending, today, projects)

    for provider in PROVIDERS:
        api_key = os.environ.get(provider["env_key"], "")
        if not api_key:
            print(f"  [skip] {provider['name']}: no {provider['env_key']} set")
            continue

        caller = _CALLERS.get(provider["name"])
        if not caller:
            continue

        print(f"[2/3] Calling {provider['name']} ({provider['model']})...")
        try:
            text = caller(prompt, provider)

            # Strip markdown fences if the model wraps anyway
            text = re.sub(r'^```(?:json)?\s*\n?', '', text)
            text = re.sub(r'\n?```\s*$', '', text)

            data = json.loads(text)
            data["_provider"] = f"{provider['name']} ({provider['model']})"
            print(f"  Success via {provider['name']}")
            return data
        except json.JSONDecodeError as e:
            print(f"  [error] {provider['name']} JSON parse failed: {e}")
            err_file = IDEAS_DIR / f"{today}-raw-{provider['name']}.txt"
            err_file.write_text(text, encoding="utf-8")
            print(f"  Saved raw response to {err_file}")
            continue
        except Exception as e:
            print(f"  [error] {provider['name']} failed: {e}")
            continue

    print("  All providers failed.")
    return None


# ---------------------------------------------------------------------------
# File writer
# ---------------------------------------------------------------------------

def write_output(data: dict, today_dir: Path):
    """Write all generated files to the dated directory."""
    today_dir.mkdir(parents=True, exist_ok=True)

    # IDEAS.md
    ideas_file = today_dir / "IDEAS.md"
    ideas_file.write_text(data["ideas_md"], encoding="utf-8")
    print(f"  Wrote {ideas_file}")

    # EXISTING-PROJECT-IMPROVEMENTS.md (only if projects were configured)
    if data.get("improvements_md"):
        improvements_file = today_dir / "EXISTING-PROJECT-IMPROVEMENTS.md"
        improvements_file.write_text(data["improvements_md"], encoding="utf-8")
        print(f"  Wrote {improvements_file}")

    # Per-idea directories
    for idea in data.get("ideas", []):
        slug = idea.get("slug", "unknown")
        idea_dir = today_dir / slug
        idea_dir.mkdir(exist_ok=True)

        desc_file = idea_dir / "DESCRIPTION.md"
        desc_file.write_text(idea.get("description_md", ""), encoding="utf-8")

        handoff_file = idea_dir / "HANDOFF.md"
        handoff_file.write_text(idea.get("handoff_md", ""), encoding="utf-8")

        print(f"  Wrote {slug}/ (DESCRIPTION.md + HANDOFF.md)")

    base_files = 2 if data.get("improvements_md") else 1
    print(f"\n  Total: {base_files + len(data.get('ideas', [])) * 2} files written")


# ---------------------------------------------------------------------------
# Email notification
# ---------------------------------------------------------------------------

def send_email(data: dict, today_str: str, provider_used: str, trending: dict):
    """Send a summary email with today's ideas and improvements."""
    gmail_user = os.environ.get("GMAIL_USER", "")
    gmail_pass = os.environ.get("GMAIL_PASSWORD", "")
    email_to = os.environ.get("EMAIL_TO", "")

    if not all([gmail_user, gmail_pass, email_to]):
        print("  [skip] Email: missing GMAIL_USER, GMAIL_PASSWORD, or EMAIL_TO")
        return

    # Build the email body from the generated markdown
    ideas_md = data.get("ideas_md", "No ideas generated.")
    improvements_md = data.get("improvements_md", "No improvements generated.")

    # Trending summary
    trending_summary = (
        f"Sources: {len(trending['github'])} GitHub repos, "
        f"{len(trending['hf_models'])} HF models, "
        f"{len(trending['hf_papers'])} HF papers, "
        f"{len(trending['arxiv'])} arXiv papers"
    )

    idea_count = len(data.get("ideas", []))
    idea_names = "\n".join(
        f"  {i+1}. {idea.get('name', idea.get('slug', '?'))}"
        for i, idea in enumerate(data.get("ideas", []))
    )

    body = textwrap.dedent(f"""\
    GitHHug Daily Report — {today_str}
    Provider: {provider_used}
    {trending_summary}

    ═══════════════════════════════════════════
    {idea_count} NEW PROJECT IDEAS
    ═══════════════════════════════════════════

    {idea_names}

    ───────────────────────────────────────────
    FULL IDEAS
    ───────────────────────────────────────────

    {ideas_md}

    ───────────────────────────────────────────
    EXISTING PROJECT IMPROVEMENTS
    ───────────────────────────────────────────

    {improvements_md}

    ───────────────────────────────────────────
    Files written to: {IDEAS_DIR / today_str}
    """)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"GitHHug — {idea_count} ideas for {today_str}"
    msg["From"] = gmail_user
    msg["To"] = email_to
    msg.attach(MIMEText(body, "plain", "utf-8"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(gmail_user, gmail_pass)
            server.sendmail(gmail_user, email_to, msg.as_string())
        print(f"  Email sent to {email_to}")
    except Exception as e:
        print(f"  [warn] Email failed: {e}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        prog="githhug",
        description="Daily project idea incubator — generates ideas from trending repos, models, and papers.",
    )
    parser.add_argument("--dry", action="store_true", help="Fetch trending data only, skip LLM call")
    parser.add_argument("--no-email", action="store_true", help="Skip sending the email digest")
    parser.add_argument("--config", type=Path, default=None, help="Path to projects.yaml (default: ./projects.yaml)")
    parser.add_argument("--force", action="store_true", help="Regenerate even if today's ideas already exist")
    args = parser.parse_args()

    today = date.today()
    today_str = today.isoformat()
    today_dir = IDEAS_DIR / today_str

    if today_dir.exists() and not args.dry and not args.force:
        existing = list(today_dir.iterdir())
        if existing:
            print(f"Ideas for {today_str} already exist ({len(existing)} items). Skipping.")
            print(f"  Use --force to regenerate, or delete {today_dir}.")
            return

    # Load project inventory
    projects = load_projects(args.config)

    print(f"\n{'='*60}")
    print(f"  GitHHug — {today.strftime('%A, %B %d, %Y')}")
    print(f"{'='*60}\n")

    if projects:
        print(f"  Loaded {len(projects)} projects from config")
    else:
        print("  No projects.yaml found — skipping improvement suggestions")

    # Phase 1: Fetch trending
    print("[1/3] Fetching trending data...")
    trending = {
        "github": fetch_github_trending(),
        "hf_models": fetch_huggingface_trending_models(),
        "hf_papers": fetch_huggingface_trending_papers(),
        "arxiv": fetch_arxiv_ai(),
    }

    counts = {k: len(v) for k, v in trending.items()}
    print(f"  GitHub: {counts['github']} repos")
    print(f"  HF Models: {counts['hf_models']} models")
    print(f"  HF Papers: {counts['hf_papers']} papers")
    print(f"  arXiv: {counts['arxiv']} papers")

    if args.dry:
        print("\n  --dry flag set, stopping before LLM call.")
        print("\n  Sample trending:")
        for r in trending["github"][:5]:
            print(f"    {r['name']}: {r['description'][:80]}")
        return

    # Phase 2: Generate
    data = generate_ideas(trending, today_str, projects)
    if not data:
        print("\n  Generation failed. Check logs above.")
        sys.exit(1)

    # Phase 3: Write
    print("[3/3] Writing output...")
    write_output(data, today_dir)

    # Phase 4: Email
    if not args.no_email:
        print("[4/4] Sending email digest...")
        send_email(data, today_str, data.get("_provider", "unknown"), trending)

    print(f"\n  Done. Output: {today_dir}\n")


if __name__ == "__main__":
    main()
