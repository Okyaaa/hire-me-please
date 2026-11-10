from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys

from rich.console import Console
from rich.table import Table

from . import config, db
from .config import EXAMPLE_PROFILE_PATH, PROFILE_PATH, ROOT

console = Console()

RESUME_TEMPLATE = """# Ada Lovelace
New York, NY · ada@example.com · +1 555 555 0100 · linkedin.com/in/your-handle · github.com/your-handle

## Summary
Backend engineer with 6 years building ...

## Experience
### Senior Software Engineer — Company · 2022–present
- Did X, which resulted in Y (metric)

## Skills
Python, Go, PostgreSQL, AWS, Kubernetes

## Education
### B.S. Computer Science — University · 2018
"""


def cmd_init(args) -> None:
    if not PROFILE_PATH.exists():
        shutil.copy(EXAMPLE_PROFILE_PATH, PROFILE_PATH)
        console.print(f"Created {PROFILE_PATH.name} — fill in your details and target companies.")
    resume = ROOT / "resume.md"
    if not resume.exists():
        resume.write_text(RESUME_TEMPLATE)
        console.print("Created resume.md — replace it with your real resume in Markdown.")
    env = ROOT / ".env"
    if not env.exists():
        env.write_text("ANTHROPIC_API_KEY=\n")
        console.print("Created .env — add your ANTHROPIC_API_KEY.")
    db.connect()
    console.print("Database ready: jobs.db")


def cmd_fetch(args) -> None:
    from .sources import fetch_all, matches_search
    cfg = config.load()
    conn = db.connect()
    console.print("Fetching jobs...")
    jobs = fetch_all(cfg.profile.get("sources", {}))
    matching = [j for j in jobs if matches_search(j, cfg.search)]
    new = sum(db.insert_job(conn, j) for j in matching)
    console.print(f"{len(jobs)} roles found, {len(matching)} match your search, [bold]{new} new[/].")


def cmd_score(args) -> None:
    from . import llm
    cfg = config.load()
    conn = db.connect()
    jobs = db.by_status(conn, "new", order_by_score=False, limit=args.limit)
    min_score = int(cfg.search.get("min_score", 70))
    console.print(f"Scoring {len(jobs)} jobs...")
    for job in jobs:
        try:
            s = llm.score_job(cfg, job)
        except Exception as e:  # noqa: BLE001 - keep going through the batch
            console.print(f"  [red]{job['company']} — {job['title']}: {e}[/]")
            continue
        status = "scored" if s.score >= min_score else "skipped"
        db.update(conn, job["id"], status, score=s.score, score_detail=s.model_dump())
        color = "green" if s.score >= min_score else "dim"
        console.print(f"  [{color}]{s.score:>3}  {job['company']} — {job['title']}[/]  {s.reasons[0] if s.reasons else ''}")


def cmd_tailor(args) -> None:
    from . import documents, llm
    cfg = config.load()
    conn = db.connect()
    jobs = [db.get(conn, args.job)] if args.job else db.by_status(conn, "scored", limit=args.top)
    jobs = [j for j in jobs if j]
    name = cfg.full_name.replace(" ", "_") or "Resume"
    for job in jobs:
        console.print(f"Tailoring for {job['company']} — {job['title']}...")
        try:
            docs = llm.tailor(cfg, job)
        except Exception as e:  # noqa: BLE001
            console.print(f"  [red]{e}[/]")
            continue
        out = documents.job_dir(job)
        resume_pdf, cover_pdf = out / f"{name}_Resume.pdf", out / f"{name}_Cover_Letter.pdf"
        documents.render([(docs.resume_markdown, resume_pdf, False), (docs.cover_letter_markdown, cover_pdf, True)])
        (out / "changes.md").write_text("\n".join(f"- {c}" for c in docs.changes))
        db.update(conn, job["id"], "tailored", resume_path=str(resume_pdf), cover_path=str(cover_pdf))
        console.print(f"  [green]→ {out.relative_to(ROOT)}[/]")


def cmd_apply(args) -> None:
    from .apply import apply_jobs
    cfg = config.load()
    conn = db.connect()
    jobs = [db.get(conn, args.job)] if args.job else db.by_status(conn, "tailored", limit=args.next)
    jobs = [j for j in jobs if j]
    if not jobs:
        console.print("Nothing to apply to. Run `jobbot tailor` first.")
        return
    auto = args.submit or bool(cfg.apply.get("auto_submit", False))
    apply_jobs(cfg, conn, jobs, auto_submit=auto)


def cmd_run(args) -> None:
    cmd_fetch(args)
    cmd_score(argparse.Namespace(limit=args.score_limit))
    cmd_tailor(argparse.Namespace(job=None, top=args.top))
    cmd_apply(argparse.Namespace(job=None, next=args.top, submit=args.submit))


def cmd_list(args) -> None:
    conn = db.connect()
    sql = "SELECT * FROM jobs"
    params: list = []
    if args.status:
        sql += " WHERE status = ?"
        params.append(args.status)
    sql += " ORDER BY COALESCE(applied_at, fetched_at) DESC LIMIT ?"
    params.append(args.limit)
    table = Table("ID", "Status", "Score", "Company", "Title", "Location", "Applied")
    for r in conn.execute(sql, params):
        table.add_row(r["id"].rsplit(":", 1)[-1][:10], r["status"], str(r["score"] or ""), r["company"],
                      r["title"][:50], (r["location"] or "")[:25], (r["applied_at"] or "")[:10])
    console.print(table)
    console.print("  ".join(f"{k}: {v}" for k, v in sorted(db.counts(conn).items())))


