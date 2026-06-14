/** Web UI orchestrator: shared state, navigation, page init, WS startup. */

import { createWS } from './modules/ws.js';
import { apiFetch } from './modules/api_client.js';
import { loadVersion, initMatrixRain } from './modules/utils.js';
import { initChat, createChatInstance } from './modules/chat.js';
import { initFiles } from './modules/files.js';

import { initLogs } from './modules/logs.js';
import { initEvolution } from './modules/evolution.js';
import { initSettings } from './modules/settings.js';
import { initCosts } from './modules/costs.js';
import { initSkills } from './modules/skills.js';
import { initWidgets } from './modules/widgets.js';
import { initUpdates } from './modules/updates.js';
import { initDashboard } from './modules/dashboard.js';
import { hydrateNavIcons } from './modules/page_icons.js';

import { initOnboardingOverlay } from './modules/onboarding_overlay.js';

const state = {
    messages: [],
    logs: [],
    dashboard: {},
    activeFilters: { tools: true, llm: true, errors: true, tasks: true, system: true, consciousness: true },
    unreadCount: 0,
    activePage: 'chat',
    settingsActiveSubtab: 'providers',
    dashboardActiveSubtab: 'logs',
    beforePageLeave: null,
    // Project-thread isolation SSOT for the live WS fan-out. Initialized to an
    // empty Set (never undefined) so chat.js::isMyThread is deterministic before
    // the first /api/state response; populated by renderProjectsNav.
    projectChatIds: new Set(),
};

// Connect only after modules register listeners.
const ws = createWS();
const beforePageLeaveHandlers = [];
let settingsControls = null;
let dashboardControls = null;
const navState = {
    activeProjectId: null,
    projectsExpanded: true,
    mobileDrawerOpen: false,
};
const primarySidebar = document.getElementById('primary-sidebar');
const navDrawerBackdrop = document.getElementById('nav-drawer-backdrop');
const projectPanelBackdrop = document.getElementById('project-panel-backdrop');
const projectPanel = document.getElementById('project-panel');
const projectPanelBody = document.getElementById('project-panel-body');
const projectPanelTitle = document.getElementById('project-panel-title');
const navProjects = document.getElementById('nav-projects');
const navProjectsToggle = document.getElementById('nav-projects-toggle');
const navProjectsCount = document.getElementById('nav-projects-count');
const navProjectsList = document.getElementById('nav-projects-list');
const projectInstances = new Map();
let knownProjectsJson = '';
let lastProjectRows = [];
let projectPanelHideTimer = null;

function setMobileDrawerOpen(open, { sync = true } = {}) {
    navState.mobileDrawerOpen = Boolean(open);
    if (sync) syncNavigationState();
}

async function showPage(name, options = {}) {
    const pageName = String(name || '').trim();
    if (!pageName) return false;
    const changingPage = state.activePage !== pageName;
    if (changingPage) {
        for (const handler of beforePageLeaveHandlers) {
            const canLeave = await handler({ from: state.activePage, to: pageName });
            if (canLeave === false) return false;
        }
        document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
        document.getElementById(`page-${pageName}`)?.classList.add('active');
        state.activePage = pageName;
        window.dispatchEvent(new CustomEvent('ouro:page-shown', { detail: { page: pageName } }));
        if (pageName === 'chat') {
            state.unreadCount = 0;
            updateUnreadBadge();
        }
    }
    if (options.closeProject !== false) closeProjectPanel({ sync: false });
    if (options.closeDrawer !== false) navState.mobileDrawerOpen = false;
    syncNavigationState();
    return true;
}

async function openSettingsTab(tabName) {
    await showPage('settings');
    if (settingsControls && typeof settingsControls.activateTab === 'function') {
        settingsControls.activateTab(tabName);
    }
}

async function openDashboardTab(tabName) {
    await showPage('dashboard');
    if (dashboardControls && typeof dashboardControls.activateTab === 'function') {
        dashboardControls.activateTab(tabName);
    }
}

function updateUnreadBadge() {
    const btn = document.querySelector('[data-nav-page="chat"]');
    let badge = btn?.querySelector('.unread-badge');
    if (state.unreadCount > 0 && state.activePage !== 'chat') {
        if (!badge) {
            badge = document.createElement('span');
            badge.className = 'unread-badge';
            btn.appendChild(badge);
        }
        badge.textContent = state.unreadCount > 99 ? '99+' : state.unreadCount;
    } else if (badge) {
        badge.remove();
    }
}

