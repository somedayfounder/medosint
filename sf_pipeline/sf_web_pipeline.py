#!/usr/bin/env python3
"""
Someday Founder Web Pipeline
Real web data via Brave Search + Jina Reader, analysed by LLM.

Usage:
    python sf_pipeline/sf_web_pipeline.py --company "IKEA" --llm gpt4o-mini
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from queue import Queue
from typing import Optional

import requests

# ── shared event queue (server.py SSE) ───────────────────────────────────────
_event_queue: Optional[Queue] = None


def set_event_queue(q: Queue):
    global _event_queue
    _event_queue = q


def emit(event_type: str, data: dict):
    data["ts"] = datetime.now().isoformat(timespec="seconds")
    data["type"] = event_type
    if _event_queue:
        _event_queue.put(data)
    else:
        print(json.dumps(data, ensure_ascii=False), flush=True)


# ── API usage counters ────────────────────────────────────────────────────────
import threading as _threading
_api = {"brave": 0, "jina": 0, "grounding": 0, "tavily": 0, "serper": 0}
_api_lock = _threading.Lock()
BRAVE_COST_PER_CALL   = 0.003   # $3 / 1000 queries
TAVILY_COST_PER_CALL  = 0.008   # $0.008/credit, basic search = 1 credit/query (free tier: 1000 cr/mo)
SERPER_COST_PER_CALL  = 0.001   # $1 / 1000 queries (free tier: 2500 queries)
GROUNDING_MONTHLY_LIMIT = 5000
GROUNDING_COST_PER_QUERY = 0.014  # $14/1000 queries after free tier
TAVILY_URL = "https://api.tavily.com/search"
SERPER_URL = "https://google.serper.dev/search"

_db_path_global: str = None
_current_company: str = ""


def _write_usage(api: str, count: int = 1, cost: float = 0.0,
                 model: str = None, tokens_in: int = 0, tokens_out: int = 0):
    if not _db_path_global:
        return
    try:
        conn = sqlite3.connect(_db_path_global)
        conn.execute(
            "INSERT INTO api_usage (ts,company,api,model,count,cost,tokens_in,tokens_out) VALUES (?,?,?,?,?,?,?,?)",
            (datetime.now().isoformat(timespec="seconds"), _current_company,
             api, model, count, round(cost, 6), tokens_in, tokens_out)
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def _load_monthly_usage(db_path: str) -> dict:
    """Load current month's api_usage totals from DB."""
    defaults = {"brave": 0, "jina": 0, "grounding": 0, "tavily": 0, "serper": 0, "llm_cost": 0.0, "llm_in": 0, "llm_out": 0}
    try:
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT api, COALESCE(SUM(count),0), COALESCE(SUM(cost),0.0), "
            "COALESCE(SUM(tokens_in),0), COALESCE(SUM(tokens_out),0) "
            "FROM api_usage WHERE ts >= strftime('%Y-%m-01','now') GROUP BY api"
        ).fetchall()
        conn.close()
        result = dict(defaults)
        for api, cnt, cost, tin, tout in rows:
            if api in ("brave", "jina", "grounding", "tavily", "serper"):
                result[api] = int(cnt)
            if api == "llm":
                result["llm_cost"] = round(float(cost), 4)
                result["llm_in"] = int(tin)
                result["llm_out"] = int(tout)
        return result
    except Exception:
        return defaults


def _emit_api(api: str, count: int = 1):
    with _api_lock:
        _api[api] += count
        total = _api[api]
    payload = {"api": api, "total": total}
    if api == "brave":
        cost = BRAVE_COST_PER_CALL * count
        payload["call_cost"] = round(cost, 4)
        payload["cost"] = round(total * BRAVE_COST_PER_CALL, 4)
        _write_usage(api, count=count, cost=cost)
    elif api == "tavily":
        cost = TAVILY_COST_PER_CALL * count
        payload["call_cost"] = round(cost, 4)
        payload["cost"] = round(total * TAVILY_COST_PER_CALL, 4)
        _write_usage(api, count=count, cost=cost)
    elif api == "serper":
        cost = SERPER_COST_PER_CALL * count
        payload["call_cost"] = round(cost, 4)
        payload["cost"] = round(total * SERPER_COST_PER_CALL, 4)
        _write_usage(api, count=count, cost=cost)
    elif api == "grounding":
        monthly = _load_monthly_usage(DB_PATH).get("grounding", 0)
        paid = max(0, monthly - GROUNDING_MONTHLY_LIMIT)
        cost = paid * GROUNDING_COST_PER_QUERY
        payload["monthly_limit"] = GROUNDING_MONTHLY_LIMIT
        payload["cost"] = round(cost, 4)
        _write_usage(api, count=count)
    else:
        _write_usage(api, count=count)
    emit("api_call", payload)


# ── Token cost table (per 1M tokens) ─────────────────────────────────────────
PRICING = {
    "gpt-4o-mini":              {"in": 0.150,  "out": 0.600},
    "gpt-4o":                   {"in": 2.500,  "out": 10.00},
    "gpt-5.6-terra":            {"in": 2.000,  "out": 12.00},
    "gpt-5.6-luna":             {"in": 0.200,  "out": 1.200},
    "deepseek-chat":            {"in": 0.270,  "out": 1.100},
    "gemini-3.5-flash-lite":  {"in": 0.300,  "out": 2.500},
    "gemini-3.5-flash-lite":     {"in": 0.300,  "out": 2.500},
    "gemini-2.0-flash":          {"in": 0.100,  "out": 0.400},
    "gemini-1.5-flash":          {"in": 0.075,  "out": 0.300},
    "gemini-1.5-pro":            {"in": 1.250,  "out": 5.000},
    "gemini-2.5-pro":            {"in": 1.250,  "out": 10.00},
    "gemini-3.1-pro-preview":    {"in": 1.250,  "out": 10.00},
    "gemini-3.6-flash":          {"in": 1.500,  "out": 7.500},
    "gemini-3-flash-preview":    {"in": 0.500,  "out": 3.000},
    "claude-haiku-4-5-20251001":{"in": 0.800,  "out": 4.000},
    "claude-sonnet-5":          {"in": 3.000,  "out": 15.00},
    "claude-opus-5-20251101":   {"in": 15.00,  "out": 75.00},
    "claude-opus-5":            {"in": 15.00,  "out": 75.00},
    "moonshot-v1-8k":           {"in": 1.700,  "out": 1.700},
    "moonshot-v1-32k":          {"in": 3.300,  "out": 3.300},
    "moonshot-v1-128k":         {"in": 8.800,  "out": 8.800},
    "kimi-latest":              {"in": 1.700,  "out": 1.700},
    "kimi-k2":                  {"in": 0.140,  "out": 0.560},
    "kimi-k2.6":                {"in": 0.140,  "out": 0.560},
    "grok-3":                   {"in": 3.000,  "out": 15.00},
    "grok-3-mini":              {"in": 0.300,  "out": 0.500},
    "grok-4.5":                 {"in": 2.000,  "out": 6.000},
    "grok-4.3":                 {"in": 1.250,  "out": 2.500},
}
_tokens = {"in": 0, "out": 0, "cost": 0.0, "model": "gpt-4o-mini", "by_model": {}}


def _emit_tokens(in_tok: int, out_tok: int, model: str = None):
    m = model or _tokens["model"]
    p = PRICING.get(m, {"in": 0.15, "out": 0.60})
    cost = (in_tok * p["in"] + out_tok * p["out"]) / 1_000_000
    _tokens["in"]   += in_tok
    _tokens["out"]  += out_tok
    _tokens["cost"] += cost
    _write_usage("llm", count=1, cost=cost, model=m, tokens_in=in_tok, tokens_out=out_tok)
    bm = _tokens["by_model"]
    if m not in bm:
        bm[m] = {"in": 0, "out": 0, "cost": 0.0}
    bm[m]["in"]   += in_tok
    bm[m]["out"]  += out_tok
    bm[m]["cost"] += cost
    emit("tokens", {
        "in": in_tok, "out": out_tok, "call_cost": round(cost, 5),
        "total_in": _tokens["in"], "total_out": _tokens["out"],
        "total_cost": round(_tokens["cost"], 4),
        "model": m,
        "by_model": {k: {"in": v["in"], "out": v["out"], "cost": round(v["cost"], 5)}
                     for k, v in bm.items()},
    })


