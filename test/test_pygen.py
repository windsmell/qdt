from unittest import (
    TestCase,
    main
)
from examples import (
    Q35Project_2_6_0
)
from common import (
    notifier,
    ee,
    intervalmap,
    same,
    PyGenVisitor
)
from traceback import (
    format_exc
)
from collections import (
    namedtuple
)
import qdt
from os.path import (
    join,
    dirname
)


PYGEN_VERBOSE = ee("PYGEN_VERBOSE")


verbose_dir = join(dirname(__file__), "pygen_code")


class PyGeneratorTestHelper(object):

    def test(self):
        self._generator = gen = PyGenVisitor(self._original).visit().gen
        buf = gen.w
        code = buf.getvalue()
        res = {}
        try:
            exec(code, self._namespace, res)
        except:
            print("\nError in generated code:\n" + format_exc())
            eq = False
        else:
            try:
                loaded = res[self._generator.nameof(self._original)]
                eq = same(loaded, self._original)
            except:
                print("\nLoaded object is bad:\n" + format_exc())
                eq = False

        if PYGEN_VERBOSE or not eq:
            code_file = join(verbose_dir, type(self).__name__ + ".py")
            # save serialized code to the file for developer
            with open(code_file, "w") as f:
                f.write(code)

        self.assertTrue(eq, "Loaded object differs.")


class TestDict(TestCase, PyGeneratorTestHelper):

    def setUp(self):
        self._namespace = {}
        self._original = {}


class TestNestedDict(TestCase, PyGeneratorTestHelper):

    def setUp(self):
        self._namespace = {
            "intervalmap" : intervalmap
        }
        self._original = dict(
            a = dict(
                im = intervalmap((
                    ((0, 10), 'a'),
                    ((10, 20), 'b')
                ))
            )
        )


class CustomDict(dict):

    def __same__(self, o):
        if type(self) is type(o):
            return super(CustomDict, self).__eq__(o)
        return False


class CustomList(list):

    def __same__(self, o):
        if type(self) is type(o):
            return super(CustomList, self).__eq__(o)
        return False


class CustomSet(set):

    def __same__(self, o):
        if type(self) is type(o):
            return super(CustomSet, self).__eq__(o)
        return False


class CustomTuple(namedtuple("CustomTuple", "inner a b")):

    def __same__(self, o):
        if type(self) is type(o):
            return super(CustomTuple, self).__eq__(o)
        return False


class TestCustomDict(TestCase, PyGeneratorTestHelper):

    def setUp(self):
        self._namespace = {
            "CustomDict": CustomDict
        }
        self._original = CustomDict()


class TestCustomList(TestCase, PyGeneratorTestHelper):

    def setUp(self):
        self._namespace = {
            "CustomList": CustomList
        }
        self._original = CustomList()


class TestCustomSet(TestCase, PyGeneratorTestHelper):

    def setUp(self):
        self._namespace = {
            "CustomSet": CustomSet
        }
        self._original = CustomSet()


class TestCustomTuple(TestCase, PyGeneratorTestHelper):

    def setUp(self):
        self._namespace = {
            "CustomTuple": CustomTuple
        }
        self._original = CustomTuple(CustomTuple(None, 0, 0), 1, 2)


class TestCustomNestedObjescts(TestCase, PyGeneratorTestHelper):
    def setUp(self):
        self._namespace = {
            "intervalmap" : intervalmap,
            "CustomDict" : CustomDict,
            "CustomList" : CustomList,
            "CustomSet" : CustomSet,
            "CustomTuple" : CustomTuple
        }
        self._original = CustomDict(
            a = CustomDict(
                im = intervalmap((
                    ((0, 10), 'a'),
                    ((10, 20), 'b')
                )),
                l = CustomList([
                    CustomList([
                        CustomSet(),
                        CustomTuple(None, 0, 0)
                    ])
                ]),
                s = CustomSet(),
                t = CustomTuple(None, -1, -1)
            )
        )


class TestQ35(TestCase, PyGeneratorTestHelper):

    def setUp(self):
        self._namespace = dict(qdt.__dict__)
        self._original = Q35Project_2_6_0()


@notifier("event")
class ANotifier(object):

    def __init__(self, arg, kwarg = None):
        self.arg = arg
        self.kwarg = kwarg

    def __gen_code__(self, g):
        g.gen_code(self)

    def __same__(self, o):
        return self.arg == o.arg and self.kwarg == self.kwarg


class TestNotifier(TestCase, PyGeneratorTestHelper):

    def setUp(self):
        self._original = ANotifier("a value", kwarg = "another value")
        self._namespace = dict(ANotifier = ANotifier)


if __name__ == "__main__":
    main()
