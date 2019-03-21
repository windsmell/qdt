#!/usr/bin/env python
""" QEMU CPU Testing Tool """

from sys import (
    stderr
)
from os.path import (
    dirname,
    join,
    exists,
    basename
)
from os import (
    makedirs,
    killpg,
    setpgrp
)
from signal import (
    SIGKILL
)
from argparse import (
    Action,
    ArgumentParser
)
from re import (
    compile
)
from multiprocessing import (
    Queue,
    Process
)
from subprocess import (
    Popen,
    PIPE
)
from platform import (
    machine
)
from collections import (
    defaultdict
)
from common import (
    filefilter,
    cli_repr,
    HelpFormatter,
    pypath
)
from debug import (
    InMemoryELFFile,
    DWARFInfoCache,
    Runtime
)
with pypath("pyrsp"):
    from pyrsp.rsp import (
        archmap
    )
    from pyrsp.utils import (
        find_free_port
    )
from c2t import (
    DebugCommandExecutor,
    C2TConfig,
    Run,
    get_new_rsp,
    DebugClient,
    DebugServer,
    TestBuilder
)

C2T_ERRMSG_FORMAT = "{prog}:\x1b[31m error:\x1b[0m {msg}\n"


def c2t_exit(msg, prog = __file__):
    print(C2T_ERRMSG_FORMAT.format(
        prog = basename(prog),
        msg = msg
    ))
    killpg(0, SIGKILL)


C2T_DIR = dirname(__file__) or '.'
C2T_CONFIGS_DIR = join(C2T_DIR, "c2t", "configs")
C2T_TEST_DIR = join(C2T_DIR, "c2t", "tests")
C2T_TEST_IR_DIR = join(C2T_TEST_DIR, "ir")
C2T_TEST_BIN_DIR = join(C2T_TEST_DIR, "bin")

ORACLE_CPU = machine()

c2t_cfg = None


class DebugSession(object):
    """ This class manages debugging session """

    def __init__(self, target, srcfile, port, elffile, queue, verbose):
        super(DebugSession, self).__init__()
        self.rsp = target(port, elffile,
            verbose = verbose
        )
        self.port = port
        self.queue = queue
        self.reset(srcfile, elffile)
        self.session_type = None

    def reset(self, srcfile, elffile):
        self.srcfile = srcfile
        self.elffile = elffile
        self.elf = InMemoryELFFile(elffile)
        di = self.elf.get_dwarf_info()
        dic = DWARFInfoCache(di,
            symtab = self.elf.get_section_by_name(".symtab")
        )
        if dic.aranges is None:
            dic.account_all_subprograms()
        self.rt = Runtime(self.rsp, dic)
        self.addr2line = {}
        self.ch_line2var = defaultdict(list)
        self.chc_line2var = defaultdict(list)

    def set_br_by_line(self, lineno, cb):
        line_map = self.rt.dic.find_line_map(basename(self.srcfile))
        line_descs = line_map[lineno]
        for desc in line_descs:
            # TODO: set a breakpoint at one address by line number?
            # if desc.state.is_stmt:
            addr = self.rt.target.reg_fmt % desc.state.address
            self.addr2line[addr] = lineno
            self.rt.add_br(addr, cb)
                # break

    def _execute_debug_comment(self):
        lineno = 1

        with open(self.srcfile, 'r') as f:
            re_comment = compile("^.*//\$(.+)$")
            re_command = compile("([^, ]+)+")
            for line in f:
                mi = re_comment.match(line)
                if mi:
                    commands = re_command.findall(mi.group(1))
                    glob = DebugCommandExecutor(locals(), lineno)
                    exec('\n'.join(commands), glob)
                lineno += 1

    @property
    def _var_size(self):
        re_size = compile("^.+_(?:u?(\d+))_.+$")
        size_str = re_size.match(basename(self.srcfile)).group(1)
        return int(size_str) / 8

    def _dump_var(self, addr, lineno, var_names):
        dump = (self.session_type, self.srcfile, dict(
            elf = self.elffile,
            addr = addr,
            lineno = lineno,
            vars = dict(
                map(lambda x: (x, self.rt[x].fetch(self._var_size)),
                    var_names if var_names else self.rt
                )
            ),
            regs = self.rt.target.regs
        ))
        if self.rt.target.verbose:
            print(dump[2].values())
        self.queue.put(dump)

    def _dump(self, addr, lineno):
        dump = (self.session_type, self.srcfile, dict(
            elf = self.elffile,
            addr = addr,
            lineno = lineno,
            regs = self.rt.target.regs
        ))
        if self.rt.target.verbose:
            print(dump[2].values())
        self.queue.put(dump)

    # debugging callbacks
    def check_cb(self):
        addr = self.rt.target.regs[self.rt.target.pc_reg]
        lineno = self.addr2line[addr]

        self._dump(addr, lineno)
        self.rt.remove_br(addr, self.check_cb)

    def cycle_check_cb(self):
        addr = self.rt.target.regs[self.rt.target.pc_reg]
        lineno = self.addr2line[addr]

        self._dump(addr, lineno)

    def check_vars_cb(self):
        addr = self.rt.target.regs[self.rt.target.pc_reg]
        lineno = self.addr2line[addr]

        var_names = self.ch_line2var.get(lineno)

        self._dump_var(addr, lineno, var_names)
        self.rt.remove_br(addr, self.check_vars_cb)

    def cycle_check_vars_cb(self):
        addr = self.rt.target.regs[self.rt.target.pc_reg]
        lineno = self.addr2line[addr]

        var_names = self.chc_line2var.get(lineno)

        self._dump_var(addr, lineno, var_names)

    def finish_cb(self):
        addr = self.rt.target.regs[self.rt.target.pc_reg]
        self.rt.remove_br(addr, self.finish_cb)

        for br in self.rt.target.br.keys()[:]:
            self.rt.target.del_br(br)
        self.rt.target.exit = True
    # end debugging callbacks

    def kill(self):
        self.rt.target.send('k')

    def detach(self):
        self.rt.target.send('D')

    def port_close(self):
        self.rt.target.port.close()