# ── 15 areas (full topic lists from sf_workflow.js) ───────────────────────────
AREA_TOPICS = {
    "Personal Growth": [
        "Habits, Daily Routines, Behavior Change",
        "Goals & Achievements, Goal Setting, Milestones",
        "Focus & Attention, Deep Work, Productivity",
        "Digital Hygiene, Screen Time, Digital Detox",
        "Critical Thinking, Problem Solving, Analytical Thinking",
        "Decision Making, Judgment, Choice Under Uncertainty",
        "Creative & Strategic Thinking, Innovation Mindset, Lateral Thinking",
        "Biases & Perception, Cognitive Biases, Mental Models",
        "Self-Talk, Inner Critic, Mindset",
        "Stress Management, Burnout, Resilience",
        "Work-Life Balance, Boundaries, Personal Sustainability",
        "Famous Personal Growth Stories, Transformational Moments, Memorable Self-Development Stories",
        "Failures, Crises & Mistakes in Personal Growth, Burnout Disasters, Mental Health Crises, Resilience Under Fire",
    ],
    "Work & Career": [
        "Career & Growth, Career Pivots, Career Development",
        "Mentors & Sponsors, Mentorship, Career Sponsorship",
        "Managing Setbacks, Failure Recovery, Bouncing Back",
        "Personal Branding, Professional Reputation, Thought Leadership",
        "Resume, CV, Job Application",
        "Job Search, Career Change, Job Hunt",
        "Interview Essentials, Job Interview, Interview Preparation",
        "Behavioral & Case Interviews, Case Studies, STAR Method",
        "Networking, Professional Network, Building Connections",
        "Self-Advocacy, Speaking Up, Career Visibility",
        "Business Writing, Professional Communication, Writing Skills",
        "Active Listening, Communication Skills, Listening",
        "Visual Communication, Data Visualization, Visual Storytelling",
        "Presentation & Public Speaking, Keynotes, Pitching",
        "Difficult Conversations, Conflict Resolution, Hard Talks",
        "Feedback Skills, Giving Feedback, Performance Reviews",
        "Famous Career Stories, Legendary Career Moves, Memorable Professional Moments",
        "Failures, Crises & Mistakes in Career, Career Disasters, Wrongful Exits, Derailed Careers",
    ],
    "Office Survival": [
        "Office Rules, Workplace Norms, Corporate Culture",
        "Chats & Emails, Business Communication, Email Etiquette",
        "Meetings, Meeting Culture, Meeting Management",
        "Small Talks, Casual Conversations, Ice Breakers",
        "Managing Up, Managing Your Boss, Upward Management",
        "Cross-Functional Collaboration, Team Collaboration, Breaking Silos",
        "Resources & Salary, Salary Negotiation, Compensation",
        "Under Pressure, High Stakes, Working Under Pressure",
        "Personal Safety, Psychological Safety, Workplace Safety",
        "Crisis Management, Workplace Crisis, Damage Control",
        "Conflicts & Escalations, Workplace Conflict, Escalation",
        "Office Politics, Workplace Dynamics, Organizational Politics",
        "Zero Effort Office, Efficiency, Working Smarter",
        "Small Wins Strategy, Quick Wins, Incremental Progress",
        "Famous Office Stories, Legendary Workplace Moments, Memorable Culture Incidents",
        "Failures, Crises & Mistakes at Work, Toxic Culture Disasters, Corporate Scandals, HR Crises",
    ],
    "Marketing": [
        "Competitive Analysis, Price Wars, Market Competition",
        "Product Positioning, Market Differentiation, Unique Selling Proposition",
        "Brand Strategy, Brand Building, Brand Turnaround",
        "Brand Identity, Visual Branding, Logo & Design",
        "Storytelling, Content Marketing, Brand Narrative",
        "Social Media, Viral Marketing, Online Community",
        "PR & Media Relations, Crisis PR, Press Coverage",
        "Partners & Affiliates, Co-marketing, Strategic Partnerships",
        "Affiliate & Referral, Word-of-Mouth, Referral Programs",
        "Events & Sponsorship, Experiential Marketing, Sponsorship Deals",
        "Community Management, Brand Advocates, Fan Community",
        "Market Research, Consumer Behavior, Survey & Focus Groups",
        "SEO & Organic, Search Marketing, Organic Growth",
        "Paid Ads, Advertising Campaigns, TV Commercials",
        "Email Campaigns, Direct Marketing, Newsletter",
        "Marketing Hacks, Growth Hacks, Unconventional Marketing Techniques",
        "Famous Marketing Stories, Legendary Campaign Moments, Memorable Marketing Decisions",
        "Failures, Crises & Mistakes in Marketing, Marketing Disasters, Failed Campaigns, Brand Failures",
    ],
    "Sales": [
        "B2B Distribution, Channel Sales, Distribution Networks",
        "Retail Sales, Store Sales, Retail Strategy",
        "E-commerce Sales, Online Sales, Digital Commerce",
        "Trade Marketing, Shopper Marketing, Retail Promotions",
        "B2B Sales Process, Sales Pipeline, Sales Methodology",
        "Account-Based Selling, Key Account Management, Enterprise Accounts",
        "Enterprise Sales, Large Account Sales, Complex Sales",
        "CRM & Systems, Sales Technology, Sales Tools",
        "Sales Playbooks, Sales Scripts, Sales Enablement",
        "Business Negotiation, Negotiation Tactics, Deal Making",
        "Objections & Closing, Handling Objections, Closing Techniques",
        "Relationship Management, Client Relationships, Account Management",
        "Famous Sales Stories, Legendary Deals, Memorable Sales Moments",
        "Failures, Crises & Mistakes in Sales, Sales Disasters, Channel Failures, Distribution Crises",
    ],
    "Customer Success": [
        "Customer Journey, Customer Experience, CX",
        "Onboarding & Adoption, Customer Onboarding, Product Adoption",
        "Churn Prevention, Retention, Reducing Churn",
        "Renewal & Growth, Upsell, Expansion Revenue",
        "Loyalty Programs, Customer Loyalty, Rewards Programs",
        "Feedback & Advocacy, Customer Feedback, NPS",
        "Famous Customer Stories, Legendary Customer Interactions, Memorable Client Relationships",
        "Failures, Crises & Mistakes in Customer Experience, Service Disasters, Customer Churn Crises, Loyalty Program Failures",
    ],
    "Finance": [
        "Budgeting & Forecasting, Financial Planning, Budget Management",
        "Accounting, Financial Reporting, Books",
        "Working Capital, Cash Flow, Liquidity",
        "Scenario Planning, Stress Testing, Financial Scenarios",
        "Applied Economics, Market Economics, Economic Analysis",
        "Revenue Models, Business Models, Monetization",
        "Unit Economics, LTV, CAC",
        "Pricing, Price Strategy, Pricing Models",
        "Risk Management, Financial Risk, Hedging",
        "Financial Modeling, Valuation, Financial Analysis",
        "Famous Financial Decisions, Legendary Investment Stories, Memorable Finance Moments",
        "Failures, Crises & Mistakes in Finance, Financial Disasters, Accounting Scandals, Cash Flow Crises",
    ],
    "Digital Product": [
        "Customer Insights, User Research, Customer Discovery",
        "User Experience & Design, UX, Product Design",
        "Validation & MVP, Product Validation, Minimum Viable Product",
        "Roadmapping & Prioritization, Product Roadmap, Feature Prioritization",
        "Business Analysis, Requirements, Product Requirements",
        "Delivery Methods, Development Process, Agile",
        "Release Management, Deployment, Product Releases",
        "Go-to-Market, Product Launch, GTM Strategy",
        "Product/Market Fit, PMF, Finding Fit",
        "Product-Led Growth, PLG, Virality",
        "Decisions with Data, Analytics, Data-Driven Product",
        "Product Lifecycle, Product Evolution, Product Maturity",
        "Famous Product Stories, Legendary Product Decisions, Memorable Launch Moments",
        "Failures, Crises & Mistakes in Digital Products, Failed Product Launches, Tech Disasters, Product Flops",
    ],
    "Goods & Services": [
        "Purchase Triggers, Buying Behavior, Consumer Decision Making",
        "Shopper Journey, Customer Path, In-Store Behavior",
        "Visual Merchandising, In-Store Display Tactics, Marketplace Psychology",
        "Category Management, Shelf Management, Retail Categories",
        "Price Anchor Products, Traffic Drivers, Hero Items, Entry Price Points",
        "Product Concept, New Product Development, Concept Testing",
        "Development Frameworks, Product Development Process, Stage-Gate",
        "Packaging Design, Product Packaging, Package Innovation",
        "Brand & Line Management, Product Portfolio, Brand Architecture",
        "Distribution Strategy, Channel Strategy, Market Distribution",
        "Product Sunset, Product Discontinuation, End of Life",
        "Named Internal Programs, Proprietary Retail Concepts, Company-Specific Named Techniques & Tactics",
        "Retail Hacks, Merchandising Tricks, Store Psychology Techniques",
        "Famous Retail Stories, Legendary Product Launch Moments, Memorable Brand or Service Stories",
        "Failures, Crises & Mistakes in Products & Retail, Product Recalls, Failed Product Lines, Quality Disasters",
    ],
    "People & Leadership": [
        "Hiring, Recruitment, Talent Acquisition",
        "Onboarding, New Employee Integration, First 90 Days",
        "Offboarding, Layoffs, Employee Exit",
        "Compensation, Pay Strategy, Salary Structure",
        "Performance Management, Performance Reviews, Employee Performance",
        "Retention, Employee Retention, Keeping Talent",
        "Employee Relations, Labor Relations, Union Negotiations",
        "From Manager to Leader, Leadership Development, Becoming a Leader",
        "Team Management, Managing Teams, Team Building",
        "Mentoring, Employee Development, Coaching",
        "Organizational Design, Org Structure, Restructuring",
        "Culture Implementation, Company Culture, Culture Change",
        "Change Management, Organizational Transformation, Restructuring",
        "Famous Leadership Stories, Legendary People Decisions, Memorable Culture Moments",
        "Failures, Crises & Mistakes in Leadership, HR Disasters, Culture Crises, Executive Misconduct",
    ],
    "IT & Technology": [
        "Architecture & APIs, System Architecture, Technical Design",
        "Cloud & Scale, Cloud Infrastructure, Scalability",
        "Cybersecurity, Security, Information Security",
        "Service Management, IT Operations, Incident Management",
        "Digital Transformation, Digital Change, Technology Adoption",
        "Budgeting & Cost Control, IT Budget, Technology Costs",
        "Risk & Compliance, IT Risk, Regulatory Compliance",
        "Data & Analytics, Data Strategy, Business Intelligence",
        "Data Governance, Data Management, Data Quality",
        "What's AI, Artificial Intelligence, Machine Learning Basics",
        "Generative AI, LLMs, AI Tools",
        "Prompt Engineering, AI Prompting, Working with AI",
        "AI Agents, Autonomous AI, AI Automation",
        "Hallucinations & Bias, AI Limitations, AI Reliability",
        "Cost of AI, AI Economics, AI ROI",
        "The AI Myths, AI Reality, AI Misconceptions",
        "Famous Technology Decisions, Legendary Engineering Stories, Memorable Tech Moments",
        "Failures, Crises & Mistakes in Technology, System Outages, Cybersecurity Breaches, IT Disasters",
    ],
    "Operations": [
        "Data-Driven Management, Metrics, KPIs",
        "Lean & Continuous Improvement, Lean, Process Improvement",
        "Change Management, Operational Change, Process Change",
        "Project Management, PM, Project Delivery",
        "Agile & Scrum, Agile Methods, Scrum",
        "Crisis Management, Operational Crisis, Business Continuity",
        "Procurement & Contracts, Sourcing, Vendor Management",
        "Inventory & Logistics, Supply Chain, Logistics",
        "Service Design, Customer Service Operations, Service Delivery",
        "Field Operations, Frontline Operations, Ground Operations",
        "Famous Operations Stories, Legendary Execution Moments, Memorable Logistics Decisions",
        "Failures, Crises & Mistakes in Operations, Supply Chain Disasters, Logistics Crises, Production Failures",
    ],
    "Strategy": [
        "Industry Intelligence, Competitive Intelligence, Market Intelligence",
        "Stakeholders, Stakeholder Management, Stakeholder Engagement",
        "Strategic Analysis, SWOT, Strategic Planning",
        "Business Model Design, Business Model Innovation, Value Proposition",
        "Growth Paths, Growth Strategy, Scaling",
        "Corporate Strategy, Company Strategy, Strategic Direction",
        "Digital Strategy, Technology Strategy, Digital Transformation Strategy",
        "Innovations & Disruption, Disruptive Innovation, Innovation Strategy",
        "OKR & Goal Alignment, OKRs, Goal Setting",
        "Execution & Implementation, Strategy Execution, Implementation",
        "Famous Strategic Decisions, Legendary Business Moves, Memorable Strategy Stories",
        "Failures, Crises & Mistakes in Strategy, Failed Acquisitions, Pivots that Failed, Strategic Mistakes",
    ],
    "The Founder": [
        "Pet Project, Side Project, Passion Project",
        "Runway & Risk, Financial Runway, Startup Risk",
        "Ready to Launch, Launch Readiness, Go-to-Market",
        "Business Plan, Business Planning, Startup Plan",
        "Co-founders & Equity, Cofounder, Equity Split",
        "Network Capital, Founder Network, Startup Network",
        "First Customers, Early Adopters, Customer Acquisition",
        "Fundraising, Startup Funding, Venture Capital",
        "First Hires, Early Team, Founding Team",
        "Legal Basics, Startup Legal, Company Formation",
        "Commercial Mindset, Business Thinking, Revenue Focus",
        "Fail Forward, Startup Failure, Learning from Failure",
        "Smart Risk, Calculated Risk, Risk Taking",
        "The Founder, Founder Story, Entrepreneurship",
        "Famous Founder Stories, Legendary Entrepreneurial Moments, Personal Anecdotes About the Founder",
        "Failures, Crises & Mistakes of Founders, Near-Death Experiences, Founder Crises, Pivots Forced by Disaster",
    ],
    "Iconic Stories": [
        "Iconic Products, Signature Products, Best-Selling Products",
        "Iconic Decisions, Turning Points, Pivotal Moments",
        "Iconic Failures, Famous Mistakes, Lessons from Failure",
        "Iconic Marketing Campaigns, Legendary Ads, Viral Campaigns",
        "Iconic People, Key Figures, Unsung Heroes",
        "Iconic Crises, Scandals, Controversies",
        "Iconic Innovations, Breakthrough Inventions, First-of-a-Kind",
        "Iconic Partnerships, Famous Collaborations, Unexpected Alliances",
        "Iconic Customer Stories, Legendary Service Moments, Fan Stories",
        "Legendary One-Liners, Famous Personal Gestures, Iconic Moments That Became Company Folklore",
        "Iconic Origin Stories, Company Myths, Founding Legends",
    ],
    "Named Concepts": [
        "Named Internal Programs, Branded Internal Initiatives, Company-Specific Named Campaigns",
        "Named Pricing Concepts, Proprietary Price Architecture Terms, Named Entry-Price or Traffic-Driver Products",
        "Named Merchandising Programs, Visual Display Philosophies, Proprietary Store Layout Concepts",
        "Named Methodologies, Internal Frameworks, Proprietary Codes of Conduct, Branded Processes",
        "Named Cultural Rituals, Internal Practices, Company-Specific Traditions with a Proper Name",
        "Named Product Development Concepts, Internal Design Philosophies, Proprietary Innovation Programs",
    ],
}

