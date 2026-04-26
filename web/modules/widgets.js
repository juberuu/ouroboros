function escapeHtml(value) {
    return String(value ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

function pageTemplate() {
    return `
        <section class="page" id="page-widgets">
            <div class="page-header">
                <h2>Widgets</h2>
                <button id="widgets-refresh" class="btn btn-default">Refresh</button>
            </div>
            <p class="muted">Reviewed extension UI surfaces live here, separate from the skill catalogue.</p>
            <div id="widgets-list" class="widgets-list"></div>
        </section>
    `;
}

async function fetchExtensions() {
    const resp = await fetch('/api/extensions', { cache: 'no-store' });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
    return data;
}

function renderShell(host, tabs) {
    if (!tabs.length) {
        host.innerHTML = '<div class="muted">No live widgets yet. Review and enable an extension that registers a UI tab.</div>';
        return;
    }
    host.innerHTML = tabs.map((tab) => `
        <article class="widgets-card" data-widget-key="${escapeHtml(tab.key || `${tab.skill}:${tab.tab_id}`)}">
            <div class="widgets-card-head">
                <strong>${escapeHtml(tab.title || tab.tab_id || tab.skill)}</strong>
                <span class="muted">${escapeHtml(tab.skill)}:${escapeHtml(tab.tab_id || 'widget')}</span>
            </div>
            <div class="widgets-card-body" data-widget-mount></div>
        </article>
    `).join('');
}

function cleanWidgetRoute(value) {
    const route = String(value || '').trim().replace(/^\/+/, '');
    const parts = route.split('/').filter(Boolean);
    if (!route || route.includes('\\') || parts.some((part) => part === '.' || part === '..')) {
        return '';
    }
    return parts.map(encodeURIComponent).join('/');
}

async function mountTab(card, tab) {
    const mount = card.querySelector('[data-widget-mount]');
    const render = tab.render || {};
    if (!mount) return;
    if (render.kind === 'iframe' && render.route) {
        const route = cleanWidgetRoute(render.route);
        if (!route) throw new Error('invalid widget iframe route');
        mount.innerHTML = `<iframe class="widgets-frame" sandbox="" src="/api/extensions/${encodeURIComponent(tab.skill)}/${route}"></iframe>`;
        return;
    }
    if (render.kind === 'inline_card' && render.api_route) {
        const apiRoute = cleanWidgetRoute(render.api_route);
        if (!apiRoute) throw new Error('invalid widget api_route');
        mount.innerHTML = `
            <form class="skill-widget-weather-form" data-widget-form>
                <input class="skill-widget-weather-city" value="Moscow" autocomplete="off" maxlength="80" aria-label="Widget query">
                <button type="submit" class="btn btn-default">Refresh</button>
            </form>
            <div class="skill-widget-weather-body" data-widget-result><div class="muted">Press Refresh.</div></div>
        `;
        const form = mount.querySelector('[data-widget-form]');
        const input = mount.querySelector('input');
        const result = mount.querySelector('[data-widget-result]');
        form.addEventListener('submit', async (event) => {
            event.preventDefault();
            const query = (input.value || '').trim();
            result.innerHTML = '<div class="muted">Loading…</div>';
            const resp = await fetch(`/api/extensions/${encodeURIComponent(tab.skill)}/${apiRoute}?city=${encodeURIComponent(query)}`);
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok || data.error) {
                result.innerHTML = `<div class="skills-load-error">${escapeHtml(data.error || `HTTP ${resp.status}`)}</div>`;
                return;
            }
            result.innerHTML = `
                <div class="skill-widget-weather-card">
                    <strong>${escapeHtml(data.resolved_to || data.city || query)}</strong>
                    <div class="skill-widget-weather-temp">${escapeHtml(data.temp_c)}°C <span class="muted">feels like ${escapeHtml(data.feels_like_c)}°C</span></div>
                    <div>${escapeHtml(data.condition || 'Unknown')}</div>
                </div>
            `;
        });
        return;
    }
    mount.innerHTML = `<div class="muted">Widget render kind <code>${escapeHtml(render.kind || 'unknown')}</code> is not supported yet.</div>`;
}

export function initWidgets() {
    const page = document.createElement('div');
    page.innerHTML = pageTemplate();
    document.getElementById('content').appendChild(page.firstElementChild);
    const list = document.getElementById('widgets-list');
    const refreshBtn = document.getElementById('widgets-refresh');

    async function render() {
        list.innerHTML = '<div class="muted">Loading widgets…</div>';
        try {
            const data = await fetchExtensions();
            const tabs = Array.isArray(data.live?.ui_tabs) ? data.live.ui_tabs : [];
            renderShell(list, tabs);
            for (const tab of tabs) {
                const key = tab.key || `${tab.skill}:${tab.tab_id}`;
                const card = list.querySelector(`[data-widget-key="${CSS.escape(key)}"]`);
                if (!card) continue;
                try {
                    await mountTab(card, tab);
                } catch (err) {
                    const mount = card.querySelector('[data-widget-mount]');
                    if (mount) mount.innerHTML = `<div class="skills-load-error">widget failed: ${escapeHtml(err.message || err)}</div>`;
                }
            }
        } catch (err) {
            list.innerHTML = `<div class="skills-load-error">Failed to load widgets: ${escapeHtml(err.message || err)}</div>`;
        }
    }

    refreshBtn.addEventListener('click', render);
    window.addEventListener('ouro:page-shown', (event) => {
        if (event.detail?.page === 'widgets') render();
    });
}
