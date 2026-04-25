/**
 * Ouroboros Skills UI — Phase 5.
 *
 * Lists every discovered skill under ``OUROBOROS_SKILLS_REPO_PATH`` plus
 * the bundled reference set, shows per-skill review status + permissions
 * + runtime-mode eligibility, and exposes the three lifecycle buttons:
 * Review, Toggle enable, Delete (placeholder — Phase 6 wires actual
 * delete). Read-only against ``/api/state`` + ``/api/extensions``.
 */

function skillsPageTemplate() {
    return `
        <section class="page" id="page-skills">
            <div class="skills-header">
                <h2>Skills</h2>
                <p class="muted">
                    Skills discovered under <code>data/skills/{native,clawhub,external}/</code>
                    plus the optional <code>OUROBOROS_SKILLS_REPO_PATH</code> checkout.
                    A skill must be <b>enabled</b> and carry a fresh <b>PASS</b> review
                    verdict before <code>skill_exec</code> (scripts) or the in-process
                    dispatch (<code>ext.&lt;skill&gt;.*</code>) will run it.
                </p>
                <div class="skills-tabs" role="tablist" aria-label="Skills views">
                    <button class="skills-tab is-active" data-tab="installed" role="tab" aria-selected="true">
                        Installed
                    </button>
                    <button class="skills-tab" data-tab="marketplace" role="tab" aria-selected="false">
                        Marketplace
                        <span class="skills-tab-pill" id="skills-tab-pill-marketplace" hidden></span>
                    </button>
                </div>
            </div>
            <div class="skills-tab-panel" id="skills-pane-installed" data-pane="installed">
                <div id="skills-migration-banner" class="skills-migration-banner" hidden></div>
                <div class="skills-controls">
                    <button id="skills-refresh" class="btn btn-default">Refresh</button>
                    <span id="skills-runtime-mode" class="muted"></span>
                </div>
                <div id="skills-list" class="skills-list"></div>
                <div id="skills-empty" class="muted" hidden>
                    No skills discovered yet. Install one from the
                    <b>Marketplace</b> tab, drop a <code>SKILL.md</code> package
                    into <code>data/skills/external/</code>, or point
                    <code>OUROBOROS_SKILLS_REPO_PATH</code> at your own checkout
                    in Settings &rarr; Behavior &rarr; External Skills Repo.
                </div>
            </div>
            <div class="skills-tab-panel" id="skills-pane-marketplace" data-pane="marketplace" hidden></div>
        </section>
    `;
}


