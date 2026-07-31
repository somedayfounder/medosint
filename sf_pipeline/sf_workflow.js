export const meta = {
  name: 'sf-pipeline',
  description: 'Someday Founder: collect principles & STAR stories for a company across all 14 areas',
  phases: [
    { title: 'Collect', detail: 'LLM memory dump per area in parallel' },
    { title: 'Search', detail: 'Web search to surface stories LLMs missed' },
  ],
}

// Usage: Workflow({ scriptPath: 'sf_pipeline/sf_workflow.js', args: { company: 'IKEA' } })
// Or with list: args: { companies: ['IKEA', 'FedEx', 'Patagonia'] }

const AREA_TOPICS = {
  'Personal Growth': [
    'Habits, Daily Routines, Behavior Change',
    'Goals & Achievements, Goal Setting, Milestones',
    'Focus & Attention, Deep Work, Productivity',
    'Digital Hygiene, Screen Time, Digital Detox',
    'Critical Thinking, Problem Solving, Analytical Thinking',
    'Decision Making, Judgment, Choice Under Uncertainty',
    'Creative & Strategic Thinking, Innovation Mindset, Lateral Thinking',
    'Biases & Perception, Cognitive Biases, Mental Models',
    'Self-Talk, Inner Critic, Mindset',
    'Stress Management, Burnout, Resilience',
    'Work-Life Balance, Boundaries, Personal Sustainability',
  ],
  'Work & Career': [
    'Career & Growth, Career Pivots, Career Development',
    'Mentors & Sponsors, Mentorship, Career Sponsorship',
    'Managing Setbacks, Failure Recovery, Bouncing Back',
    'Personal Branding, Professional Reputation, Thought Leadership',
    'Resume, CV, Job Application',
    'Job Search, Career Change, Job Hunt',
    'Interview Essentials, Job Interview, Interview Preparation',
    'Behavioral & Case Interviews, Case Studies, STAR Method',
    'Networking, Professional Network, Building Connections',
    'Self-Advocacy, Speaking Up, Career Visibility',
    'Business Writing, Professional Communication, Writing Skills',
    'Active Listening, Communication Skills, Listening',
    'Visual Communication, Data Visualization, Visual Storytelling',
    'Presentation & Public Speaking, Keynotes, Pitching',
    'Difficult Conversations, Conflict Resolution, Hard Talks',
    'Feedback Skills, Giving Feedback, Performance Reviews',
  ],
  'Office Survival': [
    'Office Rules, Workplace Norms, Corporate Culture',
    'Chats & Emails, Business Communication, Email Etiquette',
    'Meetings, Meeting Culture, Meeting Management',
    'Small Talks, Casual Conversations, Ice Breakers',
    'Managing Up, Managing Your Boss, Upward Management',
    'Cross-Functional Collaboration, Team Collaboration, Breaking Silos',
    'Resources & Salary, Salary Negotiation, Compensation',
    'Under Pressure, High Stakes, Working Under Pressure',
    'Personal Safety, Psychological Safety, Workplace Safety',
    'Crisis Management, Workplace Crisis, Damage Control',
    'Conflicts & Escalations, Workplace Conflict, Escalation',
    'Office Politics, Workplace Dynamics, Organizational Politics',
    'Zero Effort Office, Efficiency, Working Smarter',
    'Small Wins Strategy, Quick Wins, Incremental Progress',
  ],
  'Marketing': [
    'Competitive Analysis, Price Wars, Market Competition',
    'Product Positioning, Market Differentiation, Unique Selling Proposition',
    'Brand Strategy, Brand Building, Brand Turnaround',
    'Brand Identity, Visual Branding, Logo & Design',
    'Storytelling, Content Marketing, Brand Narrative',
    'Social Media, Viral Marketing, Online Community',
    'PR & Media Relations, Crisis PR, Press Coverage',
    'Partners & Affiliates, Co-marketing, Strategic Partnerships',
    'Affiliate & Referral, Word-of-Mouth, Referral Programs',
    'Events & Sponsorship, Experiential Marketing, Sponsorship Deals',
    'Community Management, Brand Advocates, Fan Community',
    'Market Research, Consumer Behavior, Survey & Focus Groups',
    'SEO & Organic, Search Marketing, Organic Growth',
    'Paid Ads, Advertising Campaigns, TV Commercials',
    'Email Campaigns, Direct Marketing, Newsletter',
  ],
  'Sales': [
    'B2B Distribution, Channel Sales, Distribution Networks',
    'Retail Sales, Store Sales, Retail Strategy',
    'E-commerce Sales, Online Sales, Digital Commerce',
    'Trade Marketing, Shopper Marketing, Retail Promotions',
    'B2B Sales Process, Sales Pipeline, Sales Methodology',
    'Account-Based Selling, Key Account Management, Enterprise Accounts',
    'Enterprise Sales, Large Account Sales, Complex Sales',
    'CRM & Systems, Sales Technology, Sales Tools',
    'Sales Playbooks, Sales Scripts, Sales Enablement',
    'Business Negotiation, Negotiation Tactics, Deal Making',
    'Objections & Closing, Handling Objections, Closing Techniques',
    'Relationship Management, Client Relationships, Account Management',
  ],
  'Customer Success': [
    'Customer Journey, Customer Experience, CX',
    'Onboarding & Adoption, Customer Onboarding, Product Adoption',
    'Churn Prevention, Retention, Reducing Churn',
    'Renewal & Growth, Upsell, Expansion Revenue',
    'Loyalty Programs, Customer Loyalty, Rewards Programs',
    'Feedback & Advocacy, Customer Feedback, NPS',
  ],
  'Finance': [
    'Budgeting & Forecasting, Financial Planning, Budget Management',
    'Accounting, Financial Reporting, Books',
    'Working Capital, Cash Flow, Liquidity',
    'Scenario Planning, Stress Testing, Financial Scenarios',
    'Applied Economics, Market Economics, Economic Analysis',
    'Revenue Models, Business Models, Monetization',
    'Unit Economics, LTV, CAC',
    'Pricing, Price Strategy, Pricing Models',
    'Risk Management, Financial Risk, Hedging',
    'Financial Modeling, Valuation, Financial Analysis',
  ],
  'Digital Product': [
    'Customer Insights, User Research, Customer Discovery',
    'User Experience & Design, UX, Product Design',
    'Validation & MVP, Product Validation, Minimum Viable Product',
    'Roadmapping & Prioritization, Product Roadmap, Feature Prioritization',
    'Business Analysis, Requirements, Product Requirements',
    'Delivery Methods, Development Process, Agile',
    'Release Management, Deployment, Product Releases',
    'Go-to-Market, Product Launch, GTM Strategy',
    'Product/Market Fit, PMF, Finding Fit',
    'Product-Led Growth, PLG, Virality',
    'Decisions with Data, Analytics, Data-Driven Product',
    'Product Lifecycle, Product Evolution, Product Maturity',
  ],
  'Goods & Services': [
    'Purchase Triggers, Buying Behavior, Consumer Decision Making',
    'Shopper Journey, Customer Path, In-Store Behavior',
    'Category Management, Shelf Management, Retail Categories',
    'Product Concept, New Product Development, Concept Testing',
    'Development Frameworks, Product Development Process, Stage-Gate',
    'Packaging Design, Product Packaging, Package Innovation',
    'Brand & Line Management, Product Portfolio, Brand Architecture',
    'Distribution Strategy, Channel Strategy, Market Distribution',
    'Product Sunset, Product Discontinuation, End of Life',
  ],
  'People & Leadership': [
    'Hiring, Recruitment, Talent Acquisition',
    'Onboarding, New Employee Integration, First 90 Days',
    'Offboarding, Layoffs, Employee Exit',
    'Compensation, Pay Strategy, Salary Structure',
    'Performance Management, Performance Reviews, Employee Performance',
    'Retention, Employee Retention, Keeping Talent',
    'Employee Relations, Labor Relations, Union Negotiations',
    'From Manager to Leader, Leadership Development, Becoming a Leader',
    'Team Management, Managing Teams, Team Building',
    'Mentoring, Employee Development, Coaching',
    'Organizational Design, Org Structure, Restructuring',
    'Culture Implementation, Company Culture, Culture Change',
    'Change Management, Organizational Transformation, Restructuring',
  ],
  'IT & Technology': [
    'Architecture & APIs, System Architecture, Technical Design',
    'Cloud & Scale, Cloud Infrastructure, Scalability',
    'Cybersecurity, Security, Information Security',
    'Service Management, IT Operations, Incident Management',
    'Digital Transformation, Digital Change, Technology Adoption',
    'Budgeting & Cost Control, IT Budget, Technology Costs',
    'Risk & Compliance, IT Risk, Regulatory Compliance',
    'Data & Analytics, Data Strategy, Business Intelligence',
    'Data Governance, Data Management, Data Quality',
    "What's AI, Artificial Intelligence, Machine Learning Basics",
    'Generative AI, LLMs, AI Tools',
    'Prompt Engineering, AI Prompting, Working with AI',
    'AI Agents, Autonomous AI, AI Automation',
    'Hallucinations & Bias, AI Limitations, AI Reliability',
    'Cost of AI, AI Economics, AI ROI',
    'The AI Myths, AI Reality, AI Misconceptions',
  ],
  'Operations': [
    'Data-Driven Management, Metrics, KPIs',
    'Lean & Continuous Improvement, Lean, Process Improvement',
    'Change Management, Operational Change, Process Change',
    'Project Management, PM, Project Delivery',
    'Agile & Scrum, Agile Methods, Scrum',
    'Crisis Management, Operational Crisis, Business Continuity',
    'Procurement & Contracts, Sourcing, Vendor Management',
    'Inventory & Logistics, Supply Chain, Logistics',
    'Service Design, Customer Service Operations, Service Delivery',
    'Field Operations, Frontline Operations, Ground Operations',
  ],
  'Strategy': [
    'Industry Intelligence, Competitive Intelligence, Market Intelligence',
    'Stakeholders, Stakeholder Management, Stakeholder Engagement',
    'Strategic Analysis, SWOT, Strategic Planning',
    'Business Model Design, Business Model Innovation, Value Proposition',
    'Growth Paths, Growth Strategy, Scaling',
    'Corporate Strategy, Company Strategy, Strategic Direction',
    'Digital Strategy, Technology Strategy, Digital Transformation Strategy',
    'Innovations & Disruption, Disruptive Innovation, Innovation Strategy',
    'OKR & Goal Alignment, OKRs, Goal Setting',
    'Execution & Implementation, Strategy Execution, Implementation',
  ],
  'The Founder': [
    'Pet Project, Side Project, Passion Project',
    'Runway & Risk, Financial Runway, Startup Risk',
    'Ready to Launch, Launch Readiness, Go-to-Market',
    'Business Plan, Business Planning, Startup Plan',
    'Co-founders & Equity, Cofounder, Equity Split',
    'Network Capital, Founder Network, Startup Network',
    'First Customers, Early Adopters, Customer Acquisition',
    'Fundraising, Startup Funding, Venture Capital',
    'First Hires, Early Team, Founding Team',
    'Legal Basics, Startup Legal, Company Formation',
    'Commercial Mindset, Business Thinking, Revenue Focus',
    'Fail Forward, Startup Failure, Learning from Failure',
    'Smart Risk, Calculated Risk, Risk Taking',
    'The Founder, Founder Story, Entrepreneurship',
  ],
  'Iconic Stories': [
    'Iconic Products, Signature Products, Best-Selling Products',
    'Iconic Decisions, Turning Points, Pivotal Moments',
    'Iconic Failures, Famous Mistakes, Lessons from Failure',
    'Iconic Marketing Campaigns, Legendary Ads, Viral Campaigns',
    'Iconic People, Key Figures, Unsung Heroes',
    'Iconic Crises, Scandals, Controversies',
    'Iconic Innovations, Breakthrough Inventions, First-of-a-Kind',
    'Iconic Partnerships, Famous Collaborations, Unexpected Alliances',
    'Iconic Customer Stories, Legendary Service Moments, Fan Stories',
    'Iconic Origin Stories, Company Myths, Founding Legends',
  ],
}

