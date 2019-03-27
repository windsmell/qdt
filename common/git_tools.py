__all__ = [
    "GGB_IBY"
  , "CommitDesc"
  , "iter_chunks"
  , "git_diff2delta_intervals"
  , "fast_repo_clone"
]

from collections import (
    namedtuple
)
from re import (
    compile
)
from .intervalmap import (
    intervalmap
)
from tempfile import (
    mkdtemp
)
from os.path import (
    join
)


# Iterations Between Yields of Git Graph Building task
GGB_IBY = 100

# Unified Diff Format:
# https://www.artima.com/weblogs/viewpost.jsp?thread=164293
Range = namedtuple("Range", "lineno count")
# 'old' - range removed from previous version of file
# 'new' - range added to new version of file
Chunk = namedtuple("Chunk", "old new")

re_chunks = compile("@@ -(\d+)(?:,?(\d*)) \+(\d+)(?:,?(\d*)) @@")


def iter_chunks(diff):
    for chunk in re_chunks.findall(diff):
        c_lineno, c_count, b_lineno, b_count = chunk
        yield Chunk(
            # empty '*_count' is equivalent to '*_count' == 1
            Range(int(c_lineno), int(c_count) if c_count != '' else 1),
            Range(int(b_lineno), int(b_count) if b_count != '' else 1)
        )


def git_diff2delta_intervals(diff):
    """
:param diff:
    is 'git diff' information between current version of file and base version
    of file
:returns:
    the line-to-delta intervalmap built from `diff`

    """
    intervals = intervalmap()
    lineno = 1
    delta = 0

    for chunk in iter_chunks(diff):
        # diff is reversive, i.e. how to get the base version from a current.
        curr_range = chunk.old
        base_range = chunk.new

        # `inclusive`: 0 - exclude `lineno` from current interval
        #              1 - include `lineno` in current interval
        #
        # `gap_size` - the size of gap between the current and next intervals
        #
        if base_range.count:
            # add a gap including `lineno`
            inclusive, gap_size = 0, base_range.count
        else:
            # include `lineno` in `intervals`
            inclusive, gap_size = 1, 1

        # current interval
        intervals[lineno: base_range.lineno + inclusive] = delta

        # calculate the left boundary for the next interval
        lineno = base_range.lineno + gap_size
        # calculate the delta for the next interval
        delta += curr_range.count - base_range.count

    # default interval if diff is empty and last interval otherwise
    intervals[lineno: None] = delta

    return intervals


class CommitDesc(object):
    def __init__(self, sha, parents, children):
        self.sha = sha
        self.parents = parents
        self.children = children

        # serial number according to the topological sorting
        self.num = None

    @classmethod
    def co_build_git_graph(klass, repo, commit_desc_nodes):
        # iterations to yield
        i2y = GGB_IBY

        # n is serial number according to the topology sorting
        n = 0
        # to_enum is used during topological sorting
        # it contains commit to enumerate
        to_enum = None
        # build_stack contains eges represented by tuples
        # (parent, child), where parent is instance of
        # git.Commit, child is instance of QemuCommitDesc
        build_stack = []
        for head in repo.references:
            # skip processed heads
            if head.commit.hexsha in commit_desc_nodes:
                continue

            head_desc = klass(head.commit.hexsha, [], [])
            commit_desc_nodes[head.commit.hexsha] = head_desc
            # add edges connected to head being processed
            for p in head.commit.parents:
                build_stack.append((p, head_desc))

            while build_stack:
                parent, child_commit_desc = build_stack.pop()

                try:
                    parent_desc = commit_desc_nodes[parent.hexsha]
                except KeyError:
                    parent_desc = klass(parent.hexsha, [], [])
                    commit_desc_nodes[parent.hexsha] = parent_desc

                    if parent.parents:
                        for p in parent.parents:
                            build_stack.append((p, parent_desc))
                    else:
                        # current edge parent is an elder commit in the tree,
                        # that is why we should enumerate starting from it
                        to_enum = parent_desc
                else:
                    # the existence of parent_desc means that parent has been
                    # enumerated before. Hence, we starts enumeration from
                    # it's child
                    to_enum = child_commit_desc
                finally:
                    parent_desc.children.append(child_commit_desc)
                    child_commit_desc.parents.append(parent_desc)

                if i2y <= 0:
                    yield True
                    i2y = GGB_IBY
                else:
                    i2y -= 1

                # numbering is performed from the 'to_enum' to either a leaf
                # commit or a commit just before a merge which have at least
                # one parent without number (except the commit)
                while to_enum is not None:
                    e = to_enum
                    to_enum = None
                    # if the number of parents in the commit_desc_nodes
                    # is equal to the number of parents in the repo,
                    # then all parents were numbered (added) earlier
                    # according to the graph building algorithm,
                    # else we cannot assign number to the commit yet
                    if len(e.parents) == len(repo.commit(e.sha).parents):
                        e.num = n
                        n = n + 1
                        # according to the algorithm, only one child
                        # have no number. Other children either have
                        # been enumerated already or are not added yet
                        for c in e.children:
                            if c.num is None:
                                to_enum = c
                                break

                    if i2y <= 0:
                        yield True
                        i2y = GGB_IBY
                    else:
                        i2y -= 1


def fast_repo_clone(repo, version = None, prefix = "repo"):
    """ Creates Git repository clone with working copy in temporal directory
as fast as possible.
    """
    if version is None:
        version = repo.head.commit.hexsha
    else:
        version = repo.commit(version).hexsha

    tmp_wc = mkdtemp(prefix = "%s-%s-" % (prefix, version))

    # Current approach uses "-s" (--shared) option to avoid copying
    # of history and "-n" (--no-checkout) to avoid redundant checking
    # out of repo's HEAD in the new clone. Requested version is checked out
    # explicitly. Therefore, overhead of cloning is low enough.
    new_repo = repo.clone(tmp_wc, no_checkout = True, shared = True)

    git = new_repo.git

    git.checkout(version, force = True)

    # Submodules also must be recursively initialized. But straightforward
    # invocation of of "update --init" command will result in downloading of
    # submodules history. Instead, submodules URLs are redirected to local
    # history copies inside original repo. As a result, submodules
    # initialization is done without redundant copying.
    # However! ".gitmodules" file is considered changed by Git because of that
    # redirection.

    # TODO: it must be done for submodules of submodules too (recursively).

    # This works incorrect with `recursive = True` because "--recursive" is
    # added before "status"
    status = git.submodule("status", "--recursive")

    submodules = []
    for l in status.splitlines(False):
        submodules.append(l.split(' ')[1])

    if submodules:
        for sm in submodules:
            # https://stackoverflow.com/a/30675130/7623015
            git.config(
                "submodule." + sm + ".url",
                join(repo.working_tree_dir, ".git", "modules", sm),
                file = ".gitmodules"
            )

        git.submodule("update", "--init", "--recursive")

    return new_repo