class ProcessWithErrCatching(Process):

    def __init__(self, command):
        Process.__init__(self)
        self.cmd = command
        self.prog = command.split(' ')[0]

    def run(self):
        process = Popen(self.cmd,
            shell = True,
            stdout = PIPE,
            stderr = PIPE
        )
        _, err = process.communicate()
        if process.returncode != 0:
            c2t_exit(err, prog = self.prog)


def oracle_tests_run(tests_queue, port_queue, res_queue, verbose):
    pass


def target_tests_run_nonkill(tests_queue, port_queue, res_queue, verbose):
    pass


def target_tests_run_kill(tests_queue, port_queue, res_queue, verbose):
    pass


class FreePortFinder(Process):

    def __init__(self, queue, count,  start = 4321):
        super(FreePortFinder, self).__init__()
        self.port_queue = queue
        self.count = count
        self.port_start = start

    def run(self):
        start = self.port_start
        for i in range(0, self.count):
            free = find_free_port(start)
            self.port_queue.put(free)
            # TODO: overflow 0x10000
            start = free + 1


class C2TTestBuilder(Process):
    """ A helper class that builds tests """

    def __init__(self, compiler, tests, tests_tail, tests_queue, verbose):
        super(C2TTestBuilder, self).__init__()
        self.compiler = compiler
        self.tests = tests
        self.tests_tail = tests_tail
        self.tests_queue = tests_queue
        self.verbose = verbose

    def test_build(self, test_src, test_ir, test_bin):
        for run_script in self.compiler.run_script:
            cmd = run_script.format(
                src = test_src,
                ir = test_ir,
                bin = test_bin,
                c2t_dir = C2T_DIR,
                c2t_test_dir = C2T_TEST_DIR
            )
            if self.verbose:
                print(cmd)
            cmpl_unit = ProcessWithErrCatching(cmd)
            cmpl_unit.start()
            cmpl_unit.join()

    def run(self):
        for test in self.tests:
            test_name = test[:-2]
            test_src = join(C2T_TEST_DIR, test)
            test_bin = join(C2T_TEST_BIN_DIR,
                test_name + "_%s" % self.tests_tail
            )

            if not exists(test_bin):
                test_ir = join(C2T_TEST_IR_DIR, test_name)

                self.test_build(test_src, test_ir, test_bin)

            self.tests_queue.put((test_src, test_bin))


def start_cpu_testing(tests, jobs, reuse, verbose):
    oracle_tests_queue = Queue(0)
    target_tests_queue = Queue(0)

    oracle_tb = C2TTestBuilder(c2t_cfg.oracle_compiler, tests,
        ORACLE_CPU, oracle_tests_queue, verbose
    )
    target_tb = C2TTestBuilder(c2t_cfg.target_compiler, tests,
        c2t_cfg.rsp_target.march, target_tests_queue, verbose
    )

    oracle_tb.start()
    target_tb.start()

    port_queue = Queue(0)

    pf = FreePortFinder(port_queue, len(tests) * 3)

    pf.start()

    res_queue = Queue(0)

    if reuse:
        target_tests_run = target_tests_run_nonkill
    else:
        target_tests_run = target_tests_run_kill

    if jobs > len(tests):
        jobs = len(tests)

    tests_run_processes = []
    for i in range(0, jobs):
        oracle_trp = Process(
            target = oracle_tests_run,
            args = [oracle_tests_queue, port_queue, res_queue, verbose]
        )
        target_trp = Process(
            target = target_tests_run,
            args = [target_tests_queue, port_queue, res_queue, verbose]
        )
        tests_run_processes.append((oracle_trp, target_trp))
        oracle_trp.start()
        target_trp.start()

    oracle_tb.join()
    target_tb.join()
    pf.join()
    for oracle_trp, target_trp in tests_run_processes:
        oracle_trp.join()
        target_trp.join()


