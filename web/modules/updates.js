import { escapeHtml } from './utils.js';

export function initUpdates({ mount, hostPage = 'settings', hostSubtab = 'updates', state = {} }) {
    const host = mount || document.getElementById('content');
    const page = document.createElement('div');
    page.id = 'page-updates';
    page.className = 'settings-embedded-content settings-updates-panel';
    // v5.7.0: drop the duplicate inner ".page-header" (the outer Dashboard
    // tab strip already labels the panel). The "Check for updates" button
    // moves into ".updates-card-head" alongside the status badge so the
    // refresh-style action sits with the status it refreshes.
    page.innerHTML = `
        <div class="updates-scroll">
            <section class="updates-card" id="updates-status-card">
                <div class="updates-card-head">
                    <div>
                        <div class="section-title">Official Updates</div>
                        <div class="updates-summary" id="updates-summary">Loading update status...</div>
                    </div>
                    <div class="updates-head-actions">
                        <span class="status-badge offline" id="updates-badge">Idle</span>
                        <button class="btn btn-default btn-sm" id="btn-update-check">Check for updates</button>
                    </div>
                </div>
                <div class="updates-meta" id="updates-meta"></div>
                <div class="updates-actions">
                    <button class="btn btn-primary" id="btn-update-apply" disabled>Update Now</button>
                </div>
            </section>
            <section class="updates-card">
                <div class="evo-versions-header">
                    <div id="updates-current" class="evo-versions-branch"></div>
                    <button class="btn btn-primary" id="updates-promote">Promote to Stable</button>
                </div>
                <div class="evo-versions-cols">
                    <div class="evo-versions-col">
                        <h3 class="section-title">Local Recovery: Recent Commits</h3>
                        <div id="updates-commits" class="log-scroll evo-versions-list"></div>
                    </div>
                    <div class="evo-versions-col">
                        <h3 class="section-title">Official Releases</h3>
                        <div id="updates-official-tags" class="log-scroll evo-versions-list"></div>
                    </div>
                    <div class="evo-versions-col">
                        <h3 class="section-title">Local Recovery: Local Tags</h3>
                        <div id="updates-tags" class="log-scroll evo-versions-list"></div>
                    </div>
                </div>
            </section>
        </div>
    `;
    host.appendChild(page);

    const checkBtn = page.querySelector('#btn-update-check');
    const applyBtn = page.querySelector('#btn-update-apply');
    const badge = page.querySelector('#updates-badge');
    const summary = page.querySelector('#updates-summary');
    const meta = page.querySelector('#updates-meta');
    const current = page.querySelector('#updates-current');
    const commitsDiv = page.querySelector('#updates-commits');
    const officialTagsDiv = page.querySelector('#updates-official-tags');
    const tagsDiv = page.querySelector('#updates-tags');
    let latestStatus = null;

    function setBadge(kind, text) {
        badge.className = `status-badge ${kind}`;
        badge.textContent = text;
    }

    function divergenceText(data) {
        const parts = [];
        if (data.behind) parts.push(`${data.behind} incoming`);
        if (data.ahead) parts.push(`${data.ahead} local`);
        if (data.dirty_count) parts.push(`${data.dirty_count} dirty`);
        return parts.join(' / ') || 'clean';
    }

    function renderStatus(data) {
        latestStatus = data;
        const unmanaged = data.managed === false
            || (Array.isArray(data.warnings) && data.warnings.includes('managed_updates_unavailable'));
        if (unmanaged) {
            summary.textContent = 'Managed updates are unavailable for this checkout.';
            meta.innerHTML = `
                <span class="evo-runtime-chip"><strong>Mode:</strong> source checkout</span>
                <span class="evo-runtime-chip"><strong>Action:</strong> use git or install a launcher-managed build</span>
            `;
            applyBtn.disabled = true;
            applyBtn.dataset.safe = '0';
            applyBtn.textContent = 'Unavailable';
            setBadge('offline', 'Unavailable');
            return;
        }
        if (Array.isArray(data.warnings) && data.warnings.includes('official_status_requires_check')) {
            summary.textContent = 'Click Check for updates to refresh official update status.';
            meta.innerHTML = '<span class="evo-runtime-chip"><strong>Official repo:</strong> joi-lab/ouroboros-desktop</span>';
            applyBtn.disabled = true;
            applyBtn.dataset.safe = '0';
            applyBtn.textContent = 'Check Required';
            setBadge('offline', 'Not checked');
            return;
        }
        const currentVersion = data.current_version || 'unknown';
        const latestVersion = data.latest_version || 'unknown';
        const currentSha = data.current_short_sha || '?';
        const latestSha = data.latest_short_sha || '?';
        const latestMsg = data.latest_message || 'No remote message.';
        const canUpdate = Boolean(data.available);
        const safe = Boolean(data.safe_to_apply);
        summary.textContent = canUpdate
            ? `Update available: ${currentVersion} (${currentSha}) -> ${latestVersion} (${latestSha})`
            : `Ouroboros is up to date at ${currentVersion} (${currentSha}).`;
        meta.innerHTML = [
            `<span class="evo-runtime-chip"><strong>Official repo:</strong> joi-lab/ouroboros-desktop</span>`,
            `<span class="evo-runtime-chip"><strong>Remote ref:</strong> ${escapeHtml(data.remote || 'managed')}/${escapeHtml(data.remote_branch || '')}</span>`,
            `<span class="evo-runtime-chip"><strong>Divergence:</strong> ${escapeHtml(divergenceText(data))}</span>`,
            `<span class="evo-runtime-chip"><strong>Latest:</strong> ${escapeHtml(latestMsg)}</span>`,
        ].join('');
        applyBtn.disabled = !canUpdate;
        applyBtn.dataset.safe = safe ? '1' : '0';
        applyBtn.textContent = !canUpdate ? 'No Update Available' : (safe ? 'Update Now' : 'Update with Options');
        setBadge(canUpdate ? (safe ? 'online' : 'starting') : 'offline', canUpdate ? 'Available' : 'Current');
    }

    async function loadStatus({ fetchRemote = false } = {}) {
        checkBtn.disabled = true;
        setBadge('starting', fetchRemote ? 'Checking...' : 'Loading...');
        try {
            const resp = await fetch(fetchRemote ? '/api/update/check' : '/api/update/status', {
                method: fetchRemote ? 'POST' : 'GET',
                cache: 'no-store',
            });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
            renderStatus(data);
            renderOfficialTags(data.official_tags || []);
        } catch (err) {
            summary.textContent = `Failed to load update status: ${err.message || err}`;
            meta.innerHTML = '';
            applyBtn.disabled = true;
            setBadge('error', 'Error');
        } finally {
            checkBtn.disabled = false;
        }
    }

    function renderVersionRow(item, labelText, targetId) {
        const row = document.createElement('div');
        row.className = 'log-entry evo-versions-row';
        const date = (item.date || '').slice(0, 16).replace('T', ' ');
        const msg = escapeHtml((item.message || '').slice(0, 72));
        row.innerHTML = `
            <span class="log-type tools evo-versions-row-label">${escapeHtml(labelText)}</span>
            <span class="log-ts">${escapeHtml(date)}</span>
            <span class="log-msg evo-versions-row-msg">${msg}</span>
            <button class="btn btn-danger btn-xs" data-target="${escapeHtml(targetId)}">Restore</button>
        `;
        row.querySelector('button').addEventListener('click', () => rollback(targetId));
        return row;
    }

    function renderOfficialTags(tags) {
        officialTagsDiv.innerHTML = '';
        (tags || []).forEach((tag) => {
            const row = document.createElement('div');
            row.className = 'log-entry evo-versions-row';
            row.innerHTML = `
                <span class="log-type tools evo-versions-row-label">${escapeHtml(tag.tag || '')}</span>
                <span class="log-msg evo-versions-row-msg">${escapeHtml((tag.sha || '').slice(0, 12))}</span>
            `;
            officialTagsDiv.appendChild(row);
        });
        if (!tags?.length) officialTagsDiv.innerHTML = '<div class="evo-empty">Check for updates to load official releases.</div>';
    }

    async function loadVersions() {
        try {
            const resp = await fetch('/api/git/log', { cache: 'no-store' });
            if (!resp.ok) throw new Error('Git log API error ' + resp.status);
            const data = await resp.json();
            current.textContent = `Branch: ${data.branch || '?'} @ ${data.sha || '?'}`;
            commitsDiv.innerHTML = '';
            (data.commits || []).forEach((commit) => {
                commitsDiv.appendChild(renderVersionRow(commit, commit.short_sha || commit.sha?.slice(0, 8), commit.sha));
            });
            if (!data.commits?.length) commitsDiv.innerHTML = '<div class="evo-empty">No commits found</div>';
            tagsDiv.innerHTML = '';
            (data.tags || []).forEach((tag) => {
                tagsDiv.appendChild(renderVersionRow(tag, tag.tag, tag.tag));
            });
            if (!data.tags?.length) tagsDiv.innerHTML = '<div class="evo-empty">No tags found</div>';
        } catch (err) {
            const msg = `<div class="evo-empty evo-empty-error">Failed to load: ${escapeHtml(err.message || err)}</div>`;
            commitsDiv.innerHTML = msg;
            tagsDiv.innerHTML = msg;
            current.textContent = 'Branch: unknown';
        }
    }

    async function rollback(target) {
        if (!confirm(`Roll back to ${target}?\n\nA rescue snapshot of the current state will be saved. The server will restart.`)) return;
        try {
            const resp = await fetch('/api/git/rollback', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ target }),
            });
            const data = await resp.json();
            alert(data.status === 'ok'
                ? `Rollback successful: ${data.message}\n\nServer is restarting...`
                : `Rollback failed: ${data.error || 'unknown error'}`);
        } catch (err) {
            alert('Rollback failed: ' + (err.message || err));
        }
    }

    async function applyUpdate() {
        if (!latestStatus?.available) return;
        const safe = latestStatus.safe_to_apply;
        let strategy = 'replace';
        if (!safe) {
            const localBits = divergenceText(latestStatus);
            const proceed = confirm(
                `This update will replace the active managed checkout with the selected official version.\n\nLocal state: ${localBits}\n\nLocal commits will be preserved on a local-keep-* branch before the active branch moves. Dirty files will be saved in a rescue snapshot. Continue?`,
            );
            if (!proceed) return;
            strategy = latestStatus.ahead ? 'stash' : 'replace';
        }
        applyBtn.disabled = true;
        applyBtn.textContent = 'Preparing...';
        try {
            const resp = await fetch('/api/update/apply', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ strategy }),
            });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
            const keep = data.keep_branch ? `\nLocal commits preserved as ${data.keep_branch}.` : '';
            alert(`Update prepared. Server is restarting.${keep}`);
        } catch (err) {
            alert('Update failed: ' + (err.message || err));
            applyBtn.disabled = false;
            applyBtn.textContent = safe ? 'Update Now' : 'Update with Options';
        }
    }

    checkBtn.addEventListener('click', () => {
        loadStatus({ fetchRemote: true });
        loadVersions();
    });
    applyBtn.addEventListener('click', applyUpdate);
    page.querySelector('#updates-promote').addEventListener('click', async () => {
        if (!confirm('Promote current ouroboros branch to ouroboros-stable?')) return;
        try {
            const resp = await fetch('/api/git/promote', { method: 'POST' });
            const data = await resp.json();
            alert(data.status === 'ok' ? data.message : 'Error: ' + (data.error || 'unknown'));
        } catch (err) {
            alert('Failed: ' + (err.message || err));
        }
    });

    window.addEventListener('ouro:settings-subtab-shown', (event) => {
        if (event.detail?.tab !== 'updates') return;
        loadStatus({ fetchRemote: false });
        loadVersions();
    });
    window.addEventListener('ouro:dashboard-subtab-shown', (event) => {
        if (event.detail?.tab !== hostSubtab || state.activePage !== hostPage) return;
        loadStatus({ fetchRemote: false });
        loadVersions();
    });
}
