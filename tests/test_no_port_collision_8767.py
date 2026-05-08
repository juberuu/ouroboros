from ouroboros.host_service_api import DEFAULT_HOST_SERVICE_PORT


def test_host_service_port_does_not_collide_with_local_model_default() -> None:
    assert DEFAULT_HOST_SERVICE_PORT != 8766
    assert DEFAULT_HOST_SERVICE_PORT == 8767