function syncNavigationState() {
    const activeProjectId = navState.activeProjectId;
    const drawerOpen = Boolean(navState.mobileDrawerOpen);
    document.body.classList.toggle('nav-drawer-open', drawerOpen);
    primarySidebar?.classList.toggle('open', drawerOpen);
    document.querySelectorAll('[data-mobile-nav-toggle]').forEach((button) => {
        button.setAttribute('aria-expanded', drawerOpen ? 'true' : 'false');
    });
    if (navDrawerBackdrop) navDrawerBackdrop.hidden = !drawerOpen;

    document.querySelectorAll('[data-nav-page]').forEach((button) => {
        const isActive = !activeProjectId && button.dataset.navPage === state.activePage;
        button.classList.toggle('active', isActive);
        if (isActive) button.setAttribute('aria-current', 'page');
        else button.removeAttribute('aria-current');
    });
    navProjectsToggle?.classList.toggle('active', Boolean(activeProjectId));
    navProjectsToggle?.setAttribute('aria-expanded', navState.projectsExpanded ? 'true' : 'false');
    navProjectsList.hidden = !navState.projectsExpanded;
    document.querySelectorAll('[data-project-id]').forEach((button) => {
        const isActive = button.dataset.projectId === activeProjectId;
        button.classList.toggle('active', isActive);
        if (isActive) button.setAttribute('aria-current', 'page');
        else button.removeAttribute('aria-current');
    });
    if (projectPanel) {
        if (projectPanelHideTimer) {
            clearTimeout(projectPanelHideTimer);
            projectPanelHideTimer = null;
        }
        if (activeProjectId) {
            projectPanel.hidden = false;
            if (projectPanelBackdrop) projectPanelBackdrop.hidden = false;
            requestAnimationFrame(() => {
                projectPanel.classList.add('open');
                projectPanelBackdrop?.classList.add('open');
            });
        } else {
            projectPanel.classList.remove('open');
            projectPanelBackdrop?.classList.remove('open');
            projectPanelHideTimer = setTimeout(() => {
                projectPanel.hidden = true;
                if (projectPanelBackdrop) projectPanelBackdrop.hidden = true;
                projectPanelHideTimer = null;
            }, 220);
        }
        document.body.classList.toggle('project-panel-open', Boolean(activeProjectId));
    }
}

document.querySelectorAll('[data-nav-page]').forEach(btn => {
    btn.addEventListener('click', () => {
        showPage(btn.dataset.navPage);
    });
});
document.addEventListener('click', (event) => {
    const toggle = event.target.closest('[data-mobile-nav-toggle]');
    if (!toggle) return;
    setMobileDrawerOpen(!navState.mobileDrawerOpen);
});
navDrawerBackdrop?.addEventListener('click', () => setMobileDrawerOpen(false));
hydrateNavIcons();

const ctx = {
    ws,
    state,
    updateUnreadBadge,
    showPage,
    openSettingsTab,
    openDashboardTab,
    setBeforePageLeave: (handler) => {
        if (typeof handler !== 'function') return () => {};
        beforePageLeaveHandlers.push(handler);
        return () => {
            const idx = beforePageLeaveHandlers.indexOf(handler);
            if (idx >= 0) beforePageLeaveHandlers.splice(idx, 1);
        };
    },
};

initChat(ctx);
initFiles(ctx);

// ---------------------------------------------------------------------------
// Multi-project navigation + right thread panel (v6.32.0). Projects come from
// /api/state; each opens as a chat instance bound to its project chat_id.
// Navigation is one state machine now: page, project, and mobile drawer are
// synchronized together so Utilities and Projects can't remain active at once.
// ---------------------------------------------------------------------------
function closeProjectPanel({ sync = true } = {}) {
    navState.activeProjectId = null;
    for (const inst of projectInstances.values()) inst.page.hidden = true;
    if (sync) syncNavigationState();
}

async function openProjectPanel(project, { closeDrawer = true } = {}) {
    if (!project?.id) return;
    if (navState.activeProjectId === project.id) {
        closeProjectPanel();
        return;
    }
    const movedToChat = await showPage('chat', { closeProject: false, closeDrawer: false });
    if (movedToChat === false) return;
    navState.activeProjectId = project.id;
    projectPanelTitle.textContent = project.name || project.id;
    let inst = projectInstances.get(project.id);
    if (!inst) {
        inst = createChatInstance({
            ...ctx,
            chatId: Number(project.chat_id) || 1,
            projectId: project.id,
            idPrefix: `pchat-${project.id}`,
            mountEl: projectPanelBody,
            asPanel: true,
            title: project.name || project.id,
        });
        projectInstances.set(project.id, inst);
    }
    for (const [pid, other] of projectInstances) other.page.hidden = pid !== project.id;
    if (closeDrawer) navState.mobileDrawerOpen = false;
    syncNavigationState();
}

