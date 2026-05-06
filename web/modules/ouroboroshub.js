import {
    clearPending,
    getPending,
    getPendingBySlug,
    lifecycleCardClassFor,
    lifecycleSpinnerFor,
    setPending,
    startLifecyclePoller,
} from './lifecycle_card.js';

function escapeHtml(value) {
    return String(value ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}


async function fetchJson(url, init) {
    const resp = await fetch(url, init);
    const body = await resp.json().catch(() => ({}));
    if (!resp.ok) {
        const err = new Error(body.error || `HTTP ${resp.status}`);
        err.body = body;
        throw err;
    }
    return body;
}


function lifecycleFor(installed, pending) {
    if (pending) {
        if (pending.failed === true) {
            return {
                tone: pending.tone || 'danger',
                label: pending.label || 'Failed',
                hint: pending.message || '',
                button: pending.retry_label || 'Retry',
                disabled: false,
            };
        }
        return {
            tone: pending.tone || 'warn',
            label: pending.label || 'Working',
            hint: pending.message || '',
            button: pending.label || 'Working…',
            disabled: true,
        };
    }
    if (installed) {
        const review = installed.review_status ? `Review ${installed.review_status}` : 'Installed';
        return {
            tone: installed.review_status === 'pass' && !installed.review_stale ? 'ok' : 'warn',
            label: review,
            hint: installed.review_stale ? 'Review is stale; re-review from My skills before enabling.' : '',
            button: 'Installed',
            disabled: true,
        };
    }
    return {
        tone: 'muted',
        label: 'Not installed',
        hint: 'Install starts security review automatically.',
        button: 'Install',
        disabled: false,
    };
}


function template() {
    return `
        <div class="marketplace-shell">
            <div class="marketplace-controls">
                <input type="search" id="oh-query" class="marketplace-search"
                       placeholder="Search official Ouroboros skills…" autocomplete="off">
                <button class="btn btn-primary" data-oh-search>Search</button>
            </div>
            <div id="oh-status" class="muted marketplace-status"></div>
            <div id="oh-results" class="marketplace-results"></div>
        </div>
    `;
}


function card(item, installed) {
    const slug = item.slug;
    const pending = getPending(slug);
    const lifecycle = lifecycleFor(installed, pending);
    const cardClass = lifecycleCardClassFor(pending);
    const spinner = lifecycleSpinnerFor(pending);
    const lifecycleHint = lifecycle.hint
        ? `<div class="marketplace-card-state-hint">${escapeHtml(lifecycle.hint)}</div>`
        : '';
    const status = installed
        ? `<span class="skills-status-chip skills-status-ok">Installed v${escapeHtml(installed.version || item.latest_version || '')}</span>`
        : '<span class="skills-status-chip skills-status-muted">Not installed</span>';
    const primary = (installed && !pending)
        ? '<button class="btn btn-default" disabled>Installed</button>'
        : `<button class="btn ${pending?.failed ? 'btn-default' : 'btn-primary'}" data-oh-install="${escapeHtml(slug)}" ${lifecycle.disabled ? 'disabled' : ''}>${escapeHtml(lifecycle.button)}</button>`;
    return `
        <article class="${cardClass}" data-slug="${escapeHtml(slug)}">
            <div class="marketplace-card-head">
                <div class="marketplace-card-title">
                    <strong>${escapeHtml(item.display_name || slug)}</strong>
                    <span class="muted">${escapeHtml(slug)} · v${escapeHtml(item.latest_version || '—')}</span>
                </div>
                <div class="marketplace-card-badges">
                    <span class="skills-badge skills-badge-ok">official</span>
                    ${status}
                </div>
            </div>
            <div class="marketplace-card-body">${escapeHtml(item.summary || item.description || '')}</div>
            <div class="marketplace-card-state marketplace-state-${lifecycle.tone}">
                <strong>${spinner}${escapeHtml(lifecycle.label)}</strong>
                ${lifecycleHint}
            </div>
            <div class="marketplace-card-actions">
                <div class="marketplace-primary-action">${primary}</div>
                <div class="marketplace-secondary-actions">
                    <button class="btn btn-default" data-oh-preview="${escapeHtml(slug)}">Details</button>
                </div>
            </div>
        </article>
    `;
}


export function initOuroborosHub(pane) {
    pane.innerHTML = template();
    const state = { query: '', results: [], installed: new Map() };
    const queryInput = pane.querySelector('#oh-query');
    const results = pane.querySelector('#oh-results');
    const status = pane.querySelector('#oh-status');

    const show = (message, tone = '') => {
        status.dataset.tone = tone;
        status.textContent = message;
    };

    function renderCards() {
        results.innerHTML = state.results.map((item) => card(item, state.installed.get(item.slug))).join('')
            || '<div class="muted">No official skills found.</div>';
    }

    async function loadInstalled() {
        const data = await fetchJson('/api/marketplace/ouroboroshub/installed').catch(() => ({ skills: [] }));
        state.installed = new Map((data.skills || []).map((skill) => [skill.name, skill]));
    }

    async function refresh() {
        show('Loading OuroborosHub…', 'muted');
        try {
            await loadInstalled();
            const params = new URLSearchParams();
            if (state.query.trim()) params.set('q', state.query.trim());
            const data = await fetchJson(`/api/marketplace/ouroboroshub/catalog?${params}`);
            state.results = data.results || [];
            state.installed.pendingBySlug = getPendingBySlug();
            renderCards();
            show(`${state.results.length} official skill${state.results.length === 1 ? '' : 's'}`, 'muted');
        } catch (err) {
            show(err.message || String(err), 'danger');
            results.innerHTML = `<div class="skills-load-error">${escapeHtml(err.message || err)}</div>`;
        }
    }

    queryInput.addEventListener('input', (event) => {
        state.query = event.target.value || '';
        clearTimeout(pane._ohTimer);
        pane._ohTimer = setTimeout(refresh, 250);
    });
    pane.querySelector('[data-oh-search]').addEventListener('click', refresh);
    startLifecyclePoller(() => {
        state.installed.pendingBySlug = getPendingBySlug();
        renderCards();
    });
    results.addEventListener('click', async (event) => {
        const install = event.target.closest('[data-oh-install]');
        const preview = event.target.closest('[data-oh-preview]');
        if (preview) {
            const slug = preview.dataset.ohPreview;
            show(`${slug}: official skill details are shown in the catalog card.`, 'muted');
            return;
        }
        if (!install) return;
        const slug = install.dataset.ohInstall;
        install.disabled = true;
        setPending(slug, { label: 'Installing', tone: 'warn', message: 'Installing official skill…' });
        show(`Installing ${slug}…`, 'muted');
        try {
            const data = await fetchJson('/api/marketplace/ouroboroshub/install', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ slug, auto_review: true }),
            });
            if (!data.ok) throw new Error(data.error || 'install failed');
            show(
                data.review_status ? `${slug}: installed, review ${data.review_status}` : `${slug}: installed`,
                data.ok ? 'ok' : 'warn',
            );
            clearPending(slug);
        } catch (err) {
            setPending(slug, {
                label: 'Failed',
                tone: 'danger',
                message: err.message || String(err),
                failed: true,
                retry_label: 'Retry',
            });
            show(`${slug}: ${err.message || err}`, 'danger');
        } finally {
            install.disabled = false;
            refresh();
        }
    });
    pane._ouroboroshubRefresh = refresh;
    refresh();
}
