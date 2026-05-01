(() => {
    const bootstrap = window.__OURO_ONBOARDING_BOOTSTRAP__ || {};
    const HOST_MODE = bootstrap.hostMode || 'desktop';
    const LOCAL_RUNTIME_CONTROLS = Boolean(bootstrap.supportsLocalRuntimeControls);
    const STEP_ORDER = bootstrap.stepOrder || ['providers', 'models', 'review_mode', 'budget', 'summary'];
    const MODEL_DEFAULTS = bootstrap.modelDefaults || {};
    const LOCAL_PRESETS = bootstrap.localPresets || {};
    const MODEL_SUGGESTIONS = bootstrap.modelSuggestions || [];
    const INITIAL_STATE = bootstrap.initialState || {};
    const root = document.getElementById('root');

    const STEP_META = {
        providers: {
            title: 'Add your access',
            railCopy: 'Keys + local',
            copy: 'Fill at least one remote key or a local model source. The next step adapts to what you configured here.',
            footer: 'Paste only what you already have. OpenRouter, direct provider keys, and an optional local model can coexist.',
        },
        models: {
            title: 'Choose models',
            railCopy: '4 model slots',
            copy: 'Review the visible model defaults derived from your current setup, then edit anything you want before launch.',
            footer: 'Plain openai/... or anthropic/... remains router-style. Direct values use openai::... and anthropic::....',
        },
        review_mode: {
            title: 'Choose review mode',
            railCopy: 'Advisory vs blocking',
            copy: 'Decide how strict pre-commit review should be before Ouroboros starts modifying itself.',
            footer: 'Pick both review enforcement and the initial runtime mode before Ouroboros starts.',
        },
        budget: {
            title: 'Set your budget',
            railCopy: 'Session limits',
            copy: 'Budget is its own step because it directly shapes how far Ouroboros can go in one session and in a single task.',
            footer: 'Total budget is global. Per-task cost cap is a soft reminder, not a hard kill switch.',
        },
        summary: {
            title: 'Review before launch',
            railCopy: 'Final check',
            copy: 'Check the final provider, model, review, and budget picture. Ouroboros will save these onboarding values before starting.',
            footer: 'The same onboarding values remain editable later in Settings.',
        },
    };

    const state = Object.assign({
        currentStep: STEP_ORDER[0],
        error: '',
        saving: false,
        modelsDirty: false,
        localSourceOpen: Boolean(INITIAL_STATE.localSource),
        localStatusText: 'Status: Offline',
        localStatusTone: 'muted',
        localTestResult: '',
        localTestTone: 'muted',
        localRuntimeReady: false,
        claudeCliInstalled: false,
        claudeCliBusy: false,
        claudeCliStatus: '',
        claudeCliStatusText: 'Checking Claude runtime...',
        claudeCliTone: 'muted',
        claudeCliError: '',
        claudeCliDismissed: false,
    }, INITIAL_STATE);

    let localStatusPollStarted = false;
    let claudeCliPollStarted = false;

    function trim(value) {
        return String(value || '').trim();
    }

    function escapeHtml(value) {
        return String(value || '')
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;');
    }

    function formatUsd(value) {
        const num = Number(value);
        return Number.isFinite(num) ? `$${num.toFixed(2)}` : '$0.00';
    }

    function hasLocalModel() {
        return trim(state.localSource).length > 0;
    }

    function hasAnthropicKeyConfigured() {
        return trim(state.anthropicKey).length >= 10;
    }

    function shouldShowClaudeCliCta() {
        return hasAnthropicKeyConfigured() && !state.claudeCliDismissed;
    }

    function isLocalFilesystemSource(value) {
        const text = trim(value);
        return text.startsWith('/') || text.startsWith('~');
    }

    function detectProviderProfile() {
        const hasOpenrouter = trim(state.openrouterKey).length >= 10;
        const hasOpenai = trim(state.openaiKey).length >= 10;
        const hasCloudru = trim(state.cloudruKey).length >= 10;
        const hasAnthropic = trim(state.anthropicKey).length >= 10;
        if (hasOpenrouter) return 'openrouter';
        if ([hasOpenai, hasCloudru, hasAnthropic].filter(Boolean).length > 1) return 'direct-multi';
        if (hasOpenai) return 'openai';
        if (hasCloudru) return 'cloudru';
        if (hasAnthropic) return 'anthropic';
        if (hasLocalModel()) return 'local';
        return 'openrouter';
    }

    function activeProviderProfile() {
        const profile = detectProviderProfile();
        state.providerProfile = profile;
        return profile;
    }

    function profileLabel(profile) {
        if (profile === 'openai') return 'OpenAI';
        if (profile === 'cloudru') return 'Cloud.ru Foundation Models';
        if (profile === 'anthropic') return 'Anthropic';
        if (profile === 'direct-multi') return 'Direct multi-provider';
        if (profile === 'local') return 'Local-first';
        return 'OpenRouter';
    }

    function reviewLabel(mode) {
        return mode === 'blocking' ? 'Blocking' : 'Advisory';
    }

    function runtimeModeLabel(mode) {
        if (mode === 'light') return 'Light';
        if (mode === 'pro') return 'Pro';
        return 'Advanced';
    }

    function localRoutingLabel(mode) {
        if (mode === 'all') return 'All models local';
        if (mode === 'fallback') return 'Fallback model local';
        return 'Cloud models only';
    }

    function nextButtonShouldBeDisabled() {
        if (state.saving) return true;
        if (state.currentStep === 'summary') return false;
        return Boolean(validateCurrentStep());
    }

    function syncCurrentStepActionState() {
        const next = document.getElementById('next-btn');
        if (next) next.disabled = nextButtonShouldBeDisabled();
    }

    function applyPresetSelection(presetId) {
        state.localPreset = presetId;
        state.localSourceOpen = Boolean(presetId);
        if (!presetId) {
            state.localSource = '';
            state.localFilename = '';
            state.localContextLength = 16384;
            state.localGpuLayers = -1;
            state.localChatFormat = '';
            state.localRoutingMode = 'cloud';
            return;
        }
        if (presetId === 'custom') {
            if (!trim(state.localSource)) {
                state.localSource = '';
                state.localFilename = '';
            }
            return;
        }
        const preset = LOCAL_PRESETS[presetId];
        if (!preset) return;
        state.localSource = preset.source;
        state.localFilename = preset.filename;
        state.localContextLength = preset.contextLength;
        state.localChatFormat = preset.chatFormat || '';
        if (activeProviderProfile() === 'local') {
            state.localRoutingMode = 'all';
        } else if (state.localRoutingMode === 'cloud') {
            state.localRoutingMode = 'fallback';
        }
    }

    function detectLocalPresetSelection() {
        const source = trim(state.localSource);
        const filename = trim(state.localFilename);
        if (!source && !filename) return '';
        for (const [presetId, preset] of Object.entries(LOCAL_PRESETS)) {
            if (source === trim(preset.source) && filename === trim(preset.filename)) {
                return presetId;
            }
        }
        return 'custom';
    }

    function applyModelDefaults(force) {
        if (state.modelsDirty && !force) return;
        const defaults = MODEL_DEFAULTS[activeProviderProfile()] || MODEL_DEFAULTS.openrouter || {};
        state.mainModel = defaults.main || '';
        state.codeModel = defaults.code || '';
        state.lightModel = defaults.light || '';
        state.fallbackModel = defaults.fallback || '';
        state.modelsDirty = false;
    }

    function validateProvidersStep() {
        const openrouterKey = trim(state.openrouterKey);
        const openaiKey = trim(state.openaiKey);
        const cloudruKey = trim(state.cloudruKey);
        const anthropicKey = trim(state.anthropicKey);
        const localSource = trim(state.localSource);
        const localFilename = trim(state.localFilename);
        if (openrouterKey && openrouterKey.length < 10) return 'OpenRouter API key looks too short.';
        if (openaiKey && openaiKey.length < 10) return 'OpenAI API key looks too short.';
        if (cloudruKey && cloudruKey.length < 10) return 'Cloud.ru Foundation Models API key looks too short.';
        if (anthropicKey && anthropicKey.length < 10) return 'Anthropic API key looks too short.';
        if (!openrouterKey && !openaiKey && !cloudruKey && !anthropicKey && !localSource) {
            return 'Enter at least one remote key or a local model source before continuing.';
        }
        if (localSource && !openrouterKey && !openaiKey && !cloudruKey && !anthropicKey && trim(state.localRoutingMode) === 'cloud') {
            return 'Local-only setups must route at least one model to the local runtime.';
        }
        if (localSource && localSource.includes('/') && !isLocalFilesystemSource(localSource) && !localFilename) {
            return 'Local HuggingFace sources need a GGUF filename.';
        }
        if (localSource && (!Number.isInteger(Number(state.localContextLength)) || Number(state.localContextLength) <= 0)) {
            return 'Local context length must be a positive integer.';
        }
        if (localSource && !Number.isInteger(Number(state.localGpuLayers))) {
            return 'Local GPU layers must be an integer.';
        }
        return '';
    }

    function validateModelsStep() {
        if (!trim(state.mainModel) || !trim(state.codeModel) || !trim(state.lightModel) || !trim(state.fallbackModel)) {
            return 'Confirm all four models before starting Ouroboros.';
        }
        return '';
    }

    function validateReviewStep() {
        if (!['advisory', 'blocking'].includes(trim(state.reviewEnforcement))) {
            return 'Choose advisory or blocking review mode.';
        }
        return '';
    }

    function validateBudgetStep() {
        const totalBudget = Number(state.totalBudget);
        const perTaskCostUsd = Number(state.perTaskCostUsd);
        if (!Number.isFinite(totalBudget) || totalBudget <= 0) {
            return 'Total budget must be greater than zero.';
        }
        if (!Number.isFinite(perTaskCostUsd) || perTaskCostUsd <= 0) {
            return 'Per-task soft threshold must be greater than zero.';
        }
        return '';
    }

    function validateCurrentStep() {
        if (state.currentStep === 'providers') return validateProvidersStep();
        if (state.currentStep === 'models') return validateModelsStep();
        if (state.currentStep === 'review_mode') return validateReviewStep();
        if (state.currentStep === 'budget') return validateBudgetStep();
        return '';
    }

    function nextStep() {
        const error = validateCurrentStep();
        state.error = error;
        if (error) {
            render();
            return;
        }
        if (state.currentStep === 'providers') applyModelDefaults(false);
        const index = STEP_ORDER.indexOf(state.currentStep);
        if (index >= 0 && index < STEP_ORDER.length - 1) {
            state.currentStep = STEP_ORDER[index + 1];
        }
        state.error = '';
        render();
    }

    function previousStep() {
        const index = STEP_ORDER.indexOf(state.currentStep);
        if (index > 0) state.currentStep = STEP_ORDER[index - 1];
        state.error = '';
        render();
    }

    async function apiRequest(url, init = {}) {
        const response = await fetch(url, init);
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
            throw new Error(data.error || `HTTP ${response.status}`);
        }
        return data;
    }

    function applyClaudeCliStatus(payload = {}) {
        const ready = Boolean(payload.ready);
        const installed = Boolean(payload.installed);
        const busy = Boolean(payload.busy);
        const errorText = trim(payload.error);
        const message = trim(payload.message)
            || (ready ? 'Claude runtime ready.' : (installed ? 'Claude runtime available but not ready.' : 'Claude runtime not available.'));
        state.claudeCliInstalled = installed || ready;
        state.claudeCliBusy = busy;
        state.claudeCliStatus = trim(payload.status) || (ready ? 'ready' : (installed ? 'installed' : 'missing'));
        state.claudeCliError = errorText;
        state.claudeCliTone = ready ? 'ok' : (errorText ? 'error' : (installed ? 'muted' : 'error'));
        state.claudeCliStatusText = message;
        renderClaudeCliStatus();
    }

    async function claudeCliRequestStatus() {
        if (HOST_MODE === 'web') {
            return apiRequest('/api/claude-code/status', { cache: 'no-store' });
        }
        if (!window.pywebview?.api?.claude_code_status) {
            throw new Error('Desktop Claude Code bridge is unavailable.');
        }
        return window.pywebview.api.claude_code_status();
    }

    async function claudeCliStartInstall() {
        if (HOST_MODE === 'web') {
            return apiRequest('/api/claude-code/install', { method: 'POST' });
        }
        if (!window.pywebview?.api?.install_claude_code) {
            throw new Error('Desktop Claude Code install bridge is unavailable.');
        }
        return window.pywebview.api.install_claude_code();
    }

    async function updateClaudeCliStatus() {
        if (!shouldShowClaudeCliCta()) return;
        try {
            applyClaudeCliStatus(await claudeCliRequestStatus());
        } catch (error) {
            state.claudeCliInstalled = false;
            state.claudeCliBusy = false;
            state.claudeCliStatus = 'error';
            state.claudeCliError = String(error?.message || error || '');
            state.claudeCliTone = 'error';
            state.claudeCliStatusText = `Claude runtime status failed: ${state.claudeCliError}`;
            renderClaudeCliStatus();
        }
    }

    function startClaudeCliStatusPolling() {
        if (claudeCliPollStarted) return;
        claudeCliPollStarted = true;
        updateClaudeCliStatus();
        setInterval(() => {
            if (shouldShowClaudeCliCta()) updateClaudeCliStatus();
        }, 3000);
    }

    function syncClaudeCliVisibility() {
        const card = document.getElementById('wizard-claude-card');
        if (card) card.style.display = shouldShowClaudeCliCta() ? '' : 'none';
        renderClaudeCliStatus();
    }

    function renderClaudeCliStatus() {
        const card = document.getElementById('wizard-claude-card');
        const statusEl = document.getElementById('wizard-claude-status');
        const installButton = document.getElementById('wizard-claude-install');
        const skipButton = document.getElementById('wizard-claude-skip');
        if (card) card.style.display = shouldShowClaudeCliCta() ? '' : 'none';
        if (statusEl) {
            statusEl.textContent = state.claudeCliStatusText || 'Checking Claude runtime...';
            statusEl.dataset.tone = state.claudeCliTone || 'muted';
        }
        if (installButton) {
            installButton.disabled = state.claudeCliBusy;
            installButton.textContent = state.claudeCliBusy
                ? 'Repairing...'
                : (state.claudeCliInstalled ? 'Runtime OK' : 'Repair Runtime');
        }
        if (skipButton) {
            skipButton.hidden = state.claudeCliBusy || state.claudeCliInstalled;
        }
    }

    function renderLocalStatus() {
        const statusEl = document.getElementById('wizard-local-status');
        const stopButton = document.getElementById('wizard-local-stop');
        const testButton = document.getElementById('wizard-local-test');
        const resultEl = document.getElementById('wizard-local-test-result');
        if (statusEl) {
            statusEl.textContent = state.localStatusText || 'Status: Offline';
            statusEl.dataset.tone = state.localStatusTone || 'muted';
        }
        if (stopButton) stopButton.disabled = !state.localRuntimeReady;
        if (testButton) testButton.disabled = !state.localRuntimeReady;
        if (resultEl) {
            resultEl.style.display = state.localTestResult ? 'block' : 'none';
            resultEl.dataset.tone = state.localTestTone || 'muted';
            resultEl.textContent = state.localTestResult || '';
        }
    }

    function setLocalTestResult(text, tone = 'muted') {
        state.localTestResult = text || '';
        state.localTestTone = tone;
        renderLocalStatus();
    }

    async function updateLocalStatus() {
        if (!LOCAL_RUNTIME_CONTROLS) return;
        try {
            const data = await apiRequest('/api/local-model/status', { cache: 'no-store' });
            const isReady = data.status === 'ready';
            let text = 'Status: ' + ((data.status || 'offline').charAt(0).toUpperCase() + (data.status || 'offline').slice(1));
            if (data.status === 'ready' && data.context_length) text += ` (ctx: ${data.context_length})`;
            if (data.status === 'downloading' && data.download_progress) text += ` ${Math.round(data.download_progress * 100)}%`;
            if (data.error) text += ` - ${data.error}`;
            state.localRuntimeReady = isReady;
            state.localStatusText = text;
            state.localStatusTone = isReady ? 'ok' : (data.status === 'error' ? 'error' : 'muted');
            renderLocalStatus();
        } catch (error) {
            state.localRuntimeReady = false;
            state.localStatusText = `Status: Error - ${error.message}`;
            state.localStatusTone = 'error';
            renderLocalStatus();
        }
    }

    function readLocalModelBody() {
        return {
            source: trim(state.localSource),
            filename: trim(state.localFilename),
            port: 8766,
            n_gpu_layers: parseInt(state.localGpuLayers, 10),
            n_ctx: parseInt(state.localContextLength, 10) || 16384,
            chat_format: trim(state.localChatFormat),
        };
    }

    function startLocalStatusPolling() {
        if (!LOCAL_RUNTIME_CONTROLS || localStatusPollStarted) return;
        localStatusPollStarted = true;
        updateLocalStatus();
        setInterval(updateLocalStatus, 3000);
    }

    function renderLocalControls() {
        if (!LOCAL_RUNTIME_CONTROLS) return '';
        return `
            <div class="wizard-runtime-strip">
                <button type="button" class="btn ghost" id="wizard-local-start">Start local runtime</button>
                <button type="button" class="btn ghost" id="wizard-local-stop" disabled>Stop</button>
                <button type="button" class="btn ghost" id="wizard-local-test" disabled>Test tool calling</button>
                <span id="wizard-local-status" class="wizard-runtime-status">Status: Offline</span>
            </div>
            <div id="wizard-local-test-result" class="wizard-test-result"></div>
        `;
    }

    function renderClaudeCliControls() {
        return `
            <div class="panel-card" id="wizard-claude-card" style="${shouldShowClaudeCliCta() ? '' : 'display:none;'}">
                <h3>Claude Runtime</h3>
                <p>Claude runtime powers delegated code editing and advisory review. It is managed automatically by the app.</p>
                <div class="wizard-runtime-strip">
                    <button type="button" class="btn ghost" id="wizard-claude-install" ${state.claudeCliBusy || state.claudeCliInstalled ? 'disabled' : ''}>
                        ${escapeHtml(state.claudeCliBusy ? 'Repairing...' : (state.claudeCliInstalled ? 'Runtime OK' : 'Repair Runtime'))}
                    </button>
                    <button type="button" class="btn secondary" id="wizard-claude-skip" ${state.claudeCliBusy || state.claudeCliInstalled ? 'hidden' : ''}>Skip for now</button>
                    <span id="wizard-claude-status" class="wizard-runtime-status" data-tone="${escapeHtml(state.claudeCliTone || 'muted')}">${escapeHtml(state.claudeCliStatusText || 'Checking Claude runtime...')}</span>
                </div>
            </div>
        `;
    }

    function summaryRows() {
        const rows = [
            ['Detected setup', profileLabel(activeProviderProfile())],
            ['Review mode', reviewLabel(state.reviewEnforcement)],
            ['Runtime mode', runtimeModeLabel(state.runtimeMode)],
            ['Total budget', formatUsd(state.totalBudget)],
            ['Per-task soft threshold', formatUsd(state.perTaskCostUsd)],
            ['Main', trim(state.mainModel)],
            ['Code', trim(state.codeModel)],
            ['Light', trim(state.lightModel)],
            ['Fallback', trim(state.fallbackModel)],
        ];
        if (trim(state.openrouterKey)) rows.splice(1, 0, ['OpenRouter', 'configured']);
        if (trim(state.openaiKey)) rows.splice(1, 0, ['OpenAI', 'configured']);
        if (trim(state.cloudruKey)) rows.splice(1, 0, ['Cloud.ru', 'configured']);
        if (trim(state.anthropicKey)) rows.splice(1, 0, ['Anthropic', 'configured']);
        if (hasLocalModel()) {
            rows.splice(
                1,
                0,
                ['Local source', trim(state.localSource) + (trim(state.localFilename) ? ` / ${trim(state.localFilename)}` : '')],
                ['Local routing', localRoutingLabel(state.localRoutingMode)],
            );
        }
        if (trim(state.skillsRepoPath)) {
            rows.push(['Skills repo', trim(state.skillsRepoPath)]);
        }
        return rows;
    }

    function renderProvidersStep() {
        const selectedProfile = activeProviderProfile();
        const localPreset = trim(state.localPreset);
        const localSourceOpen = state.localSourceOpen || hasLocalModel();
        return `
            <div class="step-header">
                <div>
                    <h2 class="step-title">${escapeHtml(STEP_META.providers.title)}</h2>
                    <p class="step-copy">${escapeHtml(STEP_META.providers.copy)}</p>
                </div>
            </div>
            <div class="panel-card">
                <h3>Keys first, routing second</h3>
                <p>${escapeHtml(
                    trim(state.openrouterKey)
                        ? 'OpenRouter is present, so the next step keeps router-style defaults while still saving any extra direct keys you paste here.'
                        : selectedProfile === 'direct-multi'
                            ? 'Multiple direct providers are present, so the next step keeps your model values editable without forcing one provider family.'
                            : selectedProfile === 'openai'
                                ? 'OpenAI is present, so the next step prefills direct openai:: model values.'
                                : selectedProfile === 'cloudru'
                                    ? 'Cloud.ru is present, so the next step prefills direct cloudru:: model values.'
                                : selectedProfile === 'anthropic'
                                    ? 'Anthropic is present, so the next step prefills direct anthropic:: model values.'
                                    : 'No remote key is present yet, so local-only setup remains available below.'
                )}</p>
            </div>
            <div class="field-grid">
                <div class="field">
                    <div class="field-label-row">
                        <label for="openrouter-key">OpenRouter API Key</label>
                        <button class="field-clear" data-clear="openrouter-key" type="button">Clear</button>
                    </div>
                    <input id="openrouter-key" type="password" placeholder="sk-or-v1-..." value="${escapeHtml(state.openrouterKey)}">
                    <div class="field-note">Optional. Best when you want one router for OpenAI, Anthropic, Google, and more.</div>
                </div>
                <div class="field">
                    <div class="field-label-row">
                        <label for="openai-key">OpenAI API Key</label>
                        <button class="field-clear" data-clear="openai-key" type="button">Clear</button>
                    </div>
                    <input id="openai-key" type="password" placeholder="sk-..." value="${escapeHtml(state.openaiKey)}">
                    <div class="field-note">Optional. If this is the only remote key, the next step prefills direct <code>openai::...</code> models.</div>
                </div>
                <div class="field">
                    <div class="field-label-row">
                        <label for="cloudru-key">Cloud.ru Foundation Models API Key</label>
                        <button class="field-clear" data-clear="cloudru-key" type="button">Clear</button>
                    </div>
                    <input id="cloudru-key" type="password" placeholder="Cloud.ru API key" value="${escapeHtml(state.cloudruKey)}">
                    <div class="field-note">Optional. If this is the only remote key, the next step prefills direct <code>cloudru::...</code> models.</div>
                </div>
                <div class="field">
                    <div class="field-label-row">
                        <label for="anthropic-key">Anthropic API Key</label>
                        <button class="field-clear" data-clear="anthropic-key" type="button">Clear</button>
                    </div>
                    <input id="anthropic-key" type="password" placeholder="sk-ant-..." value="${escapeHtml(state.anthropicKey)}">
                    <div class="field-note">Optional. Saved for direct <code>anthropic::...</code> models and Claude tooling.</div>
                </div>
            </div>
            ${renderClaudeCliControls()}
            <details class="wizard-collapse" ${localSourceOpen ? 'open' : ''}>
                <summary>
                    <span>Local model settings</span>
                    <span class="selection-badge">${hasLocalModel() ? 'Configured' : 'Optional'}</span>
                </summary>
                <div class="wizard-collapse-body">
                    <div class="field-grid">
                        <div class="field">
                            <div class="field-label-row">
                                <label for="local-preset">Preset</label>
                                <button class="field-clear" data-clear="local-preset" type="button">Clear</button>
                            </div>
                            <select id="local-preset">
                                <option value="" ${localPreset === '' ? 'selected' : ''}>None</option>
                                <option value="qwen25-7b" ${localPreset === 'qwen25-7b' ? 'selected' : ''}>Qwen2.5-7B Instruct Q3_K_M</option>
                                <option value="qwen3-14b" ${localPreset === 'qwen3-14b' ? 'selected' : ''}>Qwen3-14B Instruct Q4_K_M</option>
                                <option value="qwen3-32b" ${localPreset === 'qwen3-32b' ? 'selected' : ''}>Qwen3-32B Instruct Q4_K_M</option>
                                <option value="custom" ${localPreset === 'custom' ? 'selected' : ''}>Custom source</option>
                            </select>
                            <div class="field-note">Most people can ignore this. Open it only if you want local GGUF routing.</div>
                        </div>
                        <div class="field">
                            <div class="field-label-row"><label>Local routing</label></div>
                            <div class="selection-row">
                                <button class="selection-pill ${state.localRoutingMode === 'cloud' ? 'active' : ''}" data-local-mode="cloud" type="button">Cloud only</button>
                                <button class="selection-pill ${state.localRoutingMode === 'fallback' ? 'active' : ''}" data-local-mode="fallback" type="button">Fallback local</button>
                                <button class="selection-pill ${state.localRoutingMode === 'all' ? 'active' : ''}" data-local-mode="all" type="button">All models local</button>
                            </div>
                            <div class="field-note">Ignored unless a local model source is configured below.</div>
                        </div>
                        <div class="field field-full">
                            <div class="field-label-row">
                                <label for="local-source">Model Source</label>
                                <button class="field-clear" data-clear="local-source" type="button">Clear</button>
                            </div>
                            <input id="local-source" placeholder="Qwen/Qwen2.5-7B-Instruct-GGUF or /absolute/path/model.gguf" value="${escapeHtml(state.localSource)}">
                            <div class="field-note">Use either a HuggingFace repo ID or a local absolute GGUF path.</div>
                        </div>
                        <div class="field field-full">
                            <div class="field-label-row">
                                <label for="local-filename">GGUF Filename</label>
                                <button class="field-clear" data-clear="local-filename" type="button">Clear</button>
                            </div>
                            <input id="local-filename" placeholder="qwen2.5-7b-instruct-q3_k_m.gguf" value="${escapeHtml(state.localFilename)}">
                            <div class="field-note">Required only for HuggingFace repo IDs. Leave empty when the source is a direct filesystem path.</div>
                        </div>
                        <div class="field">
                            <label for="local-context">Context Length</label>
                            <input id="local-context" type="number" min="2048" step="1024" value="${escapeHtml(state.localContextLength)}">
                        </div>
                        <div class="field">
                            <label for="local-gpu-layers">GPU Layers</label>
                            <input id="local-gpu-layers" type="number" step="1" value="${escapeHtml(state.localGpuLayers)}">
                        </div>
                        <div class="field field-full">
                            <div class="field-label-row">
                                <label for="local-chat-format">Chat Format</label>
                                <button class="field-clear" data-clear="local-chat-format" type="button">Clear</button>
                            </div>
                            <input id="local-chat-format" placeholder="Leave empty for auto-detect" value="${escapeHtml(state.localChatFormat)}">
                        </div>
                    </div>
                    ${renderLocalControls()}
                </div>
            </details>
        `;
    }

    function modelSuggestionField({ id, label, value, note }) {
        return `
            <div class="field wizard-model-field" data-wizard-model-field>
                <label for="${id}">${label}</label>
                <input id="${id}" value="${escapeHtml(value)}" autocomplete="off" spellcheck="false" data-wizard-model-input>
                <div class="wizard-model-suggestions" hidden></div>
                <div class="field-note">${note}</div>
            </div>
        `;
    }

    function renderModelsStep() {
        return `
            <div class="step-header">
                <div>
                    <h2 class="step-title">${escapeHtml(STEP_META.models.title)}</h2>
                    <p class="step-copy">${escapeHtml(STEP_META.models.copy)}</p>
                </div>
            </div>
            <div class="panel-card">
                <h3>Current profile</h3>
                <p>${escapeHtml(
                    activeProviderProfile() === 'openai'
                        ? 'OpenAI-only setup detected. These defaults are explicit and official.'
                        : activeProviderProfile() === 'cloudru'
                            ? 'Cloud.ru-only setup detected. These defaults use explicit cloudru:: model IDs.'
                        : activeProviderProfile() === 'anthropic'
                            ? 'Anthropic-only setup detected. These defaults are explicit and official.'
                        : activeProviderProfile() === 'direct-multi'
                                ? 'Multiple direct providers are configured. Start here, then split model slots across them if you want.'
                                : activeProviderProfile() === 'local'
                                    ? 'Local-only setup detected. Review the model values and local routing before launch.'
                                    : 'OpenRouter-style routing remains active. Unprefixed provider IDs like openai/gpt-5.5 or anthropic/claude-sonnet-4.6 continue to route through OpenRouter.'
                )}</p>
            </div>
            <div class="grid two">
                ${modelSuggestionField({ id: 'main-model', label: 'Main Model', value: state.mainModel, note: 'Primary reasoning and long-form work.' })}
                ${modelSuggestionField({ id: 'code-model', label: 'Code Model', value: state.codeModel, note: 'Tool-heavy coding and edits.' })}
                ${modelSuggestionField({ id: 'light-model', label: 'Light Model', value: state.lightModel, note: 'Fast summaries and lightweight tasks.' })}
                ${modelSuggestionField({ id: 'fallback-model', label: 'Fallback Model', value: state.fallbackModel, note: 'Fallback and resilience path.' })}
            </div>
            <div class="wizard-inline-note">Direct providers use <code>openai::gpt-5.5</code>, <code>cloudru::zai-org/GLM-4.7</code>, and <code>anthropic::claude-sonnet-4-6</code>. Plain <code>openai/...</code> or <code>anthropic/...</code> stays router-style by design.</div>
        `;
    }

    function renderReviewModeStep() {
        const runtimeMode = trim(state.runtimeMode) || 'advanced';
        const runtimeModeDisabled = HOST_MODE !== 'desktop';
        const runtimeModeCopy = runtimeModeDisabled
            ? 'Runtime mode is owner-controlled in web/Docker onboarding and cannot be saved through /api/settings. Use the desktop launcher or edit settings.json while stopped.'
            : 'Separate axis from review enforcement. This first-run choice becomes the boot baseline before Ouroboros starts; later elevation requires native launcher confirmation.';
        const disabledAttr = runtimeModeDisabled ? ' disabled aria-disabled="true"' : '';
        return `
            <div class="step-header">
                <div>
                    <h2 class="step-title">${escapeHtml(STEP_META.review_mode.title)}</h2>
                    <p class="step-copy">${escapeHtml(STEP_META.review_mode.copy)}</p>
                </div>
            </div>
            <div class="wizard-choice-grid">
                <button type="button" class="wizard-choice advisory ${state.reviewEnforcement === 'advisory' ? 'active' : ''}" data-review-mode="advisory">
                    <span class="tone">Flexible</span>
                    <h3>Advisory</h3>
                    <p>Faster and cheaper. Review still runs, but you decide how to handle findings. Best when you want iteration speed and can manually watch for drift.</p>
                </button>
                <button type="button" class="wizard-choice blocking ${state.reviewEnforcement === 'blocking' ? 'active' : ''}" data-review-mode="blocking">
                    <span class="tone">Strict</span>
                    <h3>Blocking</h3>
                    <p>Slower and more expensive, but much safer. Critical review findings stop commits, which dramatically reduces the chance of gradual code degradation.</p>
                </button>
            </div>
            <div class="panel-card runtime-mode-card">
                <h3>Runtime mode</h3>
                <p class="field-note">${escapeHtml(runtimeModeCopy)}</p>
                <div class="wizard-choice-grid three">
                    <button type="button" class="wizard-choice light ${runtimeMode === 'light' ? 'active' : ''}" data-runtime-mode="light"${disabledAttr}>
                        <span class="tone">Safest</span>
                        <h3>Light</h3>
                        <p>Self-modification of the main repo is disabled. Best for trying Ouroboros out or running it as a pure assistant.</p>
                    </button>
                    <button type="button" class="wizard-choice advanced ${runtimeMode === 'advanced' ? 'active' : ''}" data-runtime-mode="advanced"${disabledAttr}>
                        <span class="tone">Default</span>
                        <h3>Advanced</h3>
                        <p>Self-modification of the evolutionary layer is allowed (current behaviour). Protected core/contract/release files stay guarded by Advanced mode.</p>
                    </button>
                    <button type="button" class="wizard-choice pro ${runtimeMode === 'pro' ? 'active' : ''}" data-runtime-mode="pro"${disabledAttr}>
                        <span class="tone">Power</span>
                        <h3>Pro</h3>
                        <p>Direct protected-surface mode. Protected core/contract/release edits are allowed on disk, but commits still require the normal triad + scope review gate.</p>
                    </button>
                </div>
                <div class="field">
                    <div class="field-label-row">
                        <label for="skills-repo-path">External skills repo (optional)</label>
                        <button class="field-clear" data-clear="skills-repo-path" type="button">Clear</button>
                    </div>
                    <input id="skills-repo-path" type="text" placeholder="~/Ouroboros/skills or /absolute/path/to/skills" value="${escapeHtml(state.skillsRepoPath || '')}">
                    <div class="field-note">Optional. Extra discovery root on top of the in-data-plane <code>data/skills/{native,clawhub,external}/</code> tree. Leave empty if you do not maintain your own skills checkout — Ouroboros never clones/pulls this directory.</div>
                </div>
            </div>
        `;
    }

    function renderBudgetStep() {
        return `
            <div class="step-header">
                <div>
                    <h2 class="step-title">${escapeHtml(STEP_META.budget.title)}</h2>
                    <p class="step-copy">${escapeHtml(STEP_META.budget.copy)}</p>
                </div>
            </div>
            <div class="grid two">
                <div class="panel-card">
                    <h3>Total budget</h3>
                    <div class="field">
                        <label for="total-budget">Total Budget (USD)</label>
                        <input id="total-budget" type="number" min="1" step="1" value="${escapeHtml(state.totalBudget)}">
                        <div class="field-note">Global spend budget across the runtime. Keep this editable even after onboarding.</div>
                    </div>
                </div>
                <div class="panel-card">
                    <h3>Per-task soft threshold</h3>
                    <div class="field">
                        <label for="per-task-budget">Per-task Cost Cap (USD)</label>
                        <input id="per-task-budget" type="number" min="1" step="1" value="${escapeHtml(state.perTaskCostUsd)}">
                        <div class="field-note">This does not hard-stop the task. It injects a budget reminder when one task starts getting expensive.</div>
                    </div>
                </div>
            </div>
        `;
    }

    function renderSummaryStep() {
        const summary = summaryRows().map(([label, value]) => `
            <div class="summary-kv">
                <strong>${escapeHtml(label)}</strong>
                <span>${escapeHtml(value)}</span>
            </div>
        `).join('');
        return `
            <div class="step-header">
                <div>
                    <h2 class="step-title">${escapeHtml(STEP_META.summary.title)}</h2>
                    <p class="step-copy">${escapeHtml(STEP_META.summary.copy)}</p>
                </div>
            </div>
            <div class="summary-card">${summary}</div>
        `;
    }

    function renderStepContent() {
        if (state.currentStep === 'providers') return renderProvidersStep();
        if (state.currentStep === 'models') return renderModelsStep();
        if (state.currentStep === 'review_mode') return renderReviewModeStep();
        if (state.currentStep === 'budget') return renderBudgetStep();
        return renderSummaryStep();
    }

    function stepCards() {
        return STEP_ORDER.map((stepId, index) => {
            const active = stepId === state.currentStep;
            const done = STEP_ORDER.indexOf(state.currentStep) > index;
            const meta = STEP_META[stepId];
            return `
                <div class="wizard-step ${active ? 'active' : ''} ${done ? 'done' : ''}">
                    <div class="wizard-step-index">Step ${index + 1}</div>
                    <p class="wizard-step-title">${escapeHtml(meta.title)}</p>
                    <p class="wizard-step-copy">${escapeHtml(meta.railCopy || '')}</p>
                </div>
            `;
        }).join('');
    }

    function render() {
        const meta = STEP_META[state.currentStep];
        const index = STEP_ORDER.indexOf(state.currentStep);
        const nextLabel = state.currentStep === 'summary'
            ? (state.saving ? 'Saving...' : 'Start Ouroboros')
            : 'Continue';
        root.innerHTML = `
            <div class="wizard-shell">
                <div class="wizard-header">
                    <div>
                        <h1 class="wizard-title">Ouroboros</h1>
                        <p class="wizard-subtitle">Shared desktop and web onboarding with the same model, review, and budget flow in both hosts.</p>
                    </div>
                    <div class="wizard-badge">Step ${index + 1} of ${STEP_ORDER.length}</div>
                </div>
                <div class="wizard-steps">${stepCards()}</div>
                <div class="wizard-content">
                    ${renderStepContent()}
                    <div class="wizard-footer">
                        <div class="footer-copy">${escapeHtml(meta.footer)}</div>
                        <div class="footer-actions">
                            <button class="btn secondary" id="back-btn" type="button" ${index === 0 || state.saving ? 'disabled' : ''}>Back</button>
                            <button class="btn primary" id="next-btn" type="button" ${nextButtonShouldBeDisabled() ? 'disabled' : ''}>${escapeHtml(nextLabel)}</button>
                        </div>
                    </div>
                    <div class="wizard-error">${escapeHtml(state.error)}</div>
                </div>
            </div>
        `;
        bindEvents();
        renderLocalStatus();
        renderClaudeCliStatus();
    }

    function bindClearButtons() {
        root.querySelectorAll('[data-clear]').forEach((button) => {
            button.addEventListener('click', () => {
                const target = button.getAttribute('data-clear');
                if (target === 'openrouter-key') state.openrouterKey = '';
                if (target === 'openai-key') state.openaiKey = '';
                if (target === 'cloudru-key') state.cloudruKey = '';
                if (target === 'anthropic-key') state.anthropicKey = '';
                if (target === 'local-preset') {
                    state.localPreset = '';
                    state.localSource = '';
                    state.localFilename = '';
                    state.localRoutingMode = 'cloud';
                    state.localSourceOpen = false;
                }
                if (target === 'local-source') {
                    state.localSource = '';
                    state.localPreset = detectLocalPresetSelection();
                }
                if (target === 'local-filename') {
                    state.localFilename = '';
                    state.localPreset = detectLocalPresetSelection();
                }
                if (target === 'local-chat-format') state.localChatFormat = '';
                if (target === 'skills-repo-path') state.skillsRepoPath = '';
                state.error = '';
                render();
            });
        });
    }

    function bindProvidersStep() {
        const details = root.querySelector('.wizard-collapse');
        if (details) {
            details.addEventListener('toggle', () => {
                state.localSourceOpen = details.open;
            });
        }
        const openrouterInput = document.getElementById('openrouter-key');
        const openaiInput = document.getElementById('openai-key');
        const cloudruInput = document.getElementById('cloudru-key');
        const anthropicInput = document.getElementById('anthropic-key');
        const localPreset = document.getElementById('local-preset');
        const localSource = document.getElementById('local-source');
        const localFilename = document.getElementById('local-filename');
        const localContext = document.getElementById('local-context');
        const localGpuLayers = document.getElementById('local-gpu-layers');
        const localChatFormat = document.getElementById('local-chat-format');

        if (openrouterInput) openrouterInput.addEventListener('input', () => { state.openrouterKey = openrouterInput.value; state.error = ''; syncCurrentStepActionState(); });
        if (openaiInput) openaiInput.addEventListener('input', () => { state.openaiKey = openaiInput.value; state.error = ''; syncCurrentStepActionState(); });
        if (cloudruInput) cloudruInput.addEventListener('input', () => { state.cloudruKey = cloudruInput.value; state.error = ''; syncCurrentStepActionState(); });
        if (anthropicInput) anthropicInput.addEventListener('input', () => {
            const wasConfigured = hasAnthropicKeyConfigured();
            state.anthropicKey = anthropicInput.value;
            if (!wasConfigured && hasAnthropicKeyConfigured()) {
                state.claudeCliDismissed = false;
                startClaudeCliStatusPolling();
                updateClaudeCliStatus();
            }
            state.error = '';
            syncClaudeCliVisibility();
            syncCurrentStepActionState();
        });
        if (localPreset) localPreset.addEventListener('change', () => { applyPresetSelection(localPreset.value); state.error = ''; render(); });
        if (localSource) localSource.addEventListener('input', () => {
            state.localSource = localSource.value;
            state.localPreset = detectLocalPresetSelection();
            if (localPreset) localPreset.value = state.localPreset || '';
            state.localSourceOpen = true;
            if (trim(state.localSource) && activeProviderProfile() === 'local' && trim(state.localRoutingMode) === 'cloud') {
                state.localRoutingMode = 'all';
            }
            state.error = '';
            syncCurrentStepActionState();
        });
        if (localFilename) localFilename.addEventListener('input', () => {
            state.localFilename = localFilename.value;
            state.localPreset = detectLocalPresetSelection();
            if (localPreset) localPreset.value = state.localPreset || '';
            state.error = '';
            syncCurrentStepActionState();
        });
        if (localContext) localContext.addEventListener('input', () => { state.localContextLength = localContext.value; state.error = ''; syncCurrentStepActionState(); });
        if (localGpuLayers) localGpuLayers.addEventListener('input', () => { state.localGpuLayers = localGpuLayers.value; state.error = ''; syncCurrentStepActionState(); });
        if (localChatFormat) localChatFormat.addEventListener('input', () => { state.localChatFormat = localChatFormat.value; state.error = ''; syncCurrentStepActionState(); });
        root.querySelectorAll('[data-local-mode]').forEach((button) => {
            button.addEventListener('click', () => {
                state.localRoutingMode = button.getAttribute('data-local-mode');
                state.error = '';
                render();
            });
        });
        if (LOCAL_RUNTIME_CONTROLS) {
            startLocalStatusPolling();
            document.getElementById('wizard-local-start')?.addEventListener('click', async () => {
                const body = readLocalModelBody();
                if (!body.source) {
                    state.error = 'Enter a local model source before starting the local runtime.';
                    render();
                    return;
                }
                setLocalTestResult('', 'muted');
                try {
                    const resp = await fetch('/api/local-model/start', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(body),
                    });
                    const data = await resp.json().catch(() => ({}));
                    if (resp.status === 412 && data.error === 'runtime_missing') {
                        // llama-cpp-python not installed — show actionable message
                        setLocalTestResult(
                            'Local runtime (llama-cpp-python) is not installed.\n' +
                            'Go to Settings → Advanced → Local Model Runtime\n' +
                            'and click "Install Local Runtime".\n\n' +
                            'Manual: ' + (data.hint || 'pip install llama-cpp-python[server]'),
                            'error'
                        );
                    } else if (data.error) {
                        setLocalTestResult(`Start failed: ${data.error}`, 'error');
                    } else {
                        updateLocalStatus();
                    }
                } catch (error) {
                    setLocalTestResult(`Start failed: ${error.message}`, 'error');
                }
            });
            document.getElementById('wizard-local-stop')?.addEventListener('click', async () => {
                try {
                    await apiRequest('/api/local-model/stop', { method: 'POST' });
                    updateLocalStatus();
                } catch (error) {
                    setLocalTestResult(`Stop failed: ${error.message}`, 'error');
                }
            });
            document.getElementById('wizard-local-test')?.addEventListener('click', async () => {
                setLocalTestResult('Running tests...', 'muted');
                try {
                    const result = await apiRequest('/api/local-model/test', { method: 'POST' });
                    const lines = [];
                    lines.push(`${result.chat_ok ? '✓' : '✗'} Basic chat${result.tokens_per_sec ? ` (${result.tokens_per_sec} tok/s)` : ''}`);
                    lines.push(`${result.tool_call_ok ? '✓' : '✗'} Tool calling`);
                    if (result.details && !result.success) lines.push(result.details);
                    setLocalTestResult(lines.join('\n'), result.success ? 'ok' : 'warn');
                } catch (error) {
                    setLocalTestResult(`Test failed: ${error.message}`, 'error');
                }
            });
        }
        document.getElementById('wizard-claude-install')?.addEventListener('click', async () => {
            state.claudeCliBusy = true;
            state.claudeCliTone = 'muted';
            state.claudeCliStatusText = 'Repairing Claude runtime...';
            renderClaudeCliStatus();
            try {
                applyClaudeCliStatus(await claudeCliStartInstall());
                if (state.claudeCliBusy) updateClaudeCliStatus();
            } catch (error) {
                state.claudeCliBusy = false;
                state.claudeCliStatus = 'error';
                state.claudeCliError = String(error?.message || error || '');
                state.claudeCliTone = 'error';
                state.claudeCliStatusText = `Claude runtime repair failed: ${state.claudeCliError}`;
                renderClaudeCliStatus();
            }
        });
        document.getElementById('wizard-claude-skip')?.addEventListener('click', () => {
            state.claudeCliDismissed = true;
            syncClaudeCliVisibility();
        });
        if (shouldShowClaudeCliCta()) {
            startClaudeCliStatusPolling();
            updateClaudeCliStatus();
        } else {
            renderClaudeCliStatus();
        }
        syncCurrentStepActionState();
    }

    function bindModelsStep() {
        const map = {
            'main-model': 'mainModel',
            'code-model': 'codeModel',
            'light-model': 'lightModel',
            'fallback-model': 'fallbackModel',
        };
        function suggestionMatches(query) {
            const needle = trim(query).toLowerCase();
            const source = MODEL_SUGGESTIONS.length ? MODEL_SUGGESTIONS : [
                'openai::gpt-5.5',
                'openai::gpt-5.5-mini',
                'anthropic::claude-opus-4-6',
                'anthropic::claude-sonnet-4-6',
                'openai/gpt-5.5',
            ];
            return source
                .filter((model) => !needle || String(model).toLowerCase().includes(needle))
                .slice(0, 8);
        }
        function closeSuggestions(exceptInput = null) {
            root.querySelectorAll('.wizard-model-suggestions').forEach((panel) => {
                if (exceptInput && panel.parentElement?.querySelector('input') === exceptInput) return;
                panel.hidden = true;
                panel.innerHTML = '';
            });
        }
        function renderSuggestions(input) {
            const panel = input.closest('[data-wizard-model-field]')?.querySelector('.wizard-model-suggestions');
            if (!panel) return;
            const matches = suggestionMatches(input.value);
            if (!matches.length) {
                panel.hidden = true;
                panel.innerHTML = '';
                return;
            }
            panel.innerHTML = matches.map((model) => (
                `<button type="button" class="wizard-model-suggestion" data-value="${escapeHtml(model)}">${escapeHtml(model)}</button>`
            )).join('');
            panel.hidden = false;
        }
        Object.entries(map).forEach(([id, key]) => {
            const input = document.getElementById(id);
            if (!input) return;
            input.addEventListener('focus', () => {
                closeSuggestions(input);
                renderSuggestions(input);
            });
            input.addEventListener('input', () => {
                state[key] = input.value;
                state.modelsDirty = true;
                state.error = '';
                closeSuggestions(input);
                renderSuggestions(input);
                syncCurrentStepActionState();
            });
        });
        root.querySelectorAll('.wizard-model-suggestions').forEach((panel) => {
            panel.addEventListener('mousedown', (event) => {
                const button = event.target.closest('.wizard-model-suggestion');
                if (!button) return;
                event.preventDefault();
                const input = panel.parentElement?.querySelector('input');
                if (!input) return;
                input.value = button.dataset.value || '';
                input.dispatchEvent(new Event('input', { bubbles: true }));
                closeSuggestions();
            });
        });
        if (root.dataset.modelSuggestionOutsideListener !== '1') {
            root.dataset.modelSuggestionOutsideListener = '1';
            document.addEventListener('mousedown', (event) => {
                if (!root.contains(event.target) || !event.target.closest('[data-wizard-model-field]')) {
                    root.querySelectorAll('.wizard-model-suggestions').forEach((panel) => {
                        panel.hidden = true;
                        panel.innerHTML = '';
                    });
                }
            });
        }
        syncCurrentStepActionState();
    }

    function bindReviewModeStep() {
        root.querySelectorAll('[data-review-mode]').forEach((button) => {
            button.addEventListener('click', () => {
                state.reviewEnforcement = button.getAttribute('data-review-mode');
                state.error = '';
                render();
            });
        });
        root.querySelectorAll('[data-runtime-mode]').forEach((button) => {
            button.addEventListener('click', () => {
                state.runtimeMode = button.getAttribute('data-runtime-mode');
                state.error = '';
                render();
            });
        });
        const skillsInput = document.getElementById('skills-repo-path');
        if (skillsInput) {
            skillsInput.addEventListener('input', () => {
                state.skillsRepoPath = skillsInput.value;
                syncCurrentStepActionState();
            });
        }
        syncCurrentStepActionState();
    }

    function bindBudgetStep() {
        const totalBudget = document.getElementById('total-budget');
        const perTaskBudget = document.getElementById('per-task-budget');
        if (totalBudget) totalBudget.addEventListener('input', () => { state.totalBudget = totalBudget.value; state.error = ''; syncCurrentStepActionState(); });
        if (perTaskBudget) perTaskBudget.addEventListener('input', () => { state.perTaskCostUsd = perTaskBudget.value; state.error = ''; syncCurrentStepActionState(); });
        syncCurrentStepActionState();
    }

    async function saveWizardPayload(payload) {
        if (HOST_MODE === 'web') {
            await apiRequest('/api/settings', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
            });
            window.parent?.postMessage({ type: 'ouroboros:onboarding-complete' }, '*');
            if (!window.parent || window.parent === window) {
                window.location.replace('/');
            }
            return 'ok';
        }
        if (!window.pywebview?.api?.save_wizard) {
            throw new Error('Desktop onboarding bridge is unavailable.');
        }
        const result = await window.pywebview.api.save_wizard(payload);
        if (result !== 'ok') throw new Error(result || 'Failed to save onboarding settings.');
        return result;
    }

    async function saveWizard() {
        const providersError = validateProvidersStep();
        const modelsError = validateModelsStep();
        const reviewError = validateReviewStep();
        const budgetError = validateBudgetStep();
        state.error = providersError || modelsError || reviewError || budgetError;
        if (state.error) {
            render();
            return;
        }
        state.saving = true;
        state.error = '';
        render();
        const payload = {
            OPENROUTER_API_KEY: trim(state.openrouterKey),
            OPENAI_API_KEY: trim(state.openaiKey),
            CLOUDRU_FOUNDATION_MODELS_API_KEY: trim(state.cloudruKey),
            ANTHROPIC_API_KEY: trim(state.anthropicKey),
            TOTAL_BUDGET: Number(state.totalBudget || 0),
            OUROBOROS_PER_TASK_COST_USD: Number(state.perTaskCostUsd || 0),
            OUROBOROS_REVIEW_ENFORCEMENT: trim(state.reviewEnforcement) || 'advisory',
            OUROBOROS_SKILLS_REPO_PATH: trim(state.skillsRepoPath),
            LOCAL_MODEL_SOURCE: trim(state.localSource),
            LOCAL_MODEL_FILENAME: trim(state.localFilename),
            LOCAL_MODEL_CONTEXT_LENGTH: Number(state.localContextLength || 0),
            LOCAL_MODEL_N_GPU_LAYERS: Number(state.localGpuLayers || 0),
            LOCAL_MODEL_CHAT_FORMAT: trim(state.localChatFormat),
            LOCAL_ROUTING_MODE: trim(state.localSource) ? (trim(state.localRoutingMode) || 'cloud') : 'cloud',
            OUROBOROS_MODEL: trim(state.mainModel),
            OUROBOROS_MODEL_CODE: trim(state.codeModel),
            OUROBOROS_MODEL_LIGHT: trim(state.lightModel),
            OUROBOROS_MODEL_FALLBACK: trim(state.fallbackModel),
        };
        if (HOST_MODE === 'desktop') {
            payload.OUROBOROS_RUNTIME_MODE = trim(state.runtimeMode) || 'advanced';
        }
        try {
            await saveWizardPayload(payload);
        } catch (error) {
            state.saving = false;
            state.error = String(error?.message || error || 'Failed to save onboarding settings.');
            render();
        }
    }

    function bindEvents() {
        bindClearButtons();
        document.getElementById('back-btn')?.addEventListener('click', previousStep);
        document.getElementById('next-btn')?.addEventListener('click', () => {
            if (state.currentStep === 'summary') saveWizard();
            else nextStep();
        });
        if (state.currentStep === 'providers') bindProvidersStep();
        if (state.currentStep === 'models') bindModelsStep();
        if (state.currentStep === 'review_mode') bindReviewModeStep();
        if (state.currentStep === 'budget') bindBudgetStep();
        syncCurrentStepActionState();
    }

    applyModelDefaults(false);
    render();
})();