function escapeHtml(value) {
    // External skill manifests are untrusted input — a malicious
    // SKILL.md could put ``<script>`` tags in ``name``/``type``/
    // ``load_error`` etc. Render every field through this helper
    // before interpolating into ``innerHTML``.
    return String(value ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}


function statusBadge(status) {
    const tone = status === 'pass' ? 'ok'
        : status === 'fail' ? 'danger'
        : status === 'advisory' ? 'warn'
        : 'muted';
    return `<span class="skills-badge skills-badge-${tone}">${escapeHtml(status)}</span>`;
}


function extensionLiveBadge(skill) {
    if (skill.type !== 'extension') return '';
    const pendingUiTabs = Array.isArray(skill.ui_tabs_pending) ? skill.ui_tabs_pending : [];
    if (pendingUiTabs.length && !skill.dispatch_live) {
        return '<span class="skills-badge skills-badge-warn">ui tab pending</span>';
    }
    if (skill.live_loaded && skill.dispatch_live) {
        return '<span class="skills-badge skills-badge-ok">live</span>';
    }
    if (skill.live_loaded) {
        return '<span class="skills-badge skills-badge-muted">loaded</span>';
    }
    if (skill.desired_live) {
        return '<span class="skills-badge skills-badge-warn">catalog only</span>';
    }
    return '<span class="skills-badge skills-badge-muted">not live</span>';
}


function extensionLiveNote(skill) {
    if (skill.type !== 'extension') return '';
    const pendingUiTabs = Array.isArray(skill.ui_tabs_pending) ? skill.ui_tabs_pending : [];
    if (pendingUiTabs.length && !skill.dispatch_live) {
        return '<div class="muted">extension runtime: ui tab declared, but the browser host does not ship extension tabs yet</div>';
    }
    const reason = escapeHtml(skill.live_reason || 'catalog_only');
    const prefix = skill.live_loaded && skill.dispatch_live
        ? 'extension runtime: live'
        : (skill.live_loaded ? 'extension runtime: loaded' : 'extension runtime');
    return `<div class="muted">${prefix}${skill.live_loaded && skill.dispatch_live ? '' : ` (${reason})`}</div>`;
}


function safeExternalUrl(value) {
    const text = String(value ?? '').trim();
    if (!text) return '';
    try {
        const parsed = new URL(text);
        if (parsed.protocol === 'http:' || parsed.protocol === 'https:') {
            return escapeHtml(parsed.toString());
        }
    } catch {
        // Not a parseable absolute URL — refuse rather than guessing.
    }
    return '';
}


function renderProvenanceBlock(prov) {
    if (!prov || typeof prov !== 'object') return '';
    const rows = [];
    if (prov.slug) {
        rows.push(`<span>slug: <code>${escapeHtml(prov.slug)}</code></span>`);
    }
    if (prov.sha256) {
        rows.push(`<span>sha256: <code>${escapeHtml(String(prov.sha256).slice(0, 12))}…</code></span>`);
    }
    if (prov.license) {
        rows.push(`<span>license: ${escapeHtml(prov.license)}</span>`);
    }
    const homepageHref = safeExternalUrl(prov.homepage);
    if (homepageHref) {
        rows.push(`<a href="${homepageHref}" target="_blank" rel="noopener noreferrer">homepage</a>`);
    }
    if (prov.registry_url) {
        rows.push(`<span>registry: <code>${escapeHtml(prov.registry_url)}</code></span>`);
    }
    const meta = rows.length ? `<div class="skills-card-provenance muted">${rows.join(' · ')}</div>` : '';
    const warnings = Array.isArray(prov.adapter_warnings) ? prov.adapter_warnings : [];
    const warningsBlock = warnings.length
        ? `<details class="skills-card-warnings">
             <summary class="muted">${warnings.length} adapter warning${warnings.length === 1 ? '' : 's'}</summary>
             <ul>${warnings.map((msg) => `<li>${escapeHtml(msg)}</li>`).join('')}</ul>
           </details>`
        : '';
    return meta + warningsBlock;
}


function renderSkillCard(skill) {
    const permissions = (skill.permissions || [])
        .map(p => `<code>${escapeHtml(p)}</code>`)
        .join(' ');
    const loadError = skill.load_error
        ? `<div class="skills-load-error">${escapeHtml(skill.load_error)}</div>`
        : '';
    const reviewStaleNote = skill.review_stale
        ? '<span class="skills-badge skills-badge-warn">stale</span>'
        : '';
    const liveBadge = extensionLiveBadge(skill);
    const safeName = escapeHtml(skill.name);
    const source = (skill.source || 'native').toLowerCase();
    const sourceLabel = source === 'clawhub' ? 'clawhub'
        : source === 'native' ? 'native'
        : source === 'external' ? 'external'
        : source === 'user_repo' ? 'user repo'
        : escapeHtml(source);
    const sourceTone = source === 'clawhub' ? 'warn' : 'muted';
    const isClawhub = source === 'clawhub';
    const provenance = isClawhub ? skill.provenance : null;
    // ``update_available`` requires both an installed version (the
    // skill manifest's ``version``) AND a registry-side latest version.
    // The installed catalogue does not currently embed the latest from
    // the registry, but the marketplace pane updates in step. Here we
    // surface the installed version pin so the card carries truthful
    // version metadata; the Marketplace tab card surfaces "update
    // available" against the registry latest. To avoid drift on the
    // Installed tab we also expose ``provenance.version`` if it
    // diverges from the manifest version (would only happen if the
    // user hand-edited the SKILL.md, which is the kind of state the
    // operator should be told about).
    const installedVersion = skill.version || '—';
    const provenanceVersion = provenance?.version || '';
    const versionDriftBadge = (provenanceVersion && provenanceVersion !== installedVersion)
        ? `<span class="skills-badge skills-badge-warn" title="Provenance version (${escapeHtml(provenanceVersion)}) differs from manifest version (${escapeHtml(installedVersion)}). Skill may have been hand-edited.">version drift</span>`
        : '';
    const updateBtn = isClawhub
        ? `<button class="btn btn-default skills-update" data-skill="${safeName}">Update</button>`
        : '';
    const uninstallBtn = isClawhub
        ? `<button class="btn btn-default skills-uninstall" data-skill="${safeName}">Uninstall</button>`
        : '';
    const provenanceBlock = renderProvenanceBlock(provenance);
    // v5: live extension widget mount-point. The widget host is
    // populated only for ``type: extension`` skills that registered an
    // ``ui_tab`` and successfully went live (``dispatch_live``). The
    // actual widget renderer is registered in
    // :func:`registerWidgetRenderer` below; we just emit the host
    // ``<div data-skill-widget>`` here and let the renderer mount in.
    const widgetMount = (skill.type === 'extension' && skill.live_loaded && skill.dispatch_live)
        ? `<div class="skills-widget-mount" data-skill-widget="${safeName}"></div>`
        : '';
    return `
        <div class="skills-card" data-skill="${safeName}">
            <div class="skills-card-head">
                <div class="skills-card-title">
                    <strong>${safeName}</strong>
                    <span class="muted">${escapeHtml(skill.type)}@${escapeHtml(installedVersion)}</span>
                </div>
                <div class="skills-card-status">
                    <span class="skills-badge skills-badge-${sourceTone}">${sourceLabel}</span>
                    ${statusBadge(skill.review_status)}
                    ${reviewStaleNote}
                    ${versionDriftBadge}
                    ${liveBadge}
                    ${skill.enabled ? '<span class="skills-badge skills-badge-ok">enabled</span>'
                                    : '<span class="skills-badge skills-badge-muted">disabled</span>'}
                </div>
            </div>
            <div class="skills-card-perms">permissions: ${permissions || '<i>none</i>'}</div>
            ${provenanceBlock}
            ${extensionLiveNote(skill)}
            ${widgetMount}
            ${loadError}
            <div class="skills-card-actions">
                <button class="btn btn-default skills-review" data-skill="${safeName}">Review</button>
                <button class="btn btn-default skills-toggle" data-skill="${safeName}" data-enabled="${skill.enabled}">
                    ${skill.enabled ? 'Disable' : 'Enable'}
                </button>
                ${updateBtn}
                ${uninstallBtn}
            </div>
        </div>
    `;
}


// ---------------------------------------------------------------------------
// Skill widget host — v5
// ---------------------------------------------------------------------------
//
// Each ``type: extension`` skill that needs a visual surface registers a
// renderer keyed by skill name. After a card render pass the host
// scans for ``[data-skill-widget="<name>"]`` mount points and invokes
// the matching renderer. Renderers receive the host element + the
// skill catalog row and own their own state.
//
// We deliberately keep this in-tree (not loaded from the extension
// itself) because:
//   1. Extension code runs in Python; the JS visual layer needs to be
//      shipped with the launcher to stay reviewable.
//   2. Cross-origin or CSP-restricted dynamic imports of arbitrary JS
//      from disk would defeat the review gate.
//   3. v5 only ships ``weather`` as an extension widget; future skills
//      with widgets will register here in the same place.
//
// A future iteration may move this to a per-skill ``widget.js`` loaded
// from the skill package after a separate UI review gate.

const _widgetRenderers = new Map();

function registerWidgetRenderer(skillName, renderFn) {
    _widgetRenderers.set(skillName, renderFn);
}

function mountSkillWidgets(rootEl) {
    if (!rootEl) return;
    const mounts = rootEl.querySelectorAll('[data-skill-widget]');
    mounts.forEach((host) => {
        const name = host.dataset.skillWidget;
        const renderer = _widgetRenderers.get(name);
        if (!renderer) {
            host.innerHTML = `<div class="muted">no widget renderer registered for <code>${escapeHtml(name)}</code></div>`;
            return;
        }
        try {
            renderer(host);
        } catch (err) {
            host.innerHTML = `<div class="skills-load-error">widget mount failed: ${escapeHtml(err.message || err)}</div>`;
        }
    });
}


/**
 * Weather extension widget — v5.
 *
 * Renders an inline live weather card under the weather skill row on
 * the Installed tab. Uses the extension's own
 * ``GET /api/extensions/weather/forecast?city=...`` route registered
 * in ``skills/weather/plugin.py``. Reactive on city input + Refresh.
 *
 * State is cached in a module-level ``Map`` so re-renders triggered
 * by other skill actions (toggle, review, install) do not re-issue
 * a wttr.in call. The cache survives within the SPA session; a hard
 * page reload starts fresh.
 */
const _weatherWidgetState = {
    city: 'Moscow',
    lastResult: null,
    lastError: '',
    lastFetchedAt: 0,
    // v5 Cycle 1 Gemini Finding 7: token guards against concurrent
    // refresh() calls. ``inflightToken`` is incremented on every
    // entry; a fetch resolves into widget state only when the token
    // it was issued under still equals the current value. This stops
    // last-writer-wins thrash when ``renderSkillsList`` re-mounts the
    // widget mid-fetch (or the user clicks Refresh twice quickly).
    inflightToken: 0,
};

function _renderWeatherWidget(host) {
    if (host.dataset.bootstrapped === '1') return;
    host.dataset.bootstrapped = '1';
    host.innerHTML = `
        <div class="skill-widget-weather" role="region" aria-label="Weather widget">
            <form class="skill-widget-weather-form" novalidate>
                <input type="text"
                       class="skill-widget-weather-city"
                       placeholder="City (e.g., Moscow)"
                       autocomplete="off"
                       maxlength="80"
                       aria-label="City">
                <button type="submit" class="btn btn-default" aria-label="Refresh weather">Refresh weather</button>
            </form>
            <div class="skill-widget-weather-body" data-state="idle">
                <div class="muted">Type a city and press Refresh.</div>
            </div>
        </div>
    `;
    const form = host.querySelector('form');
    const cityInput = host.querySelector('.skill-widget-weather-city');
    const body = host.querySelector('.skill-widget-weather-body');
    cityInput.value = _weatherWidgetState.city;

    function renderResult(data) {
        body.dataset.state = 'ok';
        body.innerHTML = `
            <div class="skill-widget-weather-card">
                <div class="skill-widget-weather-head">
                    <strong>${escapeHtml(data.resolved_to || data.city || cityInput.value)}</strong>
                    ${data.country ? `<span class="muted"> · ${escapeHtml(data.country)}</span>` : ''}
                </div>
                <div class="skill-widget-weather-temp">
                    ${escapeHtml(String(data.temp_c ?? '—'))}°C
                    <span class="muted"> · feels like ${escapeHtml(String(data.feels_like_c ?? '—'))}°C</span>
                </div>
                <div class="skill-widget-weather-cond">${escapeHtml(data.condition || 'Unknown')}</div>
                <div class="skill-widget-weather-meta muted">
                    <span>humidity: ${escapeHtml(String(data.humidity_pct ?? 0))}%</span>
                    <span>wind: ${escapeHtml(String(data.wind_kph ?? 0))} km/h ${escapeHtml(data.wind_dir || '')}</span>
                    ${data.observation_time ? `<span>at ${escapeHtml(data.observation_time)}</span>` : ''}
                </div>
            </div>
        `;
    }

    function renderError(message) {
        body.dataset.state = 'error';
        body.innerHTML = `<div class="skills-load-error">${escapeHtml(message)}</div>`;
    }

    // Restore from cache if fresh.
    if (_weatherWidgetState.lastResult) {
        renderResult(_weatherWidgetState.lastResult);
    } else if (_weatherWidgetState.lastError) {
        renderError(_weatherWidgetState.lastError);
    }

    async function refresh() {
        const myToken = ++_weatherWidgetState.inflightToken;
        const city = (cityInput.value || '').trim();
        _weatherWidgetState.city = city;
        if (!city) {
            _weatherWidgetState.lastResult = null;
            _weatherWidgetState.lastError = '';
            body.dataset.state = 'idle';
            body.innerHTML = '<div class="muted">Type a city and press Refresh.</div>';
            return;
        }
        body.dataset.state = 'loading';
        body.innerHTML = '<div class="muted">Fetching wttr.in…</div>';
        try {
            const url = `/api/extensions/weather/forecast?city=${encodeURIComponent(city)}`;
            const resp = await fetch(url);
            const data = await resp.json().catch(() => ({}));
            if (myToken !== _weatherWidgetState.inflightToken) {
                // A newer refresh fired while we were waiting — drop
                // this stale response so it does not overwrite the
                // current city's result.
                return;
            }
            if (!resp.ok || data.error) {
                _weatherWidgetState.lastResult = null;
                _weatherWidgetState.lastError = data.error || `HTTP ${resp.status}`;
                renderError(_weatherWidgetState.lastError);
                return;
            }
            _weatherWidgetState.lastResult = data;
            _weatherWidgetState.lastError = '';
            _weatherWidgetState.lastFetchedAt = Date.now();
            renderResult(data);
        } catch (err) {
            if (myToken !== _weatherWidgetState.inflightToken) return;
            _weatherWidgetState.lastResult = null;
            _weatherWidgetState.lastError = err.message || String(err);
            renderError(_weatherWidgetState.lastError);
        }
    }

    form.addEventListener('submit', (event) => {
        event.preventDefault();
        refresh();
    });
    // Auto-refresh on the very first mount in this SPA session so the
    // card shows live data without a manual click. Subsequent re-mounts
    // (triggered by other skill actions re-rendering the list) hit the
    // cache instead and stay quiet.
    const stale = (Date.now() - _weatherWidgetState.lastFetchedAt) > 5 * 60 * 1000;
    if (!_weatherWidgetState.lastResult && !_weatherWidgetState.lastError) {
        refresh();
    } else if (stale) {
        refresh();
    }
}

registerWidgetRenderer('weather', _renderWeatherWidget);


async function fetchSkills() {
    const [stateResp, extResp] = await Promise.all([
        fetch('/api/state').then(r => r.ok ? r.json() : {}),
        fetch('/api/extensions').then(r => r.ok ? r.json() : { skills: [], live: {} }),
    ]);
    // ``/api/state`` does not yet expose a ``summarize_skills`` payload
    // directly (that land in a later round if needed). For now we
    // synthesize the per-skill list via the extensions catalogue +
    // the runtime-mode / skills-repo boolean.
    const skillsRepoConfigured = Boolean(stateResp.skills_repo_configured);
    const runtimeMode = stateResp.runtime_mode || 'advanced';
    return {
        runtimeMode,
        skillsRepoConfigured,
        skills: extResp.skills || [],
        live: extResp.live || {},
    };
}


async function renderSkillsList(container, emptyEl, runtimeModeEl) {
    const { runtimeMode, skillsRepoConfigured, skills } = await fetchSkills();
    runtimeModeEl.textContent = `runtime_mode: ${runtimeMode}`;
    if (!skills.length && !skillsRepoConfigured) {
        container.innerHTML = '';
        emptyEl.hidden = false;
        return;
    }
    emptyEl.hidden = true;
    container.innerHTML = skills.map(renderSkillCard).join('')
        || '<div class="muted">No skills yet. Add a skill from the <b>Marketplace</b> tab or drop a <code>SKILL.md</code> package into <code>data/skills/external/</code>.</div>';
    // v5: mount any inline extension widgets (e.g. weather) into the
    // freshly-rendered cards. The container.innerHTML rewrite above
    // destroys every prior widget host, so each refresh re-mounts
    // from scratch. The ``data-bootstrapped`` flag on each host
    // makes the call idempotent within a single render pass; cross-
    // render fetch deduplication comes from the per-widget
    // module-level state cache (``_weatherWidgetState``) plus its
    // 5-minute staleness threshold.
    mountSkillWidgets(container);
    // v5: surface unread native-skill upgrade migrations so the
    // operator is told when the launcher silently rewrote an
    // installed skill (e.g. weather 0.1 script -> 0.2 extension).
    // Idempotent on re-render — we replace the top banner each pass.
    renderMigrationBanner();
}


async function renderMigrationBanner() {
    const host = document.getElementById('skills-migration-banner');
    if (!host) return;
    let migrations = [];
    try {
        const resp = await fetch('/api/migrations');
        if (resp.ok) {
            const data = await resp.json();
            migrations = Array.isArray(data.migrations) ? data.migrations : [];
        }
    } catch {
        // network error — leave the banner empty.
    }
    if (!migrations.length) {
        host.innerHTML = '';
        host.hidden = true;
        return;
    }
    host.hidden = false;
    host.innerHTML = migrations.map((m) => {
        const safeKey = escapeHtml(String(m.key || ''));
        const skill = escapeHtml(String(m.skill || ''));
        const oldV = escapeHtml(String(m.old_version || ''));
        const newV = escapeHtml(String(m.new_version || ''));
        const summary = escapeHtml(String(m.summary || ''));
        const ts = escapeHtml(String(m.applied_at || ''));
        return `
            <div class="skills-migration-banner-item" data-migration-key="${safeKey}">
                <div class="skills-migration-banner-text">
                    <strong>Native skill upgrade:</strong> ${skill} ${oldV ? `(${oldV} → ${newV})` : `(→ ${newV})`}
                    <span class="muted"> · ${ts}</span>
                    <div class="muted">${summary}</div>
                </div>
                <button class="btn btn-default skills-migration-dismiss" data-key="${safeKey}">Got it</button>
            </div>
        `;
    }).join('');
    // v5 Cycle 2 Gemini Finding 1 + Opus C2-2: attach the dismiss
    // listener exactly once per host element. The previous version
    // used ``{ once: true }`` which removed the listener on the FIRST
    // click anywhere inside the host — including click on the body
    // text — so subsequent clicks on the actual "Got it" button (or
    // a second migration's button) silently no-op'd. We gate the
    // listener attachment via a dataset flag instead, so each
    // re-render of the banner does NOT re-register, and ANY click
    // is delegated to the right button via ``closest()``.
    if (host.dataset.bannerListenerAttached !== '1') {
        host.dataset.bannerListenerAttached = '1';
        host.addEventListener('click', async (event) => {
            const btn = event.target.closest('.skills-migration-dismiss');
            if (!btn) return;
            const key = btn.dataset.key;
            if (!key) return;
            btn.disabled = true;
            try {
                await fetch(`/api/migrations/${encodeURIComponent(key)}/dismiss`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({}),
                });
                const item = btn.closest('.skills-migration-banner-item');
                if (item) item.remove();
                if (!host.querySelector('.skills-migration-banner-item')) {
                    host.hidden = true;
                }
            } catch {
                btn.disabled = false;
            }
        });
    }
}


