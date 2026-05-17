import { initMarketplace } from './marketplace.js';
import { initOuroborosHub } from './ouroboroshub.js';
import { renderPageHeader, renderTabStrip } from './page_header.js';
import { openConfirmDialog } from './confirm_dialog.js';
import { PAGE_ICONS } from './page_icons.js';
import { showToast } from './toast.js';
import { apiFetch } from './api_client.js';
import { renderInstalledSkillCard } from './skill_card_renderer.js';
import {
    boundedText,
    emitSkillLifecycle,
    escapeHtmlAttr as escapeHtml,
    grantReady,
    renderSkillRepairPrompt,
    reviewTone,
    reviewReady,
} from './utils.js';

const SKILLS_TABS = [
    { value: 'installed', label: 'My skills', pillId: 'skills-tab-pill-installed' },
    { value: 'marketplace', label: 'ClawHub', pillId: 'skills-tab-pill-marketplace' },
    { value: 'ouroboroshub', label: 'OuroborosHub', pillId: 'skills-tab-pill-ouroboroshub' },
];

/** Installed skills UI: review, grant, enable, repair, update, uninstall. */

function skillsPageTemplate() {
    return `
        <section class="page app-page-glass" id="page-skills">
            ${renderPageHeader({
                title: 'Skills',
                icon: PAGE_ICONS.skills,
                description: 'Skills extend Ouroboros with new tools, routes, and widgets. Each skill is reviewed for safety before you turn it on.',
                actionsHtml: '<button id="skills-refresh" class="btn btn-default btn-sm">Refresh</button>',
                tabsHtml: renderTabStrip({
                    items: SKILLS_TABS,
                    active: 'installed',
                    dataAttr: 'data-tab',
                    activeClass: 'is-active',
                    ariaLabel: 'Skills views',
                    stripClass: 'skills-tabs',
                    tabClass: 'skills-tab',
                }),
            })}
            <div class="skills-search-chrome" id="skills-pane-marketplace-chrome" data-chrome-pane="marketplace" hidden></div>
            <div class="skills-search-chrome" id="skills-pane-ouroboroshub-chrome" data-chrome-pane="ouroboroshub" hidden></div>
            <div class="skills-scroll scroll-fade-y">
                <div class="skills-tab-panel" id="skills-pane-installed" data-pane="installed">
                <div id="skills-list" class="skills-list"></div>
                <div id="skills-empty" class="muted" hidden>
                    No skills yet. Browse <b>ClawHub</b> or
                    <b>OuroborosHub</b> to add one, or import a custom
                    package from the Files tab.
                </div>
            </div>
                <div class="skills-tab-panel" id="skills-pane-marketplace" data-pane="marketplace" hidden></div>
                <div class="skills-tab-panel" id="skills-pane-ouroboroshub" data-pane="ouroboroshub" hidden></div>
            </div>
        </section>
    `;
}


function isMissingGrantLoadError(skill) {
    return !grantReady(skill) && String(skill.load_error || '').includes('missing owner grants');
}

function installTimestamp(skill) {
    const raw = skill.installed_at || skill.provenance?.installed_at || skill.provenance?.updated_at || '';
    const time = Date.parse(raw);
    return Number.isFinite(time) ? time : 0;
}

function installedAgo(skill) {
    const time = installTimestamp(skill);
    if (!time) return '';
    const seconds = Math.max(0, Math.floor((Date.now() - time) / 1000));
    if (seconds < 90) return 'Just installed';
    const minutes = Math.floor(seconds / 60);
    if (minutes < 90) return `${minutes}m ago`;
    const hours = Math.floor(minutes / 60);
    if (hours < 48) return `${hours}h ago`;
    const days = Math.floor(hours / 24);
    if (days < 45) return `${days}d ago`;
    const date = new Date(time);
    return date.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
}

function sortSkillsForDisplay(skills) {
    return [...skills].sort((a, b) => {
        if (a.lifecycle_virtual && !b.lifecycle_virtual) return -1;
        if (!a.lifecycle_virtual && b.lifecycle_virtual) return 1;
        return installTimestamp(b) - installTimestamp(a) || String(a.name || '').localeCompare(String(b.name || ''));
    });
}


