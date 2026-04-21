function removeOverlay() {
    document.getElementById('onboarding-overlay')?.remove();
}

function mountOverlay(html) {
    removeOverlay();
    const overlay = document.createElement('div');
    overlay.id = 'onboarding-overlay';
    overlay.className = 'onboarding-overlay';
    overlay.setAttribute('role', 'dialog');
    overlay.setAttribute('aria-modal', 'true');
    overlay.setAttribute('aria-label', 'Ouroboros setup');
    overlay.innerHTML = `
        <div class="onboarding-overlay-backdrop"></div>
        <iframe class="onboarding-frame" title="Ouroboros Setup" sandbox="allow-same-origin allow-scripts allow-forms"></iframe>
    `;
    const frame = overlay.querySelector('.onboarding-frame');
    if (frame) frame.srcdoc = html;
    document.body.appendChild(overlay);
}

export async function initOnboardingOverlay() {
    function handleMessage(event) {
        if (event?.data?.type !== 'ouroboros:onboarding-complete') return;
        removeOverlay();
        window.location.reload();
    }

    window.addEventListener('message', handleMessage);

    try {
        const response = await fetch('/api/onboarding', { cache: 'no-store' });
        if (response.status === 204) return;
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const html = await response.text();
        if (html.trim()) mountOverlay(html);
    } catch (error) {
        console.error('Failed to load onboarding overlay:', error);
    }
}