async function postWithFeedback(url, body) {
    const resp = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body || {}),
    });
    const payload = await resp.json().catch(() => ({}));
    if (!resp.ok) {
        throw new Error(payload.error || `HTTP ${resp.status}`);
    }
    return payload;
}


function showBanner(message, tone) {
    const existing = document.getElementById('skills-banner');
    if (existing) existing.remove();
    const banner = document.createElement('div');
    banner.id = 'skills-banner';
    banner.className = `skills-banner skills-banner-${tone}`;
    banner.textContent = message;
    document.getElementById('page-skills')?.prepend(banner);
    setTimeout(() => banner.remove(), 6000);
}


function attachActionHandlers(container, renderFn) {
    container.addEventListener('click', async (event) => {
        const target = event.target.closest('button[data-skill]');
        if (!target) return;
        const name = target.dataset.skill;
        const wantsEnabled = target.dataset.enabled === 'false';
        target.disabled = true;
        try {
            if (target.classList.contains('skills-toggle')) {
                const result = await postWithFeedback(
                    `/api/skills/${encodeURIComponent(name)}/toggle`,
                    { enabled: wantsEnabled }
                );
                // v5 (Cycle 1 GPT-10 + Cycle 2 Gemini-2): clear the
                // weather widget's cached error/result on disable AND
                // bump the in-flight token so any in-flight refresh
                // whose Promise resolves after we cleared the cache
                // is rejected by the token-check guard. Without the
                // token bump, a slow wttr.in fetch could repopulate
                // ``lastResult`` post-disable and leak pre-disable
                // data into the next enable.
                if (name === 'weather' && !wantsEnabled) {
                    _weatherWidgetState.lastResult = null;
                    _weatherWidgetState.lastError = '';
                    _weatherWidgetState.lastFetchedAt = 0;
                    _weatherWidgetState.inflightToken += 1;
                }
                const tail = result.extension_action
                    ? ` — ${result.extension_action}`
                    : '';
                showBanner(`${name} ${wantsEnabled ? 'enabled' : 'disabled'}${tail}`, 'ok');
            } else if (target.classList.contains('skills-review')) {
                showBanner(`${name}: running tri-model review (this may take ~30s)`, 'muted');
                const result = await postWithFeedback(
                    `/api/skills/${encodeURIComponent(name)}/review`,
                    {}
                );
                const findings = result.findings?.length ?? 0;
                const errorTail = result.error ? ` — ${result.error}` : '';
                showBanner(
                    `${name}: review ${result.status}${findings ? ` (${findings} findings)` : ''}${errorTail}`,
                    result.status === 'pass' ? 'ok'
                        : (result.error || result.status === 'fail') ? 'danger'
                        : 'warn'
                );
            } else if (target.classList.contains('skills-update')) {
                showBanner(`${name}: updating from ClawHub (this may take ~30s)`, 'muted');
                const result = await postWithFeedback(
                    `/api/marketplace/clawhub/update/${encodeURIComponent(name)}`,
                    {}
                );
                const tail = result.review_status ? ` — review ${result.review_status}` : '';
                showBanner(
                    result.ok
                        ? `${name}: updated${tail}`
                        : `${name}: update failed — ${result.error || 'unknown'}`,
                    result.ok ? 'ok' : 'danger',
                );
            } else if (target.classList.contains('skills-uninstall')) {
                if (!confirm(`Uninstall ${name}? This deletes data/skills/clawhub/${name}/.`)) {
                    return;
                }
                const result = await postWithFeedback(
                    `/api/marketplace/clawhub/uninstall/${encodeURIComponent(name)}`,
                    {}
                );
                showBanner(
                    result.ok ? `${name}: uninstalled` : `${name}: uninstall failed — ${result.error}`,
                    result.ok ? 'ok' : 'danger',
                );
            }
        } catch (err) {
            showBanner(`${name}: ${err.message || err}`, 'danger');
        } finally {
            target.disabled = false;
            renderFn();
        }
    });
}