async function fetchSkills() {
    const [stateResp, extResp, queueResp] = await Promise.all([
        apiFetch('/api/state').then(r => r.ok ? r.json() : {}),
        apiFetch('/api/extensions').then(r => r.ok ? r.json() : { skills: [], live: {} }),
        apiFetch('/api/skills/lifecycle-queue').then(r => r.ok ? r.json() : { events: [] }).catch(() => ({ events: [] })),
    ]);
    // Per-skill state is synthesized from extensions + lifecycle queue.
    const skillsRepoConfigured = Boolean(stateResp.skills_repo_configured);
    const githubTokenConfigured = Boolean(stateResp.github_token_configured);
    return {
        skillsRepoConfigured,
        githubTokenConfigured,
        skills: mergeLifecycleEvents(extResp.skills || [], queueResp.events || []),
        live: extResp.live || {},
        queue: queueResp,
    };
}


function mergeLifecycleEvents(skills, events) {
    const out = [...skills];
    const names = new Set(out.map((skill) => skill.name));
    for (const event of [...events].reverse()) {
        if (!['queued', 'running', 'failed'].includes(event.status)) continue;
        const name = event.target;
        if (!name || names.has(name)) continue;
        names.add(name);
        out.unshift({
            name,
            description: event.message || event.error || 'Skill lifecycle operation',
            version: '—',
            type: 'skill',
            enabled: false,
            review_status: 'pending',
            review_stale: true,
            permissions: [],
            load_error: event.status === 'failed' ? event.error : '',
            source: event.source || 'external',
            lifecycle_kind: event.kind || '',
            lifecycle_virtual: true,
            grants: { all_granted: true },
        });
    }
    updateQueueBadges(events);
    return out;
}


function updateQueueBadges(events) {
    const actionable = events.filter((event) => ['queued', 'running', 'failed'].includes(event.status));
    const bySource = new Map();
    for (const event of actionable) {
        const source = event.source === 'ouroboroshub' ? 'ouroboroshub'
            : event.source === 'clawhub' ? 'marketplace'
            : 'installed';
        bySource.set(source, (bySource.get(source) || 0) + 1);
    }
    for (const [id, count] of bySource.entries()) {
        const el = document.getElementById(`skills-tab-pill-${id}`);
        if (!el) continue;
        el.hidden = !count;
        el.textContent = count ? String(count) : '';
    }
    for (const id of ['installed', 'marketplace', 'ouroboroshub']) {
        if (bySource.has(id)) continue;
        const el = document.getElementById(`skills-tab-pill-${id}`);
        if (!el) continue;
        el.hidden = true;
        el.textContent = '';
    }
}


async function renderSkillsList(container, emptyEl, reviewingSkills = new Set(), repairingSkills = new Set()) {
    const { skillsRepoConfigured, githubTokenConfigured, skills, live } = await fetchSkills();
    if (!skills.length && !skillsRepoConfigured) {
        container.innerHTML = '';
        if (emptyEl) emptyEl.hidden = false;
        return;
    }
    if (emptyEl) emptyEl.hidden = true;
    container.innerHTML = sortSkillsForDisplay(skills).map((skill) => renderInstalledSkillCard(
        skill,
        reviewingSkills,
        repairingSkills,
        live,
        { githubTokenConfigured },
    )).join('')
        || '<div class="muted">No skills yet. Add one from <b>ClawHub</b> or <b>OuroborosHub</b>.</div>';
}


