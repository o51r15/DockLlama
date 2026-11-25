"""Build the GitHub wiki from docs/ and the README, so the two never drift apart.

    git clone https://github.com/o51r15/DockLlama.wiki.git ../DockLlama.wiki
    python docs/make_wiki.py ../DockLlama.wiki
    cd ../DockLlama.wiki && git add -A && git commit -m "Wiki for <version>" && git push

Pages written here are overwritten each time; pages starting with "v1-" (the original version's
docs) are left alone.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
REPO = "https://github.com/o51r15/DockLlama"

# docs file -> wiki page name, in sidebar order
PAGES = {
    "setup.md": "Installation",
    "ai-and-models.md": "AI-and-Models",
    "how-checks-work.md": "How-Checks-Work",
    "notifications.md": "Notifications",
    "restarts.md": "Restarts",
    "other-machines.md": "Other-Machines",
    "backup.md": "Backup-and-Restore",
    "upgrading-from-v1.md": "Upgrading-from-v1",
    "troubleshooting.md": "Troubleshooting",
}
TITLES = {
    "Installation": "Installation", "AI-and-Models": "AI and models", "How-Checks-Work": "How checks work",
    "Notifications": "Notifications", "Restarts": "Restarts and quiet hours", "Other-Machines": "Watching other machines",
    "Backup-and-Restore": "Backup and restore", "Upgrading-from-v1": "Upgrading from v1",
    "Troubleshooting": "Troubleshooting", "FAQ": "FAQ",
}
V1_PAGES = ["v1-Overview", "v1-Installation", "v1-Configuration", "v1-Web-UI", "v1-AI-Engine", "v1-Models",
            "v1-Notifications", "v1-API-Reference", "v1-FAQ", "v1-Troubleshooting"]

FOOTER = (f"\n\n---\n*These pages are built from [`docs/`]({REPO}/tree/v2/docs) in the repository "
          f"for DockLlama {{version}}.*\n")


def version() -> str:
    m = re.search(r'__version__ = "([^"]+)"', (ROOT / "dockllama" / "__init__.py").read_text())
    return m.group(1) if m else "2"


def fix_links(text: str) -> str:
    """Turn links between docs files into links between wiki pages."""
    def repl(m: re.Match) -> str:
        label, target = m.group(1), m.group(2)
        path, _, anchor = target.partition("#")
        name = Path(path).name
        if path.endswith("README.md"):
            page = "Installation" if anchor == "install" else "Home"
            return f"[{label}]({page}{'#' + anchor if anchor and page != 'Installation' else ''})"
        if name in PAGES:
            return f"[{label}]({PAGES[name]}{'#' + anchor if anchor else ''})"
        if path.startswith("../../"):  # GitHub-relative (branches, tags) from the README
            return f"[{label}]({REPO}/{path[len('../../'):]}{'#' + anchor if anchor else ''})"
        if name == "releasing.md":
            return f"[{label}]({REPO}/blob/v2/docs/releasing.md)"
        return m.group(0)
    return re.sub(r"\[([^\]]+)\]\(((?!https?://|#|mailto:)[^)\s]+)\)", repl, text)


def section(readme: str, heading: str) -> str:
    """One '## heading' section of the README, without the heading line."""
    m = re.search(rf"^## {re.escape(heading)}\n(.*?)(?=^## |\Z)", readme, re.S | re.M)
    return m.group(1).strip() if m else ""


def home(readme: str) -> str:
    intro = readme.split("## Install", 1)[0]
    intro = re.sub(r"^<p align=\"center\">.*?</p>\n+", "", intro, flags=re.S)
    intro = intro.replace("# DockLlama", "# DockLlama 2", 1)
    pages = "\n".join(f"- [{TITLES[p]}]({p})" for p in [*PAGES.values(), "FAQ"])
    return (f"<p align=\"center\"><img src=\"{REPO}/raw/v2/assets/banner.svg\" alt=\"DockLlama\" width=\"600\"></p>\n\n"
            f"{intro.strip()}\n\n**Install in two minutes:** see [Installation](Installation).\n\n"
            f"## What you get\n\n{section(readme, 'What you get')}\n\n## Pages\n\n{pages}\n\n"
            f"Running the original version? Its pages are kept under **v1 (original)** in the sidebar.\n")


def installation(readme: str, setup: str) -> str:
    setup_body = setup.split("\n", 1)[1].strip()  # drop "# Setup in detail"
    return (f"# Installation\n\n## Install\n\n{section(readme, 'Install')}\n\n"
            f"## The setup guide\n\n{section(readme, 'The setup guide')}\n\n"
            f"## Forgot your password?\n\n{section(readme, 'Forgot your password?')}\n\n"
            f"# Setup in detail\n\n{setup_body}\n")


FAQ = """# FAQ

## What does DockLlama actually do?

It watches your Docker containers and tells you in plain words which ones need attention and what to do. First it
reads Docker's own facts (is it running, did it crash, is it restarting in a loop, was it killed for using too much
memory, is its health check failing). Then, if you have Ollama, a local AI reads the container's new log lines and
explains what's going on. You get a message when something breaks and when it recovers.

## How is this different from Uptime Kuma or Portainer?