function activateTab(tabName) {
    const buttons = document.querySelectorAll('.skills-tab');
    const panels = document.querySelectorAll('.skills-tab-panel');
    buttons.forEach((btn) => {
        const isActive = btn.dataset.tab === tabName;
        btn.classList.toggle('is-active', isActive);
        btn.setAttribute('aria-selected', isActive ? 'true' : 'false');
    });
    panels.forEach((panel) => {
        panel.hidden = panel.dataset.pane !== tabName;
    });
}


async function renderMarketplacePane() {
    const pane = document.getElementById('skills-pane-marketplace');
    if (!pane) return;
    if (pane.dataset.bootstrapped === 'true') {
        // Trigger a refresh so installed-state updates persist when the
        // operator switches between tabs.
        const refresh = pane.querySelector('[data-mp-refresh]');
        if (refresh) refresh.click();
        return;
    }
    pane.dataset.bootstrapped = 'true';
    const mod = await import('./marketplace.js');
    mod.initMarketplace(pane);
}


export function initSkills(ctx) {
    const page = document.createElement('div');
    page.innerHTML = skillsPageTemplate();
    document.getElementById('content').appendChild(page.firstElementChild);

    const container = document.getElementById('skills-list');
    const emptyEl = document.getElementById('skills-empty');
    const runtimeModeEl = document.getElementById('skills-runtime-mode');
    const refreshBtn = document.getElementById('skills-refresh');

    const renderFn = () => renderSkillsList(container, emptyEl, runtimeModeEl);

    refreshBtn.addEventListener('click', renderFn);
    attachActionHandlers(container, renderFn);

    document.querySelectorAll('.skills-tab').forEach((btn) => {
        btn.addEventListener('click', () => {
            const tabName = btn.dataset.tab;
            activateTab(tabName);
            if (tabName === 'marketplace') {
                renderMarketplacePane();
            }
        });
    });

    window.addEventListener('ouro:page-shown', (event) => {
        if (event.detail?.page === 'skills') {
            renderFn();
        }
    });
}
