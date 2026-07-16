import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import {
    formatReviewProjection,
    taskOutcomeSeverity,
} from '../modules/log_events.js';

test('shared task severity never paints degraded or best-effort outcomes green', () => {
    const clean = {
        outcome_axes: {
            lifecycle: { status: 'completed' }, execution: { status: 'ok' },
            objective: { status: 'pass' }, review: { status: 'pass' },
            artifacts: { status: 'ready' },
        },
    };
    assert.equal(taskOutcomeSeverity(clean), 'done');
    assert.equal(taskOutcomeSeverity({
        ...clean, outcome_axes: { ...clean.outcome_axes, objective: { status: 'best_effort' } },
    }), 'warn');
    assert.equal(taskOutcomeSeverity({
        ...clean, outcome_axes: { ...clean.outcome_axes, objective: { status: 'degraded' } },
    }), 'warn');
    assert.equal(taskOutcomeSeverity({
        ...clean, outcome_axes: { ...clean.outcome_axes, review: { status: 'degraded' } },
    }), 'warn');
    assert.equal(taskOutcomeSeverity({
        ...clean, outcome_axes: { ...clean.outcome_axes, review: { status: 'fail' } },
    }), 'error');
    assert.equal(taskOutcomeSeverity({
        ...clean,
        outcome_axes: {
            ...clean.outcome_axes,
            execution: { status: 'degraded', reason_code: 'review_skipped_deadline_reserve' },
            objective: { status: 'degraded', source: 'task_acceptance_deadline_reserve' },
            review: {
                status: 'skipped', eligibility: 'eligible', run_count: 0,
                acceptance_decision: { status: 'review_skipped_deadline_reserve' },
            },
        },
    }), 'warn');
});

test('review projection exposes panel binding and every actor without raw output', () => {
    const text = formatReviewProjection({ panels: [{
        panel_id: 'panel_abc', surface: 'task_acceptance', authority: 'host_root',
        aggregate_signal: 'DEGRADED', transport_status: 'partial', parse_status: 'malformed',
        quorum: { required: 2, contributed: 1, configured: 3 },
        enforcement_impact: 'degrades_completion', reason: 'one actor malformed',
        candidate_hash: 'candidate', evidence_revision: 'evidence', fence_hash: 'fence-hash',
        actors: [
            {
                slot_id: 'slot_1', actor_role: 'task acceptance', provider: 'anthropic',
                model: 'anthropic/claude-fable-5', transport_status: 'success',
                parse_status: 'valid', semantic_verdict: 'DEGRADED',
                outcome_tier: 'best_effort',
                quorum_contribution: false, enforcement_impact: 'abstains', reason: 'uncertain',
            },
            {
                slot_id: 'slot_2', actor_role: 'task acceptance', provider: 'openai',
                model: 'openai/gpt-5.6-sol', transport_status: 'timeout',
                parse_status: 'malformed', semantic_verdict: '',
                quorum_contribution: false, enforcement_impact: 'abstains', reason: 'timeout',
            },
        ],
    }] });
    assert.match(text, /panel_abc/);
    assert.match(text, /candidate_hash=candidate/);
    assert.match(text, /fence_hash=fence-hash/);
    assert.match(text, /slot_1.*claude-fable-5.*verdict=DEGRADED/);
    assert.match(text, /slot_1.*outcome_tier=best_effort/);
    assert.match(text, /slot_2.*transport=timeout.*parse=malformed/);
    assert.doesNotMatch(text, /raw_text/);
});

test('mixed panel renders quorum participation separately from pass support and veto', () => {
    const text = formatReviewProjection({ panels: [{
        panel_id: 'panel_mixed', surface: 'task_acceptance', authority: 'host_root',
        aggregate_signal: 'FAIL', transport_status: 'success', parse_status: 'valid',
        quorum: { required: 2, contributed: 3, configured: 3 },
        enforcement_impact: 'requires_revision', reason: 'one valid reviewer vetoed',
        actors: [
            {
                slot_id: 'slot_1', actor_role: 'task acceptance', provider: 'anthropic',
                model: 'anthropic/claude-fable-5', transport_status: 'success',
                parse_status: 'valid', semantic_verdict: 'PASS',
                quorum_contribution: true, enforcement_impact: 'supports_pass', reason: 'ready',
            },
            {
                slot_id: 'slot_2', actor_role: 'task acceptance', provider: 'openai',
                model: 'openai/gpt-5.6-sol', transport_status: 'success',
                parse_status: 'valid', semantic_verdict: 'PASS',
                quorum_contribution: true, enforcement_impact: 'supports_pass', reason: 'ready',
            },
            {
                slot_id: 'slot_3', actor_role: 'task acceptance', provider: 'google',
                model: 'google/gemini-3.5-flash', transport_status: 'success',
                parse_status: 'valid', semantic_verdict: 'FAIL',
                quorum_contribution: true, enforcement_impact: 'veto', reason: 'gap',
            },
        ],
    }] });
    assert.match(text, /panel_mixed.*verdict=FAIL.*quorum=3\/3 \(required 2\)/);
    assert.match(text, /slot_1.*verdict=PASS.*quorum=contributes.*enforcement=supports_pass/);
    assert.match(text, /slot_3.*verdict=FAIL.*quorum=contributes.*enforcement=veto/);
});

test('subagent terminal routing preserves the compact review projection', () => {
    const source = readFileSync(new URL('../modules/chat.js', import.meta.url), 'utf8');
    assert.match(source, /review_projection:\s*evt\.review_projection/);
    assert.match(source, /const reviewDetails = formatReviewProjection\(evt\.review_projection\)/);
    assert.match(source, /body:\s*reviewDetails/);
    assert.match(source, /expandByDefault:\s*Boolean\(reviewDetails\)/);
});
