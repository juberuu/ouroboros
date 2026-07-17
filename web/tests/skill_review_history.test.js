import assert from 'node:assert/strict';
import test from 'node:test';

import { renderInstalledSkillCard } from '../modules/skill_card_renderer.js';

test('skill card shows current review round and collapses only the last ten group rows', () => {
    const history = Array.from({ length: 10 }, (_, idx) => ({
        status: 'clean',
        content_hash: `snapshot-${idx}`,
        group_id: 'manual:alpha',
        review_round: idx + 3,
        snapshot_attempt: 1,
        snapshot_revised: idx === 9,
        raw_actor_records: [{ raw_text: 'must stay private' }],
    }));
    const skill = {
        name: 'alpha',
        type: 'instruction',
        version: '1.0.0',
        description: 'test',
        source: 'external',
        enabled: false,
        review_status: 'clean',
        review_gate: { executable_review: true },
        review_findings: [],
        permissions: [],
        grants: {},
        skill_review: {
            current: history[history.length - 1],
            history,
        },
    };

    const html = renderInstalledSkillCard(skill);

    assert.match(html, /Skill review round 12 — snapshot snapshot-9 \(attempt 1\) — revised snapshot/);
    assert.match(html, /<details class="skills-review-history ui-rich-content">/);
    assert.match(html, /Skill Review history \(10\)/);
    assert.doesNotMatch(html, /must stay private/);
});
