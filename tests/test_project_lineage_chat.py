"""v6.37.0 guard (C4.4): a project's chat must include its whole subagent TREE.
project_chat_for_task_tree resolves membership by lineage (own binding -> parent ->
root) so a subagent (never bound itself) routes to its root project's thread — the
cyber-racing 'subagents vanished from the project chat' gap."""


def test_project_chat_for_task_tree_inherits_from_root(monkeypatch, tmp_path):
    from ouroboros import projects_registry as pr

    bound = {"root": 9, "mid": 0}
    monkeypatch.setattr(pr, "project_chat_for_task", lambda dr, tid: bound.get(tid, 0))

    # own binding wins
    assert pr.project_chat_for_task_tree(tmp_path, "root") == 9
    # a child with no own/parent binding inherits from its root
    assert pr.project_chat_for_task_tree(tmp_path, "child", parent_task_id="mid", root_task_id="root") == 9
    # a child inherits from a bound PARENT before the root
    bound["mid"] = 5
    assert pr.project_chat_for_task_tree(tmp_path, "child", parent_task_id="mid", root_task_id="root") == 5
    # nothing in the lineage is bound -> 0 (stays in main chat)
    assert pr.project_chat_for_task_tree(tmp_path, "x", parent_task_id="y", root_task_id="z") == 0
