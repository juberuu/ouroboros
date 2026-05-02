import { escapeHtml } from './utils.js';

export function initEvolution({ ws, state, mount = null, embedded = false, chartOnly = false, hostPage = 'settings', hostSubtab = 'evolution' }) {
    const page = document.createElement('div');
    page.id = 'page-evolution';
    page.className = embedded ? 'settings-embedded-content settings-evolution-panel' : 'page';
    page.innerHTML = `
        <div class="page-header">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" stroke-width="2"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>
            <h2>Evolution</h2>
            <div class="spacer"></div>
            <div class="evo-subtabs" ${chartOnly ? 'hidden' : ''}>
                <button class="evo-subtab active" data-subtab="chart">Chart</button>
                <button class="evo-subtab" data-subtab="versions">Versions</button>
            </div>
            <button id="evo-refresh" class="btn btn-default btn-sm evo-refresh-btn" type="button">Refresh</button>
            <span id="evo-status" class="status-badge">Loading...</span>
        </div>
        <!-- Chart sub-tab -->
        <div id="evo-chart-content" class="evolution-container">
            <div class="evo-runtime-card">
                <div class="evo-runtime-head">
                    <div>
                        <div class="section-title">Runtime Status</div>
                        <div id="evo-runtime-detail" class="evo-runtime-detail">Loading evolution and consciousness state...</div>
                    </div>
                    <div class="evo-runtime-pills">
                        <span id="evo-mode-pill" class="evo-runtime-pill">Evolution</span>
                        <span id="evo-bg-pill" class="evo-runtime-pill">Consciousness</span>
                    </div>
                </div>
                <div id="evo-runtime-meta" class="evo-runtime-meta"></div>
            </div>
            <div class="evo-chart-wrap">
                <canvas id="evo-chart"></canvas>
            </div>
            <div id="evo-tags-list" class="evo-tags-list"></div>
        </div>
        <!-- Versions sub-tab -->
        <div id="evo-versions-content" class="evo-versions-content">
            <div class="evo-versions-header">
                <div id="ver-current" class="evo-versions-branch"></div>
                <button class="btn btn-primary" id="btn-promote">Promote to Stable</button>
            </div>
            <div class="evo-versions-cols">
                <div class="evo-versions-col">
                    <h3 class="section-title">Recent Commits</h3>
                    <div id="ver-commits" class="log-scroll evo-versions-list"></div>
                </div>
                <div class="evo-versions-col">
                    <h3 class="section-title">Tags</h3>
                    <div id="ver-tags" class="log-scroll evo-versions-list"></div>
                </div>
            </div>
        </div>
    `;
    (mount || document.getElementById('content')).appendChild(page);

    // -----------------------------------------------------------------------
    // Sub-tab switching
    // -----------------------------------------------------------------------
    let activeSubtab = 'chart';
    const subtabButtons = page.querySelectorAll('.evo-subtab');
    const chartContent = document.getElementById('evo-chart-content');
    const versionsContent = document.getElementById('evo-versions-content');
    if (chartOnly && versionsContent) versionsContent.hidden = true;

    function isEvolutionVisible() {
        return embedded
            ? state.activePage === hostPage && (hostPage === 'dashboard' ? state.dashboardActiveSubtab : state.settingsActiveSubtab) === hostSubtab
            : state.activePage === 'evolution';
    }

    function showSubtab(name) {
        activeSubtab = name;
        subtabButtons.forEach(btn => btn.classList.toggle('active', btn.dataset.subtab === name));
        chartContent.style.display = name === 'chart' ? '' : 'none';
        versionsContent.style.display = name === 'versions' ? 'flex' : 'none';
        if (name === 'chart' && isEvolutionVisible()) ensureEvolutionLoaded(false);
        if (name === 'versions' && !versionsLoaded) loadVersions();
    }

    subtabButtons.forEach(btn => {
        btn.addEventListener('click', () => showSubtab(btn.dataset.subtab));
    });

    // -----------------------------------------------------------------------
    // Chart sub-tab (existing evolution logic)
    // -----------------------------------------------------------------------
    let evoChart = null;
    let loadSequence = 0;
    let chartLoaded = false;
    const refreshBtn = document.getElementById('evo-refresh');
    const statusBadge = document.getElementById('evo-status');
    const runtimeDetail = document.getElementById('evo-runtime-detail');
    const runtimeMeta = document.getElementById('evo-runtime-meta');
    const evolutionPill = document.getElementById('evo-mode-pill');
    const consciousnessPill = document.getElementById('evo-bg-pill');
    const tagsList = document.getElementById('evo-tags-list');

    const COLORS = {
        code_lines: '#60a5fa',
        bible_kb:   '#f97316',
        system_kb:  '#a78bfa',
        identity_kb:'#34d399',
        scratchpad_kb: '#fbbf24',
        memory_kb:  '#fb7185',
    };
    const LABELS = {
        code_lines: 'Code (lines)',
        bible_kb:   'BIBLE.md (KB)',
        system_kb:  'SYSTEM.md (KB)',
        identity_kb:'identity.md (KB)',
        scratchpad_kb: 'Scratchpad (KB)',
        memory_kb:  'Memory (KB)',
    };

    function setBadge(kind, text) {
        if (!statusBadge) return;
        statusBadge.textContent = text;
        statusBadge.className = `status-badge ${kind}`;
    }

    function formatTs(value) {
        if (!value) return '';
        const parsed = new Date(value);
        if (Number.isNaN(parsed.getTime())) return '';
        return parsed.toLocaleString([], {
            year: 'numeric',
            month: 'short',
            day: 'numeric',
            hour: '2-digit',
            minute: '2-digit',
        });
    }

    function pillTone(status) {
        if (['running', 'queued', 'idle_ready'].includes(status)) return 'online';
        if (['waiting_for_idle', 'waiting_for_owner_chat', 'paused', 'starting'].includes(status)) return 'starting';
        if (['budget_blocked', 'budget_stopped', 'paused_failures', 'error_backoff'].includes(status)) return 'error';
        return 'offline';
    }

    function shortStatusLabel(status, fallback = 'off') {
        if (status === 'running') return 'running';
        if (status === 'queued') return 'queued';
        if (status === 'idle_ready') return 'idle';
        if (status === 'waiting_for_idle') return 'waiting';
        if (status === 'waiting_for_owner_chat') return 'needs owner';
        if (status === 'paused' || status === 'paused_failures') return 'paused';
        if (status === 'budget_blocked' || status === 'budget_stopped') return 'budget';
        if (status === 'error_backoff') return 'retrying';
        if (status === 'stopped') return 'stopped';
        return fallback;
    }

    function runtimeChip(label, value) {
        if (value === null || value === undefined || value === '') return '';
        return `<span class="evo-runtime-chip"><strong>${label}:</strong> ${value}</span>`;
    }

    function renderRuntimeState(runtime = {}, generatedAt = '') {
        const evolution = runtime.evolution_state || {};
        const consciousness = runtime.bg_consciousness_state || {};
        const evolutionStatus = evolution.status || (runtime.evolution_enabled ? 'idle_ready' : 'disabled');
        const consciousnessStatus = consciousness.status || (runtime.bg_consciousness_enabled ? 'running' : 'disabled');

        evolutionPill.className = `evo-runtime-pill ${pillTone(evolutionStatus)}`;
        evolutionPill.textContent = `Evolution ${shortStatusLabel(evolutionStatus, 'off')}`;

        consciousnessPill.className = `evo-runtime-pill ${pillTone(consciousnessStatus)}`;
        consciousnessPill.textContent = `Consciousness ${shortStatusLabel(consciousnessStatus, 'off')}`;

        const lines = [];
        if (evolution.detail) lines.push(evolution.detail);
        if (consciousness.detail) lines.push(`Consciousness: ${consciousness.detail}`);
        runtimeDetail.textContent = lines.filter(Boolean).join(' ');

        runtimeMeta.innerHTML = [
            runtimeChip('Cycle', evolution.cycle || 0),
            runtimeChip('Queue', `${evolution.pending_count || 0} pending / ${evolution.running_count || 0} running`),
            runtimeChip('Failures', evolution.consecutive_failures || 0),
            runtimeChip('Budget left', Number.isFinite(Number(evolution.budget_remaining_usd)) ? `$${Number(evolution.budget_remaining_usd).toFixed(2)}` : ''),
            runtimeChip('Last evolution', formatTs(evolution.last_task_at)),
            runtimeChip('Next wakeup', consciousness.next_wakeup_sec ? `${consciousness.next_wakeup_sec}s` : ''),
            runtimeChip('Last background cycle', formatTs(consciousness.last_cycle_finished_at || consciousness.last_cycle_started_at)),
            runtimeChip('Updated', formatTs(generatedAt)),
        ].filter(Boolean).join('');
    }

    function renderEmptyState(message) {
        if (evoChart) {
            evoChart.destroy();
            evoChart = null;
        }
        tagsList.innerHTML = `<div class="evo-empty">${message}</div>`;
    }

    async function loadEvolution(force = false) {
        chartLoaded = true;
        const requestId = ++loadSequence;
        refreshBtn.disabled = true;
        setBadge('starting', force ? 'Refreshing...' : 'Loading...');
        try {
            const suffix = force ? '?force=1' : '';
            const [stateResp, evoResp] = await Promise.all([
                fetch('/api/state', { cache: 'no-store' }),
                fetch(`/api/evolution-data${suffix}`, { cache: 'no-store' }),
            ]);
            if (!stateResp.ok) throw new Error('State API error ' + stateResp.status);
            if (!evoResp.ok) throw new Error('Evolution API error ' + evoResp.status);
            const runtime = await stateResp.json();
            const data = await evoResp.json();
            if (requestId !== loadSequence) return;
            renderRuntimeState(runtime, data.generated_at || '');
            const points = data.points || [];
            if (points.length === 0) {
                renderEmptyState('No evolution tags yet. When evolution commits start landing, the chart will appear here.');
                setBadge('offline', 'No data');
                return;
            }
            setBadge('online', data.cached ? `${points.length} tags (cached)` : `${points.length} tags`);
            renderChart(points);
            renderTagsList(points);
        } catch (err) {
            console.error('Evolution load error:', err);
            if (requestId !== loadSequence) return;
            renderEmptyState('Failed to load evolution data. Use Refresh to try again.');
            setBadge('error', 'Error');
            runtimeDetail.textContent = 'Failed to load evolution state. Try Refresh or wait for the runtime to reconnect.';
            runtimeMeta.innerHTML = '';
        } finally {
            if (requestId === loadSequence) refreshBtn.disabled = false;
        }
    }

    function ensureEvolutionLoaded(force = false) {
        if (activeSubtab !== 'chart') return;
        if (!force && chartLoaded) {
            loadEvolution(false);
            return;
        }
        loadEvolution(force);
    }

    function renderChart(points) {
        const labels = points.map(p => p.tag);
        const datasets = Object.keys(COLORS).map(key => ({
            label: LABELS[key],
            data: points.map(p => p[key] ?? null),
            borderColor: COLORS[key],
            backgroundColor: COLORS[key] + '22',
            borderWidth: 2,
            pointRadius: 4,
            pointHoverRadius: 6,
            tension: 0.3,
            fill: false,
            yAxisID: key === 'code_lines' ? 'y' : 'y1',
        }));
        const ctx = document.getElementById('evo-chart').getContext('2d');
        if (evoChart) evoChart.destroy();
        evoChart = new Chart(ctx, {
            type: 'line',
            data: { labels, datasets },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                interaction: {
                    mode: 'index',
                    intersect: false,
                },
                plugins: {
                    legend: {
                        position: 'top',
                        labels: {
                            color: '#94a3b8',
                            usePointStyle: true,
                            pointStyle: 'circle',
                            padding: 16,
                            font: { size: 12, family: 'JetBrains Mono, monospace' },
                        },
                    },
                    tooltip: {
                        backgroundColor: 'rgba(26, 21, 32, 0.95)',
                        titleColor: '#e2e8f0',
                        bodyColor: '#94a3b8',
                        borderColor: 'rgba(201, 53, 69, 0.18)',
                        borderWidth: 1,
                        titleFont: { family: 'JetBrains Mono, monospace', size: 12 },
                        bodyFont: { family: 'JetBrains Mono, monospace', size: 11 },
                        callbacks: {
                            title: function(items) {
                                if (!items.length) return '';
                                const p = points[items[0].dataIndex];
                                return p.tag + ' (' + new Date(p.date).toLocaleDateString() + ')';
                            },
                            label: function(ctx) {
                                const val = ctx.parsed.y;
                                if (val === null || val === undefined) return null;
                                const key = Object.keys(COLORS)[ctx.datasetIndex];
                                if (key === 'code_lines') return ' ' + ctx.dataset.label + ': ' + val.toLocaleString() + ' lines';
                                return ' ' + ctx.dataset.label + ': ' + val.toFixed(1) + ' KB';
                            },
                        },
                    },
                },
                scales: {
                    x: {
                        ticks: { color: '#64748b', font: { size: 10, family: 'JetBrains Mono, monospace' }, maxRotation: 45 },
                        grid: { color: '#1e293b' },
                    },
                    y: {
                        type: 'linear',
                        position: 'left',
                        title: { display: true, text: 'Lines of Code', color: '#60a5fa', font: { size: 11 } },
                        ticks: { color: '#60a5fa', font: { size: 10 } },
                        grid: { color: '#1e293b' },
                    },
                    y1: {
                        type: 'linear',
                        position: 'right',
                        title: { display: true, text: 'Size (KB)', color: '#94a3b8', font: { size: 11 } },
                        ticks: { color: '#94a3b8', font: { size: 10 } },
                        grid: { drawOnChartArea: false },
                    },
                },
            },
        });
    }

    function renderTagsList(points) {
        const rows = points.map(p => {
            const d = new Date(p.date);
            const dateStr = d.toLocaleDateString() + ' ' + d.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});
            return `<tr>
                <td><code>${p.tag}</code></td>
                <td>${dateStr}</td>
                <td>${(p.code_lines || 0).toLocaleString()}</td>
                <td>${(p.bible_kb || 0).toFixed(1)}</td>
                <td>${(p.system_kb || 0).toFixed(1)}</td>
                <td>${(p.identity_kb || 0).toFixed(1)}</td>
                <td>${(p.scratchpad_kb || 0).toFixed(1)}</td>
                <td>${(p.memory_kb || 0).toFixed(1)}</td>
            </tr>`;
        }).reverse().join('');
        tagsList.innerHTML = `
            <table class="cost-table">
                <thead><tr>
                    <th>Tag</th><th>Date</th><th>Code Lines</th>
                    <th>BIBLE (KB)</th><th>SYSTEM (KB)</th>
                    <th>Identity (KB)</th><th>Scratchpad (KB)</th><th>Memory (KB)</th>
                </tr></thead>
                <tbody>${rows}</tbody>
            </table>
        `;
    }

    // -----------------------------------------------------------------------
    // Versions sub-tab (merged from versions.js)
    // -----------------------------------------------------------------------
    const commitsDiv = document.getElementById('ver-commits');
    const tagsDiv = document.getElementById('ver-tags');
    const currentDiv = document.getElementById('ver-current');
    let versionsLoaded = false;

    function renderRow(item, labelText, targetId) {
        const row = document.createElement('div');
        row.className = 'log-entry evo-versions-row';
        const date = (item.date || '').slice(0, 16).replace('T', ' ');
        const msg = escapeHtml((item.message || '').slice(0, 60));
        row.innerHTML = `
            <span class="log-type tools evo-versions-row-label">${escapeHtml(labelText)}</span>
            <span class="log-ts">${date}</span>
            <span class="log-msg evo-versions-row-msg">${msg}</span>
            <button class="btn btn-danger btn-xs" data-target="${escapeHtml(targetId)}">Restore</button>
        `;
        row.querySelector('button').addEventListener('click', () => rollback(targetId));
        return row;
    }

    async function loadVersions() {
        try {
            const resp = await fetch('/api/git/log');
            if (!resp.ok) throw new Error('Git log API error ' + resp.status);
            const data = await resp.json();
            currentDiv.textContent = `Branch: ${data.branch || '?'} @ ${data.sha || '?'}`;

            commitsDiv.innerHTML = '';
            (data.commits || []).forEach(c => {
                commitsDiv.appendChild(renderRow(c, c.short_sha || c.sha?.slice(0, 8), c.sha));
            });
            if (!data.commits?.length) commitsDiv.innerHTML = '<div class="evo-empty">No commits found</div>';

            tagsDiv.innerHTML = '';
            (data.tags || []).forEach(t => {
                tagsDiv.appendChild(renderRow(t, t.tag, t.tag));
            });
            if (!data.tags?.length) tagsDiv.innerHTML = '<div class="evo-empty">No tags found</div>';
            versionsLoaded = true;
        } catch (e) {
            const errHtml = `<div class="evo-empty evo-empty-error">Failed to load: ${escapeHtml(e.message)}</div>`;
            commitsDiv.innerHTML = errHtml;
            tagsDiv.innerHTML = errHtml;
            currentDiv.textContent = 'Branch: unknown';
            versionsLoaded = false;
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
            if (data.status === 'ok') {
                alert('Rollback successful: ' + data.message + '\n\nServer is restarting...');
            } else {
                alert('Rollback failed: ' + (data.error || 'unknown error'));
            }
        } catch (e) {
            alert('Rollback failed: ' + e.message);
        }
    }

    document.getElementById('btn-promote').addEventListener('click', async () => {
        if (!confirm('Promote current ouroboros branch to ouroboros-stable?')) return;
        try {
            const resp = await fetch('/api/git/promote', { method: 'POST' });
            const data = await resp.json();
            alert(data.status === 'ok' ? data.message : 'Error: ' + (data.error || 'unknown'));
        } catch (e) {
            alert('Failed: ' + e.message);
        }
    });

    // -----------------------------------------------------------------------
    // Refresh button + event listeners
    // -----------------------------------------------------------------------
    refreshBtn.addEventListener('click', () => {
        if (chartOnly || activeSubtab === 'chart') loadEvolution(true);
        else loadVersions();
    });

    ws.on('open', () => {
        if (isEvolutionVisible()) {
            ensureEvolutionLoaded(false);
            if (activeSubtab === 'versions') loadVersions();
        }
    });

    window.addEventListener('ouro:page-shown', (event) => {
        if (!embedded && event?.detail?.page === 'evolution') {
            if (activeSubtab === 'chart') ensureEvolutionLoaded(false);
            else loadVersions();
        }
    });
    window.addEventListener('ouro:settings-subtab-shown', (event) => {
        if (embedded && event?.detail?.tab === 'evolution') ensureEvolutionLoaded(false);
    });
    window.addEventListener('ouro:dashboard-subtab-shown', (event) => {
        if (embedded && event?.detail?.tab === 'evolution') ensureEvolutionLoaded(false);
    });

    document.addEventListener('visibilitychange', () => {
        if (!document.hidden && isEvolutionVisible()) {
            if (activeSubtab === 'chart' && chartLoaded) loadEvolution(false);
        }
    });
}