async function postWithFeedback(url, body) {
    const resp = await apiFetch(url, {
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

function buildHealPrompt(skill) {
    const findings = Array.isArray(skill.review_findings) ? skill.review_findings : [];
    const diagnostics = {
        name: boundedText(skill.name, 200),
        source: boundedText(skill.source || 'unknown', 80),
        payload_root: boundedText(skill.payload_root || '', 300),
        type: boundedText(skill.type || 'unknown', 80),
        review_status: boundedText(skill.review_status || 'pending', 80),
        review_stale: Boolean(skill.review_stale),
        load_error: boundedText(skill.load_error || 'none', 2000),
        review_findings: findings.slice(0, 12).map((finding) => ({
            item: boundedText(finding.item || finding.check || finding.title || 'finding', 200),
            verdict: boundedText(finding.verdict || finding.severity || '', 80),
            reason: boundedText(finding.reason || finding.message || JSON.stringify(finding), 1200),
        })),
    };
    return renderSkillRepairPrompt(
        'Repair the installed Ouroboros skill selected in the Skills UI.',
        JSON.stringify(diagnostics, null, 2),
    );
}


function attachActionHandlers(container, renderFn, reviewingSkills, repairingSkills, ctx = {}) {
    function closeSkillMenus(exceptMenu = null) {
        container.querySelectorAll('.skills-card-menu').forEach((menu) => {
            if (menu === exceptMenu) return;
            const popover = menu.querySelector('.skills-card-menu-dialog');
            const trigger = menu.querySelector('[data-skill-menu-trigger]');
            if (popover?.open) popover.close();
            if (trigger) trigger.setAttribute('aria-expanded', 'false');
        });
    }

    async function requestMissingKeyGrants(name, keys) {
        const cleanKeys = (keys || []).map((k) => String(k || '').trim()).filter(Boolean);
        if (!cleanKeys.length) return;
        const ok = await openConfirmDialog({
            title: `Grant access to ${name}`,
            body: `Grant access to ${cleanKeys.join(', ')} for ${name}? Required keys are taken from your settings. The desktop launcher will request a second confirmation.`,
            confirmLabel: 'Grant access',
        });
        if (!ok) throw new Error('Skill key grant cancelled.');
        const bridge = window.pywebview?.api?.request_skill_key_grant;
        if (!bridge) {
            throw new Error('Skill key grants require the desktop launcher confirmation bridge.');
        }
        const result = await bridge(name, cleanKeys);
        if (!result?.ok) {
            throw new Error(result?.error || 'Skill key grant was cancelled.');
        }
        return result;
    }

    async function triggerSkillAction(name, action, options = {}) {
        if (!name || !action) return;
        if (action === 'open_widgets') {
            document.querySelector('.nav-btn[data-page="widgets"]')?.click();
            return;
        }
        const { skills } = await fetchSkills();
        const skill = (skills || []).find((item) => item.name === name);
        if (!skill) throw new Error('Skill not found in current catalogue.');

        if (action === 'retry_install') {
            showToast(`${name}: retrying ClawHub install (this may take ~30s)`, 'muted');
            const result = await postWithFeedback('/api/marketplace/clawhub/install', {
                slug: name,
                overwrite: true,
                auto_review: true,
            });
            const tail = result.review_status ? ` — review ${result.review_status}` : '';
            showToast(
                result.ok
                    ? `${name}: install retried${tail}`
                    : `${name}: install retry failed — ${result.error || 'unknown'}`,
                result.ok ? 'ok' : 'danger',
            );
            if (result.ok) emitSkillLifecycle('retry_install', name, result);
            return;
        }

        if (action === 'review' || action === 'rereview') {
            const ok = await openConfirmDialog({
                title: action === 'rereview' ? `Re-review ${name}` : `Review ${name}`,
                body: `Run security review for ${name}? It can take a few minutes and runs in the background.`,
                confirmLabel: action === 'rereview' ? 'Re-review' : 'Run review',
            });
            if (!ok) return;
            await reviewSkillInBackground(name);
            return;
        }

        if (action === 'grant') {
            const grants = skill.grants || {};
            const keys = (options.keys || '').split(',').map((k) => k.trim()).filter(Boolean);
            const missingKeys = Array.isArray(grants.missing_keys) ? grants.missing_keys : (grants.requested_keys || []);
            const missingPermissions = Array.isArray(grants.missing_permissions) ? grants.missing_permissions : (grants.requested_permissions || []);
            const missing = keys.length ? keys : [...missingKeys, ...missingPermissions];
            const result = await requestMissingKeyGrants(name, missing);
            if (result) {
                showToast(`${name}: requested key grants saved`, 'ok');
                emitSkillLifecycle('grant', name, result);
            }
            return;
        }

        if (action === 'approve_enable') {
            const grants = skill.grants || {};
            const keys = (options.keys || '').split(',').map((k) => k.trim()).filter(Boolean);
            const missingKeys = Array.isArray(grants.missing_keys) ? grants.missing_keys : (grants.requested_keys || []);
            const missingPermissions = Array.isArray(grants.missing_permissions) ? grants.missing_permissions : (grants.requested_permissions || []);
            const missing = keys.length ? keys : [...missingKeys, ...missingPermissions];
            if (missing.length) await requestMissingKeyGrants(name, missing);
            await toggleSkillEnabled(name, true);
            return;
        }

        if (action === 'repair') {
            if (repairingSkills.has(name)) {
                showToast(`${name}: repair is already being queued`, 'muted');
                return;
            }
            const ok = await openConfirmDialog({
                title: `Repair ${name}`,
                body: `Send a repair task for ${name} to Ouroboros? The agent will work on the skill in chat.`,
                confirmLabel: 'Start repair',
                danger: true,
            });
            if (!ok) return;
            repairingSkills.add(name);
            renderFn();
            try {
                const prompt = buildHealPrompt(skill);
                await postWithFeedback('/api/command', {
                    cmd: prompt,
                    task_constraint: { mode: 'skill_repair', skill_name: skill.name || name, payload_root: skill.payload_root || '', allow_enable: false, allow_review: true },
                    visible_text: `Repair task queued for ${name}. Ouroboros will inspect the skill payload and re-run review.`,
                    visible_task_id: `skill_repair_${name}`,
                });
                showToast(`${name}: repair task sent to Ouroboros`, 'ok');
                emitSkillLifecycle('repair', name);
                if (typeof ctx.showPage === 'function') {
                    ctx.showPage('chat');
                } else {
                    document.querySelector('.nav-btn[data-page="chat"]')?.click();
                }
            } finally {
                repairingSkills.delete(name);
                renderFn();
            }
            return;
        }

        if (action === 'submit_hub') {
            const ok = await openConfirmDialog({
                title: `Submit ${name} to OuroborosHub`,
                body: `Open a public GitHub pull request submitting ${name} to OuroborosHub? The PR will contain the reviewed skill payload and an updated catalog entry.`,
                confirmLabel: 'Submit to OuroborosHub',
                danger: true,
            });
            if (!ok) return;
            const message = `Submit skill ${name} to OuroborosHub`;
            await postWithFeedback('/api/command', {
                cmd: message,
                visible_text: `Submission task queued for ${name}. Ouroboros will open a PR to OuroborosHub if validation passes.`,
                visible_task_id: `skill_submit_${name}`,
            });
            showToast(`${name}: submission task sent to Ouroboros`, 'ok');
            emitSkillLifecycle('submit_hub', name);
            if (typeof ctx.showPage === 'function') {
                ctx.showPage('chat');
            } else {
                document.querySelector('.nav-btn[data-page="chat"]')?.click();
            }
        }
    }

    async function toggleSkillEnabled(name, wantsEnabled) {
        const result = await postWithFeedback(
            `/api/skills/${encodeURIComponent(name)}/toggle`,
            { enabled: wantsEnabled }
        );
        const actionLabels = {
            extension_loaded: 'live',
            extension_unloaded: 'stopped',
            extension_already_live: '',
            extension_inactive: '',
            extension_load_error: 'load failed',
        };
        const friendlyAction = actionLabels[result.extension_action];
        const tail = friendlyAction ? ` — ${friendlyAction}` : '';
        showToast(`${name} ${wantsEnabled ? 'turned on' : 'turned off'}${tail}`, 'ok');
        emitSkillLifecycle(wantsEnabled ? 'enable' : 'disable', name, result);
        return result;
    }

    async function reviewSkillInBackground(name) {
        if (reviewingSkills.has(name)) return null;
        reviewingSkills.add(name);
        renderFn();
        try {
            showToast(`${name}: security review started; this can take a few minutes`, 'muted');
            const result = await postWithFeedback(
                `/api/skills/${encodeURIComponent(name)}/review`,
                {}
            );
            const findings = result.findings?.length ?? 0;
            const errorTail = result.error ? ` — ${result.error}` : '';
            showToast(
                `${name}: review ${result.status}${findings ? ` (${findings} findings)` : ''}${errorTail}`,
                reviewTone(result.status, result.error)
            );
            emitSkillLifecycle('review', name, result);
            return result;
        } finally {
            reviewingSkills.delete(name);
            renderFn();
        }
    }

    // Checkbox toggle uses change so keyboard and mouse activation match.
    container.addEventListener('change', async (event) => {
        const target = event.target;
        if (!target || !target.classList || !target.classList.contains('skills-toggle')) {
            return;
        }
        const name = target.dataset.skill;
        if (!name) return;
        const wantsEnabled = Boolean(target.checked);
        target.disabled = true;
        try {
            if (wantsEnabled) {
                let current = (await fetchSkills()).skills.find((skill) => skill.name === name);
                if (!current) throw new Error('Skill not found in current catalogue.');
                if ((current.review_status === 'blockers' && !reviewReady(current)) || (current.load_error && !isMissingGrantLoadError(current))) {
                    throw new Error('Repair this skill before enabling it.');
                }
                if (!reviewReady(current)) {
                    throw new Error('Run review and wait for a fresh executable review before enabling this skill.');
                }
                if (!grantReady(current)) {
                    const grants = current.grants || {};
                    const missing = Array.isArray(grants.missing_keys) ? grants.missing_keys : (grants.requested_keys || []);
                    await requestMissingKeyGrants(name, missing);
                }
            }
            await toggleSkillEnabled(name, wantsEnabled);
            target.setAttribute('aria-checked', wantsEnabled ? 'true' : 'false');
        } catch (err) {
            // Roll back to server-truth state on failed enable/disable.
            target.checked = !wantsEnabled;
            target.setAttribute('aria-checked', (!wantsEnabled).toString());
            showToast(`${name}: ${err.message || err}`, (err.message || '').includes('cancel') ? 'warn' : 'danger');
        } finally {
            target.disabled = false;
            renderFn();
        }
    });
    container.addEventListener('keydown', (event) => {
        const actionTarget = event.target.closest?.('[data-skill-action]');
        if (!actionTarget) return;
        if (event.key !== 'Enter' && event.key !== ' ') return;
        event.preventDefault();
        actionTarget.click();
    });
    container.addEventListener('click', async (event) => {
        const menuTrigger = event.target.closest('[data-skill-menu-trigger]');
        if (menuTrigger) {
            const menu = menuTrigger.closest('.skills-card-menu');
            const popover = menu?.querySelector('.skills-card-menu-dialog');
            const opening = !popover?.open;
            closeSkillMenus(opening ? menu : null);
            if (popover && menu) {
                menuTrigger.setAttribute('aria-expanded', opening ? 'true' : 'false');
                // Non-modal anchored popover; outside handlers close it.
                if (opening) popover.show();
                else popover.close();
            }
            return;
        }
        if (event.target.closest('[data-skill-menu-close]')) {
            closeSkillMenus();
            return;
        }
        const actionTarget = event.target.closest('[data-skill-action]');
        if (actionTarget) {
            const name = actionTarget.dataset.skill;
            const action = actionTarget.dataset.skillAction;
            if (action === 'repair' && repairingSkills.has(name)) {
                return;
            }
            actionTarget.disabled = true;
            try {
                await triggerSkillAction(name, action, { keys: actionTarget.dataset.keys || '' });
            } catch (err) {
                showToast(`${name}: ${err.message || err}`, (err.message || '').includes('cancel') ? 'warn' : 'danger');
            } finally {
                actionTarget.disabled = false;
                renderFn();
            }
            return;
        }
        const target = event.target.closest('button[data-skill]');
        if (!target) return;
        if (target.classList.contains('skills-toggle')) {
            // Checkbox handler above owns current toggles; ignore legacy buttons.
            return;
        }
        const name = target.dataset.skill;
        if (target.classList.contains('skills-review')) {
            if (reviewingSkills.has(name)) return;
            target.disabled = true;
            try {
                await reviewSkillInBackground(name);
            } catch (err) {
                showToast(`${name}: ${err.message || err}`, 'danger');
            } finally {
                target.disabled = false;
                renderFn();
            }
            return;
        }
        target.disabled = true;
        try {
            if (target.classList.contains('skills-next-toggle')) {
                const wantsEnabled = target.dataset.enabled === 'true';
                await toggleSkillEnabled(name, wantsEnabled);
            } else if (target.classList.contains('skills-grant')) {
                const keys = (target.dataset.keys || '').split(',').map((k) => k.trim()).filter(Boolean);
                if (!keys.length) {
                    showToast(`${name}: no requested keys to grant`, 'warn');
                } else {
                    const result = await requestMissingKeyGrants(name, keys);
                    // Grant may persist even if live extension reconcile fails.
                    const reason = result.extension_reason;
                    const action = result.extension_action;
                    const loadError = result.load_error;
                    if (reason === 'reconcile_call_failed') {
                        showToast(
                            `${name}: grant saved, but server reconcile failed \u2014 toggle disable/enable to retry`,
                            'warn'
                        );
                    } else if (loadError) {
                        showToast(
                            `${name}: grant saved, but extension load failed: ${loadError}`,
                            'warn'
                        );
                    } else if (action === 'extension_loaded') {
                        showToast(`${name}: grant saved and extension loaded`, 'ok');
                    } else {
                        showToast(`${name}: requested key grants saved`, 'ok');
                    }
                }
            } else if (target.classList.contains('skills-update')) {
                const source = target.dataset.source === 'ouroboroshub' ? 'ouroboroshub' : 'clawhub';
                showToast(`${name}: updating from ${source === 'ouroboroshub' ? 'OuroborosHub' : 'ClawHub'} (this may take ~30s)`, 'muted');
                const url = source === 'ouroboroshub'
                    ? `/api/marketplace/ouroboroshub/install`
                    : `/api/marketplace/clawhub/update/${encodeURIComponent(name)}`;
                const body = source === 'ouroboroshub' ? { slug: name, overwrite: true, auto_review: true } : {};
                const result = await postWithFeedback(url, body);
                const tail = result.review_status ? ` — review ${result.review_status}` : '';
                showToast(
                    result.ok
                        ? `${name}: updated${tail}`
                        : `${name}: update failed — ${result.error || 'unknown'}`,
                    result.ok ? 'ok' : 'danger',
                );
            } else if (target.classList.contains('skills-submit-hub')) {
                await triggerSkillAction(name, 'submit_hub');
            } else if (target.classList.contains('skills-uninstall')) {
                const source = target.dataset.source === 'ouroboroshub' ? 'ouroboroshub' : 'clawhub';
                const ok = await openConfirmDialog({
                    title: `Uninstall ${name}`,
                    body: `Uninstall ${name}? This deletes data/skills/${source}/${name}/.`,
                    confirmLabel: 'Uninstall',
                    danger: true,
                });
                if (!ok) {
                    return;
                }
                const url = source === 'ouroboroshub'
                    ? `/api/marketplace/ouroboroshub/uninstall/${encodeURIComponent(name)}`
                    : `/api/marketplace/clawhub/uninstall/${encodeURIComponent(name)}`;
                const result = await postWithFeedback(url, {});
                showToast(
                    result.ok ? `${name}: uninstalled` : `${name}: uninstall failed — ${result.error}`,
                    result.ok ? 'ok' : 'danger',
                );
                if (result.ok) emitSkillLifecycle('uninstall', name, result);
            }
        } catch (err) {
            showToast(`${name}: ${err.message || err}`, 'danger');
        } finally {
            target.disabled = false;
            closeSkillMenus();
            renderFn();
        }
    });

    document.addEventListener('click', (event) => {
        if (container.contains(event.target)) return;
        closeSkillMenus();
    });
    document.addEventListener('keydown', (event) => {
        if (event.key === 'Escape') closeSkillMenus();
    });
    window.addEventListener('scroll', () => closeSkillMenus(), true);
}


function activateTab(tabName) {
    const buttons = document.querySelectorAll('.skills-tab');
    const panels = document.querySelectorAll('.skills-tab-panel');
    const chromeRows = document.querySelectorAll('.skills-search-chrome');
    buttons.forEach((btn) => {
        const isActive = btn.dataset.tab === tabName;
        btn.classList.toggle('is-active', isActive);
        btn.setAttribute('aria-selected', isActive ? 'true' : 'false');
    });
    panels.forEach((panel) => {
        panel.hidden = panel.dataset.pane !== tabName;
    });
    chromeRows.forEach((row) => {
        row.hidden = row.dataset.chromePane !== tabName;
    });
}


async function renderMarketplacePane() {
    const pane = document.getElementById('skills-pane-marketplace');
    if (!pane) return;
    if (pane.dataset.bootstrapped === 'true') {
        // Tab entry refreshes installed state without simulating Search.
        if (typeof pane._marketplaceRefresh === 'function') {
            pane._marketplaceRefresh();
        }
        return;
    }
    pane.innerHTML = '<div class="muted">Loading marketplace…</div>';
    try {
        initMarketplace(pane, document.getElementById('skills-pane-marketplace-chrome'));
        pane.dataset.bootstrapped = 'true';
    } catch (err) {
        pane.dataset.bootstrapped = '';
        pane.innerHTML = `<div class="skills-load-error">Failed to load marketplace UI: ${escapeHtml(err.message || err)}</div>`;
        throw err;
    }
}


async function renderOuroborosHubPane() {
    const pane = document.getElementById('skills-pane-ouroboroshub');
    if (!pane) return;
    if (pane.dataset.bootstrapped === 'true') {
        if (typeof pane._ouroboroshubRefresh === 'function') {
            pane._ouroboroshubRefresh();
        }
        return;
    }
    pane.innerHTML = '<div class="muted">Loading OuroborosHub…</div>';
    try {
        initOuroborosHub(pane, document.getElementById('skills-pane-ouroboroshub-chrome'));
        pane.dataset.bootstrapped = 'true';
    } catch (err) {
        pane.dataset.bootstrapped = '';
        pane.innerHTML = `<div class="skills-load-error">Failed to load OuroborosHub UI: ${escapeHtml(err.message || err)}</div>`;
        throw err;
    }
}


export function initSkills(ctx) {
    const page = document.createElement('div');
    page.innerHTML = skillsPageTemplate();
    document.getElementById('content').appendChild(page.firstElementChild);

    const container = document.getElementById('skills-list');
    const emptyEl = document.getElementById('skills-empty');
    const refreshBtn = document.getElementById('skills-refresh');
    const reviewingSkills = new Set();
    const repairingSkills = new Set();

    const renderFn = async () => {
        refreshBtn.disabled = true;
        refreshBtn.classList.add('is-loading');
        const originalText = refreshBtn.textContent || 'Refresh';
        refreshBtn.textContent = 'Refreshing';
        try {
            await Promise.all([
                renderSkillsList(container, emptyEl, reviewingSkills, repairingSkills),
                new Promise((resolve) => setTimeout(resolve, 250)),
            ]);
        } catch (err) {
            container.innerHTML = `<div class="skills-load-error">Failed to render skills: ${escapeHtml(err.message || err)}</div>`;
            console.warn('skills: render failed', err);
        } finally {
            refreshBtn.disabled = false;
            refreshBtn.classList.remove('is-loading');
            refreshBtn.textContent = originalText === 'Refreshing' ? 'Refresh' : originalText;
        }
    };

    refreshBtn.addEventListener('click', renderFn);
    attachActionHandlers(container, renderFn, reviewingSkills, repairingSkills, ctx);

    document.querySelectorAll('.skills-tab').forEach((btn) => {
        btn.addEventListener('click', () => {
            const tabName = btn.dataset.tab;
            activateTab(tabName);
            if (tabName === 'marketplace') {
                renderMarketplacePane().catch((err) => {
                    showToast(`ClawHub failed: ${err.message || err}`, 'danger');
                });
            } else if (tabName === 'ouroboroshub') {
                renderOuroborosHubPane().catch((err) => {
                    showToast(`OuroborosHub failed: ${err.message || err}`, 'danger');
                });
            }
        });
    });

    window.addEventListener('ouro:page-shown', (event) => {
        if (event.detail?.page === 'skills') {
            renderFn();
        }
    });
    renderFn();
}
