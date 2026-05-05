export const LOG_CATEGORIES = {
    tools: { label: 'Tools', color: 'var(--blue)' },
    llm: { label: 'LLM', color: 'var(--accent)' },
    errors: { label: 'Errors', color: 'var(--red)' },
    tasks: { label: 'Tasks', color: 'var(--amber)' },
    system: { label: 'System', color: 'var(--text-muted)' },
    consciousness: { label: 'Consciousness', color: 'var(--accent)' },
};

export function categorizeLogEvent(evt) {
    const t = evt.type || evt.event || '';
    if (evt.is_progress) {
        return evt.task_id === 'bg-consciousness' ? 'consciousness' : 'tasks';
    }
    if (t.includes('error') || t.includes('crash') || t.includes('fail')) return 'errors';
    if (t.includes('llm') || t.includes('model')) return 'llm';
    if (t.includes('tool') || evt.tool) return 'tools';
    if (t.includes('task') || t.includes('evolution') || t.includes('review')) return 'tasks';
    if (t.includes('consciousness') || t.includes('bg_')) return 'consciousness';
    return 'system';
}

export function normalizeLogTs(isoStr) {
    if (!isoStr) return '';
    try {
        const d = new Date(isoStr);
        if (Number.isNaN(d.getTime())) return '';
        return d.toLocaleTimeString([], { hour12: false });
    } catch {
        return '';
    }
}

function shortText(text, maxLen = 180) {
    const s = String(text || '').replace(/\s+/g, ' ').trim();
    if (!s) return '';
    return s.length > maxLen ? s.slice(0, maxLen - 3) + '...' : s;
}

function describeText(text, maxLen = 180) {
    const full = String(text || '').replace(/\s+/g, ' ').trim();
    if (!full) return { preview: '', full: '' };
    return {
        preview: full.length > maxLen ? full.slice(0, maxLen - 3) + '...' : full,
        full,
    };
}

export function formatLogMoney(value) {
    const num = Number(value);
    if (!Number.isFinite(num) || num <= 0) return '';
    return `$${num.toFixed(4)}`;
}

export function formatLogDuration(sec) {
    const num = Number(sec);
    if (!Number.isFinite(num) || num < 0) return '';
    if (num >= 60) {
        const mins = Math.floor(num / 60);
        const rem = Math.round(num % 60);
        return `${mins}m ${rem}s`;
    }
    return `${num < 10 ? num.toFixed(1) : Math.round(num)}s`;
}

function formatLogTokens(evt) {
    const prompt = Number(evt.prompt_tokens || 0);
    const completion = Number(evt.completion_tokens || 0);
    if (!prompt && !completion) return '';
    return `${prompt}\u2192${completion} tok`;
}

function compactJson(value, maxLen = 220) {
    if (value == null) return '';
    let txt = '';
    try {
        txt = JSON.stringify(value);
    } catch {
        txt = String(value);
    }
    return shortText(txt, maxLen);
}

function extractCommandText(args) {
    if (!args || typeof args !== 'object') return '';
    const cmd = args.cmd;
    if (Array.isArray(cmd)) {
        return cmd.map((part) => String(part || '').trim()).filter(Boolean).join(' ');
    }
    if (typeof cmd === 'string') return cmd;
    return '';
}

function describeStartupChecks(checks) {
    if (!checks || typeof checks !== 'object') return '';
    const parts = [];
    for (const [key, value] of Object.entries(checks)) {
        if (value && typeof value === 'object' && value.status) {
            parts.push(`${key}:${value.status}`);
        }
    }
    return shortText(parts.join(' | '), 240);
}

