from portaria import network


def found(*addresses):
    return [(2, 2, 17, "", (address, 0)) for address in addresses]


def test_the_address_that_reaches_other_networks_comes_first(monkeypatch):
    monkeypatch.setattr(network.socket, "getaddrinfo", lambda *args: found("10.0.0.5", "127.0.0.1", "192.168.0.19"))
    monkeypatch.setattr(network, "_main_ipv4", lambda: "192.168.0.19")
    assert network.local_ipv4() == ["192.168.0.19", "10.0.0.5"]


def test_without_a_network_there_are_no_addresses(monkeypatch):
    monkeypatch.setattr(network.socket, "getaddrinfo", lambda *args: found("127.0.0.1"))
    monkeypatch.setattr(network, "_main_ipv4", lambda: None)
    assert network.local_ipv4() == []