def cmd_show(args) -> None:
    conn = db.connect()
    job = db.get(conn, args.job)
    if not job:
        sys.exit(f"No unique job matching {args.job!r}")
    console.print(f"[bold]{job['company']} — {job['title']}[/]  ({job['location']})")
    console.print(f"id: {job['id']}\nstatus: {job['status']}  score: {job['score']}\nurl: {job['url']}\napply: {job['apply_url']}")
    if job["score_detail"]:
        d = json.loads(job["score_detail"])
        console.print("\n[bold]Why[/]\n" + "\n".join(f"  + {r}" for r in d.get("reasons", [])))
        if d.get("red_flags"):
            console.print("\n".join(f"  - {r}" for r in d["red_flags"]))
    if job["resume_path"]:
        console.print(f"\nresume: {job['resume_path']}\ncover:  {job['cover_path']}")
    if job["notes"]:
        console.print(f"\nnotes: {job['notes']}")
    console.print("\n[bold]History[/]")
    for e in conn.execute("SELECT * FROM events WHERE job_id = ? ORDER BY id", (job["id"],)):
        console.print(f"  {e['at']}  {e['status']}  {e['note'] or ''}")


def cmd_mark(args) -> None:
    conn = db.connect()
    job = db.get(conn, args.job)
    if not job:
        sys.exit(f"No unique job matching {args.job!r}")
    extra = {"applied_at": db.now()} if args.status == "applied" and not job["applied_at"] else {}
    db.update(conn, job["id"], args.status, note=args.note, **extra)
    console.print(f"{job['company']} — {job['title']}: {args.status}")


def cmd_import(args) -> None:
    """Add jobs collected elsewhere (LinkedIn, JobStreet, Indeed...) from a JSON list.

    Each item: {source, company, title, location, url, description, apply_url?, external_id?}
    """
    import hashlib
    import re
    conn = db.connect()
    items = json.loads(open(args.file).read())
    new = 0
    for it in items:
        ext = it.get("external_id") or hashlib.sha1(it["url"].encode()).hexdigest()[:12]
        company_slug = re.sub(r"[^a-z0-9]+", "-", it["company"].lower()).strip("-")
        job = {
            "id": f"{it['source']}:{company_slug}:{ext}",
            "source": it["source"],
            "company": it["company"],
            "title": it["title"],
            "location": it.get("location", ""),
            "url": it["url"],
            "apply_url": it.get("apply_url") or it["url"],
            "description": it.get("description", ""),
            "posted_at": it.get("posted_at"),
        }
        if db.insert_job(conn, job):
            new += 1
            console.print(f"  + {job['source']}: {job['company']} — {job['title']}")
    console.print(f"{len(items)} jobs in file, [bold]{new} new[/].")


def cmd_export(args) -> None:
    conn = db.connect()
    cols = ["id", "status", "score", "company", "title", "location", "url", "applied_at", "resume_path", "notes"]
    with open(args.out, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for r in conn.execute(f"SELECT {', '.join(cols)} FROM jobs ORDER BY fetched_at DESC"):
            w.writerow(list(r))
    console.print(f"Wrote {args.out}")


def main() -> None:
    p = argparse.ArgumentParser(prog="jobbot", description="Find, score, tailor, fill and submit job applications.")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create profile.yaml, resume.md, .env and the database").set_defaults(fn=cmd_init)
    sub.add_parser("fetch", help="pull open roles from your watched companies").set_defaults(fn=cmd_fetch)

    s = sub.add_parser("import", help="add jobs from a JSON file (e.g. collected from LinkedIn / JobStreet / Indeed)")
    s.add_argument("file")
    s.set_defaults(fn=cmd_import)

    s = sub.add_parser("score", help="score new jobs against your profile with Claude")
    s.add_argument("--limit", type=int, default=50)
    s.set_defaults(fn=cmd_score)

    s = sub.add_parser("tailor", help="write a tailored resume + cover letter (PDF) per job")
    s.add_argument("--job", help="a specific job id")
    s.add_argument("--top", type=int, default=5, help="tailor the N highest-scored jobs")
    s.set_defaults(fn=cmd_tailor)

    s = sub.add_parser("apply", help="open, fill and submit application forms")
    s.add_argument("--job", help="a specific job id")
    s.add_argument("--next", type=int, default=5, help="apply to the next N tailored jobs")
    s.add_argument("--submit", action="store_true", help="auto-submit forms that need no human input")
    s.set_defaults(fn=cmd_apply)

    s = sub.add_parser("run", help="fetch → score → tailor → apply in one go")
    s.add_argument("--top", type=int, default=5)
    s.add_argument("--score-limit", type=int, default=50)
    s.add_argument("--submit", action="store_true")
    s.set_defaults(fn=cmd_run)

    s = sub.add_parser("list", help="show tracked jobs")
    s.add_argument("--status", choices=db.STATUSES)
    s.add_argument("--limit", type=int, default=40)
    s.set_defaults(fn=cmd_list)

    s = sub.add_parser("show", help="details and history for one job")
    s.add_argument("job")
    s.set_defaults(fn=cmd_show)

    s = sub.add_parser("mark", help="update a job's status, e.g. interviewing")
    s.add_argument("job")
    s.add_argument("status", choices=db.STATUSES)
    s.add_argument("--note")
    s.set_defaults(fn=cmd_mark)

    s = sub.add_parser("export", help="export the tracker to CSV")
    s.add_argument("--out", default="applications.csv")
    s.set_defaults(fn=cmd_export)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