export function summarizeLogEvent(evt) {
    const t = evt.type || evt.event || 'unknown';
    const base = {
        typeLabel: t,
        phase: '',
        headline: '',
        body: '',
        meta: [],
    };

    if (evt.is_progress || t === 'send_message') {
        return {
            ...base,
            phase: evt.task_id === 'bg-consciousness' ? 'thought' : 'progress',
            headline: shortText(String(evt.content || evt.text || '').replace(/^💬\s*/, ''), 240) || 'Progress update',
            meta: [evt.task_id === 'bg-consciousness' ? 'background' : 'task'].filter(Boolean),
        };
    }

    if (t === 'task_started') {
        return {
            ...base,
            phase: 'start',
            headline: `Started ${evt.task_type || 'task'}`,
            body: shortText(evt.task_text, 220),
            meta: [evt.task_id ? `task=${evt.task_id}` : '', evt.direct_chat ? 'chat' : 'queued'].filter(Boolean),
        };
    }

    if (t === 'task_received') {
        const task = evt.task || {};
        return {
            ...base,
            phase: 'queued',
            headline: `Received ${task.type || 'task'}`,
            body: shortText(task.text, 220),
            meta: [task.id ? `task=${task.id}` : '', task.text_len ? `${task.text_len} chars` : ''].filter(Boolean),
        };
    }

    if (t === 'context_building_started') {
        return {
            ...base,
            phase: 'context',
            headline: 'Building context',
            meta: [evt.task_id ? `task=${evt.task_id}` : '', evt.task_type || ''].filter(Boolean),
        };
    }

    if (t === 'context_building_finished') {
        return {
            ...base,
            phase: 'ready',
            headline: 'Context ready',
            meta: [
                evt.task_id ? `task=${evt.task_id}` : '',
                evt.message_count != null ? `${evt.message_count} msgs` : '',
                Number.isFinite(Number(evt.budget_remaining_usd)) ? `$${Number(evt.budget_remaining_usd).toFixed(2)} left` : '',
            ].filter(Boolean),
        };
    }

    if (t === 'task_heartbeat') {
        return {
            ...base,
            phase: evt.phase || 'alive',
            headline: 'Still working',
            meta: [
                evt.task_id ? `task=${evt.task_id}` : '',
                evt.task_type || '',
                formatLogDuration(evt.runtime_sec),
            ].filter(Boolean),
        };
    }

    if (t === 'llm_round_started') {
        return {
            ...base,
            phase: 'calling',
            headline: `Calling ${evt.model || 'model'}`,
            meta: [
                evt.task_id ? `task=${evt.task_id}` : '',
                evt.round ? `r${evt.round}` : '',
                evt.attempt ? `try ${evt.attempt}` : '',
                evt.reasoning_effort || '',
                evt.use_local ? 'local' : '',
            ].filter(Boolean),
        };
    }

    if (t === 'llm_round_finished' || t === 'llm_round') {
        return {
            ...base,
            phase: 'done',
            headline: `LLM round ${evt.round || ''} finished`.trim(),
            meta: [
                evt.task_id ? `task=${evt.task_id}` : '',
                evt.model || '',
                formatLogTokens(evt),
                formatLogMoney(evt.cost_usd || evt.cost),
                evt.response_kind === 'tool_calls' ? `${evt.tool_call_count || 0} tool calls` : evt.response_kind || '',
            ].filter(Boolean),
        };
    }

    if (t === 'llm_round_empty' || t === 'llm_empty_response') {
        return {
            ...base,
            phase: 'empty',
            headline: 'Model returned empty response',
            meta: [evt.task_id ? `task=${evt.task_id}` : '', evt.model || '', evt.round ? `r${evt.round}` : ''].filter(Boolean),
        };
    }

    if (t === 'llm_round_error' || t === 'llm_api_error') {
        return {
            ...base,
            phase: 'error',
            headline: 'LLM call failed',
            body: shortText(evt.error, 260),
            meta: [evt.task_id ? `task=${evt.task_id}` : '', evt.model || '', evt.round ? `r${evt.round}` : ''].filter(Boolean),
        };
    }

    if (t === 'llm_usage') {
        return {
            ...base,
            phase: 'usage',
            headline: 'LLM usage recorded',
            meta: [
                evt.task_id ? `task=${evt.task_id}` : '',
                evt.model || '',
                formatLogTokens(evt),
                formatLogMoney(evt.cost_usd || evt.cost),
                evt.category || '',
            ].filter(Boolean),
        };
    }

    if (t === 'tool_call_started') {
        return {
            ...base,
            phase: 'start',
            headline: `Running ${evt.tool || 'tool'}`,
            body: compactJson(evt.args, 260),
            meta: [evt.task_id ? `task=${evt.task_id}` : '', evt.timeout_sec ? `timeout ${evt.timeout_sec}s` : ''].filter(Boolean),
        };
    }

    if (t === 'tool_call_finished') {
        return {
            ...base,
            phase: evt.is_error ? 'error' : 'done',
            headline: `${evt.tool || 'tool'} ${evt.is_error ? 'failed' : 'finished'}`,
            body: shortText(evt.result_preview, 260),
            meta: [evt.task_id ? `task=${evt.task_id}` : '', formatLogDuration(evt.duration_sec)].filter(Boolean),
        };
    }

    if (t === 'tool_call_timeout' || t === 'tool_timeout') {
        return {
            ...base,
            phase: 'timeout',
            headline: `${evt.tool || 'tool'} timed out`,
            body: compactJson(evt.args, 220),
            meta: [evt.task_id ? `task=${evt.task_id}` : '', evt.timeout_sec ? `limit ${evt.timeout_sec}s` : '', formatLogDuration(evt.duration_sec)].filter(Boolean),
        };
    }

    if (t === 'tool_call' || evt.tool) {
        return {
            ...base,
            phase: 'result',
            headline: `${evt.tool || 'tool'} result`,
            body: shortText(evt.result_preview || compactJson(evt.args, 220), 260),
            meta: [evt.task_id ? `task=${evt.task_id}` : ''].filter(Boolean),
        };
    }

    if (t === 'task_metrics_event' || t === 'task_eval') {
        return {
            ...base,
            phase: 'metrics',
            headline: 'Task metrics',
            meta: [
                evt.task_id ? `task=${evt.task_id}` : '',
                evt.task_type || '',
                formatLogDuration(evt.duration_sec),
                evt.tool_calls != null ? `${evt.tool_calls} tools` : '',
                evt.tool_errors ? `${evt.tool_errors} errors` : '',
                evt.response_len ? `${evt.response_len} chars` : '',
            ].filter(Boolean),
        };
    }

    if (t === 'task_done') {
        return {
            ...base,
            phase: 'done',
            headline: `Finished ${evt.task_type || 'task'}`,
            meta: [
                evt.task_id ? `task=${evt.task_id}` : '',
                formatLogMoney(evt.cost_usd || evt.cost),
                evt.total_rounds ? `${evt.total_rounds} rounds` : '',
                formatLogTokens(evt),
            ].filter(Boolean),
        };
    }

    if (t === 'startup_verification') {
        return {
            ...base,
            phase: Number(evt.issues_count || 0) > 0 ? 'warn' : 'ok',
            headline: 'Startup verification',
            body: describeStartupChecks(evt.checks),
            meta: [
                evt.git_sha ? String(evt.git_sha).slice(0, 8) : '',
                `${evt.issues_count || 0} issues`,
            ].filter(Boolean),
        };
    }

    if (t === 'worker_spawn_start') {
        return {
            ...base,
            phase: 'start',
            headline: `Spawning ${evt.count || '?'} workers`,
            meta: [evt.start_method || ''].filter(Boolean),
        };
    }

    if (t === 'worker_sha_verify') {
        return {
            ...base,
            phase: evt.ok ? 'ok' : 'warn',
            headline: evt.ok ? 'Worker SHA verified' : 'Worker SHA mismatch',
            meta: [
                evt.expected_sha ? `exp ${String(evt.expected_sha).slice(0, 8)}` : '',
                evt.observed_sha ? `got ${String(evt.observed_sha).slice(0, 8)}` : '',
                evt.worker_pid ? `pid ${evt.worker_pid}` : '',
            ].filter(Boolean),
        };
    }

    if (t === 'worker_boot') {
        return {
            ...base,
            phase: 'boot',
            headline: 'Worker booted',
            meta: [
                evt.pid ? `pid ${evt.pid}` : '',
                evt.git_sha ? String(evt.git_sha).slice(0, 8) : '',
            ].filter(Boolean),
        };
    }

    if (t === 'deps_sync_ok') {
        return {
            ...base,
            phase: 'ok',
            headline: 'Dependencies in sync',
            meta: [evt.reason || '', shortText(evt.source, 60)].filter(Boolean),
        };
    }

    if (t === 'reset_unsynced_rescued_then_reset') {
        return {
            ...base,
            phase: 'warn',
            headline: 'Recovered dirty worktree before restart',
            meta: [
                evt.reason || '',
                evt.dirty_count != null ? `${evt.dirty_count} dirty` : '',
                evt.unpushed_count != null ? `${evt.unpushed_count} unpushed` : '',
            ].filter(Boolean),
        };
    }

    if (t === 'task_checkpoint') {
        const cpNum = evt.checkpoint_number || Math.floor((evt.round || 0) / 15);
        return {
            ...base,
            phase: 'thinking',
            headline: `Checkpoint ${cpNum}`,
            meta: [
                evt.task_id ? `task=${evt.task_id}` : '',
                evt.round ? `r${evt.round}` : '',
                evt.context_tokens ? `~${evt.context_tokens} tok` : '',
                formatLogMoney(evt.task_cost),
            ].filter(Boolean),
        };
    }

    if (t.includes('error') || t.includes('crash') || t.includes('fail')) {
        return {
            ...base,
            phase: 'error',
            headline: t,
            body: shortText(evt.error || evt.result_preview || evt.text || '', 260),
            meta: [evt.task_id ? `task=${evt.task_id}` : '', evt.tool ? `tool=${evt.tool}` : ''].filter(Boolean),
        };
    }

    return {
        ...base,
        phase: 'info',
        headline: shortText(t, 120),
        body: shortText(
            evt.text || evt.error || evt.result_preview || compactJson(evt.args || evt.task || evt.checks, 260),
            260,
        ),
        meta: [
            evt.task_id ? `task=${evt.task_id}` : '',
            evt.model || '',
            formatLogMoney(evt.cost_usd || evt.cost),
        ].filter(Boolean),
    };
}

