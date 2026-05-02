export function initCosts({ ws, state, mount = null, embedded = false, hostPage = 'settings', hostSubtab = 'costs' }) {
    const page = document.createElement('div');
    page.id = 'page-costs';
    page.className = embedded ? 'settings-embedded-content settings-costs-panel' : 'page';
    page.innerHTML = `
        <div class="page-header">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" stroke-width="2"><line x1="12" y1="1" x2="12" y2="23"/><path d="M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/></svg>
            <h2>Costs</h2>
            <div class="spacer"></div>
            <button class="btn btn-default btn-sm" id="btn-refresh-costs">Refresh</button>
        </div>
        <div class="costs-scroll">
            <div class="costs-budget-card">
                <h3 class="costs-budget-title">Budget</h3>
                <div class="costs-budget-fields">
                    <div class="form-field">
                        <label>Total Budget ($)</label>
                        <input id="s-budget" type="number" min="1" value="10">
                    </div>
                    <div class="form-field">
                        <label>Per-task Cost Cap ($)</label>
                        <input id="s-per-task-cost" type="number" min="1" value="20">
                        <div class="settings-inline-note">Soft threshold only. When a task crosses it, Ouroboros is asked to wrap up rather than being hard-killed.</div>
                    </div>
                </div>
                <button class="btn btn-save costs-budget-save" id="btn-save-budget">Save Budget</button>
                <div id="budget-save-status" class="settings-inline-status"></div>
            </div>
            <div class="costs-stats-grid">
                <div class="stat-card"><div class="label">Total Spent</div><div class="value" id="cost-total">$0.00</div></div>
                <div class="stat-card"><div class="label">Total Calls</div><div class="value" id="cost-calls">0</div></div>
                <div class="stat-card"><div class="label">Top Model</div><div class="value cost-top-model" id="cost-top-model">-</div></div>
            </div>
            <div class="costs-tables-grid">
                <div>
                    <h3 class="costs-table-label">By Model</h3>
                    <table class="cost-table" id="cost-by-model"><thead><tr><th>Model</th><th>Calls</th><th>Cost</th><th></th></tr></thead><tbody></tbody></table>
                </div>
                <div>
                    <h3 class="costs-table-label">By API Key</h3>
                    <table class="cost-table" id="cost-by-key"><thead><tr><th>Key</th><th>Calls</th><th>Cost</th><th></th></tr></thead><tbody></tbody></table>
                </div>
                <div>
                    <h3 class="costs-table-label">By Model Category</h3>
                    <table class="cost-table" id="cost-by-model-cat"><thead><tr><th>Category</th><th>Calls</th><th>Cost</th><th></th></tr></thead><tbody></tbody></table>
                </div>
                <div>
                    <h3 class="costs-table-label">By Task Category</h3>
                    <table class="cost-table" id="cost-by-task-cat"><thead><tr><th>Category</th><th>Calls</th><th>Cost</th><th></th></tr></thead><tbody></tbody></table>
                </div>
            </div>
        </div>
    `;
    (mount || document.getElementById('content')).appendChild(page);

    function renderBreakdownTable(tableId, data, totalCost) {
        const tbody = document.querySelector('#' + tableId + ' tbody');
        tbody.innerHTML = '';
        for (const [name, info] of Object.entries(data)) {
            const pct = totalCost > 0 ? (info.cost / totalCost * 100) : 0;
            const tr = document.createElement('tr');

            const tdName = document.createElement('td');
            tdName.className = 'cost-cell-name';
            tdName.setAttribute('title', name);
            tdName.textContent = name;

            const tdCalls = document.createElement('td');
            tdCalls.className = 'cost-cell-right';
            tdCalls.textContent = info.calls;

            const tdCost = document.createElement('td');
            tdCost.className = 'cost-cell-right';
            tdCost.textContent = '$' + info.cost.toFixed(3);

            const bar = document.createElement('div');
            bar.className = 'cost-bar';
            bar.style.width = Math.min(100, pct) + '%';

            const tdBar = document.createElement('td');
            tdBar.className = 'cost-bar-cell';
            tdBar.appendChild(bar);

            tr.append(tdName, tdCalls, tdCost, tdBar);
            tbody.appendChild(tr);
        }
        if (Object.keys(data).length === 0) {
            const tr = document.createElement('tr');
            const td = document.createElement('td');
            td.className = 'cost-empty-cell';
            td.setAttribute('colspan', '4');
            td.textContent = 'No data';
            tr.appendChild(td);
            tbody.appendChild(tr);
        }
    }

    async function loadCosts() {
        try {
            const resp = await fetch('/api/cost-breakdown');
            const d = await resp.json();
            document.getElementById('cost-total').textContent = '$' + (d.total_cost || 0).toFixed(2);
            document.getElementById('cost-calls').textContent = d.total_calls || 0;
            const models = Object.entries(d.by_model || {});
            document.getElementById('cost-top-model').textContent = models.length > 0 ? models[0][0] : '-';
            renderBreakdownTable('cost-by-model', d.by_model || {}, d.total_cost);
            renderBreakdownTable('cost-by-key', d.by_api_key || {}, d.total_cost);
            renderBreakdownTable('cost-by-model-cat', d.by_model_category || {}, d.total_cost);
            renderBreakdownTable('cost-by-task-cat', d.by_task_category || {}, d.total_cost);
        } catch {}
    }

    async function loadBudget() {
        try {
            const resp = await fetch('/api/settings', { cache: 'no-store' });
            const s = await resp.json().catch(() => ({}));
            if (s.TOTAL_BUDGET) document.getElementById('s-budget').value = s.TOTAL_BUDGET;
            if (s.OUROBOROS_PER_TASK_COST_USD != null) document.getElementById('s-per-task-cost').value = s.OUROBOROS_PER_TASK_COST_USD;
        } catch {}
    }

    document.getElementById('btn-refresh-costs').addEventListener('click', loadCosts);

    document.getElementById('btn-save-budget').addEventListener('click', async () => {
        const statusEl = document.getElementById('budget-save-status');
        const budget = parseFloat(document.getElementById('s-budget').value) || 10;
        const perTask = parseFloat(document.getElementById('s-per-task-cost').value) || 20;
        try {
            const resp = await fetch('/api/settings', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ TOTAL_BUDGET: budget, OUROBOROS_PER_TASK_COST_USD: perTask }),
            });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
            let msg;
            if (data.no_changes) {
                msg = 'No changes.';
            } else if (data.restart_required) {
                msg = 'Saved. Restart required.';
            } else if (data.immediate_changed && data.next_task_changed) {
                msg = 'Saved. Budget limit took effect immediately; per-task cap applies on the next task.';
            } else if (data.immediate_changed) {
                msg = 'Saved. Took effect immediately.';
            } else {
                msg = 'Saved. Applies on the next task.';
            }
            if (data.warnings && data.warnings.length) msg += ' ⚠️ ' + data.warnings.join(' | ');
            statusEl.textContent = msg;
        } catch (e) {
            statusEl.textContent = 'Error: ' + e.message;
        }
        setTimeout(() => { statusEl.textContent = ''; }, 4000);
    });

    function refreshCostsPanel() {
        loadCosts();
        loadBudget();
    }

    if (embedded) {
        window.addEventListener('ouro:settings-subtab-shown', (event) => {
            if (event.detail?.tab === 'costs') refreshCostsPanel();
        });
        window.addEventListener('ouro:dashboard-subtab-shown', (event) => {
            if (event.detail?.tab === hostSubtab && state.activePage === hostPage) refreshCostsPanel();
        });
    } else {
        const obs = new MutationObserver(() => {
            if (page.classList.contains('active')) refreshCostsPanel();
        });
        obs.observe(page, { attributes: true, attributeFilter: ['class'] });
    }
}
