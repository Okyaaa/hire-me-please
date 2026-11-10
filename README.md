# hire-me-please 🙏

> *Because job hunting shouldn't feel like a second full-time job.*

A command-line job-hunting assistant built on Claude and Playwright. It finds jobs, scores them against your resume, writes a tailored resume and cover letter for each good match, fills in application forms, and keeps track of every application.

**find → score → tailor → fill → submit → track**

It works with a Claude subscription through the Claude Code CLI (no API key needed) or with an Anthropic API key.

## Features

- **Job discovery**
  - Pulls open roles from the public **Greenhouse**, **Lever** and **Ashby** job-board APIs for the companies you choose, then keeps only those matching your title and location keywords.
  - **Imports** jobs from any other site (LinkedIn, JobStreet, Indeed, remote boards) from a simple JSON file. You can collect these by hand or let Claude in Chrome do it (see [Daily routine with Claude Code](#daily-routine-with-claude-code)).
- **AI match scoring.** Claude rates each job 0–100 against your profile and resume and explains why. Jobs below your `min_score` are skipped automatically.
- **Tailored documents.** For each strong match Claude rewrites your resume and drafts a cover letter using **only facts from your resume**. Both are saved as Markdown and PDF, plus a `changes.md` explaining what was changed.
- **Form filling.** Opens the application page in Chromium, uploads your PDFs, and has Claude answer every field, including custom questions. It flags anything it can't answer from your profile instead of guessing.
- **You stay in control.** By default nothing is submitted without your OK. Auto-submit is opt-in and only fires when every field is filled and no CAPTCHA is present. hire-me-please never solves CAPTCHAs.
- **Application tracker.** A local SQLite database records every job, its score, documents, status (`new`, `scored`, `skipped`, `tailored`, `applied`, `interviewing`, `rejected`, `offer`, `withdrawn`, `error`) and a full history. You can export it to CSV.
- **Daily limit.** Caps how many applications go out per day.

## Requirements

- Python 3.11 or newer
- One of:
  - a Claude plan plus the [Claude Code CLI](https://docs.claude.com/en/docs/claude-code) (`npm install -g @anthropic-ai/claude-code`, then `claude auth login`), or
  - an Anthropic API key

## Setup

```bash
git clone https://github.com/Okyaaa/hire-me-please.git
cd hire-me-please
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
playwright install chromium
hire-me-please init
```

`hire-me-please init` creates your private working files: `profile.yaml`, `resume.md`, `.env` and the `jobs.db` database. All of them are in `.gitignore`, so your personal data never gets committed.

Then:

1. **Connect Claude.**
   - **Claude plan (no API key):** sign in once with `claude auth login`. This uses your plan's usage limits.
   - **API key:** put `ANTHROPIC_API_KEY=...` in `.env`. This is billed per token.

   With `backend: auto` in `profile.yaml`, hire-me-please uses the key if there is one, otherwise the CLI.
2. **Add your resume.** Replace `resume.md` with your real resume in Markdown.
3. **Fill in `profile.yaml`.** See [`profile.example.yaml`](profile.example.yaml) for every option. It covers:
   - your contact details, work authorization, salary expectation and notice period
   - search keywords, locations and the minimum score
   - the companies to watch (`sources`). The board slug is the last part of the company's job-board URL, e.g. `job-boards.greenhouse.io/stripe` → `stripe`.

The more you fill in, the fewer questions get flagged for you to answer by hand.

## Usage

### All in one

```bash
hire-me-please run --top 5            # fetch, score, tailor the 5 best, then apply
```

### Step by step

```bash
hire-me-please fetch                          # pull new roles from your watched companies
hire-me-please import imports/linkedin.json   # add jobs collected from other sites
hire-me-please score                          # Claude scores every new job
hire-me-please list --status scored           # see what scored well
hire-me-please tailor --top 5                 # tailored resume + cover letter for the top 5
hire-me-please tailor --job <id>              # ...or for one specific job
hire-me-please apply --next 5                 # open and fill the next 5 application forms
hire-me-please mark <id> interviewing --note "recruiter call Friday"
hire-me-please show <id>                      # details and history for one job
hire-me-please export                         # write the tracker to applications.csv
```

### Importing jobs from other sites

`hire-me-please import` takes a JSON list like this:

```json
[
  {
    "source": "linkedin",
    "external_id": "4012345678",
    "company": "Example Corp",
    "title": "QA Engineer",
    "location": "Jakarta, Indonesia (On-site)",
    "url": "https://www.linkedin.com/jobs/view/4012345678/",
    "description": "Full job description text..."
  }
]
```

Jobs already in the database are ignored, so importing the same file twice is safe.

### Submitting

For each form, hire-me-please fills it in, prints a table of what it entered, flags anything it couldn't answer, and waits:

`[s]ubmit for me · [d]one, I submitted it · [k] skip job · [l]ater · [q]uit`

To let it click Submit itself, set `apply.auto_submit: true` in `profile.yaml` or pass `--submit`. It still falls back to asking you whenever a field needs your input or a CAPTCHA is on the page. `apply.daily_limit` caps applications per day.

## Daily routine with Claude Code

Many job sites (LinkedIn, JobStreet, Indeed) have no public API. [`examples/daily-routine.md`](examples/daily-routine.md) is a prompt for a scheduled Claude Code task, which runs it on a timer while the Claude app is open. The routine:

1. runs `hire-me-please fetch` and checks remote job boards,
2. searches LinkedIn and other sites with Claude in Chrome and imports what it finds,
3. scores and tailors the strong matches,
4. sends you a summary with every planned form answer, and **waits for your OK**,
5. submits only the jobs you approve and marks them `applied`.

Copy the file, replace the `<ANGLE BRACKET>` values with your own rules and answers, and keep your filled-in copy out of git.

## Project layout

```
hire_me_please/
  cli.py        command-line entry point
  sources.py    Greenhouse / Lever / Ashby fetchers
  llm.py        Claude calls: scoring, tailoring, form answers (API or Claude Code CLI)
  documents.py  Markdown → PDF rendering
  apply.py      Playwright form filling and submission
  db.py         SQLite tracker
  config.py     profile and settings loading
profile.example.yaml   template for your profile
examples/              daily-routine prompt template
```

Your private files, all git-ignored: `profile.yaml`, `resume.md`, `.env`, `jobs.db`, `output/` (generated documents), `imports/` (collected jobs), `.browser-profile/` (browser sign-ins).

## Good to know

- Claude is told never to invent experience and never to guess work authorization, sponsorship or legal attestations. Those are always flagged for you.
- The browser profile in `.browser-profile/` is persistent, so sign-ins you do yourself on careers sites are remembered.
- Default model: `claude-opus-5-5`, set with `model:` in `profile.yaml`. Scoring runs at low effort; tailoring and form answers at medium. Your profile and resume are prompt-cached, so repeated calls cost less.
- Check each site's terms of service before automating applications on it, and always review documents before they go out.

## License

[MIT](LICENSE)
