from types import SimpleNamespace
from unittest.mock import patch

import pytest

import project_relay.cli as cli
import project_relay.core as core


def config_fixture():
    return {
        "watchdog": {
            "ollama_url":
                "http://127.0.0.1:11434",
            "voters": [
                {
                    "name": "local_llm",
                    "model": "qwen3.5:4b",
                    "enabled": True,
                    "role": "progress_judge",
                },
                {
                    "name": "logic_model",
                    "model": "qwen3:1.7b",
                    "enabled": True,
                    "role": "logic_judge",
                },
            ],
        },
    }


def args(
    local_llm=None,
    logic_model=None,
    ollama_url=None,
):
    return SimpleNamespace(
        local_llm=local_llm,
        logic_model=logic_model,
        ollama_url=ollama_url,
    )


def test_models_rejects_missing_model_without_save():
    config = config_fixture()

    with (
        patch.object(
            cli,
            "load_config",
            return_value=config,
        ),
        patch.object(
            cli,
            "save_config",
        ) as save,
        patch.object(
            cli,
            "list_ollama_models",
            return_value=[
                "qwen3.5:4b",
                "qwen3:1.7b",
            ],
        ),
    ):
        with pytest.raises(
            core.RelayError,
        ):
            cli.cmd_models(
                args(
                    logic_model=
                        "YOUR_LOGIC_MODEL",
                )
            )

    save.assert_not_called()


def test_models_accepts_installed_model_and_saves():
    config = config_fixture()

    with (
        patch.object(
            cli,
            "load_config",
            return_value=config,
        ),
        patch.object(
            cli,
            "save_config",
        ) as save,
        patch.object(
            cli,
            "list_ollama_models",
            return_value=[
                "qwen3.5:4b",
                "qwen3:1.7b",
                "llama3.2:3b",
            ],
        ),
    ):
        rc = cli.cmd_models(
            args(
                logic_model=
                    "llama3.2:3b",
            )
        )

    assert rc == 0

    assert (
        config["watchdog"]
        ["voters"][1]
        ["model"]
        == "llama3.2:3b"
    )

    save.assert_called_once()


def test_models_read_only_does_not_save_or_query():
    config = config_fixture()

    with (
        patch.object(
            cli,
            "load_config",
            return_value=config,
        ),
        patch.object(
            cli,
            "save_config",
        ) as save,
        patch.object(
            cli,
            "list_ollama_models",
        ) as inventory,
    ):
        rc = cli.cmd_models(
            args()
        )

    assert rc == 0

    save.assert_not_called()
    inventory.assert_not_called()
