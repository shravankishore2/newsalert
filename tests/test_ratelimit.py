from newsalert.ratelimit import RateLimiter


class FakeClock:
    def __init__(self):
        self.t = 0.0
        self.sleeps = []

    def __call__(self):
        return self.t

    async def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


async def test_enforces_per_second_and_per_minute_windows():
    c = FakeClock()
    rl = RateLimiter([(3, 1.0), (5, 60.0)], clock=c, sleep=c.sleep)
    times = []
    for _ in range(7):
        await rl.acquire()
        times.append(c.t)
    assert times[:3] == [0, 0, 0]          # burst up to 3/s
    assert times[3] == 1.0 and times[4] == 1.0
    assert times[5] == 60.0 and times[6] == 60.0  # minute window full after 5
    # never more than 5 in any 60 s window
    for i, t in enumerate(times):
        assert sum(1 for u in times if t - 60 < u <= t) <= 5


async def test_pause_blocks_callers():
    c = FakeClock()
    rl = RateLimiter([(100, 1.0)], clock=c, sleep=c.sleep)
    rl.pause(30)
    await rl.acquire()
    assert c.t == 30