document.getElementById('project-panel-close')?.addEventListener('click', () => closeProjectPanel());
projectPanelBackdrop?.addEventListener('click', () => closeProjectPanel());
navProjectsToggle?.addEventListener('click', () => {
    navState.projectsExpanded = !navState.projectsExpanded;
    syncNavigationState();
});

function renderProjectsNav(projects, projectChatIds) {
    const all = projects || [];
    // Isolation fan-out SSOT: recognize EVERY registered project chat_id (incl.
    // archived / file-less / no-activity / beyond the sidebar summary cap),
    // matching the backend registered_project_chat_ids, so chat.js::isMyThread
    // never treats a project frame as a main-thread frame on the live WS path.
    // Prefer the COMPLETE /api/state `project_chat_ids` (uncapped); fall back to
    // the (capped) projects array only if that field is absent. Sidebar
    // visibility is a SEPARATE concern (the filtered `rows` below).
    const completeChatIds = Array.isArray(projectChatIds)
        ? projectChatIds
        : all.map(p => Number(p && p.chat_id) || 0);
    state.projectChatIds = new Set(completeChatIds.map(Number).filter(Boolean));
    const rows = all
        .filter(p => p && p.id && p.status !== 'archived' && p.has_thread_activity !== false)
        .sort((a, b) => String(b.last_active_at || b.updated_at || b.created_at || '')
            .localeCompare(String(a.last_active_at || a.updated_at || a.created_at || '')));
    const json = JSON.stringify(rows.map(p => [p.id, p.name, p.status, p.chat_id]));
    if (json === knownProjectsJson) return;
    knownProjectsJson = json;
    lastProjectRows = rows;
    paintProjectsNav();
    syncNavigationState();
}

// Paint the collapsible, scrollable projects list from the cached rows.
function paintProjectsNav() {
    const rows = lastProjectRows;
    navProjectsList.textContent = '';
    navProjects.hidden = false;
    if (navProjectsCount) navProjectsCount.textContent = rows.length ? String(rows.length) : '';
    for (const project of rows) {
        const btn = document.createElement('button');
        btn.className = 'nav-row nav-project-row';
        btn.dataset.projectId = project.id;
        btn.title = `${project.name || project.id} (${project.status})`;
        const label = document.createElement('span');
        label.className = 'nav-row-label';
        label.textContent = project.name || project.id;
        btn.appendChild(label);
        if (project.status === 'sleeping') {
            btn.classList.add('sleeping');
            const meta = document.createElement('span');
            meta.className = 'nav-row-meta';
            meta.textContent = 'sleep';
            btn.appendChild(meta);
        }
        if (project.id === navState.activeProjectId) btn.classList.add('active');
        btn.addEventListener('click', () => openProjectPanel(project));
        navProjectsList.appendChild(btn);
    }
}

async function refreshProjectsNav() {
    try {
        const resp = await apiFetch('/api/state', { cache: 'no-store' });
        if (!resp.ok) return;
        const data = await resp.json();
        renderProjectsNav(data.projects || [], data.project_chat_ids);
    } catch {}
}

window.addEventListener('ouro:project-created', async (event) => {
    const project = event?.detail?.project;
    knownProjectsJson = '';
    await refreshProjectsNav();
    if (project?.id) {
        const resolved = lastProjectRows.find((item) => item.id === project.id) || project;
        openProjectPanel(resolved);
    }
});

ws.on('open', refreshProjectsNav);
// A backend-created project (e.g. the agent's promote_chat_to_task tool) pushes
// this so the live WS fan-out learns the new project chat_id immediately, instead
// of waiting for the periodic poll and misrouting early frames into the main chat.
// Add the chat_id SYNCHRONOUSLY from the payload so fan-out is correct before the
// async /api/state refresh returns; then refresh the full nav/list.
ws.on('projects_changed', (msg) => {
    const cid = Number(msg && msg.chat_id) || 0;
    if (cid) state.projectChatIds.add(cid);
    refreshProjectsNav();
});
setInterval(refreshProjectsNav, 20000);
settingsControls = initSettings(ctx);
dashboardControls = initDashboard(ctx);
initLogs({ ...ctx, mount: document.getElementById('dashboard-panel-logs') });
initEvolution({ ...ctx, mount: document.getElementById('dashboard-panel-evolution') });
initUpdates({ ...ctx, mount: document.getElementById('dashboard-panel-updates') });
initCosts({ ...ctx, mount: document.getElementById('dashboard-panel-costs') });
initSkills(ctx);
initWidgets(ctx);

