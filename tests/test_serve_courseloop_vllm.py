import scripts.serve_courseloop_vllm as server


def test_vllm_command_loads_one_base_and_every_selected_lora(monkeypatch):
    monkeypatch.setattr(server, "build", lambda _root: {
        "base_model": "/models/qwen3-14b",
        "adapters": {
            "courseloop-default": "/loras/default",
            "courseloop-grounded-qa": "/loras/qa",
        },
        "routes": {},
        "env": {},
    })

    command, routes = server.command(8123, "0", "/env/bin/vllm")

    assert command[:3] == ["/env/bin/vllm", "serve", "/models/qwen3-14b"]
    modules = command[command.index("--lora-modules") + 1:
                      command.index("--max-lora-rank")]
    assert modules == [
        "courseloop-default=/loras/default",
        "courseloop-grounded-qa=/loras/qa",
    ]
    assert command[command.index("--max-lora-rank") + 1] == "16"
    assert command[command.index("--max-cpu-loras") + 1] == "2"
    assert command[command.index("--port") + 1] == "8123"
    assert routes["adapters"]["courseloop-grounded-qa"] == "/loras/qa"


def test_vllm_command_keeps_capacity_for_future_dynamic_adapter(monkeypatch):
    monkeypatch.setattr(server, "build", lambda _root: {
        "base_model": "/models/qwen3-14b",
        "adapters": {"courseloop-default": "/loras/default"},
        "routes": {},
        "env": {},
    })

    command, _ = server.command(8003, "0", "vllm")

    assert command[command.index("--max-cpu-loras") + 1] == "2"
    assert command[command.index("--max-loras") + 1] == "1"
