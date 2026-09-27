"""Fixture core."""
import json
import os.path as osp
from . import util
from .util import helper as hp, CONST
from .sub.deep import deep_fn


def top():
    return hp(1)


def plain():
    top()
    util.helper(2)
    deep_fn()
    json.dumps({})
    len([])


class Base:
    def ping(self):
        return 1


class Engine(Base):
    def __init__(self, root):
        self.path = root + "/data/events.jsonl"

    def run(self):
        self.step()
        self.ping()
        Engine.step(self)
        other = Engine("x")
        other.step()
        self.missing()
        return [top() for _ in range(2)]

    def step(self):
        def inner():
            return plain()
        return inner()

    def load(self):
        return self.path.read_text()

    def save(self, text):
        self.path.write_text(text)

    @staticmethod
    def deco():
        pass

    async def aio(self):
        await self.run()


def writer(p):
    with open("out.json", "w") as f:
        f.write("x")
    open("in.txt").read()
