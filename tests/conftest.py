from __future__ import annotations

from typing import Iterator, List

import pytest

from stub_server import StubServer
from zebracat import Zebracat


@pytest.fixture
def stub() -> Iterator[StubServer]:
    with StubServer() as server:
        yield server


class FakeTime:
    """Replaces the client's sleep and clock: sleeping only moves the clock."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: List[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def clock(self) -> float:
        return self.now


@pytest.fixture
def fake_time() -> FakeTime:
    return FakeTime()


@pytest.fixture
def client(stub: StubServer, fake_time: FakeTime) -> Iterator[Zebracat]:
    c = Zebracat(api_key="sk-test", base_url=stub.url)
    c._sleep = fake_time.sleep
    c._clock = fake_time.clock
    yield c
    c.close()
