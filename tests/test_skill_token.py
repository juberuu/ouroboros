import copy
import pickle

import pytest

from ouroboros.skill_token import SkillToken


def test_skill_token_redacts_string_forms() -> None:
    token = SkillToken("secret-token")

    assert "secret-token" not in repr(token)
    assert "secret-token" not in str(token)
    assert "secret-token" not in f"{token}"
    assert token.use_in_request() == "secret-token"


def test_skill_token_blocks_serialization_and_copy() -> None:
    token = SkillToken("secret-token")

    with pytest.raises(TypeError):
        pickle.dumps(token)
    with pytest.raises(Exception):
        copy.copy(token)
    with pytest.raises(Exception):
        copy.deepcopy(token)
