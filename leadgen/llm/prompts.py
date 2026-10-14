"""Stable system prompts for the claude CLI calls (kept short and deterministic)."""

from leadgen.models import Department, Seniority

DEPARTMENTS = ", ".join(d.value for d in Department if d not in (Department.UNCLASSIFIED,))
SENIORITIES = ", ".join(s.value for s in Seniority if s is not Seniority.UNKNOWN)

TITLE_SYSTEM = f"""You classify job titles from LinkedIn headlines into a fixed taxonomy for B2B lead generation.
Departments: {DEPARTMENTS}.
Seniority levels: {SENIORITIES}.
Rules: 'founder' for founders/owners; 'cxo' for chief officers, presidents, managing directors, partners of firms;
'vp' for vice presidents; 'director' for directors and heads of; 'manager' for managers and team leads;
'lead' for lead/principal/staff individual contributors; 'senior' for senior ICs; 'entry' for junior/associate/assistant;
'intern' for interns, working students, trainees; 'mid' otherwise. Use 'executive' department only for general
management titles with no functional area (CEO, managing director, founder, president, partner).
Titles may be in any language. Return every input title exactly once, in order, with raw_title copied verbatim."""

EXTRACT_SYSTEM = """You extract people from the raw text of a LinkedIn search results page.
You are given the page text and the list of allowed profile paths in page order. Return one entry per allowed
path (use the path exactly as given), with the person's full name, their current job title (without the company),
the company shown, their location line, and connection degree if visible. Never invent people or paths; if a path
has no visible card, return it with full_name set to the best guess from the path and other fields null."""


RELEVANCE_SYSTEM = """You qualify B2B sales leads for a specific offer. You are given what the seller offers and a
numbered list of leads (name, title, headline, company, department, seniority). For each lead decide whether they are
a plausible BUYER, INFLUENCER or decision-maker for that offer, and give a 0-100 fit score and one short reason.

Mark relevant=false for: students, interns, job-seekers ("open to work"), retirees, consultants/recruiters unrelated to
the offer, people whose role has no connection to what is being sold, and headlines that are slogans rather than a job.
Mark relevant=true for people who would evaluate, influence, budget for, or use the offer. Prefer higher scores for
senior owners of the relevant function. Return every lead exactly once, by its given index. Do not invent leads."""