AREAS = list(AREA_TOPICS.keys())

# Areas where subject = "key people at {company}" instead of "{company}" itself
PEOPLE_AREAS = {"Personal Growth", "Work & Career", "Office Survival"}

# Canonical order for all area calls: (area_slug, area_name, group_slug)
AREA_ORDER = [
    ("personal_growth",   "Personal Growth",    "people_skills"),
    ("work_career",       "Work & Career",       "people_skills"),
    ("office_survival",   "Office Survival",     "people_skills"),
    ("marketing",         "Marketing",           "commercial"),
    ("sales",             "Sales",               "commercial"),
    ("customer_success",  "Customer Success",    "commercial"),
    ("finance",           "Finance",             "business"),
    ("digital_product",   "Digital Product",     "product_tech"),
    ("goods_services",    "Goods & Services",    "product_tech"),
    ("people_leadership", "People & Leadership", "people_iconic"),
    ("it_technology",     "IT & Technology",     "product_tech"),
    ("operations",        "Operations",          "business"),
    ("strategy",          "Strategy",            "business"),
    ("the_founder",       "The Founder",         "people_iconic"),
    ("iconic_stories",    "Iconic Stories",      "people_iconic"),
    ("named_concepts",    "Named Concepts",      "named_concepts"),
]


# ── LLM clients ───────────────────────────────────────────────────────────────

def call_openai(prompt: str, model: str = "gpt-4o-mini", max_tokens: int = 4096) -> str:
    from openai import OpenAI
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        max_completion_tokens=max_tokens,
    )
    u = resp.usage
    if u:
        _emit_tokens(u.prompt_tokens, u.completion_tokens, model=model)
    return resp.choices[0].message.content or ""


def call_deepseek(prompt: str, model: str = "deepseek-chat", max_tokens: int = 4096) -> str:
    from openai import OpenAI
    client = OpenAI(api_key=os.environ["DEEPSEEK_API_KEY"], base_url="https://api.deepseek.com")
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
    )
    u = resp.usage
    if u:
        _emit_tokens(u.prompt_tokens, u.completion_tokens, model=model)
    return resp.choices[0].message.content or ""


def call_grok(prompt: str, model: str = "grok-3", max_tokens: int = 4096) -> str:
    from openai import OpenAI
    client = OpenAI(api_key=os.environ["XAI_API_KEY"], base_url="https://api.x.ai/v1")
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
    )
    u = resp.usage
    if u:
        _emit_tokens(u.prompt_tokens, u.completion_tokens, model=model)
    return resp.choices[0].message.content or ""


def call_kimi(prompt: str, model: str = "moonshot-v1-8k", max_tokens: int = 4096) -> str:
    from openai import OpenAI
    client = OpenAI(api_key=os.environ["KIMI_API_KEY"], base_url="https://api.moonshot.ai/v1")
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
    )
    u = resp.usage
    if u:
        _emit_tokens(u.prompt_tokens, u.completion_tokens, model=model)
    return resp.choices[0].message.content or ""


def _interruptible_call(stop_event, fn, *args, **kwargs):
    """Run fn(*args, **kwargs) in a daemon thread; return None if stop_event fires."""
    result = [None]
    exc = [None]
    def _run():
        try:
            result[0] = fn(*args, **kwargs)
        except Exception as e:
            exc[0] = e
    t = _threading.Thread(target=_run, daemon=True)
    t.start()
    while t.is_alive():
        if stop_event and stop_event.is_set():
            return None
        t.join(timeout=0.5)
    if exc[0]:
        raise exc[0]
    return result[0]


def call_claude(prompt: str, model: str = "claude-haiku-4-5-20251001", max_tokens: int = 4096) -> str:
    import anthropic
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    u = resp.usage
    if u:
        _emit_tokens(u.input_tokens, u.output_tokens, model=model)
    texts = [b.text for b in resp.content if hasattr(b, "text")]
    return "\n".join(texts) if texts else ""


def call_gemini(prompt: str, model: str = "gemini-3.5-flash-lite", max_tokens: int = 4096, grounding: bool = False) -> str:
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    config = types.GenerateContentConfig(
        max_output_tokens=max_tokens,
        tools=[types.Tool(google_search=types.GoogleSearch())] if grounding else [],
    )
    resp = client.models.generate_content(
        model=model,
        contents=prompt,
        config=config,
    )
    try:
        u = resp.usage_metadata
        in_tok  = u.prompt_token_count    or 0
        out_tok = u.candidates_token_count or 0
        if in_tok or out_tok:
            _emit_tokens(in_tok, out_tok, model=model)
    except Exception:
        pass
    if grounding:
        try:
            queries = resp.candidates[0].grounding_metadata.web_search_queries
            if queries:
                emit("log", {"msg": f"[grounding] queries: {list(queries)}"})
                _emit_api("grounding", len(queries))
        except Exception:
            pass
    try:
        text = resp.text or ""
    except Exception:
        # resp.text throws if finish_reason != STOP (e.g. MAX_TOKENS)
        try:
            parts = resp.candidates[0].content.parts
            text = "".join(p.text for p in parts if hasattr(p, "text")) or ""
        except Exception:
            return ""
    # Strip grounding citation markers like [1], [1.1], [2.3]
    text = re.sub(r'\[\d+(?:\.\d+)*\]', '', text)
    return text


def make_llm(llm_arg: str):
    if llm_arg == "gemini":
        _tokens["model"] = "gemini-3.5-flash-lite"
        return lambda prompt, max_tokens=4096: call_gemini(prompt, max_tokens=max_tokens)
    elif llm_arg in ("gpt4o-mini", "gpt-4o-mini"):
        _tokens["model"] = "gpt-4o-mini"
        return lambda prompt, max_tokens=4096: call_openai(prompt, model="gpt-4o-mini", max_tokens=max_tokens)
    elif llm_arg in ("gpt4o", "gpt-4o"):
        _tokens["model"] = "gpt-4o"
        return lambda prompt, max_tokens=4096: call_openai(prompt, model="gpt-4o", max_tokens=max_tokens)
    elif llm_arg in ("claude", "claude-opus"):
        _tokens["model"] = "claude-opus-5-20251101"
        return lambda prompt, max_tokens=4096: call_claude(prompt, model="claude-opus-5-20251101", max_tokens=max_tokens)
    elif llm_arg in ("claude-sonnet",):
        _tokens["model"] = "claude-sonnet-5"
        return lambda prompt, max_tokens=4096: call_claude(prompt, model="claude-sonnet-5", max_tokens=max_tokens)
    elif llm_arg in ("deepseek", "deepseek-chat"):
        _tokens["model"] = "deepseek-chat"
        return lambda prompt, max_tokens=4096: call_deepseek(prompt, model="deepseek-chat", max_tokens=max_tokens)
    else:
        raise ValueError(f"Unknown --llm value: {llm_arg}")


def _call_knowledge(prompt: str, model_name: str) -> str:
    """Call a specific model for knowledge dump; retries once on empty/error."""
    for attempt in range(2):
        try:
            if model_name == "openai":
                result = call_openai(prompt, model="gpt-5.6-luna", max_tokens=24000)
            elif model_name == "claude":
                emit("log", {"msg": f"[claude] attempt={attempt} calling claude-sonnet-5, prompt_len={len(prompt)}"})
                result = call_claude(prompt, model="claude-sonnet-5", max_tokens=24000)
                emit("log", {"msg": f"[claude] attempt={attempt} result len={len(result)} first80={result[:80]!r}"})
            elif model_name == "haiku":
                result = call_claude(prompt, model="claude-haiku-4-5-20251001", max_tokens=24000)
            elif model_name == "grok":
                result = call_grok(prompt, model="grok-4.3", max_tokens=24000)
            elif model_name == "gemini":
                result = call_gemini(prompt, model="gemini-3.6-flash", max_tokens=24000, grounding=True)
            elif model_name == "deepseek":
                result = call_deepseek(prompt, model="deepseek-chat", max_tokens=3000)
            elif model_name == "kimi":
                result = call_kimi(prompt, model="kimi-k2.6", max_tokens=8000)
            else:
                return ""
            stripped = result.strip() if result else ""
            if stripped == "(unknown company)":
                return result  # valid sentinel — model signals no knowledge, don't retry
            if stripped and len(stripped) > 20:
                return result
            if attempt == 0:
                emit("error", {"step": "knowledge_dump", "model": model_name, "error": f"empty response ({len(result)} chars), retrying"})
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            emit("error", {"step": "knowledge_dump", "model": model_name, "error": str(e), "trace": tb[-500:]})
    return ""