Uptime monitors check that an address answers; Portainer shows you containers and lets you manage them. DockLlama
explains *what* is wrong and *what to do*, from the logs and Docker's facts, and shows the evidence. Its
[web page checks](How-Checks-Work#web-page-check) only use local addresses, so a Cloudflare error page can't make a
frozen app look fine.

## Do my logs leave my network?

No. The AI runs on your own Ollama server. Passwords and tokens in log lines are masked before anything is stored,
shown or sent. The only things that leave are the notifications you set up, to the services you choose.

## Do I need a GPU, or even AI?

No to both. Without AI, DockLlama still catches crashes, restart loops, out-of-memory kills, failing health checks and
frozen web pages. With Ollama on a computer without a graphics card, small models work, just slower. The setup guide
asks what your Ollama machine has and suggests a model that fits; [AI and models](AI-and-Models) explains testing.

## Can Ollama be on a different machine?

Yes. Ollama only answers on its own computer until you change one setting; [Installation](Installation#ollama-on-another-computer)
shows how for Windows, Mac, Linux and Docker.

## Which model should I use?

Let DockLlama test them on your own Ollama server (Settings → AI → Test all). The same model can score and run very
differently on different hardware, so results from someone else's computer are only a starting point.
DockLlama suggests one; you choose.

## Will DockLlama restart my containers?

Not unless you turn it on. Pick **Auto-restart** in Settings → Checking and fixing and choose the containers it may
restart. Even then it only restarts when a restart would help, within limits and outside quiet hours, and never crash
loops or out-of-memory kills (Docker already handles those). See [Restarts](Restarts).

## What happens if Ollama is down?

Containers show **Couldn't check** with the reason, never "fine". The rules that don't need AI keep working. After three
rounds in a row DockLlama sends one message saying it can't reach its AI server.

## How do I teach the AI what's normal for a container?

On the container's page, use **Tell DockLlama what's normal**, for example "This is a VPN. Brief reconnects are normal."
Advanced users can also write their own AI instructions (Settings → Advanced) and test them on every model first.

## Can it watch containers on other machines?

Yes, with a small helper, DockLlama Remote, on each other machine. It never sends container environment variables
(which can hold passwords). See [Watching other machines](Other-Machines).

## How do I back it up?

Settings → Backup. Backups are automatic (daily by default) and land in a `backups` folder next to the compose file.
See [Backup and restore](Backup-and-Restore).
"""


def sidebar() -> str:
    lines = ["**DockLlama 2**", "", "- [Home](Home)"]
    lines += [f"- [{TITLES[p]}]({p})" for p in [*PAGES.values(), "FAQ"]]
    lines += ["", "**v1 (original)**", ""]
    lines += [f"- [{p[3:].replace('-', ' ')}]({p})" for p in V1_PAGES]
    return "\n".join(lines) + "\n"


V1_BANNER = ("> [!NOTE]\n> **This page describes the original DockLlama (v1)**, kept for anyone still running it. "
             "DockLlama 2 works differently: see [Home](Home).\n\n")


def build(out: Path) -> None:
    readme = (ROOT / "README.md").read_text()
    v = version()
    footer = FOOTER.format(version=v)
    pages: dict[str, str] = {"Home": home(readme), "FAQ": FAQ}
    pages["Installation"] = installation(readme, (DOCS / "setup.md").read_text())
    for src, name in PAGES.items():
        if name != "Installation":
            pages[name] = (DOCS / src).read_text()
    for name, text in pages.items():
        (out / f"{name}.md").write_text(fix_links(text).rstrip() + footer)
    (out / "_Sidebar.md").write_text(sidebar())
    print(f"Wrote {len(pages)} pages and the sidebar for DockLlama {v} to {out}")


def migrate_v1(out: Path) -> None:
    """One time: keep the original pages as v1-*.md with a banner. Skipped once they exist."""
    if (out / "v1-Overview.md").exists():
        return
    for old in ("Home", "Installation", "Configuration", "Web-UI", "AI-Engine", "Models", "Notifications",
                "API-Reference", "FAQ", "Troubleshooting"):
        src = out / f"{old}.md"
        if not src.exists():
            continue
        new = "v1-Overview" if old == "Home" else f"v1-{old}"
        text = src.read_text()
        for o in ("Home", "Installation", "Configuration", "Web-UI", "AI-Engine", "Models", "Notifications",
                  "API-Reference", "FAQ", "Troubleshooting"):  # keep links inside the v1 pages pointing at v1 pages
            target = "v1-Overview" if o == "Home" else f"v1-{o}"
            text = re.sub(rf"\]\({o}(#[^)]*)?\)", lambda m, t=target: f"]({t}{m.group(1) or ''})", text)
        (out / f"{new}.md").write_text(V1_BANNER + text)
        if old not in ("Home", "Installation", "Notifications", "FAQ", "Troubleshooting"):
            src.unlink()  # pages with no v2 counterpart
    print("Kept the original pages as v1-*")


if __name__ == "__main__":
    if len(sys.argv) != 2 or not Path(sys.argv[1]).is_dir():
        sys.exit("usage: python docs/make_wiki.py <path to a clone of DockLlama.wiki>")
    target = Path(sys.argv[1])
    migrate_v1(target)
    build(target)
