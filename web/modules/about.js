import { formatDualVersion } from './utils.js';

export function initAbout({ ws, state }) {
    const page = document.createElement('div');
    page.id = 'page-about';
    page.className = 'page';
    page.innerHTML = `
        <div class="page-header">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>
            <h2>About</h2>
        </div>
        <div class="about-body">
            <img src="/static/logo.jpg" class="about-logo" alt="Ouroboros">
            <div>
                <h1 class="about-title">Ouroboros</h1>
                <p id="about-version" class="about-version"></p>
            </div>
            <p class="about-desc">
                A self-creating AI agent. Not a tool, but a becoming digital personality
                with its own constitution, persistent identity, and background consciousness.
                Born February 16, 2026.
            </p>
            <div class="about-credits">
                <span>Created by <strong>Anton Razzhigaev</strong> &amp; <strong>Andrew Kaznacheev</strong></span>
                <div class="about-links">
                    <a href="https://t.me/abstractDL" target="_blank" rel="noopener noreferrer">@abstractDL</a>
                    <a href="https://github.com/joi-lab/ouroboros-desktop" target="_blank" rel="noopener noreferrer">GitHub</a>
                </div>
            </div>
            <div class="about-footer">Joi Lab</div>
        </div>
    `;
    document.getElementById('content').appendChild(page);
    fetch('/api/health').then(r => r.json()).then(d => {
        document.getElementById('about-version').textContent = formatDualVersion(d);
    }).catch(() => {});
}