export function summarizeChatLiveEvent(evt) {
    const t = evt.type || evt.event || 'unknown';
    const progressText = describeText(String(evt.content || evt.text || '').replace(/^💬\s*/, ''), 240);

    if (evt.is_progress || t === 'send_message') {
        const lifecycleTerminal = String(evt.task_id || '').startsWith('skill_lifecycle_')
            && /\s—\s(completed|failed)\b/i.test(progressText.full);
        return {
            phase: evt.task_id === 'bg-consciousness'
                ? 'thinking'
                : (lifecycleTerminal ? (/failed\b/i.test(progressText.full) ? 'lifecycle_error' : 'done') : 'working'),
            headline: progressText.preview || 'Working...',
            fullHeadline: progressText.full || '',
            body: '',
            visible: Boolean(progressText.preview),
            promote: true,
            human: true,
            dedupeKey: progressText.full ? `progress:${progressText.full}` : `progress:${evt.task_id || ''}`,
        };
    }

    if (t === 'task_started' || t === 'task_received') {
        return {
            phase: 'working',
            headline: 'Working on it',
            body: '',
            visible: false,
            promote: true,
            human: false,
            dedupeKey: `${t}:${getLogTaskGroupId(evt)}`,
        };
    }

    if (t === 'context_building_started') {
        return {
            phase: 'working',
            headline: 'Getting ready',
            body: '',
            visible: false,
            promote: true,
            human: false,
            dedupeKey: `${t}:${getLogTaskGroupId(evt)}`,
        };
    }

    if (t === 'context_building_finished') {
        return {
            phase: 'working',
            headline: 'Looking through the context',
            body: '',
            visible: false,
            promote: false,
            human: false,
            dedupeKey: `${t}:${getLogTaskGroupId(evt)}`,
        };
    }

    if (t === 'task_heartbeat') {
        return {
            phase: 'working',
            headline: 'Still working',
            body: '',
            visible: false,
            promote: false,
            human: false,
            dedupeKey: `${t}:${getLogTaskGroupId(evt)}:${evt.phase || ''}`,
        };
    }

    if (t === 'llm_round_started') {
        return {
            phase: 'thinking',
            headline: 'Thinking',
            body: '',
            visible: false,
            promote: false,
            human: false,
            dedupeKey: `${t}:${getLogTaskGroupId(evt)}:${evt.round || ''}:${evt.attempt || ''}`,
        };
    }

    if (t === 'tool_call_started') {
        return {
            phase: 'working',
            headline: 'Working through the next step',
            body: '',
            visible: false,
            promote: false,
            human: false,
            dedupeKey: `${t}:${getLogTaskGroupId(evt)}:${evt.tool || ''}`,
        };
    }

    if (t === 'task_checkpoint') {
        // Not visible in chat live card — the emit_progress message is the visible source
        // for the chat timeline (avoids duplicate timeline entries). This event remains
        // visible in the Logs tab via summarizeLogEvent.
        const cpNum = evt.checkpoint_number || Math.floor((evt.round || 0) / 15);
        return {
            phase: 'thinking',
            headline: `Checkpoint ${cpNum} — periodic self-check`,
            body: '',
            visible: false,
            promote: false,
            human: false,
            dedupeKey: `${t}:${getLogTaskGroupId(evt)}:${cpNum}`,
        };
    }

    if (t === 'llm_round_error' || t === 'llm_api_error') {
        const errorText = describeText(evt.error, 220);
        return {
            phase: 'error',
            headline: 'Ran into an issue while thinking',
            body: errorText.preview,
            fullBody: errorText.full,
            visible: true,
            promote: true,
            human: false,
            dedupeKey: `${t}:${getLogTaskGroupId(evt)}:${evt.round || ''}`,
        };
    }

    if (t === 'tool_call_timeout' || t === 'tool_timeout') {
        return {
            phase: 'error',
            headline: 'One of the steps took too long',
            body: '',
            visible: true,
            promote: true,
            human: false,
            dedupeKey: `${t}:${getLogTaskGroupId(evt)}:${evt.tool || ''}`,
        };
    }

    if (t === 'tool_call_finished' && evt.is_error) {
        const commandText = describeText(extractCommandText(evt.args), 120);
        const errorResult = describeText(evt.result_preview || evt.error, 220);
        if (evt.status === 'non_zero_exit') {
            const exitCode = Number(evt.exit_code);
            const exitLabel = Number.isFinite(exitCode)
                ? `exit code ${exitCode}`
                : 'a non-zero exit code';
            const bodyParts = [];
            const fullBodyParts = [];
            if (commandText.preview) bodyParts.push(`Command: ${commandText.preview}`);
            if (errorResult.preview) bodyParts.push(errorResult.preview);
            if (commandText.full) fullBodyParts.push(`Command: ${commandText.full}`);
            if (errorResult.full) fullBodyParts.push(errorResult.full);
            return {
                phase: 'warn',
                headline: `A command returned ${exitLabel}`,
                body: shortText(bodyParts.join(' '), 220),
                fullBody: fullBodyParts.join('\n\n'),
                visible: true,
                promote: false,
                human: false,
                dedupeKey: `${t}:${getLogTaskGroupId(evt)}:${evt.tool || ''}:${evt.status || ''}:${evt.exit_code || ''}:${commandText.full || errorResult.full}`,
            };
        }
        const bodyParts = [];
        const fullBodyParts = [];
        if (commandText.preview) bodyParts.push(`Command: ${commandText.preview}`);
        if (errorResult.preview) bodyParts.push(errorResult.preview);
        if (commandText.full) fullBodyParts.push(`Command: ${commandText.full}`);
        if (errorResult.full) fullBodyParts.push(errorResult.full);
        return {
            phase: 'error',
            headline: 'One of the steps failed',
            body: shortText(bodyParts.join(' '), 220),
            fullBody: fullBodyParts.join('\n\n'),
            visible: true,
            promote: true,
            human: false,
            dedupeKey: `${t}:${getLogTaskGroupId(evt)}:${evt.tool || ''}:${evt.status || ''}:${evt.exit_code || ''}:${commandText.full || errorResult.full}`,
        };
    }

    if (t === 'task_done') {
        return {
            phase: 'done',
            headline: 'Done',
            body: '',
            visible: true,
            promote: true,
            human: false,
            dedupeKey: `${t}:${getLogTaskGroupId(evt)}`,
        };
    }

    if (t.includes('error') || t.includes('crash') || t.includes('fail')) {
        const genericError = describeText(evt.error || evt.result_preview || evt.text || '', 220);
        return {
            phase: 'error',
            headline: 'Ran into an issue',
            body: genericError.preview,
            fullBody: genericError.full,
            visible: true,
            promote: true,
            human: false,
            dedupeKey: `${t}:${getLogTaskGroupId(evt)}`,
        };
    }

    return {
        phase: 'working',
        headline: 'Working...',
        body: '',
        visible: false,
        promote: false,
        human: false,
        dedupeKey: `${t}:${getLogTaskGroupId(evt)}`,
    };
}

export function duplicateLogEventKey(evt) {
    const t = evt.type || evt.event || '';
    if (t === 'startup_verification') return `${t}:${evt.git_sha || ''}:${evt.issues_count || 0}`;
    if (t === 'worker_sha_verify') return `${t}:${evt.expected_sha || ''}:${evt.observed_sha || ''}:${evt.ok ? 1 : 0}`;
    if (t === 'deps_sync_ok') return `${t}:${evt.reason || ''}:${evt.source || ''}`;
    return '';
}

export function prettyLogEvent(evt) {
    try {
        return JSON.stringify(evt, null, 2);
    } catch {
        return String(evt);
    }
}

export function getLogTaskGroupId(evt) {
    if (evt.task_id) return String(evt.task_id);
    const task = evt.task;
    if (task && typeof task === 'object' && task.id) return String(task.id);
    return '';
}

export function isGroupedTaskEvent(evt) {
    const groupId = getLogTaskGroupId(evt);
    if (!groupId) return false;
    const t = evt.type || evt.event || '';
    return (
        evt.is_progress
        || t.startsWith('task_')
        || t.startsWith('llm_')
        || t.startsWith('tool_')
        || t === 'context_building_started'
        || t === 'context_building_finished'
        || t === 'send_message'
    );
}