const AREAS = Object.keys(AREA_TOPICS)
const PEOPLE_AREAS = new Set(['Personal Growth', 'Work & Career', 'Office Survival'])

function buildPrompt(company, area) {
  const topics = AREA_TOPICS[area].map(t => `- ${t}`).join('\n')
  const subject = PEOPLE_AREAS.has(area) ? `key people at ${company}` : company
  const areaSlug = area.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/(^-|-$)/g, '')
  return `List every specific named story, decision, campaign, crisis, internal practice, or event you know about ${subject}. Be exhaustive — recall everything concrete. Aim for 3–5+ entries per topic, not just one.

Work through each topic below:
${topics}

For each entry, output exactly this format:

ID: ${areaSlug}-N
P: [one sentence — universal principle or named technique, applicable to any business]
S: [year, location, context — be specific]
T: [specific person or team, specific problem they faced]
A: [exactly what was done — names, numbers, dollar amounts, campaign names, product names]
R: [outcome — with numbers if available; if not, describe the qualitative impact]

---

Rules:
- Include person names, dollar amounts, product names, campaign names whenever known
- Skip generic culture descriptions or programs with no specific named incident
- If you have little verified knowledge about a topic for this company, skip it — do not invent
- Mark uncertain details [?]
- No intros, no summaries, no commentary — only the list`
}

