(() => {
    // Self-contained IIFE mirror of utils.escapeHtmlAttr; SSOT drift is tested.
    function escapeHtml(value) {
        return String(value ?? '')
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;')
            .replace(/`/g, '&#96;');
    }

        const bootstrap = window.__OURO_ONBOARDING_BOOTSTRAP__ || {};
        const SETUP_CONTRACT = bootstrap.contract || {};
        const HOST_MODE = bootstrap.hostMode || 'desktop';
        const LOCAL_RUNTIME_CONTROLS = Boolean(bootstrap.supportsLocalRuntimeControls);
        const STEP_ORDER = bootstrap.stepOrder || (SETUP_CONTRACT.steps || []).map((step) => step.id);
        const STEP_META = Object.fromEntries((SETUP_CONTRACT.steps || []).map((step) => [step.id, step]));
        const PROVIDER_FIELDS = SETUP_CONTRACT.providerFields || [];
        const PROVIDER_PROFILES = SETUP_CONTRACT.providerProfiles || {};
        const MODEL_SLOTS = SETUP_CONTRACT.modelSlots || [];
        const REVIEW_MODES = SETUP_CONTRACT.reviewModes || [];
        const RUNTIME_MODES = SETUP_CONTRACT.runtimeModes || [];
        const LOCAL_ROUTING_MODES = SETUP_CONTRACT.localRoutingModes || [];
        const BUDGET_FIELDS = SETUP_CONTRACT.budgetFields || [];
        const LOCAL_FIELDS = [
            ['local-source', 'localSource', 'Model Source', 'Qwen/Qwen2.5-7B-Instruct-GGUF or /absolute/path/model.gguf', 'Use either a HuggingFace repo ID or a local absolute GGUF path.', 'field field-full'],
            ['local-filename', 'localFilename', 'GGUF Filename', 'qwen2.5-7b-instruct-q3_k_m.gguf', 'Required only for HuggingFace repo IDs. Leave empty when the source is a direct filesystem path.', 'field field-full'],
            ['local-context', 'localContextLength', 'Context Length', '', '', 'field', 'number', '2048', '1024'],
            ['local-gpu-layers', 'localGpuLayers', 'GPU Layers', '', '', 'field', 'number', '', '1'],
            ['local-chat-format', 'localChatFormat', 'Chat Format', 'Leave empty for auto-detect', '', 'field field-full'],
        ];
        const MODEL_DEFAULTS = bootstrap.modelDefaults || {};
        const LOCAL_PRESETS = bootstrap.localPresets || {};
        const MODEL_SUGGESTIONS = bootstrap.modelSuggestions || [];
        const INITIAL_STATE = bootstrap.initialState || {};
        const root = document.getElementById('root');

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

        function optionByValue(items, value) {
            return (items || []).find((item) => item.value === value) || {};
        }

        function detectProviderProfile() {
            const configured = Object.fromEntries(PROVIDER_FIELDS.map((field) => [
                field.settingKey,
                trim(state[field.stateKey]).length >= 10,
            ]));
            const hasOpenrouter = configured.OPENROUTER_API_KEY;
            const hasCompatible = trim(state.compatibleBaseUrl).length > 0;
            const direct = [
                ['OPENAI_API_KEY', 'openai'],
                ['CLOUDRU_FOUNDATION_MODELS_API_KEY', 'cloudru'],
                ['ANTHROPIC_API_KEY', 'anthropic'],
            ].filter(([settingKey]) => configured[settingKey]);
            if (hasOpenrouter) return 'openrouter';
            if (hasCompatible) return 'openai-compatible';
            if (direct.length > 1) return 'direct-multi';
            if (direct.length === 1) return direct[0][1];
            if (hasLocalModel()) return 'local';
            return 'openrouter';
        }

    function activeProviderProfile() {
        const profile = detectProviderProfile();
        state.providerProfile = profile;
        return profile;
    }

        function profileLabel(profile) {
            return PROVIDER_PROFILES[profile]?.label || PROVIDER_PROFILES.openrouter?.label || 'OpenRouter';
        }

        function reviewLabel(mode) {
            return optionByValue(REVIEW_MODES, mode).label || 'Advisory';
        }

        function runtimeModeLabel(mode) {
            return optionByValue(RUNTIME_MODES, mode).label || 'Advanced';
        }

        function localRoutingLabel(mode) {
            return optionByValue(LOCAL_ROUTING_MODES, mode).label || 'Cloud models only';
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

    function markStepEdited() {
        state.error = '';
        syncCurrentStepActionState();
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
            const keyValues = PROVIDER_FIELDS.map((field) => [field, trim(state[field.stateKey])]);
            const localSource = trim(state.localSource);
            const localFilename = trim(state.localFilename);
            const shortKey = keyValues.find(([field, value]) => value && (field.inputType || 'password') === 'password' && value.length < 10);
            if (shortKey) return `${shortKey[0].label.replace(' API Key', '')} API key looks too short.`;
            const hasRemote = keyValues.some(([field, value]) => value && field.settingKey !== 'OPENAI_COMPATIBLE_API_KEY');
            if (!hasRemote && !localSource) {
                return 'Enter at least one remote key or a local model source before continuing.';
            }
            if (localSource && !hasRemote && trim(state.localRoutingMode) === 'cloud') {
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
        for (const field of BUDGET_FIELDS) {
            const value = Number(state[field.stateKey]);
            const min = Number(field.min || 0.01);
            if (!Number.isFinite(value) || value < min) {
                return `${field.title || field.label || 'Budget'} must be greater than zero.`;
            }
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
        const status = trim(payload.status) || (ready ? 'ready' : (installed ? 'installed' : 'missing'));
        const pendingUnsavedAnthropicKey = status === 'no_api_key' && hasAnthropicKeyConfigured();
        const message = trim(payload.message)
            || (ready ? 'Claude runtime ready.' : (installed ? 'Claude runtime available but not ready.' : 'Claude runtime not available.'));
        state.claudeCliInstalled = installed || ready;
        state.claudeCliBusy = busy;
        state.claudeCliStatus = status;
        state.claudeCliError = pendingUnsavedAnthropicKey ? '' : errorText;
        state.claudeCliTone = ready ? 'ok' : (pendingUnsavedAnthropicKey ? 'muted' : (errorText ? 'error' : (installed ? 'muted' : 'error')));
        state.claudeCliStatusText = pendingUnsavedAnthropicKey
            ? 'Claude runtime will use the Anthropic key after setup is saved.'
            : message;
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
        if (card) card.hidden = !shouldShowClaudeCliCta();
        renderClaudeCliStatus();
    }

    function renderClaudeCliStatus() {
        const card = document.getElementById('wizard-claude-card');
        const statusEl = document.getElementById('wizard-claude-status');
        const installButton = document.getElementById('wizard-claude-install');
        const skipButton = document.getElementById('wizard-claude-skip');
        if (card) card.hidden = !shouldShowClaudeCliCta();
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
            resultEl.hidden = !state.localTestResult;
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
                <button type="button" class="btn btn-ghost" id="wizard-local-start">Start local runtime</button>
                <button type="button" class="btn btn-ghost" id="wizard-local-stop" disabled>Stop</button>
                <button type="button" class="btn btn-ghost" id="wizard-local-test" disabled>Test tool calling</button>
                <span id="wizard-local-status" class="wizard-runtime-status">Status: Offline</span>
            </div>
            <div id="wizard-local-test-result" class="wizard-test-result"></div>
        `;
    }

    function renderClaudeCliControls() {
        return `
            <div class="panel-card" id="wizard-claude-card"${shouldShowClaudeCliCta() ? '' : ' hidden'}>
                <h3>Claude Runtime</h3>
                <p>Claude runtime powers delegated code editing and advisory review. It is managed automatically by the app.</p>
                <div class="wizard-runtime-strip">
                    <button type="button" class="btn btn-ghost" id="wizard-claude-install" ${state.claudeCliBusy || state.claudeCliInstalled ? 'disabled' : ''}>
                        ${escapeHtml(state.claudeCliBusy ? 'Repairing...' : (state.claudeCliInstalled ? 'Runtime OK' : 'Repair Runtime'))}
                    </button>
                    <button type="button" class="btn btn-secondary" id="wizard-claude-skip" ${state.claudeCliBusy || state.claudeCliInstalled ? 'hidden' : ''}>Skip for now</button>
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

        function providerKeyField({ id, label, placeholder, value, note, inputType }) {
            const type = inputType || 'password';
            return `
                <div class="field">
                <div class="field-label-row">
                    <label for="${escapeHtml(id)}">${escapeHtml(label)}</label>
                    <button class="field-clear" data-clear="${escapeHtml(id)}" type="button">Clear</button>
                </div>
                <input id="${escapeHtml(id)}" type="${escapeHtml(type)}" placeholder="${escapeHtml(placeholder)}" value="${escapeHtml(value)}">
                <div class="field-note">${escapeHtml(note)}</div>
            </div>
            `;
        }

        function localInputField([id, stateKey, label, placeholder, note, className, type = 'text', min = '', step = '']) {
            const clear = ['local-source', 'local-filename', 'local-chat-format'].includes(id)
                ? `<button class="field-clear" data-clear="${id}" type="button">Clear</button>`
                : '';
            return `
                <div class="${className}">
                    <div class="field-label-row"><label for="${id}">${label}</label>${clear}</div>
                    <input id="${id}" type="${type}" ${min ? `min="${min}"` : ''} ${step ? `step="${step}"` : ''} placeholder="${placeholder}" value="${escapeHtml(state[stateKey])}">
                    ${note ? `<div class="field-note">${note}</div>` : ''}
                </div>
            `;
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
                    <p>${escapeHtml(PROVIDER_PROFILES[selectedProfile]?.providerCopy || '')}</p>
                </div>
                <div class="field-grid">
                    ${PROVIDER_FIELDS.map((field) => providerKeyField({
                        ...field,
                        value: state[field.stateKey],
                    })).join('')}
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
                                    ${Object.entries(LOCAL_PRESETS).map(([id, preset]) => `<option value="${escapeHtml(id)}" ${localPreset === id ? 'selected' : ''}>${escapeHtml(preset.label)}</option>`).join('')}
                                    <option value="custom" ${localPreset === 'custom' ? 'selected' : ''}>Custom source</option>
                                </select>
                            <div class="field-note">Most people can ignore this. Open it only if you want local GGUF routing.</div>
                        </div>
                        <div class="field">
                                <div class="field-label-row"><label>Local routing</label></div>
                                <div class="selection-row">
                                    ${LOCAL_ROUTING_MODES.map((mode) => `<button class="selection-pill ${state.localRoutingMode === mode.value ? 'active' : ''}" data-local-mode="${escapeHtml(mode.value)}" type="button">${escapeHtml(mode.buttonLabel || mode.label)}</button>`).join('')}
                                </div>
                                <div class="field-note">Ignored unless a local model source is configured below.</div>
                            </div>
                            ${LOCAL_FIELDS.map(localInputField).join('')}
                        </div>
                    ${renderLocalControls()}
                </div>
            </details>
        `;
    }

    function modelSuggestionField({ id, label, value, note }) {
        return `
            <div class="field wizard-model-field" data-wizard-model-field>
                <label for="${escapeHtml(id)}">${escapeHtml(label)}</label>
                <input id="${escapeHtml(id)}" value="${escapeHtml(value)}" autocomplete="off" spellcheck="false" data-wizard-model-input>
                <div class="wizard-model-suggestions" hidden></div>
                <div class="field-note">${escapeHtml(note)}</div>
            </div>
        `;
    }

        function renderCompatibleModelLoader() {
            return `
            <div class="panel-card" id="compatible-model-loader">
                <h3>Load models from endpoint</h3>
                <p class="field-note">Fetch the model list from your configured URL, then click a model to fill all empty slots.</p>
                <div class="compatible-model-actions">
                    <button type="button" class="btn btn-secondary" id="load-compatible-models">Load models</button>
                    <span id="compatible-load-status" class="field-note compatible-load-status"></span>
                </div>
                <div id="compatible-model-list" class="compatible-model-list" hidden></div>
            </div>
            `;
        }

        function renderModelsStep() {
            const profile = activeProviderProfile();
            return `
            <div class="step-header">
                <div>
                    <h2 class="step-title">${escapeHtml(STEP_META.models.title)}</h2>
                    <p class="step-copy">${escapeHtml(STEP_META.models.copy)}</p>
                </div>
            </div>
                <div class="panel-card">
                    <h3>Current profile</h3>
                    <p>${escapeHtml(PROVIDER_PROFILES[profile]?.modelCopy || '')}</p>
                </div>
                ${profile === 'openai-compatible' ? renderCompatibleModelLoader() : ''}
                <div class="grid two">
                    ${MODEL_SLOTS.map((slot) => modelSuggestionField({
                        id: slot.inputId,
                        label: slot.label,
                        value: state[slot.stateKey],
                        note: slot.note,
                    })).join('')}
                </div>
            <div class="wizard-inline-note">Direct providers use <code>openai::gpt-5.5</code>, <code>cloudru::zai-org/GLM-4.7</code>, and <code>anthropic::claude-sonnet-4-6</code>. OpenAI-compatible endpoints use <code>openai-compatible::your-model-name</code>. Plain <code>openai/...</code> or <code>anthropic/...</code> stays router-style by design.</div>
        `;
    }

    function renderReviewModeStep() {
        const runtimeMode = trim(state.runtimeMode) || 'advanced';
        const runtimeModeCopy = HOST_MODE === 'desktop'
            ? 'Separate axis from review enforcement. This first-run choice becomes the boot baseline before Ouroboros starts; later elevation requires native launcher confirmation.'
            : 'Separate axis from review enforcement. Web/Docker onboarding saves this through the owner endpoint; the selected mode becomes active after restart.';
        return `
            <div class="step-header">
                <div>
                    <h2 class="step-title">${escapeHtml(STEP_META.review_mode.title)}</h2>
                    <p class="step-copy">${escapeHtml(STEP_META.review_mode.copy)}</p>
                </div>
                </div>
                <div class="wizard-choice-grid">
                    ${REVIEW_MODES.map((mode) => `
                        <button type="button" class="wizard-choice ${escapeHtml(mode.className || mode.value)} ${state.reviewEnforcement === mode.value ? 'active' : ''}" data-review-mode="${escapeHtml(mode.value)}">
                            <span class="tone">${escapeHtml(mode.tone)}</span>
                            <h3>${escapeHtml(mode.label)}</h3>
                            <p>${escapeHtml(mode.copy)}</p>
                        </button>
                    `).join('')}
                </div>
            <div class="panel-card runtime-mode-card">
                <h3>Runtime mode</h3>
                    <p class="field-note">${escapeHtml(runtimeModeCopy)}</p>
                    <div class="wizard-choice-grid three">
                        ${RUNTIME_MODES.map((mode) => `
                            <button type="button" class="wizard-choice ${escapeHtml(mode.className || mode.value)} ${runtimeMode === mode.value ? 'active' : ''}" data-runtime-mode="${escapeHtml(mode.value)}">
                                <span class="tone">${escapeHtml(mode.tone)}</span>
                                <h3>${escapeHtml(mode.label)}</h3>
                                <p>${escapeHtml(mode.copy)}</p>
                            </button>
                        `).join('')}
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
                    ${BUDGET_FIELDS.map((field) => `
                        <div class="panel-card">
                            <h3>${escapeHtml(field.title)}</h3>
                            <div class="field">
                                <label for="${escapeHtml(field.inputId)}">${escapeHtml(field.label)}</label>
                                <input id="${escapeHtml(field.inputId)}" type="number" min="${escapeHtml(field.min || '0.01')}" step="${escapeHtml(field.step || 'any')}" value="${escapeHtml(state[field.stateKey])}">
                                <div class="field-note">${escapeHtml(field.note)}</div>
                            </div>
                        </div>
                    `).join('')}
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
                            <button class="btn btn-secondary" id="back-btn" type="button" ${index === 0 || state.saving ? 'disabled' : ''}>Back</button>
                            <button class="btn btn-primary" id="next-btn" type="button" ${nextButtonShouldBeDisabled() ? 'disabled' : ''}>${escapeHtml(nextLabel)}</button>
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
            const clearActions = Object.fromEntries(PROVIDER_FIELDS.map((field) => [
                field.id,
                () => { state[field.stateKey] = ''; },
            ]));
            Object.assign(clearActions, {
                'local-preset': () => {
                    state.localPreset = '';
                    state.localSource = '';
                state.localFilename = '';
                state.localRoutingMode = 'cloud';
                state.localSourceOpen = false;
            },
            'local-source': () => {
                state.localSource = '';
                state.localPreset = detectLocalPresetSelection();
            },
            'local-filename': () => {
                state.localFilename = '';
                state.localPreset = detectLocalPresetSelection();
                },
                'local-chat-format': () => { state.localChatFormat = ''; },
                'skills-repo-path': () => { state.skillsRepoPath = ''; },
            });
        root.querySelectorAll('[data-clear]').forEach((button) => {
            button.addEventListener('click', () => {
                const target = button.getAttribute('data-clear');
                if (clearActions[target]) clearActions[target]();
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
            const localPreset = document.getElementById('local-preset');
            const localSource = document.getElementById('local-source');
        const localFilename = document.getElementById('local-filename');
        const localContext = document.getElementById('local-context');
        const localGpuLayers = document.getElementById('local-gpu-layers');
        const localChatFormat = document.getElementById('local-chat-format');

        function bindStateInput(input, key, after = null) {
            if (!input) return;
            input.addEventListener('input', () => {
                state[key] = input.value;
                if (after) after(input);
                markStepEdited();
            });
        }

            PROVIDER_FIELDS.forEach((field) => {
                const input = document.getElementById(field.id);
                if (field.settingKey !== 'ANTHROPIC_API_KEY') {
                    bindStateInput(input, field.stateKey);
                    return;
                }
                if (!input) return;
                input.addEventListener('input', () => {
                    const wasConfigured = hasAnthropicKeyConfigured();
                    state[field.stateKey] = input.value;
                    if (!wasConfigured && hasAnthropicKeyConfigured()) {
                        state.claudeCliDismissed = false;
                        startClaudeCliStatusPolling();
                        updateClaudeCliStatus();
                    }
                    syncClaudeCliVisibility();
                    markStepEdited();
                });
            });
        if (localPreset) localPreset.addEventListener('change', () => { applyPresetSelection(localPreset.value); state.error = ''; render(); });
        bindStateInput(localSource, 'localSource', () => {
            state.localPreset = detectLocalPresetSelection();
            if (localPreset) localPreset.value = state.localPreset || '';
            state.localSourceOpen = true;
            if (trim(state.localSource) && activeProviderProfile() === 'local' && trim(state.localRoutingMode) === 'cloud') {
                state.localRoutingMode = 'all';
            }
        });
        bindStateInput(localFilename, 'localFilename', () => {
            state.localPreset = detectLocalPresetSelection();
            if (localPreset) localPreset.value = state.localPreset || '';
        });
        bindStateInput(localContext, 'localContextLength');
        bindStateInput(localGpuLayers, 'localGpuLayers');
        bindStateInput(localChatFormat, 'localChatFormat');
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

        function bindCompatibleModelLoader() {
            const loadBtn = document.getElementById('load-compatible-models');
            if (!loadBtn) return;
            loadBtn.addEventListener('click', async () => {
                const baseUrl = trim(state.compatibleBaseUrl).replace(/\/+$/, '');
                const apiKey = trim(state.compatibleApiKey);
                const statusEl = document.getElementById('compatible-load-status');
                const listEl = document.getElementById('compatible-model-list');
                if (!baseUrl) {
                    if (statusEl) statusEl.textContent = 'Go back and enter a base URL first.';
                    return;
                }
                if (statusEl) statusEl.textContent = 'Loading…';
                loadBtn.disabled = true;
                try {
                    let models;
                    if (HOST_MODE === 'web') {
                        const resp = await fetch('/api/openai-compatible/models', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ baseUrl, apiKey }),
                            cache: 'no-store',
                        });
                        const data = await resp.json().catch(() => ({}));
                        if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
                        models = (data.models || []).map((m) => trim(m)).filter(Boolean).sort();
                    } else {
                        if (!window.pywebview?.api?.fetch_compatible_models) {
                            throw new Error('Desktop model-fetch bridge unavailable.');
                        }
                        const result = await window.pywebview.api.fetch_compatible_models({ baseUrl, apiKey });
                        if (result?.error) throw new Error(result.error);
                        models = (result?.models || []).map((m) => trim(m)).filter(Boolean).sort();
                    }
                    if (!models.length) throw new Error('No models returned by endpoint.');
                    if (statusEl) statusEl.textContent = `${models.length} model${models.length === 1 ? '' : 's'} found — click one to fill empty slots.`;
                    if (listEl) {
                        listEl.hidden = false;
                        listEl.innerHTML = models.map((m) =>
                            `<button type="button" class="selection-pill" data-apply-model="${escapeHtml(m)}">${escapeHtml(m)}</button>`
                        ).join('');
                    }
                } catch (err) {
                    const msg = String(err?.message || err || 'Unknown error');
                    if (statusEl) statusEl.textContent = `Failed: ${msg}`;
                    if (listEl) { listEl.hidden = true; listEl.innerHTML = ''; }
                } finally {
                    loadBtn.disabled = false;
                }
            });
            if (!root._compatModelListenerBound) {
                root._compatModelListenerBound = true;
                root.addEventListener('click', (event) => {
                    const pill = event.target.closest('[data-apply-model]');
                    if (!pill) return;
                    const modelId = `openai-compatible::${pill.dataset.applyModel}`;
                    if (!trim(state.mainModel)) state.mainModel = modelId;
                    if (!trim(state.codeModel)) state.codeModel = modelId;
                    if (!trim(state.lightModel)) state.lightModel = modelId;
                    if (!trim(state.fallbackModel)) state.fallbackModel = modelId;
                    state.modelsDirty = true;
                    render();
                });
            }
        }

        function bindModelsStep() {
            const modelInputMap = Object.fromEntries(MODEL_SLOTS.map((slot) => [slot.inputId, slot.stateKey]));
            bindCompatibleModelLoader();
            function suggestionMatches(query) {
                const needle = trim(query).toLowerCase();
                return MODEL_SUGGESTIONS
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
            Object.entries(modelInputMap).forEach(([id, key]) => {
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
        if (skillsInput) skillsInput.addEventListener('input', () => { state.skillsRepoPath = skillsInput.value; markStepEdited(); });
        syncCurrentStepActionState();
    }

        function bindBudgetStep() {
            BUDGET_FIELDS.forEach((field) => {
                const input = document.getElementById(field.inputId);
                if (input) input.addEventListener('input', () => { state[field.stateKey] = input.value; markStepEdited(); });
            });
            syncCurrentStepActionState();
        }

    async function saveWizardPayload(payload) {
        if (HOST_MODE === 'web') {
            const runtimeMode = trim(state.runtimeMode) || 'advanced';
            await apiRequest('/api/settings', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
            });
            const runtimeResult = await apiRequest('/api/owner/runtime-mode', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ mode: runtimeMode }),
            });
            window.parent?.postMessage({
                type: 'ouroboros:onboarding-complete',
                restart_required: Boolean(runtimeResult?.restart_required),
                runtime_mode: runtimeResult?.runtime_mode || runtimeMode,
            }, '*');
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
                ...Object.fromEntries(PROVIDER_FIELDS.map((field) => [field.settingKey, trim(state[field.stateKey])])),
                ...Object.fromEntries(BUDGET_FIELDS.map((field) => [field.settingKey, Number(state[field.stateKey] || 0)])),
                OUROBOROS_REVIEW_ENFORCEMENT: trim(state.reviewEnforcement) || 'advisory',
                OUROBOROS_SKILLS_REPO_PATH: trim(state.skillsRepoPath),
                LOCAL_MODEL_SOURCE: trim(state.localSource),
            LOCAL_MODEL_FILENAME: trim(state.localFilename),
            LOCAL_MODEL_CONTEXT_LENGTH: Number(state.localContextLength || 0),
                LOCAL_MODEL_N_GPU_LAYERS: Number(state.localGpuLayers || 0),
                LOCAL_MODEL_CHAT_FORMAT: trim(state.localChatFormat),
                LOCAL_ROUTING_MODE: trim(state.localSource) ? (trim(state.localRoutingMode) || 'cloud') : 'cloud',
                ...Object.fromEntries(MODEL_SLOTS.map((slot) => [slot.settingKey, trim(state[slot.stateKey])])),
            };
        payload.OUROBOROS_RUNTIME_MODE = trim(state.runtimeMode) || 'advanced';
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