initOnboardingOverlay();

initMatrixRain();
loadVersion();
syncNavigationState();

// Mobile soft-keyboard handling: --vvh + keyboard-open without inline styles.
(function () {
    const vvhStyle = document.createElement('style');
    vvhStyle.id = 'runtime-vvh';
    document.head.appendChild(vvhStyle);

    let wasKeyboardOpen = false;
    let keyboardTouchStartY = 0;
    let frozenBaseline = 0;

    function findScrollableKeyboardNode(target) {
        let el = target;
        while (el && el !== document.body) {
            // Class twins cover secondary chat instances (project panels);
            // the main chat keeps its historic ids.
            if (
                el.id === 'chat-messages'
                || el.id === 'chat-input'
                || el.classList?.contains('chat-messages')
                || el.classList?.contains('chat-input')
                || el.classList?.contains('chat-live-timeline')
            ) return el;
            el = el.parentElement;
        }
        return null;
    }

    function lockTouchStart(e) {
        if (e.touches && e.touches.length) keyboardTouchStartY = e.touches[0].clientY;
    }

    // Stop chat overscroll from moving the document while the keyboard is open.
    function lockBoundaryTouch(e) {
        const touch = e.touches && e.touches.length ? e.touches[0] : null;
        const scrollable = findScrollableKeyboardNode(e.target);
        if (scrollable && touch) {
            const dy = touch.clientY - keyboardTouchStartY;
            const atTop = scrollable.scrollTop <= 0;
            const atBottom = Math.ceil(scrollable.scrollTop + scrollable.clientHeight) >= scrollable.scrollHeight;
            if ((!atTop && dy > 0) || (!atBottom && dy < 0)) return;
        }
        e.preventDefault();
    }

    function captureFrozenBaseline() {
        if (window.innerWidth > 640 || wasKeyboardOpen) return;
        const candidates = [
            document.documentElement.clientHeight,
            window.innerHeight,
            window.screen.availHeight || 0,
            window.screen.height || 0,
        ];
        const best = Math.max(...candidates);
        if (best > frozenBaseline) frozenBaseline = best;
    }

    captureFrozenBaseline();

    const updateVvh = () => {
        const viewport = window.visualViewport;
        const h = viewport ? viewport.height : window.innerHeight;

        if (window.innerWidth <= 640) {
            const safeHeight = Math.max(320, Math.ceil(h || window.innerHeight || 0));
            vvhStyle.textContent = ':root{--vvh:' + safeHeight + 'px}';
            if (!wasKeyboardOpen) captureFrozenBaseline();
            const stableHeight = frozenBaseline || document.documentElement.clientHeight;
            const keyboardVisible = viewport
                ? (stableHeight - h) > Math.max(120, stableHeight * 0.25)
                : false;

            if (keyboardVisible && !wasKeyboardOpen) {
                window.scrollTo(0, 0);
                document.addEventListener('touchstart', lockTouchStart, { passive: true });
                document.addEventListener('touchmove', lockBoundaryTouch, { passive: false });
            }
            if (!keyboardVisible && wasKeyboardOpen) {
                document.removeEventListener('touchstart', lockTouchStart);
                document.removeEventListener('touchmove', lockBoundaryTouch);
            }
            document.documentElement.classList.toggle('keyboard-open', keyboardVisible);
            document.body.classList.toggle('keyboard-open', keyboardVisible);
            wasKeyboardOpen = keyboardVisible;
        } else {
            if (wasKeyboardOpen) {
                document.removeEventListener('touchstart', lockTouchStart);
                document.removeEventListener('touchmove', lockBoundaryTouch);
            }
            document.documentElement.classList.remove('keyboard-open');
            document.body.classList.remove('keyboard-open');
            wasKeyboardOpen = false;
            vvhStyle.textContent = ':root{--vvh:100dvh}';
        }
    };
    if (window.visualViewport) {
        window.visualViewport.addEventListener('resize', updateVvh);
        window.visualViewport.addEventListener('scroll', updateVvh);
    }
    window.addEventListener('resize', updateVvh);
    window.addEventListener('orientationchange', () => {
        frozenBaseline = 0;
        captureFrozenBaseline();
        updateVvh();
    });
    updateVvh();
}());

// Populate the project-thread isolation set BEFORE opening the socket so the live
// fan-out never misclassifies an early project frame as main-chat traffic during
// startup (chat.js::isMyThread relies on state.projectChatIds). Connect even if
// the prefetch fails, then ws.on('open') keeps it fresh.
refreshProjectsNav().finally(() => ws.connect());
