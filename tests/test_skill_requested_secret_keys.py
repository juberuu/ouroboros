from ouroboros.skill_loader import requested_core_setting_keys


def test_known_transport_secret_is_grantable_when_absent():
    assert requested_core_setting_keys(["TELEGRAM_BOT_TOKEN", "SLACK_WEBHOOK_URL"]) == [
        "TELEGRAM_BOT_TOKEN",
    ]


def test_ouroboros_internal_settings_are_not_skill_grantable():
    assert "OUROBOROS_RUNTIME_MODE" not in requested_core_setting_keys(["OUROBOROS_RUNTIME_MODE"])
