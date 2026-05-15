import './api_types.js';

/**
 * Single browser-side gateway client. Keep backend calls here so UI modules
 * depend on named boundary helpers rather than raw transport details.
 */
export async function apiFetch(url, init = {}) {
    return fetch(url, init);
}

export async function fetchJson(url, init = {}) {
    const response = await apiFetch(url, init);
    let data = null;
    try {
        data = await response.json();
    } catch {
        data = { error: `non-json response (HTTP ${response.status})` };
    }
    if (!response.ok) {
        const message = (data && (data.error || data.message)) || `HTTP ${response.status}`;
        const error = new Error(message);
        error.status = response.status;
        error.body = data;
        error.payload = data;
        throw error;
    }
    return data;
}

export const apiClient = {
    /** @returns {Promise<import('./api_types.js').HealthResponse>} */
    health: () => fetchJson('/api/health', { cache: 'no-store' }),
    /** @returns {Promise<import('./api_types.js').StateResponse>} */
    state: () => fetchJson('/api/state', { cache: 'no-store' }),
    settings: () => fetchJson('/api/settings', { cache: 'no-store' }),
    saveSettings: (payload) => fetchJson('/api/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
    }),
    extensions: () => fetchJson('/api/extensions', { cache: 'no-store' }),
    skillLifecycleQueue: () => fetchJson('/api/skills/lifecycle-queue', { cache: 'no-store' }),
    chatHistory: (limit = 1000) => fetchJson(`/api/chat/history?limit=${encodeURIComponent(limit)}`, { cache: 'no-store' }),
};