def gather_llm_knowledge(company: str, stop_event=None, continue_event=None, active_groups=None, active_models=None) -> str:
    """Query all available models in parallel for their list of named stories/events.

    openai/gemini: 5 parallel per-group calls with STAR format.
    deepseek/kimi:  1 call with simple single-line format (all areas).
    active_groups: list of group slugs to run (None = all 5).
    active_models: list of model keys to use (None = all available).
    """
    import threading

    _ACTIVE_MODELS = set(active_models) if active_models else {"openai", "gemini"}
    _GROUP_MODELS  = _ACTIVE_MODELS
    _N_ROUNDS = 1

    knowledge_items = []   # list of {"area": ..., "text": ...} for parse_carq_entries
    ki_lock = threading.Lock()

    areas_to_run = [
        (area_slug, area_name, group_slug) for area_slug, area_name, group_slug in AREA_ORDER
        if active_groups is None or area_slug in active_groups
    ]

    # Prompt builders
    def _build_group_prompt(group_slug, areas):
        topics_lines = []
        all_topics = []
        for area in areas:
            topics_lines.append(f"\n### {area}")
            for t in AREA_TOPICS.get(area, []):
                topics_lines.append(f"- {t}")
                all_topics.append(t)
        topics_text = "\n".join(topics_lines)
        is_people = all(a in PEOPLE_AREAS for a in areas)
        subject = f"key people at {company}" if is_people else company

        # One pass per topic, all topics
        _ORDINALS = [
            "ВТОРОЙ", "ТРЕТИЙ", "ЧЕТВЁРТЫЙ", "ПЯТЫЙ", "ШЕСТОЙ",
            "СЕДЬМОЙ", "ВОСЬМОЙ", "ДЕВЯТЫЙ", "ДЕСЯТЫЙ", "ОДИННАДЦАТЫЙ",
            "ДВЕНАДЦАТЫЙ", "ТРИНАДЦАТЫЙ", "ЧЕТЫРНАДЦАТЫЙ", "ПЯТНАДЦАТЫЙ",
            "ШЕСТНАДЦАТЫЙ", "СЕМНАДЦАТЫЙ", "ВОСЕМНАДЦАТЫЙ", "ДЕВЯТНАДЦАТЫЙ",
            "ДВАДЦАТЫЙ",
        ]
        focus_topics = all_topics[:len(_ORDINALS)]
        total_passes = len(focus_topics) + 1
        pass_markers = [f"=== {_ORDINALS[i]} ПРОХОД ===" for i in range(len(focus_topics))]
        pass_parts = []
        for i, topic in enumerate(focus_topics):
            marker = pass_markers[i]
            pass_parts.append(
                f"When your list is complete, write exactly:\n\n{marker}\n\n"
                f"Now focus exclusively on: {topic}\n"
                f"List every specific story, decision, or event related to this topic that you have NOT yet written. "
                f"After each entry, ask yourself: «Знаю ли я ещё одну историю по этой теме?» — if yes, write it. "
                f"Keep going until the answer is no. Same format."
            )
        if pass_parts:
            pass_parts[-1] += f"\n\nIMPORTANT: Do not stop until you have written all {total_passes} pass markers."
        topic_passes = "\n\n".join(pass_parts)

        prompt = (LLM_KNOWLEDGE_PROMPT_GROUP
                  .replace("{subject}", subject)
                  .replace("{topics}", topics_text)
                  .replace("{group_slug}", group_slug)
                  .replace("{topic_passes}", topic_passes))
        return prompt, pass_markers, focus_topics

    models = []
    if os.environ.get("OPENAI_API_KEY"):
        models.append("openai")
    if os.environ.get("GEMINI_API_KEY"):
        models.append("gemini")
    if os.environ.get("ANTHROPIC_API_KEY"):
        models.append("claude")
        models.append("haiku")
    if os.environ.get("XAI_API_KEY"):
        models.append("grok")
    models = [m for m in models if m in _ACTIVE_MODELS]

    tasks = [(m, r) for m in models for r in range(1, _N_ROUNDS + 1)]
    emit("step", {"step": "knowledge_gather", "status": "start", "models": [f"{m}_{r}" for m, r in tasks]})

    results = {}
    results_lock = threading.Lock()
    threads = []

    def _fetch_round(name, round_num):
        area_out = {}
        area_threads = []

        def _fetch_area(area_slug, area_name, group_slug):
            p, _pass_markers, _focus_topics = _build_group_prompt(area_slug, [area_name])
            text = _call_knowledge(p, name)
            with results_lock:
                area_out[area_name] = text
            emit("knowledge_group", {"model": f"{name}_{round_num}", "group": area_slug,
                                     "chars": len(text)})
            first_line = text.split('\n')[0].strip().lower() if text else ""
            _refusal = any(kw in text[:300].lower() for kw in ["я не могу", "i cannot", "i don't have", "у меня нет", "не располагаю"])
            _unknown_phrases = {"(unknown company)", "(неизвестная компания)", "(unknown)", "(нет данных)"}
            unknown = not text or first_line in _unknown_phrases or _refusal
            passes = sum(text.count(m) for m in _pass_markers) + 1 if not unknown else 0

            # Count new IDs per pass; build id→topic map
            pass_counts = []
            id_topic_map = {}
            if not unknown and passes > 1:
                markers = _pass_markers
                sections = re.split("|".join(re.escape(m) for m in markers), text)
                seen_ids = set()
                for i, sec in enumerate(sections):
                    topic = _focus_topics[i - 1] if i > 0 and i - 1 < len(_focus_topics) else None
                    ids = set(re.findall(r'ID:\s*(\S+)', sec))
                    new_ids = ids - seen_ids
                    pass_counts.append(len(new_ids))
                    if topic:
                        for id_val in new_ids:
                            id_topic_map[id_val] = topic
                    seen_ids |= ids
                pass_summary = " · ".join(
                    f"pass{i+1}: {c}" + (" new" if i > 0 else "") for i, c in enumerate(pass_counts)
                )
                emit("log", {"msg": f"[passes] {area_name} · {name}: {pass_summary}"})
                emit("log", {"msg": f"[topics] {area_name}: {len(id_topic_map)} IDs с топиком, примеры: {list(id_topic_map.items())[:3]}"})
            elif passes == 1 and not unknown:
                ids = re.findall(r'ID:\s*\S+', text)
                pass_counts = [len(ids)]

            emit("knowledge_model", {
                "model": f"{name}_{round_num}",
                "area": area_name,
                "group": group_slug,
                "text": text.strip() if not unknown else "",
                "unknown": unknown,
                "passes": passes,
                "pass_counts": pass_counts,
                "id_topics": id_topic_map,
            })
            if not unknown and text.strip():
                with ki_lock:
                    knowledge_items.append({"area": area_name, "text": text.strip(), "id_topics": id_topic_map})

        for area_slug, area_name, group_slug in areas_to_run:
            gt = threading.Thread(target=_fetch_area, args=(area_slug, area_name, group_slug), daemon=True)
            area_threads.append(gt)
            gt.start()
        for gt in area_threads:
            gt.join(timeout=600)

        combined = "\n\n".join(
            f"### {area_name}\n{area_out.get(area_name, '').strip()}"
            for _, area_name, _ in areas_to_run
            if area_out.get(area_name, "").strip()
        )
        with results_lock:
            results[(name, round_num)] = combined

    for m, r in tasks:
        t = threading.Thread(target=_fetch_round, args=(m, r), daemon=True)
        threads.append(t)
        t.start()

    deadline = time.time() + 700  # claude-sonnet-5 can take ~4 min per group
    for t in threads:
        while t.is_alive():
            if stop_event and stop_event.is_set():
                break
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            t.join(timeout=min(0.5, remaining))
        if stop_event and stop_event.is_set():
            break

    # Build model_texts from combined area results (knowledge_model events already emitted per-area)
    model_texts = {}
    for m, r in tasks:
        key = f"{m}_{r}"
        text = (results.get((m, r)) or "").strip()
        model_texts[key] = (text, not text)



    # Phase 2: concat all rounds — dedup+refine handled together by terra
    active_texts = [(key, text) for key, (text, unknown) in model_texts.items()
                    if not unknown and text.strip()]
    combined = "\n\n".join(f"=== {key.upper()} ===\n{text}" for key, text in active_texts)

    emit("step", {
        "step": "knowledge_gather", "status": "done",
        "models_responded": [f"{m}_{r}" for m, r in tasks if results.get((m, r), "").strip()],
        "models_count": len(active_texts),
        "merged_text": combined,
    })
    return knowledge_items


# ── PSTARQ query extractor (disabled — using grounding queries instead) ───────

# def extract_pstarq_queries(raw: str) -> dict:
#     result = {}
#     current_id = None
#     for line in raw.splitlines():
#         stripped = line.strip()
#         if stripped.startswith("ID:"):
#             current_id = stripped[3:].strip()
#         elif stripped.startswith("Q:") and current_id:
#             queries_raw = stripped[2:].strip()
#             queries = [q.strip() for q in queries_raw.split(",") if q.strip()]
#             if queries:
#                 result[current_id] = queries
#     return result


# ── Step 1: Query generation + domain detection ───────────────────────────────

def _parse_refine_events(text: str) -> list:
    """Parse refine output into [{title, summary_ru, score}] list."""
    events = []
    current = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if re.match(r'^\d+\.', stripped):
            if current is not None:
                events.append(current)
            current = {"title": re.sub(r'^\d+\.\s*', '', stripped), "summary_ru": "", "score": 2}
        elif stripped.startswith("RU:") and current is not None:
            current["summary_ru"] = stripped[3:].strip()
        elif stripped.startswith("SCORE:") and current is not None:
            try:
                current["score"] = int(stripped[6:].strip()[0])
            except (ValueError, IndexError):
                pass
    if current is not None:
        events.append(current)
    return events


def refine_events_with_sonnet(company: str, raw_knowledge: str) -> tuple:
    """Returns (refined_text, events_structured). Deduplicates and adds Russian summaries."""
    if not raw_knowledge.strip():
        return "", []
    raw_lines = [l for l in raw_knowledge.splitlines() if l.strip()]
    emit("step", {"step": "knowledge_refine", "status": "start", "input_lines": len(raw_lines)})
    prompt = REFINE_EVENTS_PROMPT.replace("{company}", company).replace("{raw}", raw_knowledge)
    try:
        if os.environ.get("OPENAI_API_KEY"):
            result = call_openai(prompt, model="gpt-5.6-terra", max_tokens=32000)
        elif os.environ.get("ANTHROPIC_API_KEY"):
            result = call_claude(prompt, model="claude-sonnet-5", max_tokens=32000)
        else:
            result = raw_knowledge
    except Exception as e:
        emit("error", {"step": "knowledge_refine", "error": str(e)})
        result = raw_knowledge
    events_structured = _parse_refine_events(result)
    emit("step", {
        "step": "knowledge_refine", "status": "done",
        "events": len(events_structured),
        "refined_text": result,
        "events_structured": events_structured,
    })
    return result, events_structured


def generate_story_queries(company: str, events_text: str, llm, max_n: int = 20) -> list:
    """Turn refined event list into targeted Brave search queries."""
    if not events_text.strip():
        return []
    emit("step", {"step": "story_queries", "status": "start"})
    n_events = len([l for l in events_text.splitlines() if l.strip()])
    n = min(max_n, max(5, n_events))
    prompt = STORY_QUERIES_PROMPT.replace("{company}", company).replace("{events}", events_text).replace("{n}", str(n))
    raw = llm(prompt, max_tokens=800)
    match = re.search(r"\[.*\]", raw, re.DOTALL)
    queries = []
    if match:
        try:
            queries = json.loads(match.group())
        except json.JSONDecodeError:
            pass
    emit("step", {"step": "story_queries", "status": "done", "count": len(queries), "queries": queries})
    return queries