const SUMMARY_SCHEMA = {
  type: 'object',
  properties: {
    top: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          id: { type: 'string' },
          story_ru: { type: 'string' },
          principle_ru: { type: 'string' },
        },
        required: ['id', 'story_ru', 'principle_ru']
      }
    }
  },
  required: ['top']
}

async function runCompany(company) {
  log(`Starting: ${company}`)

  const results = await parallel(AREAS.map(area => async () => {
    const text = await agent(buildPrompt(company, area), { label: `${company}/${area}` })
    return { area, text }
  }))

  const valid = results.filter(Boolean).filter(r => r.text && r.text.length > 500)
  const count = valid.reduce((n, r) => n + (r.text.match(/\nID:/g) || []).length, 0)

  log(`Done: ${company} — ${valid.length} areas, ~${count} entries. Building summary...`)

  const allText = valid.map(r => `## ${r.area}\n\n${r.text}`).join('\n\n---\n\n')

  const summary = await agent(
    `Ниже — истории и техники о компании ${company}, собранные по 14 областям.

Выбери топ-30 самых ярких, конкретных и запоминающихся записей. Приоритет: операционные детали, неочевидные решения, парадоксы, конкретные цифры.

Для каждой из топ-30:
- id: точный ID из текста
- story_ru: 1–2 предложения — что конкретно сделали и что получили, с цифрами если есть
- principle_ru: 1 фраза — универсальный принцип или название техники, применимые к любому бизнесу

${allText}`,
    { label: `summary/${company}`, schema: SUMMARY_SCHEMA }
  )

  return { company, areas: valid, count, summary: summary?.top || [] }
}

// args may arrive as a JSON string — parse if needed
const parsed = typeof args === 'string' ? JSON.parse(args) : (args || {})

// Support single company or list
const companies = parsed.companies
  ? parsed.companies
  : parsed.company
    ? [parsed.company]
    : ['Southwest Airlines']

const allResults = []
for (const company of companies) {
  const result = await runCompany(company)
  allResults.push(result)
}

return allResults
