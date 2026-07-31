const REPO = 'somedayfounder/medosint';
const WORKFLOW = 'gemotest_run.yml';

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const cors = { 'Access-Control-Allow-Origin': '*', 'Content-Type': 'application/json' };

    if (request.method === 'OPTIONS')
      return new Response(null, { status: 204, headers: { 'Access-Control-Allow-Origin': '*', 'Access-Control-Allow-Methods': 'GET,POST', 'Access-Control-Allow-Headers': 'Content-Type' } });

    const gh = (path, method = 'GET', body = null) => fetch(
      `https://api.github.com/repos/${REPO}/${path}`,
      { method, headers: { Authorization: `token ${env.GH_TOKEN}`, Accept: 'application/vnd.github.v3+json', 'Content-Type': 'application/json', 'User-Agent': 'gemotest-pipeline' }, body: body ? JSON.stringify(body) : null }
    );

    if (request.method === 'POST' && url.pathname === '/trigger') {
      const inputs = await request.json();
      const r = await gh(`actions/workflows/${WORKFLOW}/dispatches`, 'POST', { ref: 'main', inputs });
      const body = await r.text();
      return new Response(JSON.stringify({ ok: r.status === 204, gh_status: r.status, gh_body: body }), { headers: cors });
    }
    if (url.pathname === '/runs') {
      const r = await gh('actions/runs?event=workflow_dispatch&per_page=10');
      const d = await r.json();
      const since = url.searchParams.get('since') || '';
      const run = (d.workflow_runs || []).find(x => !since || new Date(x.created_at) >= new Date(since));
      return new Response(JSON.stringify({ run: run || null }), { headers: cors });
    }
    if (url.pathname === '/status') {
      const r = await gh('actions/runs/' + url.searchParams.get('runId'));
      return new Response(JSON.stringify(await r.json()), { headers: cors });
    }
    return new Response('ok', { headers: cors });
  }
};