def generate_area_queries(company: str, llm, n: int = 15) -> tuple:
    """Generate area-based queries + detect company domain."""
    emit("step", {"step": "area_queries", "status": "start", "company": company})
    areas_text = ", ".join(AREAS)
    prompt = AREA_QUERIES_PROMPT.format(n=n, company=company, areas=areas_text)
    raw = llm(prompt, max_tokens=1200)
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        emit("error", {"msg": "Area query generation returned no JSON", "raw": raw[:300]})
        return [], ""
    try:
        data = json.loads(match.group())
    except json.JSONDecodeError:
        emit("error", {"msg": "JSON parse error in area query generation", "raw": raw[:300]})
        return [], ""
    queries = data.get("queries", [])
    domain = data.get("domain", "").lower().strip()
    emit("step", {
        "step": "area_queries", "status": "done",
        "count": len(queries), "queries": queries, "domain": domain,
    })
    return queries, domain


# ── Step 2: Brave Search ──────────────────────────────────────────────────────

BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"


def brave_search(query: str, api_key: str, count: int = 5) -> list:
    _emit_api("brave")
    headers = {"Accept": "application/json", "X-Subscription-Token": api_key}
    params = {"q": query, "count": count, "text_decorations": False}
    try:
        resp = requests.get(BRAVE_URL, headers=headers, params=params, timeout=10)
        resp.raise_for_status()
        # extract quota from response headers
        h = {k.lower(): v for k, v in resp.headers.items()}
        quota = {}
        for key in ("x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset",
                    "ratelimit-limit", "ratelimit-remaining"):
            if key in h:
                try:
                    quota[key.replace("x-", "").replace("ratelimit-", "")] = int(h[key])
                except ValueError:
                    pass
        if quota:
            emit("api_quota", {"api": "brave", "quota": quota, "calls": _api["brave"]})
        data = resp.json()
        results = data.get("web", {}).get("results", [])
        if not results:
            # surface empty/error body so we can diagnose quota/auth issues
            emit("warn", {"step": "brave_search", "query": query,
                          "status": resp.status_code, "response_keys": list(data.keys()),
                          "detail": str(data.get("message") or data.get("error") or "")[:200]})
        return [{"url": r["url"], "title": r.get("title", ""), "description": r.get("description", "")} for r in results]
    except Exception as e:
        emit("error", {"step": "brave_search", "query": query, "error": str(e)})
        return []