class testfilter(filefilter):

    def __str__(self):
        res = []
        for inclusive, pattern in self:
            res.append(("-t " if inclusive else "-s ") + cli_repr(pattern))
        return " ".join(res)


class TestfilterCLI(Action):

    def __call__(self, parser, namespace, values, option_strings = None):
        dest = getattr(namespace, self.dest, self.default)
        val = (getattr(dest, self.metavar), values)
        if dest is self.default:
            setattr(namespace, self.dest, testfilter([val]))
        else:
            dest.append(val)


def verify_config_components(config):
    if c2t_cfg.rsp_target.rsp is None:
        c2t_exit("unsupported GDB RSP target: %s" % c2t_cfg.rsp_target.march,
            prog = config
        )

    # TODO: check for {bin} usage

    for compiler, compiler_name in (
        (c2t_cfg.target_compiler, "target_compiler"),
        (c2t_cfg.oracle_compiler, "oracle_compiler")
    ):
        for run in compiler.run_script:
            if run.find("{bin}") != -1:
                break
        else:
            c2t_exit("{bin} doesn't exist", prog = "%s: %s" % (
                config, compiler_name
            ))


class C2TArgumentParser(ArgumentParser):
    """ ArgumentParser with custom error method """

    def error(self, msg):
        self.print_usage(stderr)
        self.exit(2, C2T_ERRMSG_FORMAT.format(
            prog = self.prog,
            msg = msg
        ))


def main():
    setpgrp()

    parser = C2TArgumentParser(
        description = "QEMU CPU Testing Tool",
        epilog = ("supported GDB RSP targets: {rsp}".format(
            rsp = ', '.join(archmap.keys())
        )),
        formatter_class = HelpFormatter
    )
    parser.add_argument("config",
        type = str,
        help = ("configuration file for {prog} (see sample and examples in "
            "{dir})".format(
                prog = parser.prog,
                dir = C2T_CONFIGS_DIR
            )
        )
    )
    DEFAULT_REGEXPS = testfilter([(testfilter.RE_INCLD, ".*\.c"),])
    parser.add_argument("-t", "--include",
        type = str,
        metavar = "RE_INCLD",
        action = TestfilterCLI,
        dest = "regexps",
        default = DEFAULT_REGEXPS,
        help = ("regular expressions to include a test set "
            "(tests are located in %s)" % C2T_TEST_DIR
        )
    )
    parser.add_argument("-s", "--exclude",
        type = str,
        metavar = "RE_EXCLD",
        action = TestfilterCLI,
        dest = "regexps",
        default = DEFAULT_REGEXPS,
        help = ("regular expressions to exclude a test set "
            "(tests are located in %s)" % C2T_TEST_DIR
        )
    )
    parser.add_argument("-j", "--jobs",
        type = int,
        dest = "jobs",
        default = 1,
        help = "allow N debugging jobs at once"
    )
    parser.add_argument("-r", "--reuse",
        action = "store_true",
        help = "reuse debug servers after each test (now only QEMU)"
    )
    parser.add_argument("-v", "--verbose",
        action = "store_true",
        help = "increase output verbosity"
    )

    args = parser.parse_args()

    config = args.config
    cfg_file = "%s.py" % config if not config.endswith(".py") else config

    config = cfg_file
    if not exists(config):
        config = join(C2T_CONFIGS_DIR, cfg_file)
        if not exists(config):
            config = join(C2T_DIR, cfg_file)
            if not exists(config):
                parser.error(
                    "configuration file doesn't exist: " + args.config
                )

    glob = {
        "C2TConfig": C2TConfig,
        "Run": Run,
        "get_new_rsp": get_new_rsp,
        "DebugClient": DebugClient,
        "DebugServer": DebugServer,
        "TestBuilder": TestBuilder
    }

    # getting `c2t_cfg` configuration for cpu testing tool
    try:
        execfile(config, glob)
    except Exception as e:
        c2t_exit(e, prog = config)
    else:
        global c2t_cfg
        for val in glob.values():
            if isinstance(val, C2TConfig):
                c2t_cfg = val
                break
        else:
            c2t_exit("No `C2TConfig` instance was defined by the config "
                     "(see sample and examples in {dir})".format(
                    dir = C2T_CONFIGS_DIR
                ),
                prog = config
            )

    verify_config_components(config)

    incl, regexp, tests = args.regexps.find_files(C2T_TEST_DIR)
    if not tests:
        parser.error("no matches in {dir} with {var} {regexp}".format(
            dir = C2T_TEST_DIR,
            var = "inclusive" if incl else "exclusive",
            regexp = cli_repr(regexp)
        ))

    jobs = args.jobs
    if jobs < 1:
        parser.error("wrong number of jobs: %s" % jobs)

    # creates tests subdirectories if they don't exist
    for sub_dir in (C2T_TEST_IR_DIR, C2T_TEST_BIN_DIR):
        if not exists(sub_dir):
            makedirs(sub_dir)

    start_cpu_testing(tests, jobs, args.reuse, args.verbose)


if __name__ == "__main__":
    main()