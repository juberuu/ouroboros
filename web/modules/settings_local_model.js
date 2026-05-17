import { showToast } from './toast.js';
import { apiFetch } from './api_client.js';


function readLocalModelBody() {
    return {
        source: document.getElementById('s-local-source').value.trim(),
        filename: document.getElementById('s-local-filename').value.trim(),
        port: parseInt(document.getElementById('s-local-port').value, 10) || 8766,
        n_gpu_layers: parseInt(document.getElementById('s-local-gpu-layers').value, 10),
        n_ctx: parseInt(document.getElementById('s-local-ctx').value, 10) || 16384,
        chat_format: document.getElementById('s-local-chat-format').value.trim(),
    };
}

function setTestResult(text, tone = 'muted') {
    const el = document.getElementById('local-model-test-result');
    if (!el) return;
    el.style.display = text ? 'block' : 'none';
    el.textContent = text;
    el.dataset.tone = tone;
}

function setLocalStatus(text, tone = 'muted') {
    const el = document.getElementById('local-model-status');
    if (el) { el.textContent = text; el.dataset.tone = tone; }
}

function setInstallBtnVisible(visible) {
    const btn = document.getElementById('btn-local-install-runtime');
    if (btn) btn.classList.toggle('local-model-hidden', !visible);
}

function setProgressBar(fraction) {
    const wrap = document.getElementById('local-model-progress-wrap');
    const bar = document.getElementById('local-model-progress-bar');
    if (!wrap || !bar) return;
    if (fraction === null || fraction === undefined) {
        wrap.classList.add('local-model-hidden');
        return;
    }
    wrap.classList.remove('local-model-hidden');
    bar.style.width = Math.round(fraction * 100) + '%';
    bar.setAttribute('aria-valuenow', Math.round(fraction * 100));
}