def tavily_search(query: str, api_key: str, max_results: int = 5) -> list:
    """Tavily Search API — returns search results with full article content included.
    Each result: {url, title, content (snippet), raw_content (full text), score}
    """
    _emit_api("tavily")
    try:
        resp = requests.post(
            TAVILY_URL,
            json={
                "api_key": api_key,
                "query": query,
                "search_depth": "basic",
                "include_raw_content": True,
                "max_results": max_results,
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results", [])
        if not results:
            emit("warn", {"step": "tavily_search", "query": query,
                          "detail": str(data.get("error") or data.get("message") or "empty")[:200]})
        return results
    except Exception as e:
        emit("error", {"step": "tavily_search", "query": query, "error": str(e)})
        return []


def serper_search(query: str, api_key: str, num: int = 10) -> list:
    """Google search via Serper.dev — returns [{url, title, snippet}].
    $0.001/query, free tier 2500 queries/month.
    """
    _emit_api("serper")
    try:
        resp = requests.post(
            SERPER_URL,
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            json={"q": query, "num": num},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        results = data.get("organic", [])
        if not results:
            emit("warn", {"step": "serper_search", "query": query,
                          "detail": str(data.get("error") or "empty")[:200]})
        return [{"url": r["link"], "title": r.get("title", ""), "snippet": r.get("snippet", "")}
                for r in results if r.get("link")]
    except Exception as e:
        emit("error", {"step": "serper_search", "query": query, "error": str(e)})
        return []


def tavily_extract(urls: list, api_key: str) -> list:
    """Extract full content from URLs via Tavily Extract API.
    Returns [{url, raw_content}] for successful extractions.
    Cost: 1 credit per 5 URLs (basic). Max 20 URLs per request.
    """
    if not urls:
        return []
    n_credits = max(1, (len(urls) + 4) // 5)
    _emit_api("tavily", count=n_credits)
    try:
        resp = requests.post(
            "https://api.tavily.com/extract",
            json={"api_key": api_key, "urls": urls},
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("results", [])
    except Exception as e:
        emit("error", {"step": "tavily_extract", "error": str(e)})
        return []


def is_company_domain(url: str, domain: str) -> bool:
    if not domain:
        return False
    try:
        from urllib.parse import urlparse
        host = urlparse(url).netloc.lower().lstrip("www.")
        # match exact domain or subdomains
        return host == domain or host.endswith("." + domain)
    except Exception:
        return False


def run_brave_searches(queries: list, api_key: str, exclude_domain: str = "",
                       results_per_query: int = 5, stop_event=None,
                       step_key: str = "brave_search") -> list:
    emit("step", {"step": step_key, "status": "start", "queries": len(queries)})
    all_results = []
    seen_urls = set()
    filtered_count = 0

    for i, q in enumerate(queries):
        if stop_event and stop_event.is_set():
            break
        emit("search", {"query": q, "index": i + 1, "total": len(queries)})
        results = brave_search(q, api_key, count=results_per_query)
        new_count = 0
        for r in results:
            if r["url"] in seen_urls:
                continue
            if is_company_domain(r["url"], exclude_domain):
                filtered_count += 1
                continue
            seen_urls.add(r["url"])
            r["query"] = q
            all_results.append(r)
            new_count += 1
        emit("search_results", {"query": q, "index": i + 1, "count": new_count})
        time.sleep(0.15)

    emit("step", {
        "step": step_key, "status": "done",
        "urls_found": len(all_results), "filtered": filtered_count,
    })
    return all_results


# ── Step 3: Jina Reader scraping ─────────────────────────────────────────────

JINA_BASE = "https://r.jina.ai/"
MAX_CHARS = 20000

# Domains that consistently return 0 bytes through Jina (paywalled or bot-blocked)
BLOCKED_DOMAINS = {
    "nytimes.com", "wsj.com", "ft.com", "bloomberg.com",
    "reuters.com", "apnews.com",
    "dallasnews.com", "chicagotribune.com", "chicagobusiness.com",
    "forbes.com", "businessinsider.com",
    "npr.org", "washingtonpost.com",
    "medium.com",
    "chegg.com", "coursehero.com", "studocu.com", "scribd.com",
    "case-law.vlex.com", "casetext.com",
    "sec.gov",
    "downdetector.com",
}


def _is_blocked(url: str) -> bool:
    from urllib.parse import urlparse
    host = urlparse(url).hostname or ""
    host = host.removeprefix("www.")
    return any(host == d or host.endswith("." + d) for d in BLOCKED_DOMAINS)


def scrape_jina(url: str) -> str:
    _emit_api("jina")
    try:
        resp = requests.get(JINA_BASE + url, timeout=20, headers={"Accept": "text/plain"})
        # extract Jina quota from headers
        h = {k.lower(): v for k, v in resp.headers.items()}
        quota = {}
        for key in ("x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset",
                    "x-ratelimit-limit-requests", "x-ratelimit-remaining-requests",
                    "ratelimit-limit", "ratelimit-remaining"):
            if key in h:
                try:
                    short = key.replace("x-ratelimit-", "").replace("ratelimit-", "").replace("-requests", "")
                    quota[short] = int(h[key])
                except ValueError:
                    pass
        if quota:
            emit("api_quota", {"api": "jina", "quota": quota, "calls": _api["jina"]})
        if resp.status_code == 200:
            text = resp.text
            if len(text) > MAX_CHARS:
                text = text[:MAX_CHARS].rsplit(" ", 1)[0] + " …"
            return text
        return ""
    except Exception:
        return ""


def scrape_all(search_results: list, stop_event=None, step_key: str = "scraping") -> list:
    emit("step", {"step": step_key, "status": "start", "urls": len(search_results)})
    scraped = []
    for i, r in enumerate(search_results):
        if stop_event and stop_event.is_set():
            break
        url = r["url"]
        if _is_blocked(url):
            emit("scrape_done", {"url": url, "chars": 0, "ok": False, "skipped": True})
            continue
        emit("scrape", {"url": url, "index": i + 1, "total": len(search_results)})
        content = scrape_jina(url)
        chars = len(content)
        emit("scrape_done", {"url": url, "chars": chars, "ok": chars > 200})
        if chars > 200:
            scraped.append({**r, "content": content})
    emit("step", {"step": step_key, "status": "done", "scraped": len(scraped)})
    return scraped


# ── Step 4: Analysis ──────────────────────────────────────────────────────────

# Per-group CARQ-format prompt for openai/gemini (5 calls per model per round)
LLM_KNOWLEDGE_PROMPT_GROUP = """List every specific named story, decision, practice, technique, ritual, tactic, principle, insight, campaign, or event you know about {subject}. Be exhaustive — recall everything concrete. Include famous and widely-cited stories too — do not skip them because they seem well-known.

Work through each topic below:
{topics}

Use this format for each entry (C, A and I in Russian, Q in English):

ID: {group_slug}-N
C: [year, location — names of the people involved]
A: [Full story including outcome and impact. Exhaust everything you know: who was involved, what exactly happened step by step, exact numbers/amounts, specific decisions and their reasons, concrete results and lasting impact. Write every detail you can recall — what was said, who was in the room, what happened first/then/next. No length limit. Mark uncertain details with [?].]
I: [Два предложения по-русски. Первое: что нестандартного или неочевидного в этом решении — чем оно отличается от того, что делают в похожей ситуации обычно. Второе: универсальный принцип — в каких ситуациях и почему этот подход работает, без привязки к конкретному бизнесу (например: «Работает тогда, когда...» или «Принцип: если [условие] → то [механизм] → потому что [причина]»).]
Q: [5 English search queries to find this specific story online, comma-separated]

---

Rules:
- C, A, I: write in Russian
- Q: write in English — 5 short search queries on one line, comma-separated
- A: field has no length limit — write every detail you know, not a summary; include the outcome and numbers at the end
- If you know a quote or letter only approximately, write your best recollection and mark it [?] — never leave quotes empty
- Each entry must describe ONE specific event, decision, or moment — if you know several related stories, write each as a separate entry with its own ID
- Only write what you actually know — do not invent details you are unsure of
- Mark uncertain details [?]
- Include person names, dollar amounts, product names, campaign names whenever known
- Actively recall named proprietary concepts, internal company terms, and branded techniques specific to this company — these are often the most valuable entries (e.g. a named ritual, a specific internal program, a trademarked method)
- If you have little verified knowledge about a topic, skip it entirely
- No intros, no summaries, no commentary — only the list
- If you have little knowledge about this company, write "(unknown company)" on the first line and stop

{topic_passes}"""

# Simpler single-line format for deepseek/kimi (single call, all areas)
LLM_KNOWLEDGE_PROMPT_SIMPLE = """List every specific named business story, decision, campaign, crisis, or event you know about "{company}". Be exhaustive — recall everything concrete you can.

Work through each domain below. For each domain, list ALL specific named stories you know — aim for 3–5+ per domain:
{areas}

Format — one line per entry, no numbers, no bullets:
[Domain] — [specific named event with details] — [year if known]

Good examples:
Finance — "Fuel hedging program locked in oil at $51/barrel, generating ~$1.4B gain when crude hit $147" — 2008
Operations — "Malice in Dallas — arm wrestling match vs Stevens Aviation CEO Kurt Herwald over Plane Smart slogan" — 1992

Rules:
- Only include events you are confident about — no guesses
- Include the person's name, number, or campaign name whenever possible
- Skip generic programs or culture descriptions that have no specific story
- Do NOT add commentary — only the list
- If you have little knowledge about this company, write "(unknown company)" on the first line and stop"""



REFINE_EVENTS_PROMPT = """You received story entries about "{company}" from multiple AI models. Each entry has fields: ID, C (context: year + actors), A (full story with outcome), I (insight — what's clever or unexpected about this decision), Q (search queries).

Your task:
1. Remove ONLY true duplicates — entries describing THE EXACT SAME specific event, practice, or technique. Use the A field to distinguish similar-sounding entries. When in doubt — keep BOTH.
2. Convert each remaining entry to the output format below.

CRITICAL RULES:
- Output must contain AT LEAST 80% of distinct input entries. Do NOT cherry-pick. Do NOT summarize or reduce.
- Two entries with different A are DIFFERENT — keep both.
- Remove only: exact/near-exact duplicates (same specific event or technique from different models).

Output format — exactly three lines per entry:
N. [group or area] — [event/practice title — derive from C or A] — [year from C, if known]
   RU: [In Russian: who did what, what exactly happened, what concrete numbers/outcomes resulted. Include all key specifics — names, amounts, decisions, consequences. No length limit.]
   SCORE: [1, 2, or 3]

RU must answer: "What is the story here?" — not "what does this program aim to do".
BAD: "Программа направлена на улучшение навыков управления."
GOOD: "Когда конкурент снизил цену до $13, Southwest предложила выбор: лететь за $13 или за $26 и получить бутылку Chivas Regal — и временно стала крупнейшим продавцом алкоголя в Техасе."

SCORE criteria:
3 — specific story: named people or dates, concrete numbers/amounts, counterintuitive decision, clear mechanism of why it worked
2 — has some specifics but less vivid; or mechanism is obvious but outcome is concrete
1 — generic: no numbers, no named actors, sounds like any company ("launched a program", "improved processes", "focused on quality")

Return ONLY the numbered list. No intro, no commentary.

RAW INPUT:
{raw}"""


STORY_QUERIES_PROMPT = """Based on this list of named events and stories about "{company}":
{events}

Write {n} precise search queries — one per major event — to find detailed journalism, books, or case studies about each specific event.

Rules:
- Name the specific event, campaign, or person in the query (not just the company name)
- Include year if known
- 5–10 words per query
- Target: journalism, books, HBR, podcasts, Wikipedia

Return ONLY a JSON array: ["query 1", "query 2", ...]"""


AREA_QUERIES_PROMPT = """You are preparing research on "{company}".

Task A — return the primary website domain of {company} (e.g. "ikea.com"). Just the domain, no protocol.

Task B — generate {n} search queries to find business analysis and journalism about "{company}" across different areas.

Areas to cover: {areas}

Rules:
- Cover as many different areas as possible
- 4–8 words per query
- Find EXTERNAL analysis: journalism, books, podcasts, HBR, case studies
- Different angles: history, decisions, strategy, crises, people, operations

Return ONLY valid JSON:
{{"domain": "example.com", "queries": ["query 1", "query 2", ...]}}"""


ANALYSIS_PROMPT = """You extract RICH, DETAILED business stories from content about "{company}".

TARGET EVENTS — research ONLY these specific events. For each event, output AT MOST ONE story, combining the best details from all sources below:
{selected_events}

CRITICAL: One story per target event maximum. If multiple sources describe the same event, pick the details from the most informative source and merge unique facts. Do NOT create separate stories for the same event from different sources.

If a target event has no concrete verifiable details in the content below, skip it entirely.

Output FORMAT — copy exactly, no deviations:

## [Area Name]

ID: [area-slug]-N
P: [one sentence — a transferable universal principle or named insight]
S: [STORY: 3–5 sentence narrative with: specific year, named people, named campaign/program/decision, dollar amounts or percentages, concrete outcome — all from the content below]
M: [MECHANISM: 1–2 sentences — WHY or HOW this worked, the underlying business logic]
TERM: [2–4 key business terms in English, comma-separated]

---

QUALITY EXAMPLE:

## Finance

ID: finance-1
P: Securitizing an intangible asset converts future income streams into immediate liquidity.
S: In 2020, American Airlines pledged their AAdvantage loyalty program as collateral for a $10B loan — the largest airline asset-backed loan in history, upsized from $7.5B due to investor oversubscription. The program was structured through a bankruptcy-remote SPV, isolating the asset from the airline's operational risks. Analyst Stifel publicly stated that AAdvantage was "the only reason" American Airlines avoided bankruptcy during the pandemic.
M: Converting contractual future cash flows (miles redemption obligations) into present liquidity via securitization allows an intangible asset to generate immediate capital — a mechanism unavailable through traditional physical-asset collateral.
TERM: asset securitization, collateralization of intangibles, bankruptcy-remote SPV

---

AREAS TO COVER:
{areas}

STRICT RULES:
- S must use ONLY facts present in the CONTENT below
- 3 verifiable facts minimum in S — if you can't find 3, SKIP the story entirely
- M explains WHY/HOW — never repeats what happened
- TERM stays in English
- Named events, campaigns, people as they appear in the source text
- [?] for uncertain details; never invent what isn't there

CONTENT:
---
{content}
---"""

STAR_PATTERN = re.compile(
    r"ID:\s*(?P<id>[^\n]+)\nP:\s*"
    r"(?P<p>[^\n]+)\nS:\s*"
    r"(?P<s>.+?)\nM:\s*"
    r"(?P<m>.+?)\nTERM:\s*"
    r"(?P<term>.+?)(?=\n---|\nID:|\Z)",
    re.DOTALL,
)


def parse_stories(raw: str, company: str, source_urls: list) -> list:
    stories = []
    sections = re.split(r"(?=^## )", raw, flags=re.MULTILINE)
    for section in sections:
        area_m = re.match(r"##\s*(.+)", section.strip())
        area = area_m.group(1).strip() if area_m else "Unknown"
        for m in STAR_PATTERN.finditer(section):
            stories.append({
                "company": company,
                "area": area,
                "story_id": m.group("id").strip(),
                "principle": m.group("p").strip(),
                "situation": m.group("s").strip(),   # full story narrative
                "task": m.group("m").strip(),          # mechanism
                "action": m.group("term").strip(),     # business terms
                "result": "",
                "source_urls": json.dumps(source_urls[:5]),
                "created_at": datetime.now().isoformat(),
            })
    return stories


def analyze_content(company: str, scraped: list, llm, stop_event=None, selected_events=None) -> str:
    MIN_CHARS = 500
    MAX_BATCH_CHARS = 60_000  # safe for gpt-4o-mini 128k context

    quality = [s for s in scraped if len(s.get("content", "")) >= MIN_CHARS]
    quality.sort(key=lambda s: len(s.get("content", "")), reverse=True)

    emit("step", {"step": "analysis", "status": "start", "pages": len(quality), "filtered_out": len(scraped) - len(quality)})
    areas_text = "\n".join(f"- {a}" for a in AREAS)
    sel_text = "\n".join(f"- {e}" for e in selected_events) if selected_events else "(extract any notable business story)"

    # Build batches: pack pages until MAX_BATCH_CHARS, then start new batch
    batches: list[list[dict]] = []
    current_batch: list[dict] = []
    current_chars = 0
    for doc in quality:
        chunk = f"SOURCE: {doc['url']}\nTITLE: {doc.get('title', '')}\n{doc['content']}"
        if current_chars + len(chunk) > MAX_BATCH_CHARS and current_batch:
            batches.append(current_batch)
            current_batch = [{"doc": doc, "chunk": chunk}]
            current_chars = len(chunk)
        else:
            current_batch.append({"doc": doc, "chunk": chunk})
            current_chars += len(chunk)
    if current_batch:
        batches.append(current_batch)

    all_output = []
    global_story_n = [0]

    for bi, batch in enumerate(batches):
        if stop_event and stop_event.is_set():
            break
        combined_content = "\n\n--- NEXT SOURCE ---\n\n".join(item["chunk"] for item in batch)
        urls = [item["doc"]["url"] for item in batch]
        total_chars = len(combined_content)
        emit("llm_call", {
            "chunk": bi + 1, "total_chunks": len(batches),
            "pages_in_batch": len(batch), "content_chars": total_chars,
        })
        prompt = (ANALYSIS_PROMPT
                  .replace("{company}", company)
                  .replace("{selected_events}", sel_text)
                  .replace("{areas}", areas_text)
                  .replace("{content}", combined_content))
        # Use gpt-4o-mini directly — cheap, sufficient for factual extraction
        result = _interruptible_call(
            stop_event, call_openai, prompt,
            model="gpt-4o-mini", max_tokens=4096,
        )
        if result is None:
            break
        story_count = len(re.findall(r"\nID:", result))
        emit("llm_done", {"chunk": bi + 1, "stories_found": story_count, "output_chars": len(result)})

        partial = parse_stories(result, company, urls)
        for s in partial:
            global_story_n[0] += 1
            s["story_id"] = f"{s['story_id']}-{global_story_n[0]}"
            emit("story_found", {
                "area": s["area"],
                "id": s["story_id"],
                "principle": s["principle"],
                "situation": s["situation"],
            })
        all_output.append(result)

    combined = "\n\n".join(all_output)
    total_stories = len(re.findall(r"\nID:", combined))
    emit("step", {"step": "analysis", "status": "done", "total_stories": total_stories})
    return combined


# ── Step 5: Translation ───────────────────────────────────────────────────────

TRANSLATE_PROMPT = """Переведи на русский язык следующие бизнес-истории.

Переводи поля P, S, M — точно, сохраняя все факты, числа, даты, имена программ.
Поле TERM — НЕ переводи (бизнес-термины остаются на английском).
Сохраняй структуру: ID, P, S, M, TERM, ---.
Имена людей (CEO, основатели), названия компаний, программ, кампаний — не переводи.
Цифры, проценты, даты — точно.

{text}"""


def translate_stories(raw: str, llm, stop_event=None) -> str:
    n = len(re.findall(r"\nID:", raw))
    if not n:
        return raw
    emit("step", {"step": "translation", "status": "start", "stories": n})

    MAX = 35000
    if len(raw) <= MAX:
        parts = [raw]
    else:
        sections = re.split(r"(?=^## )", raw, flags=re.MULTILINE)
        parts, cur, cur_len = [], [], 0
        for sec in sections:
            if cur_len + len(sec) > MAX and cur:
                parts.append("".join(cur))
                cur, cur_len = [sec], len(sec)
            else:
                cur.append(sec); cur_len += len(sec)
        if cur:
            parts.append("".join(cur))

    translated = []
    for i, part in enumerate(parts):
        if stop_event and stop_event.is_set():
            break
        emit("llm_call", {"chunk": i + 1, "total_chunks": len(parts), "content_chars": len(part), "prompt_chars": len(part) + 200})
        result = _interruptible_call(stop_event, llm, TRANSLATE_PROMPT.replace("{text}", part), max_tokens=8000)
        if result is None:
            break
        emit("llm_done", {"chunk": i + 1, "stories_found": len(re.findall(r"\nID:", result))})
        translated.append(result)

    emit("step", {"step": "translation", "status": "done"})
    return "\n\n".join(translated)


# ── Step 6: Persist ───────────────────────────────────────────────────────────

def init_db(db_path: str):
    conn = sqlite3.connect(db_path)
    conn.execute("""CREATE TABLE IF NOT EXISTS stories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company TEXT NOT NULL, area TEXT NOT NULL, story_id TEXT,
        principle TEXT, situation TEXT, task TEXT, action TEXT, result TEXT,
        source_urls TEXT, created_at TEXT
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS api_usage (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        company TEXT,
        api TEXT NOT NULL,
        model TEXT,
        count INTEGER DEFAULT 1,
        cost REAL DEFAULT 0.0,
        tokens_in INTEGER DEFAULT 0,
        tokens_out INTEGER DEFAULT 0
    )""")
    conn.commit()
    return conn


def save_to_db(conn: sqlite3.Connection, stories: list):
    conn.executemany("""
        INSERT INTO stories (company,area,story_id,principle,situation,task,action,result,source_urls,created_at)
        VALUES (:company,:area,:story_id,:principle,:situation,:task,:action,:result,:source_urls,:created_at)
    """, stories)
    conn.commit()


def save_to_md(company: str, stories: list, output_dir: Path, suffix: str = "") -> Path:
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M")
    fname = output_dir / f"{company.replace(' ', '_')}_{ts}{suffix}.md"
    by_area = {}
    for s in stories:
        by_area.setdefault(s["area"], []).append(s)
    with open(fname, "w", encoding="utf-8") as f:
        f.write(f"# {company}\n\n")
        f.write(f"_Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}_\n\n")
        for area, area_stories in by_area.items():
            f.write(f"## {area} ({len(area_stories)} entries)\n\n")
            for s in area_stories:
                f.write(f"ID: {s['story_id']}\n")
                f.write(f"P: {s['principle']}\n")
                f.write(f"S: {s['situation']}\n")
                f.write(f"T: {s['task']}\n")
                f.write(f"A: {s['action']}\n")
                f.write(f"R: {s['result']}\n\n---\n\n")
    return fname


# ── Web enrichment (from scratch) ────────────────────────────────────────────

_SKIP_DOMAINS = {"pinterest.com", "instagram.com", "facebook.com", "twitter.com",
                 "x.com", "tiktok.com", "youtube.com", "quora.com"}

_PREFER_DOMAINS = {"reuters.com", "bbc.com", "nytimes.com", "wsj.com", "bloomberg.com",
                   "ft.com", "hbr.org", "inc.com", "forbes.com", "fastcompany.com",
                   "businessinsider.com", "medium.com", "wikipedia.org"}


def _domain_score(url: str) -> int:
    try:
        from urllib.parse import urlparse
        domain = urlparse(url).netloc.lower().replace("www.", "")
    except Exception:
        return 0
    if any(d in domain for d in _SKIP_DOMAINS):
        return -1
    if any(d in domain for d in _PREFER_DOMAINS):
        return 2
    return 1


def parse_carq_entries(raw_knowledge: list, company: str) -> list:
    """Parse CARQ-formatted text blocks from gather_llm_knowledge output."""
    stories = []
    seen_ids = set()
    for item in raw_knowledge:
        area = item.get("area", "")
        text = item.get("text", "")
        # split into blocks starting with ID:
        blocks, cur = [], None
        for line in text.split('\n'):
            if re.match(r'^ID:\s*\S', line.strip(), re.IGNORECASE):
                if cur:
                    blocks.append(cur)
                cur = [line]
            elif cur is not None:
                cur.append(line)
        if cur:
            blocks.append(cur)

        id_topics = item.get("id_topics", {})
        for block in blocks:
            id_val = re.sub(r'^ID:\s*', '', block[0].strip(), flags=re.IGNORECASE)
            if id_val in seen_ids:
                continue
            fields, cur_key, cur_val = {}, None, []
            for line in block[1:]:
                tk = line.strip()
                if tk.startswith('===') or tk.startswith('#'):
                    continue
                m = re.match(r'^([A-Z]+):\s*(.*)', tk)
                if m:
                    if cur_key:
                        fields[cur_key] = '\n'.join(cur_val).strip()
                    cur_key, cur_val = m.group(1), [m.group(2)]
                elif cur_key:
                    cur_val.append(tk)
            if cur_key:
                fields[cur_key] = '\n'.join(cur_val).strip()

            q_raw = fields.get('Q', '')
            queries = []
            for q in q_raw.split(','):
                q = q.strip()
                for noise in ['Знаю ли', 'Now focus', '===', '\n']:
                    idx = q.find(noise)
                    if idx == 0:
                        q = ''
                        break
                    elif idx > 0:
                        q = q[:idx].strip()
                if q and not q.startswith('#') and len(q) < 200:
                    queries.append(q)
            if fields.get('A'):
                seen_ids.add(id_val)
                stories.append({
                    'id': id_val,
                    'company': company,
                    'area': area,
                    'topic': id_topics.get(id_val, ''),
                    'C': fields.get('C', ''),
                    'A': fields.get('A', ''),
                    'I': fields.get('I', ''),
                    'Q': queries,
                })
    return stories


FACT_EXTRACT_PROMPT = """Ты помощник по извлечению фактов из веб-статьи для обогащения бизнес-истории.

Исходная история (A):
{story_a}

Текст веб-страницы (может быть шумным — игнорируй навигацию, рекламу, не связанный контент):
---
{article_text}
---

Извлеки только то, что относится к истории выше. Кратко, по пунктам. Отвечай сразу с первого раздела — без вступления и преамбулы.

ПОДТВЕРЖДАЕТ: [факты, совпадающие с историей]
ДОБАВЛЯЕТ: [новые имена, даты, цифры, цитаты, детали которых нет в истории]
ПРОТИВОРЕЧИТ: [факты, расходящиеся с историей; «—» если нет]
КОНЦЕПТЫ: [именованные программы, внутренние термины, брендированные техники; «—» если нет]"""


NARRATIVE_PROMPT = """Ты обогащаешь конкретную бизнес-историю верифицированными фактами из веб-источников.

Исходная история описывает КОНКРЕТНОЕ СОБЫТИЕ или РЕШЕНИЕ:
C: {story_c}
A: {story_a}
I (ключевой инсайт): {story_i}

Факты из веб-источников (по каждому URL — разделы ПОДТВЕРЖДАЕТ/ДОБАВЛЯЕТ/ПРОТИВОРЕЧИТ/КОНЦЕПТЫ):
{fact_sheet}

Напиши обогащённый текст поля A на русском языке.

ГЛАВНОЕ ПРАВИЛО: ты обогащаешь ТОЛЬКО ту историю, которая описана в A выше. Не добавляй другие истории про эту компанию, даже если источники их упоминают. Если источник говорит о другом событии или другом человеке — игнорируй.

Остальные правила:
- Добавляй из «ДОБАВЛЯЕТ» только детали про это же конкретное событие: имена, цифры, цитаты, механику, итоги
- Уточняй уже имеющееся с помощью «ПОДТВЕРЖДАЕТ»
- Если источники противоречат оригиналу — отметь коротко [источник расходится: ...]
- Не выдумывай ничего, чего нет в оригинале или в фактах
- Сохраняй стиль и язык оригинала; только текст истории — никаких вступлений"""


def _extract_fact_card(story_a: str, article_text: str, cheap_llm) -> str:
    prompt = FACT_EXTRACT_PROMPT.format(
        story_a=story_a[:1500],
        article_text=article_text[:12000],
    )
    try:
        return cheap_llm(prompt, max_tokens=1200)
    except Exception as e:
        return f"[ошибка: {e}]"


def enrich_story_web(story: dict, serper_key: str, tavily_key: str, cheap_llm, expensive_llm) -> dict:
    story_id = story.get('id', '?')
    queries = story.get('Q', [])[:5]
    story_a = story.get('A', '')

    if not queries:
        emit("log", {"msg": f"[enrich] {story_id} · нет Q:-запросов, пропуск"})
        return {**story, 'A_enriched': story_a, 'sources': [], 'fact_cards': [], 'failed_sources': []}

    emit("log", {"msg": f"[enrich] {story_id} · Serper поиск: {queries}"})

    # 1. Serper (Google) search: 3 queries in parallel → collect URLs
    def _search(q):
        return serper_search(q, serper_key, num=10)

    all_results = []
    with ThreadPoolExecutor(max_workers=3) as ex:
        for results in ex.map(_search, queries):
            all_results.extend(results)

    seen, urls = set(), []
    for r in all_results:
        url = r.get('url', '')
        if url and url not in seen and _domain_score(url) >= 0:
            seen.add(url); urls.append(url)
    urls = urls[:20]

    emit("log", {"msg": f"[enrich] {story_id} · {len(urls)} URL → Tavily Extract"})

    if not urls:
        return {**story, 'A_enriched': story_a, 'sources': [], 'fact_cards': [], 'failed_sources': []}

    # 2. Tavily Extract: scrape all URLs at once
    extracted = tavily_extract(urls, tavily_key)
    extracted_urls = {r['url'] for r in extracted}
    useful = [r for r in extracted if r.get('raw_content') and len(r['raw_content']) > 300]
    failed_urls = [u for u in urls if u not in extracted_urls] + \
                  [r['url'] for r in extracted if not r.get('raw_content') or len(r.get('raw_content','')) <= 300]

    emit("log", {"msg": f"[enrich] {story_id} · {len(useful)}/{len(urls)} с контентом"})

    if not useful:
        return {**story, 'A_enriched': story_a, 'sources': [], 'fact_cards': [], 'failed_sources': failed_urls}

    # 3. Parallel Flash Lite fact extraction
    def _ext_one(r):
        try:
            card = _extract_fact_card(story_a, r['raw_content'], cheap_llm)
            return {'url': r['url'], 'card': card}
        except Exception as e:
            emit("log", {"msg": f"[enrich] {r['url'][:60]} · fact card error: {e}"})
            return None

    with ThreadPoolExecutor(max_workers=min(5, len(useful))) as ex:
        fact_cards = [c for c in ex.map(_ext_one, useful) if c]

    if not fact_cards:
        return {**story, 'A_enriched': story_a, 'sources': [], 'fact_cards': [], 'failed_sources': failed_urls}

    # 4. Expensive model writes final narrative
    fact_sheet = "\n\n".join(
        f"[{i+1}] {fc['url']}\n{fc['card']}" for i, fc in enumerate(fact_cards))
    prompt = NARRATIVE_PROMPT.format(
        story_c=story.get('C', ''), story_a=story_a,
        story_i=story.get('I', ''), fact_sheet=fact_sheet)
    try:
        a_enriched = expensive_llm(prompt, max_tokens=4000)
    except Exception as e:
        a_enriched = story_a
        emit("log", {"msg": f"[enrich] {story_id} · ошибка нарратива: {e}"})

    sources = [fc['url'] for fc in fact_cards]
    emit("log", {"msg": f"[enrich] {story_id} · готово ({len(sources)} источников, {len(failed_urls)} без контента)"})
    emit("enrich_story", {
        'id': story_id, 'area': story.get('area', ''), 'company': story.get('company', ''),
        'C': story.get('C', ''), 'A': story_a, 'I': story.get('I', ''),
        'A_enriched': a_enriched, 'sources': sources,
    })
    return {**story, 'A_enriched': a_enriched, 'sources': sources, 'fact_cards': fact_cards, 'failed_sources': failed_urls}


def enrich_all_stories(stories: list, serper_key: str, tavily_key: str, cheap_llm, expensive_llm,
                       stop_event=None, max_workers: int = 4) -> list:
    if not stories:
        return []

    # ── Phase 1: Serper (Google) search → collect URLs ────────────────────────
    def _serper_search(s):
        queries = s.get('Q', [])[:5]
        if not queries:
            return {**s, '_urls': []}
        story_id = s.get('id', '?')

        def _search(q):
            return serper_search(q, serper_key, num=10)

        all_results = []
        with ThreadPoolExecutor(max_workers=3) as ex:
            for results in ex.map(_search, queries):
                all_results.extend(results)

        seen, urls = set(), []
        for r in all_results:
            url = r.get('url', '')
            if url and url not in seen and _domain_score(url) >= 0:
                seen.add(url); urls.append(url)

        emit("log", {"msg": f"[serper] {story_id} · {len(urls)} URLs from {len(queries)} queries"})
        return {**s, '_urls': urls}

    # ── Phase 2: Tavily Extract → Flash Lite fact extraction ─────────────────
    def _extract(s):
        urls = s.get('_urls', [])[:25]
        story_id = s.get('id', '?')
        story_a = s.get('A', '')

        if not urls:
            return {**s, '_fact_cards': [], '_sources': [], '_failed': []}

        extracted = tavily_extract(urls, tavily_key)
        useful = [r for r in extracted if r.get('raw_content') and len(r['raw_content']) > 300]
        extracted_urls = {r['url'] for r in extracted}
        failed = [u for u in urls if u not in extracted_urls] + \
                 [r['url'] for r in extracted if not r.get('raw_content') or len(r.get('raw_content','')) <= 300]

        def _ext_one(r):
            card = _extract_fact_card(story_a, r['raw_content'], cheap_llm)
            return {'url': r['url'], 'card': card}

        fact_cards = []
        if useful:
            with ThreadPoolExecutor(max_workers=min(5, len(useful))) as ex:
                fact_cards = list(ex.map(_ext_one, useful))

        emit("log", {"msg": f"[extract] {story_id} · {len(fact_cards)}/{len(urls)} with content"})
        return {**s, '_fact_cards': fact_cards, '_sources': [fc['url'] for fc in fact_cards], '_failed': failed}

    # ── Phase 3: Flash narrative ──────────────────────────────────────────────
    def _narrative(s):
        fact_cards = s.get('_fact_cards', [])
        sources = s.get('_sources', [])
        failed = s.get('_failed', [])
        story_id = s.get('id', '?')
        clean = {k: v for k, v in s.items() if not k.startswith('_')}
        if not fact_cards:
            return {**clean, 'A_enriched': s.get('A', ''), 'sources': [], 'fact_cards': [], 'failed_sources': failed}
        fact_sheet = "\n\n".join(
            f"[{i+1}] {fc['url']}\n{fc['card']}" for i, fc in enumerate(fact_cards))
        prompt = NARRATIVE_PROMPT.format(
            story_c=s.get('C', ''), story_a=s.get('A', ''),
            story_i=s.get('I', ''), fact_sheet=fact_sheet)
        try:
            a_enriched = expensive_llm(prompt, max_tokens=4000)
        except Exception as e:
            a_enriched = s.get('A', '')
            emit("log", {"msg": f"[narrative] {story_id} · ошибка: {e}"})
        emit("log", {"msg": f"[narrative] {story_id} · готово ({len(sources)} источников, {len(failed)} без контента)"})
        emit("enrich_story", {
            'id': story_id, 'area': s.get('area', ''), 'company': s.get('company', ''),
            'C': s.get('C', ''), 'A': s.get('A', ''), 'I': s.get('I', ''),
            'A_enriched': a_enriched, 'sources': sources,
        })
        return {**clean, 'A_enriched': a_enriched, 'sources': sources, 'fact_cards': fact_cards, 'failed_sources': failed}

    # ── Orchestration ─────────────────────────────────────────────────────────
    emit("step", {"step": "enrich_brave", "status": "start", "total": len(stories)})
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        searched = list(ex.map(_serper_search, stories))
    emit("step", {"step": "enrich_brave", "status": "done"})

    if stop_event and stop_event.is_set():
        return []

    emit("step", {"step": "enrich_extract", "status": "start", "total": len(stories)})
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        extracted = list(ex.map(_extract, searched))
    emit("step", {"step": "enrich_extract", "status": "done"})

    if stop_event and stop_event.is_set():
        return []

    emit("step", {"step": "enrich_narrative", "status": "start", "total": len(stories)})
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        results = list(ex.map(_narrative, extracted))
    emit("step", {"step": "enrich_narrative", "status": "done"})

    return results


# ── Main pipeline ─────────────────────────────────────────────────────────────

def run_pipeline(company: str, llm_arg: str, n_queries: int = 15,
                 db_path: str = None, output_dir: str = None,
                 stop_event=None, continue_event=None, pipeline_context=None,
                 active_groups=None, active_models=None,
                 enrich_model: str = "gemini-3.6-flash"):
    serper_key = os.environ.get("SERPER_API_KEY", "")
    tavily_key = os.environ.get("TAVILY_API_KEY", "")
    if not serper_key:
        emit("error", {"msg": "SERPER_API_KEY not set"})
        return None
    if not tavily_key:
        emit("error", {"msg": "TAVILY_API_KEY not set"})
        return None

    db_path = db_path or str(Path(__file__).parent / "data.db")
    output_dir = Path(output_dir or Path(__file__).parent / "output")
    output_dir.mkdir(exist_ok=True)

    global _db_path_global, _current_company
    _db_path_global = db_path
    _current_company = company

    monthly = _load_monthly_usage(db_path)
    _tokens.update({"in": 0, "out": 0, "cost": 0.0, "by_model": {}})
    _api.update({"brave": monthly["brave"], "jina": monthly["jina"], "grounding": monthly["grounding"],
                 "tavily": monthly["tavily"], "serper": monthly["serper"]})
    emit("pipeline_start", {"company": company, "llm": llm_arg, "n_queries": n_queries,
                             "monthly": monthly})

    def stopped():
        return stop_event and stop_event.is_set()

    # ── 1. Gather knowledge (CARQ format, grounding enabled) ──────────────────
    raw_knowledge = gather_llm_knowledge(company, stop_event=stop_event,
                                         active_groups=active_groups, active_models=active_models)
    if stopped():
        emit("pipeline_stopped", {"stage": "after_knowledge_gather"})
        return None

    # ── 2. Parse CARQ entries from all areas ──────────────────────────────────
    stories = parse_carq_entries(raw_knowledge, company)
    emit("log", {"msg": f"[pipeline] {len(stories)} историй распарсено"})
    for s in stories:
        emit("story_found", s)
    if not stories:
        emit("error", {"msg": "Нет историй после парсинга"})
        return None
    if stopped():
        emit("pipeline_stopped", {"stage": "after_parse"})
        return None

    # ── 3. Done — enrichment runs separately via Enrich buttons ─────────────────
    emit("pipeline_done", {
        "company": company,
        "total_stories": len(stories),
        "llm_cost": round(_tokens["cost"], 4),
    })
    return {"stories": stories}


# ── Story enrichment (web search → LLM) ──────────────────────────────────────

def enrich_story(story: dict) -> dict:
    """Search web for a story, scrape top articles, ask LLM to add details."""
    brave_key = os.environ.get("BRAVE_API_KEY", "")
    if not brave_key:
        return {"error": "No BRAVE_API_KEY"}

    company  = story.get("company", "")
    principle = story.get("principle", "")
    situation = story.get("situation", "")
    action    = story.get("action", "")

    # Build query from company + first 6 words of principle + year from situation
    year_m = re.search(r'\b(19|20)\d{2}\b', situation or "")
    year   = year_m.group(0) if year_m else ""
    key    = " ".join(principle.split()[:6]) if principle else ""
    query  = f'"{company}" {key} {year}'.strip()

    results = brave_search(query, brave_key, count=5)
    urls = [r["url"] for r in results if r.get("url")][:3]

    # Scrape up to 3 articles
    articles = []
    for url in urls:
        try:
            text = scrape_jina(url)
            if text and len(text) > 200:
                articles.append({"url": url, "text": text[:3000]})
        except Exception:
            pass

    if not articles:
        return {"error": "No articles found", "sources": []}

    context = "\n\n".join(
        f"SOURCE: {a['url']}\n{a['text']}" for a in articles
    )

    prompt = f"""You are enriching a business story about {company}.

ORIGINAL STORY:
Principle: {principle}
Situation: {situation}
Action: {action}

WEB SOURCES:
{context}

Based on these sources, rewrite the Action field in Russian with every specific detail you find:
- Exact quotes and dialogue verbatim
- Specific numbers, dollar amounts, percentages
- Full names of all people involved
- The mechanism explaining WHY the result happened
- Sequence of events step by step

Only include details found in the sources. If sources conflict, note it with [?].
After the enriched action, add a line: ИСТОЧНИКИ: followed by the URLs used, one per line.
Return ONLY the enriched action text + sources. No intro."""

    enriched = call_openai(prompt, model="gpt-4o-mini", max_tokens=2000)
    return {"enriched_action": enriched, "sources": [a["url"] for a in articles]}


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--company", required=True)
    parser.add_argument("--llm", default="gpt4o-mini")
    parser.add_argument("--queries", type=int, default=15)
    parser.add_argument("--db", default=None)
    parser.add_argument("--output", default=None)
    args = parser.parse_args()
    run_pipeline(args.company, args.llm, n_queries=args.queries,
                 db_path=args.db, output_dir=args.output)
