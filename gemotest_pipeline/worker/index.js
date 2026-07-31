const REPO = 'somedayfounder/medosint';
const WORKFLOW = 'run.yml';
const ALLOWED_ORIGIN = 'https://somedayfounder.github.io';

const cors = (origin) => ({
  'Access-Control-Allow-Origin': origin === ALLOWED_ORIGIN ? ALLOWED_ORIGIN : 'null',
  'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
  'Access-Control-Allow-Headers': 'Content-Type',
});

function json(data, status = 200, origin = '') {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'Content-Type': 'application/json', ...cors(origin) },
  });
}

async function gh(env, path, method = 'GET', body = null) {
  const opts = {
    method,
    headers: {
      Authorization: `token ${env.GH_TOKEN}`,
      Accept: 'application/vnd.github.v3+json',
      'Content-Type': 'application/json',
    },
  };
  if (body) opts.body = JSON.stringify(body);
  return fetch(`https://api.github.com/repos/${REPO}/${path}`, opts);
}

export default {
  async fetch(request, env) {
    const origin = request.headers.get('Origin') || '';
    const url = new URL(request.url);

    if (request.method === 'OPTIONS') {
      return new Response(null, { status: 204, headers: cors(origin) });
    }

    // POST /trigger  — запустить workflow
    if (request.method === 'POST' && url.pathname === '/trigger') {
      const inputs = await request.json();
      const r = await gh(env, `actions/workflows/${WORKFLOW}/dispatches`, 'POST', {
        ref: 'main',
        inputs,
      });
      if (r.status === 204) return json({ ok: true }, 200, origin);
      const err = await r.text();
      return json({ ok: false, error: err }, r.status, origin);
    }

    // GET /runs?since=ISO  — найти последний запуск после времени
    if (request.method === 'GET' && url.pathname === '/runs') {
      const since = url.searchParams.get('since') || '';
      const r = await gh(env, 'actions/runs?event=workflow_dispatch&per_page=10');
      if (!r.ok) return json({ error: 'github error' }, r.status, origin);
      const d = await r.json();
      const runs = (d.workflow_runs || []).filter(
        run => !since || new Date(run.created_at) >= new Date(since)
      );
      return json({ run: runs[0] || null }, 200, origin);
    }

    // GET /status?runId=xxx  — статус конкретного запуска
    if (request.method === 'GET' && url.pathname === '/status') {
      const runId = url.searchParams.get('runId');
      if (!runId) return json({ error: 'missing runId' }, 400, origin);
      const r = await gh(env, `actions/runs/${runId}`);
      if (!r.ok) return json({ error: 'github error' }, r.status, origin);
      return json(await r.json(), 200, origin);
    }

    return json({ error: 'not found' }, 404, origin);
  },
};