export function bindLocalModelControls({ state }) {
    async function updateLocalStatus() {
        if (state.activePage !== 'settings') return;
        try {
            const resp = await apiFetch('/api/local-model/status', { cache: 'no-store' });
            const d = await resp.json();
            const el = document.getElementById('local-model-status');
            if (!el) return;
            const isReady = d.status === 'ready';
            const isInstalling = d.runtime_status === 'installing';
            const isDownloading = d.status === 'downloading';
            const runtimeMissing = d.runtime_status === 'missing' || d.runtime_status === 'install_error';

            let text = 'Status: ' + (d.status || 'offline').charAt(0).toUpperCase() + (d.status || 'offline').slice(1);
            if (isReady && d.context_length) text += ` (ctx: ${d.context_length})`;
            if (isDownloading && d.download_progress != null) {
                const pct = Math.round(d.download_progress * 100);
                text += ` ${pct}%`;
                setProgressBar(d.download_progress);
            } else {
                setProgressBar(null);
            }
            if (isInstalling) text = 'Status: Installing local runtime…';
            if (d.runtime_status === 'install_ok') text += ' — Runtime installed ✓';
            if (d.runtime_status === 'install_error') text += ' — Runtime install failed';
            if (d.error && !isInstalling) text += ' — ' + d.error;

            el.textContent = text;
            el.dataset.tone = isReady ? 'ok' : (d.status === 'error' || d.runtime_status === 'install_error' ? 'error' : 'muted');

            document.getElementById('btn-local-stop').disabled = !isReady;
            document.getElementById('btn-local-test').disabled = !isReady;
            document.getElementById('btn-local-start').disabled = isInstalling || isDownloading;

            setInstallBtnVisible(runtimeMissing);
            const installBtn = document.getElementById('btn-local-install-runtime');
            if (installBtn) installBtn.disabled = isInstalling;

            if (d.runtime_status === 'install_error' && d.runtime_install_log) {
                setTestResult('Install failed:\n' + d.runtime_install_log, 'error');
            }

            if (d.runtime_status === 'install_ok' && state._pendingLocalStart) {
                state._pendingLocalStart = false;
                triggerStart();
            }

            ['s-local-main', 's-local-code', 's-local-light', 's-local-fallback'].forEach((id) => {
                const cb = document.getElementById(id);
                const label = cb?.closest('.local-toggle');
                if (!cb || !label) return;
                if (cb.checked && !isReady) {
                    label.title = 'Local server is not running - requests will fail until started';
                    label.dataset.warning = '1';
                } else {
                    label.title = '';
                    delete label.dataset.warning;
                }
            });
        } catch {}
    }

    async function triggerStart() {
        const body = readLocalModelBody();
        if (!body.source) {
            showToast('Enter a model source (HuggingFace repo ID or local path)', 'error');
            return;
        }
        setTestResult('');
        setProgressBar(null);
        try {
            const resp = await apiFetch('/api/local-model/start', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            const data = await resp.json();
            if (resp.status === 412 && data.error === 'runtime_missing') {
                setInstallBtnVisible(true);
                setLocalStatus('Local runtime not installed. Click "Install Local Runtime" below.', 'error');
                setTestResult(
                    'llama-cpp-python is not installed.\n' +
                    'Click "Install Local Runtime" to install it automatically,\n' +
                    'then the model will start automatically.\n\n' +
                    'Manual install: ' + (data.hint || 'pip install llama-cpp-python[server]'),
                    'error'
                );
            } else if (data.error) {
                setLocalStatus('Error: ' + data.error, 'error');
            } else {
                updateLocalStatus();
            }
        } catch (e) {
            setLocalStatus('Failed: ' + e.message, 'error');
        }
    }

    document.getElementById('btn-local-start').addEventListener('click', triggerStart);

    document.getElementById('btn-local-stop').addEventListener('click', async () => {
        try {
            await apiFetch('/api/local-model/stop', { method: 'POST' });
            setProgressBar(null);
            updateLocalStatus();
        } catch (e) {
            showToast('Failed: ' + e.message, 'error');
        }
    });

    document.getElementById('btn-local-test').addEventListener('click', async () => {
        setTestResult('Running tests...', 'muted');
        try {
            const resp = await apiFetch('/api/local-model/test', { method: 'POST' });
            const r = await resp.json();
            if (r.error) {
                setTestResult('Error: ' + r.error, 'error');
                return;
            }
            const lines = [];
            lines.push((r.chat_ok ? '✓' : '✗') + ' Basic chat' + (r.tokens_per_sec ? ` (${r.tokens_per_sec} tok/s)` : ''));
            lines.push((r.tool_call_ok ? '✓' : '✗') + ' Tool calling');
            if (r.details && !r.success) lines.push(r.details);
            setTestResult(lines.join('\n'), r.success ? 'ok' : 'warn');
            const el = document.getElementById('local-model-test-result');
            if (el) el.style.whiteSpace = 'pre-wrap';
        } catch (e) {
            setTestResult('Test failed: ' + e.message, 'error');
        }
    });

    const installBtn = document.getElementById('btn-local-install-runtime');
    if (installBtn) {
        installBtn.addEventListener('click', async () => {
            installBtn.disabled = true;
            setTestResult('Installing llama-cpp-python, this may take a few minutes…', 'muted');
            setLocalStatus('Status: Installing local runtime…', 'muted');
            const body = readLocalModelBody();
            state._pendingLocalStart = !!body.source;
            try {
                const resp = await apiFetch('/api/local-model/install-runtime', { method: 'POST' });
                const d = await resp.json();
                if (d.error) {
                    setTestResult('Install request failed: ' + d.error, 'error');
                    installBtn.disabled = false;
                }
            } catch (e) {
                setTestResult('Install failed: ' + e.message, 'error');
                installBtn.disabled = false;
            }
        });
    }

    updateLocalStatus();
    setInterval(updateLocalStatus, 3000);
}
