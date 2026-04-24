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
                    External + bundled skills discovered under
                    <code>OUROBOROS_SKILLS_REPO_PATH</code> + <code>repo/skills/</code>.
                    A skill must be <b>enabled</b> and carry a fresh <b>PASS</b> review
                    verdict before <code>skill_exec</code> (scripts) or the in-process
                    dispatch (<code>ext.&lt;skill&gt;.*</code>) will run it.
                </p>
                <div class="skills-controls">
                    <button id="skills-refresh" class="btn btn-default">Refresh</button>
                    <span id="skills-runtime-mode" class="muted"></span>
                </div>
            </div>
            <div id="skills-list" class="skills-list"></div>
            <div id="skills-empty" class="muted" hidden>
                No skills discovered. Point
                <code>OUROBOROS_SKILLS_REPO_PATH</code> at a directory with
                <code>SKILL.md</code> / <code>skill.json</code> packages in
                Settings → Behavior → External Skills Repo.
            </div>
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


function renderSkillCard(skill) {
    const permissions = (skill.permissions || [])
        .map(p => `<code>${escapeHtml(p)}</code>`)
        .join(' ');
    const loadError = skill.load_error
        ? `<div class="skills-load-error">⚠️ ${escapeHtml(skill.load_error)}</div>`
        : '';
    const reviewStaleNote = skill.review_stale
        ? '<span class="skills-badge skills-badge-warn">stale</span>'
        : '';
    const liveBadge = extensionLiveBadge(skill);
    const safeName = escapeHtml(skill.name);
    return `
        <div class="skills-card" data-skill="${safeName}">
            <div class="skills-card-head">
                <div class="skills-card-title">
                    <strong>${safeName}</strong>
                    <span class="muted">${escapeHtml(skill.type)}@${escapeHtml(skill.version || '—')}</span>
                </div>
                <div class="skills-card-status">
                    ${statusBadge(skill.review_status)}
                    ${reviewStaleNote}
                    ${liveBadge}
                    ${skill.enabled ? '<span class="skills-badge skills-badge-ok">enabled</span>'
                                    : '<span class="skills-badge skills-badge-muted">disabled</span>'}
                </div>
            </div>
            <div class="skills-card-perms">permissions: ${permissions || '<i>none</i>'}</div>
            ${extensionLiveNote(skill)}
            ${loadError}
            <div class="skills-card-actions">
                <button class="btn btn-default skills-review" data-skill="${safeName}">Review</button>
                <button class="btn btn-default skills-toggle" data-skill="${safeName}" data-enabled="${skill.enabled}">
                    ${skill.enabled ? 'Disable' : 'Enable'}
                </button>
            </div>
        </div>
    `;
}


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
        || '<div class="muted">No skills yet. Bundled <code>type: script</code> skills like <code>weather</code> can still be reviewed and invoked via <code>skill_exec</code>.</div>';
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
                const tail = result.extension_action
                    ? ` — ${result.extension_action}`
                    : '';
                showBanner(`${name} ${wantsEnabled ? 'enabled' : 'disabled'}${tail}`, 'ok');
            } else if (target.classList.contains('skills-review')) {
                showBanner(`${name}: running tri-model review… (this may take ~30s)`, 'muted');
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
            }
        } catch (err) {
            showBanner(`${name}: ${err.message || err}`, 'danger');
        } finally {
            target.disabled = false;
            renderFn();
        }
    });
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

    window.addEventListener('ouro:page-shown', (event) => {
        if (event.detail?.page === 'skills') {
            renderFn();
        }
    });
}
