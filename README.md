# claude-skills

Skills I built for Claude Code and use myself: tidy and recover your
conversations, pull your own data out of Claude and ChatGPT, turn Markdown or HTML
into print-quality PDFs, and drive your real browser or let Claude run admin
commands on your machine, safely. Each one lives in its own folder with its own
README.

## Working with your conversations

### archive-conversation-forks
Declutters your Claude Code conversation picker. It sorts a project's sessions
into topics, keeps the fullest version of each, and archives the redundant forks,
moved to `~/claude-archive`, never deleted. [Read more](archive-conversation-forks).

### remote-shell-over-chisel

Gives Claude a shell (and optional Chrome/CDP control) on a machine you own from
a sandbox whose egress only allows HTTPS - the kind of TLS-MITM L7 proxy that
rejects raw SSH on any port. Tunnels SSH and the cdp-daemon port inside an HTTPS
WebSocket via chisel, with a self-healing connect script that detects stale
forwards and caches state across sessions. All host/user/secret details live in
a gitignored `config.sh`; nothing machine-specific is committed. Pairs with
`cdp-daemon` for driving your real logged-in browser remotely.

### open-thinking

Produce reasoning in the visible output channel instead of the hidden thinking
block, so it persists in Claude's own context across turns and reaches the user
unsummarized. Covers the dot-starve technique for the forced initial block,
backtick-quoted stance markers, failure modes (execution-planning drops, dot
ritualization, performative thinking), tool-call boundaries, and recovery. Seeded
with real artifacts from the session where the technique was developed - including
the DASH backronym, the Habsburg camel solicitor, and the Zagreb ATM ferrets.

### recover-deleted-sessions-ext4
Gets back Claude Code conversations you have already lost, whether to an
accidental delete or Claude Code's own automatic cleanup. It looks everywhere a
trace might still survive, from the easy wins (a backup, the filesystem's journal)
down to reading the raw disk when nothing else is left, then repairs and restores
what it finds. Bundles the scripts from a real recovery that brought back 60 of 66
sessions. [Read more](recover-deleted-sessions-ext4).

### claude-web-fetcher
Pulls your own data off claude.ai with nothing but your session cookie:
conversations, file attachments, and Claude-Code-web sessions. It clears the
Cloudflare check for you. [Read more](claude-web-fetcher).

### chatgpt-archive-toolkit
Backs up a logged-in ChatGPT account, conversations, branch variants, files and
all, and comes with a dark, local reader so you can browse the whole archive
offline. [Read more](chatgpt-archive-toolkit).

## Turning documents into PDFs

### markdown-latex-report
Turns a single Markdown file into a book-quality PDF with pandoc and lualatex.
Wide tables size their own columns, long code lines wrap instead of running off
the page, and you get a table of contents and running headers, with no full TeX
install. [Read more](markdown-latex-report).

<p align="center">
  <a href="markdown-latex-report/docs/sample-report.pdf"><img src="markdown-latex-report/docs/preview/01-title-toc.png" width="270" alt="title and contents"></a>
  <a href="markdown-latex-report/docs/sample-report.pdf"><img src="markdown-latex-report/docs/preview/02-code-wrapping.png" width="270" alt="code listing with line wrapping"></a>
  <a href="markdown-latex-report/docs/sample-report.pdf"><img src="markdown-latex-report/docs/preview/03-chart-and-table.png" width="270" alt="chart and a content-rich table"></a>
</p>
<p align="center"><em>Title and contents &middot; code with line wrapping &middot; a generated chart with a column-sized table.<br>Click any page for the full PDF.</em></p>

### claude-html-pdf-polisher
Turns HTML into a magazine-quality PDF with the layout and fonts pinned, so the
same input renders the same way every time and no font is silently swapped.
Especially handy for getting ChatGPT to iterate on a tricky render without it
switching the engine under you. [Read more](claude-html-pdf-polisher).

## Driving your browser and system

### cdp-daemon
Lets Claude, or any script, drive the real Chrome you already have open and logged
in, so it can get things done on websites for you without a permission popup
interrupting every step. Under the hood, it speaks Chrome's DevTools Protocol over
one held-open connection, so you can also read cookies, run JavaScript, or watch
network traffic. [Read more](cdp-daemon).

### sudo-listener
Lets Claude run the commands on your machine that normally need an administrator
password, without you handing over blanket access or leaving a password lying
around, and with a log of everything it ran. For the technically minded, it is one
dependency-free Python file: each request is signed and accepted only from your own
machine, a safer alternative to passwordless (`NOPASSWD`) sudo.
[Read more](sudo-listener).

## Installing a skill

Copy any skill's directory into your Claude Code skills folder
(`~/.claude/skills/`), or install the packaged `.skill` from its release.

## License

See each skill's LICENSE.
