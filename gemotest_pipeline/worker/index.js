const REPO = 'somedayfounder/medosint';
const WORKFLOW = 'gemotest_run.yml';

const CORS = {
  'Access-Control-Allow-Origin': '*',
  'Content-Type': 'application/json',
};

function json(data, status = 200) {
  return new Response(JSON.stringify(data), { status, headers: CORS });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (request.method === 'OPTIONS')
      return new Response(null, {
        status: 204,
        headers: {
          'Access-Control-Allow-Origin': '*',
          'Access-Control-Allow-Methods': 'GET,POST',
          'Access-Control-Allow-Headers': 'Content-Type',
        },
      });

    const gh = async (path, method = 'GET', body = null) => {
      const r = await fetch(
        `https://api.github.com/repos/${REPO}/${path}`,
        {
          method,
          headers: {
            Authorization: `token ${env.GH_TOKEN}`,
            Accept: 'application/vnd.github.v3+json',
            'Content-Type': 'application/json',
            'User-Agent': 'gemotest-pipeline',
          },
          body: body ? JSON.stringify(body) : null,
        }
      );
      return r;
    };

    try {
      if (request.method === 'POST' && url.pathname === '/trigger') {
        const inputs = await request.json();
        const r = await gh(`actions/workflows/${WORKFLOW}/dispatches`, 'POST', { ref: 'main', inputs });
        const body = await r.text();
        return json({ ok: r.status === 204, gh_status: r.status, gh_body: body });
      }

      if (url.pathname === '/runs') {
        const r = await gh('actions/runs?event=workflow_dispatch&per_page=10');
        const text = await r.text();
        let d;
        try { d = JSON.parse(text); } catch { return json({ error: 'GitHub API returned non-JSON', raw: text.slice(0, 200), run: null }, 502); }
        const since = url.searchParams.get('since') || '';
        const run = (d.workflow_runs || []).find(x => !since || new Date(x.created_at) >= new Date(since));
        return json({ run: run || null });
      }

      if (url.pathname === '/status') {
        const r = await gh('actions/runs/' + url.searchParams.get('runId'));
        const text = await r.text();
        let d;
        try { d = JSON.parse(text); } catch { return json({ error: 'GitHub API returned non-JSON', status: 'unknown' }, 502); }
        return json(d);
      }

      return json({ ok: true });
    } catch (e) {
      return json({ error: e.message }, 500);
    }
  },
};
