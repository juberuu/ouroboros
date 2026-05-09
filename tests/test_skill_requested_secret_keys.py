from ouroboros.skill_loader import requested_core_setting_keys


def test_skill_requested_custom_secret_keys_are_grantable_when_absent():
    assert requested_core_setting_keys(["TELEGRAM_BOT_TOKEN", "SLACK_WEBHOOK_URL"]) == [
        "TELEGRAM_BOT_TOKEN",
        "SLACK_WEBHOOK_URL",
    ]


def test_ouroboros_internal_settings_are_not_skill_grantable():
    assert "OUROBOROS_RUNTIME_MODE" not in requested_core_setting_keys(["OUROBOROS_RUNTIME_MODE"])
