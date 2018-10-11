__all__ = [
    "Source"
      , "Header"
  , "AddTypeRefToDefinerException"
  , "TypeNotRegistered"
  , "Type"
      , "TypeReference"
      , "Structure"
      , "Function"
      , "Pointer"
      , "Macro"
      , "MacroType"
      , "Enumeration"
  , "Initializer"
  , "Variable"
  , "SourceChunk"
      , "HeaderInclusion"
      , "MacroDefinition"
      , "PointerTypeDeclaration"
      , "FunctionPointerTypeDeclaration"
      , "MacroTypeUsage"
      , "PointerVariableDeclaration"
      , "FunctionPointerDeclaration"
      , "VariableDeclaration"
      , "VariableDefinition"
      , "StructureDeclarationBegin"
      , "StructureDeclaration"
      , "FunctionDeclaration"
      , "FunctionDefinition"
      , "UsageTerminator"
  , "SourceFile"
  , "SourceTreeContainer"
]

from os import (
    listdir
)
from os.path import (
    basename,
    splitext,
    join,
    isdir
)
from copy import (
    copy
)
import sys

from re import (
    compile
)
from itertools import (
    chain
)
from common import (
    path2tuple,
    ply2path, # PLY`s C preprocessor is used for several QEMU code analysis
    OrderedSet,
    ObjectVisitor,
    BreakVisiting
)
from ply.lex import (
    lex
)
from ply.cpp import (
    Preprocessor,
    literals,
    tokens,
    t_error
)
exec("from ply.cpp import t_" + ", t_".join(tokens))

from itertools import (
    count
)
from six import (
    string_types,
    text_type,
    binary_type
)
from collections import (
    OrderedDict
)
from .tools import (
    get_cpp_search_paths
)

# Used for sys.stdout recovery
sys_stdout_recovery = sys.stdout

macro_forbidden = compile("[^0-9A-Z_]")


# Code generation model
class ChunkGenerator(object):
    """ Maintains context of source code chunks generation precess."""

    def __init__(self, for_header = False):
        self.chunk_cache = {}
        self.for_header = for_header
        """ Tracking of recursive calls of `provide_chunks`. Currently used only
        to generate "extern" keyword for global variables in header and to
        distinguish structure fields and normal variables. """
        self.stack = []

    def provide_chunks(self, origin, **kw):
        """ Given origin the method returns chunk list generating it on first
        access. """
        try:
            chunks = self.chunk_cache[origin]
        except KeyError:
            self.stack.append(origin)

            if isinstance(origin, Function):
                if self.for_header and (not origin.static or not origin.inline):
                    chunks = origin.gen_declaration_chunks(self, **kw)
                else:
                    chunks = origin.gen_definition_chunks(self, **kw)
            elif isinstance(origin, Variable):
                # A variable in a header does always have `extern` modifier.
                # Note that some "variables" do describe `struct`/`enum`
                # entries and must not have it.
                if len(self.stack) == 1:
                    if self.for_header:
                        kw["extern"] = True
                        chunks = origin.gen_declaration_chunks(self, **kw)
                    else:
                        chunks = origin.get_definition_chunks(self, **kw)
                else:
                    if isinstance(self.stack[-2], Structure):
                        # structure fields
                        chunks = origin.gen_declaration_chunks(self, **kw)
                    elif isinstance(self.stack[-2], Enumeration):
                        kw["enum"] = True
                        chunks = origin.get_definition_chunks(self, **kw)
                    else:
                        # Something like a static inline function in a header
                        # may request chunks for a global variable. This case
                        # the stack height is greater than 1.
                        if self.for_header:
                            kw["extern"] = True
                            chunks = origin.gen_declaration_chunks(self, **kw)
                        else:
                            chunks = origin.get_definition_chunks(self, **kw)
            else:
                chunks = origin.gen_defining_chunk_list(self, **kw)

            self.stack.pop()

            # Note that conversion to a tuple is performed to prevent further
            # modifications of chunk list.
            self.chunk_cache[key] = tuple(chunks)

        return chunks

    def get_all_chunks(self):
        res = set()

        for chunks in self.chunk_cache.values():
            for chunk in chunks:
                if chunk not in res:
                    res.add(chunk)

        return list(res)

# Source code models


class Source(object):

    def __init__(self, path):
        self.path = path
        self.types = {}
        self.inclusions = {}
        self.global_variables = {}
        self.references = set()
        self.protection = False

    def add_reference(self, ref):
        if not isinstance(ref, Type) and not isinstance(ref, Variable):
            raise ValueError("Trying to add source reference which is not a"
                " type"
            )
        if isinstance(ref, TypeReference):
            raise ValueError("Source reference may not be TypeReference."
                "Only original types are allowed."
            )
        self.references.add(ref)

        return self

    def add_references(self, refs):
        for ref in refs:
            self.add_reference(ref)

        return self

    def add_global_variable(self, var):
        if var.name in self.global_variables:
            raise RuntimeError("Variable with name %s is already in file %s"
                % (var.name, self.name)
            )

        TypeFixerVisitor(self, var).visit()

        # Auto add definers for type
        for s in var.get_definers():
            if s == self:
                continue
            if not isinstance(s, Header):
                raise RuntimeError("Attempt to define variable %s whose type "
                    " is defined in non-header file %s" % (var.name, s.path)
                )
            self.add_inclusion(s)
        # Auto add definers for types used by variable initializer
        if type(self) is Source: # exactly a module, not a header
            if var.initializer is not None:
                for t in var.initializer.used_types:
                    for s in t.get_definers():
                        if s == self:
                            continue
                        if not isinstance(s, Header):
                            raise RuntimeError("Attempt to define variable"
                                " {var} whose initializer code uses type {t}"
                                " defined in non-header file {file}".format(
                                var = var.name,
                                t = t.name,
                                file = s.path
                            ))
                        self.add_inclusion(s)

        self.global_variables[var.name] = var

        return self

    def add_inclusion(self, header):
        if not isinstance(header, Header):
            raise ValueError("Inclusion of a non-header file is forbidden (%s)"
                % header.path
            )

        if header.path not in self.inclusions:
            self.inclusions[header.path] = header

            for t in header.types.values():
                try:
                    if isinstance(t, TypeReference):
                        self._add_type_recursive(TypeReference(t.type))
                    else:
                        self._add_type_recursive(TypeReference(t))
                except AddTypeRefToDefinerException:
                    # inclusion cycles will cause this exceptions
                    pass

            if self in header.includers:
                raise RuntimeError("Header %s is among includers of %s but does"
                    " not includes it" % (self.path, header.path)
                )

            header.includers.append(self)

        return self

    def _add_type_recursive(self, type_ref):
        if type_ref.name in self.types:
            t = self.types[type_ref.name]
            if isinstance(t, TypeReference):
                # To check incomplete type case
                if not t.type.definer == type_ref.type.definer:
                    raise RuntimeError("Conflict reference to type %s found in"
                        " source %s. The type is defined both in %s and %s"
                        % (t.name, self.path, type_ref.type.definer.path,
                            t.type.definer.path
                        )
                    )
            # To make more conflicts checking
            return False

        self.types[type_ref.name] = type_ref
        return True

    def add_types(self, types):
        for t in types:
            self.add_type(t)

        return self

    def add_type(self, _type):
        if isinstance(_type, TypeReference):
            raise ValueError("A type reference (%s) cannot be added to a"
                " source (%s) externally" % (_type.name, self.path)
            )

        TypeFixerVisitor(self, _type).visit()

        _type.definer = self
        self.types[_type.name] = _type

        # Auto include type definers
        for s in _type.get_definers():
            if s == self:
                continue
            if not isinstance(s, Header):
                raise ValueError("Attempt to define structure %s that has a"
                    " field of a type defined in another non-header file %s."
                    % (_type.name, s.path)
                )
            self.add_inclusion(s)

        return self

    def gen_chunks(self, inherit_references = False):
        """ In some use cases header should not satisfy references of its
inclusions by itself. Instead, it must inherit them. A source file must
satisfy the references in such case. Set inherit_references to True for
switching to that mode.
        """

        if inherit_references:
            assert(isinstance(self, Header))

        ref_list = []

        if isinstance(self, Header):
            for user in self.includers:
                for ref in user.references:
                    if ref.definer not in user.inclusions:
                        ref_list.append(TypeReference(ref))

        tf = TypeFixerVisitor(self, self.global_variables)
        tf.visit()

        # fix up types for headers with references
        # list of types must be copied because it is changed during each
        # loop iteration. values() returns generator in Python 3.x, which
        # must be explicitly enrolled to a list. Though is it redundant
        # copy operation in Python 2.x.
        l = list(self.types.values()) + ref_list

        while True:
            for t in l:
                if not isinstance(t, TypeReference):
                    continue

                if t.definer_references is not None:
                    # References are already specified
                    continue

                if inherit_references:
                    t.definer_references = set()
                    for ref in t.type.definer.references:
                        for self_ref in self.references:
                            if self_ref is ref:
                                break
                        else:
                            self.references.add(ref)
                else:
                    t.definer_references = set(t.type.definer.references)

            replaced = False
            for t in l:
                if not isinstance(t, TypeReference):
                    continue

                tfv = TypeFixerVisitor(self, t)
                tfv.visit()

                if tfv.replaced:
                    replaced = True

            if not replaced:
                break
            # Preserve current types list. See the comment above.
            l = list(self.types.values()) + ref_list

        gen = ChunkGenerator(for_header = isinstance(self, Header))

        for t in self.types.values():
            if isinstance(t, TypeReference):
                for inc in self.inclusions.values():
                    if t.name in inc.types:
                        break
                else:
                    raise RuntimeError("Any type reference in a file must "
"be provided by at least one inclusion (%s: %s)" % (self.path, t.name)
                    )
                continue

            if t.definer is not self:
                raise RuntimeError("Type %s is defined in %s but presented in"
" %s not by a reference." % (t.name, t.definer.path, self.path)
                )

            gen.provide_chunks(t)

        for gv in self.global_variables.values():
            gen.provide_chunks(gv)

        if isinstance(self, Header):
            for r in ref_list:
                gen.provide_chunks(r)

        chunks = gen.get_all_chunks()

        # Account extra references
        chunk_cache = {}

        # Build mapping
        for ch in chunks:
            origin = ch.origin
            chunk_cache.setdefault(origin, []).append(ch)

        # link chunks
        for ch in chunks:
            origin = ch.origin

            """ Any object that could be an origin of chunk may provide an
iterable container of extra references. A reference must be another origin.
Chunks originated from referencing origin are referenced to chunks originated
from each referenced origin.

    Extra references may be used to apply extended (semantic) order when syntax
order does not meet all requirements.
            """
            try:
                refs = origin.extra_references
            except AttributeError:
                continue

            for r in refs:
                try:
                    referenced_chunks = chunk_cache[r]
                except KeyError:
                    # no chunk was generated for that referenced origin
                    continue

                ch.add_references(referenced_chunks)

        return chunks

    def generate(self, inherit_references = False):
        Header.propagate_references()

        source_basename = basename(self.path)
        name = splitext(source_basename)[0]

        file = SourceFile(name, isinstance(self, Header),
            protection = self.protection
        )

        file.add_chunks(self.gen_chunks(inherit_references))

        return file


class AddTypeRefToDefinerException(RuntimeError):
    pass


class ParsePrintFilter(object):

    def __init__(self, out):
        self.out = out
        self.written = False

    def write(self, str):
        if str.startswith("Info:"):
            self.out.write(str + "\n")
            self.written = True

    def flush(self):
        if self.written:
            self.out.flush()
            self.written = False


class Header(Source):
    reg = {}

    @staticmethod
    def _on_include(includer, inclusion, is_global):
        if path2tuple(inclusion) not in Header.reg:
            print("Info: parsing " + inclusion + " as inclusion")
            h = Header(path = inclusion, is_global = is_global)
            h.parsed = True
        else:
            h = Header.lookup(inclusion)

        Header.lookup(includer).add_inclusion(h)

    @staticmethod
    def _on_define(definer, macro):
        # macro is ply.cpp.Macro

        if "__FILE__" == macro.name:
            return

        h = Header.lookup(definer)

        try:
            m = Type.lookup(macro.name)
            if not m.definer.path == definer:
                print("Info: multiple definitions of macro %s in %s and %s"\
                     % (macro.name, m.definer.path, definer)
                )
        except:
            m = Macro(
                name = macro.name,
                text = "".join(tok.value for tok in macro.value),
                args = (
                    None if macro.arglist is None else list(macro.arglist)
                )
            )
            h.add_type(m)

    @staticmethod
    def _build_inclusions_recursive(start_dir, prefix):
        full_name = join(start_dir, prefix)
        if (isdir(full_name)):
            for entry in listdir(full_name):
                yield Header._build_inclusions_recursive(
                    start_dir,
                    join(prefix, entry)
                )
        else:
            (name, ext) = splitext(prefix)
            if ext == ".h":
                if path2tuple(prefix) not in Header.reg:
                    h = Header(path = prefix, is_global = False)
                    h.parsed = False
                else:
                    h = Header.lookup(prefix)

                if not h.parsed:
                    h.parsed = True
                    print("Info: parsing " + prefix)

                    p = Preprocessor(lex())
                    p.add_path(start_dir)

                    global cpp_search_paths
                    for path in cpp_search_paths:
                        p.add_path(path)

                    p.on_include = Header._on_include
                    p.on_define.append(Header._on_define)

                    if sys.version_info[0] == 3:
                        header_input = open(full_name, "r", encoding = "UTF-8").read()
                    else:
                        header_input = open(full_name, "rb").read().decode("UTF-8")

                    p.parse(input = header_input, source = prefix)

                    yields_per_current_header = 0

                    tokens_before_yield = 0
                    while p.token():
                        if not tokens_before_yield:

                            yields_per_current_header += 1

                            yield True
                            tokens_before_yield = 1000 # an adjusted value
                        else:
                            tokens_before_yield -= 1

                    Header.yields_per_header.append(yields_per_current_header)

    @staticmethod
    def co_build_inclusions(dname):
        # Default include search folders should be specified to
        # locate and parse standard headers.
        # parse `cpp -v` output to get actual list of default
        # include folders. It should be cross-platform
        global cpp_search_paths
        cpp_search_paths = get_cpp_search_paths()

        Header.yields_per_header = []

        if not isinstance(sys.stdout, ParsePrintFilter):
            sys.stdout = ParsePrintFilter(sys.stdout)

        for h in Header.reg.values():
            h.parsed = False

        for entry in listdir(dname):
            yield Header._build_inclusions_recursive(dname, entry)

        for h in Header.reg.values():
            del h.parsed

        sys.stdout = sys_stdout_recovery

        yields_total = sum(Header.yields_per_header)

        print("""Header inclusions build statistic:
    Yields total: %d
    Max yields per header: %d
    Min yields per header: %d
    Average yields per headed: %f
""" % (
    yields_total,
    max(Header.yields_per_header),
    min(Header.yields_per_header),
    yields_total / float(len(Header.yields_per_header))
)
        )

        del Header.yields_per_header

    @staticmethod
    def lookup(path):
        tpath = path2tuple(path)

        if tpath not in Header.reg:
            raise RuntimeError("Header with path %s is not registered" % path)
        return Header.reg[tpath]

    def __init__(self, path, is_global = False, protection = True):
        super(Header, self).__init__(path)
        self.is_global = is_global
        self.includers = []
        self.protection = protection

        tpath = path2tuple(path)
        if tpath in Header.reg:
            raise RuntimeError("Header %s is already registered" % path)

        Header.reg[tpath] = self

    def _add_type_recursive(self, type_ref):
        if type_ref.type.definer == self:
            raise AddTypeRefToDefinerException("Adding a type reference (%s) to"
                " a file (%s) defining the type"
                % (type_ref.type.name, self.path)
            )

        # Preventing infinite recursion by header inclusion loop
        if super(Header, self)._add_type_recursive(type_ref):
            # Type was added. Hence, It is new type for the header
            for s in self.includers:
                try:
                    s._add_type_recursive(TypeReference(type_ref.type))
                except AddTypeRefToDefinerException:
                    # inclusion cycles will cause this exception
                    pass

    def add_type(self, _type):
        super(Header, self).add_type(_type)

        # Auto add type references to self includers
        for s in self.includers:
            s._add_type_recursive(TypeReference(_type))

        return self

    def __hash__(self):
        # key contains of 'g' or 'l' and header path
        # 'g' and 'l' are used to distinguish global and local
        # headers with same
        return hash("{}{}".format(
                "g" if self.is_global else "l",
                self.path))

    @staticmethod
    def _propagate_reference(h, ref):
        h.add_reference(ref)

        for u in h.includers:
            if type(u) is Source: # exactly a module, not a header
                continue
            if ref in u.references:
                continue
            """ u is definer of ref or u has type reference to ref (i.e.
transitively includes definer of ref)"""
            if ref.name in u.types:
                continue
            Header._propagate_reference(u, ref)

    @staticmethod
    def propagate_references():
        for h in Header.reg.values():
            if not isinstance(h, Header):
                continue

            for ref in h.references:
                for u in h.includers:
                    if type(u) is Source: # exactly a module, not a header
                        continue
                    if ref in u.references:
                        continue
                    if ref.name in u.types:
                        continue
                    Header._propagate_reference(u, ref)

# Type models


class TypeNotRegistered(RuntimeError):
    pass


class Type(object):
    reg = {}

    @staticmethod
    def lookup(name):
        if name not in Type.reg:
            raise TypeNotRegistered("Type with name %s is not registered"
                % name
            )
        return Type.reg[name]

    @staticmethod
    def exists(name):
        try:
            Type.lookup(name)
            return True
        except TypeNotRegistered:
            return False

    def __init__(self, name,
        incomplete = True,
        base = False
    ):
        self.name = name
        self.incomplete = incomplete
        self.definer = None
        self.base = base

        if name in Type.reg:
            raise RuntimeError("Type %s is already registered" % name)

        Type.reg[name] = self

    def gen_var(self, name,
        pointer = False,
        initializer = None,
        static = False,
        array_size = None,
        used = False
    ):
        if self.incomplete:
            if not pointer:
                raise ValueError("Cannon create non-pointer variable %s"
                    " of incomplete type %s." % (name, self.name)
                )

        if pointer:
            return Variable(name, Pointer(self),
                initializer = initializer,
                static = static,
                array_size = array_size,
                used = used
            )
        else:
            return Variable(name, self,
                initializer = initializer,
                static = static,
                array_size = array_size,
                used = used
            )

    def get_definers(self):
        if self.definer is None:
            return []
        else:
            return [self.definer]

    def gen_chunks(self, generator):
        raise ValueError("Attempt to generate source chunks for stub"
            " type %s" % self.name
        )

    def gen_defining_chunk_list(self, generator, **kw):
        if self.base:
            return []
        else:
            return self.gen_chunks(generator, **kw)

    def gen_usage_string(self, initializer):
        # Usage string for an initializer is code of the initializer. It is
        # legacy behavior.
        return initializer.code

    def __eq__(self, other):
        if isinstance(other, TypeReference):
            return other.type == self
        # This code assumes that one type cannot be represented by several
        # objects.
        return self is other

    def __hash__(self):
        return hash(self.name)


class TypeReference(Type):

    def __init__(self, _type):
        if isinstance(_type, TypeReference):
            raise ValueError("Attempt to create type reference to"
                " another type reference %s." % _type.name
            )

        # super(TypeReference, self).__init__(_type.name, _type.incomplete)
        self.name = _type.name
        self.incomplete = _type.incomplete
        self.base = _type.base
        self.type = _type

        self.definer_references = None

    def get_definers(self):
        return self.type.get_definers()

    def gen_chunks(self, generator):
        if self.definer_references is None:
            raise RuntimeError("Attempt to generate chunks for reference to"
                " type %s without the type reference adjusting"
                " pass." % self.name
            )

        inc = HeaderInclusion(self.type.definer)

        refs = []
        for r in self.definer_references:
            refs.extend(generator.provide_chunks(r))

        inc.add_references(refs)

        return [inc]

    def gen_var(self, *args, **kw):
        raise ValueError("Attempt to generate variable of type %s"
            " using a reference" % self.type.name
        )

    def gen_usage_string(self, initializer):
        # redirect to referenced type
        return self.type.gen_usage_string(initializer)

    __type_references__ = ["definer_references"]

    def __eq__(self, other):
        return self.type == other

    def __hash__(self):
        return hash(self.type)


class Structure(Type):

    def __init__(self, name, fields = None):
        super(Structure, self).__init__(name, incomplete = False)
        self.fields = OrderedDict()
        if fields is not None:
            for v in fields:
                self.append_field(v)

    def get_definers(self):
        if self.definer is None:
            raise RuntimeError("Getting definers for structure %s that is not"
                " added to a source", self.name
            )

        definers = [self.definer]

        for f in self.fields.values():
            definers.extend(f.get_definers())

        return definers

    def append_field(self, variable):
        v_name = variable.name
        if v_name in self.fields:
            raise RuntimeError("A field with name %s already exists in"
                " the structure %s" % (v_name, self.name)
            )

        self.fields[v_name] = variable

    def append_field_t(self, _type, name, pointer = False):
        self.append_field(_type.gen_var(name, pointer))

    def append_field_t_s(self, type_name, name, pointer = False):
        self.append_field_t(Type.lookup(type_name), name, pointer)

    def gen_chunks(self, generator):
        fields_indent = "    "
        indent = ""

        struct_begin = StructureDeclarationBegin(self, indent)

        struct_end = StructureDeclaration(self, fields_indent, indent, True)

        """
        References map of structure definition chunks:

              ____--------> [self references of struct_begin ]
             /    ___-----> [ united references of all fields ]
            |    /     _--> [ references of struct_end ] == empty
            |    |    /
            |    |   |
           struct_begin
                ^
                |
             field_0
                ^
                |
             field_1
                ^
                |
               ...
                ^
                |
             field_N
                ^
                |
            struct_end

        """

        field_indent = indent + fields_indent
        field_refs = []
        top_chunk = struct_begin

        for f in self.fields.values():
            # Note that 0-th chunk is field and rest are its dependencies
            decl_chunks = generator.provide_chunks(f, indent = field_indent)

            field_declaration = decl_chunks[0]

            field_refs.extend(list(field_declaration.references))
            field_declaration.clean_references()
            field_declaration.add_reference(top_chunk)
            top_chunk = field_declaration

        struct_begin.add_references(field_refs)
        struct_end.add_reference(top_chunk)

        return [struct_end, struct_begin]

    def gen_usage_string(self, init):
        if init is None:
            return "{ 0 };" # zero structure initializer by default

        code = init.code
        if not isinstance(code, dict):
            # Support for legacy initializers
            return code

        # Use entries of given dict to initialize fields. Field name is used
        # as entry key.

        fields_code = []
        for name in self.fields.keys():
            try:
                val_str = init[name]
            except KeyError: # no initializer for this field
                continue
            fields_code.append("    .%s@b=@s%s" % (name, val_str))

        return "{\n" + ",\n".join(fields_code) + "\n}";

    __type_references__ = ["fields"]


class Enumeration(Type):

    def __init__(self, type_name, elems_dict, enum_name = ""):
        super(Enumeration, self).__init__(type_name)
        self.elems = []
        self.enum_name = enum_name
        t = [Type.lookup("int")]
        for key, val in elems_dict.items():
            self.elems.append(
                Variable(key, self,
                    initializer = Initializer(str(val), t)
                )
            )

        self.elems.sort(key = lambda x: int(x.initializer.code))

    def get_field(self, name):
        for e in self.elems:
            if name == e.name:
                return e
        return None

    def gen_chunks(self, generator):
        fields_indent = "    "
        indent = ""

        enum_begin = EnumerationDeclarationBegin(self, indent)
        enum_end = EnumerationDeclaration(self, indent)

        field_indent = indent + fields_indent
        field_refs = []
        top_chunk = enum_begin

        for f in self.elems:
            # Note that 0-th chunk is field and rest are its dependencies
            decl_chunks = generator.provide_chunks(f, indent = field_indent)

            field_declaration = decl_chunks[0]

            field_refs.extend(list(field_declaration.references))
            field_declaration.clean_references()
            field_declaration.add_reference(top_chunk)
            top_chunk = field_declaration

        enum_begin.add_references(field_refs)
        enum_end.add_reference(top_chunk)

        return [enum_end, enum_begin]

    __type_references__ = ["elems"]


class FunctionBodyString(object):

    def __init__(self, body = None, used_types = None, used_globals = None):
        self.body = body
        self.used_types = set() if used_types is None else set(used_types)
        self.used_globals = [] if used_globals is None else list(used_globals)

    def __str__(self):
        return self.body

    __type_references__ = ["used_types"]


class Function(Type):

    def __init__(self, name,
        body = None,
        ret_type = None,
        args = None,
        static = False,
        inline = False,
        used_types = None,
        used_globals = None
    ):
        # args is list of Variables
        super(Function, self).__init__(name,
            # function cannot be a 'type' of variable. Only function
            # pointer type is permitted.
            incomplete = True
        )
        self.static = static
        self.inline = inline
        self.ret_type = Type.lookup("void") if ret_type is None else ret_type
        self.args = args

        if isinstance(body, str):
            self.body = FunctionBodyString(
                body = body,
                used_types = used_types,
                used_globals = used_globals
            )
        else:
            self.body = body
            if (used_types or used_globals) is not None:
                raise ValueError("Specifing of used types or globals for non-"
                    "string body is redundant."
                )

    def gen_declaration_chunks(self, generator):
        indent = ""
        ch = FunctionDeclaration(self, indent)

        refs = gen_function_decl_ref_chunks(self, generator)

        ch.add_references(refs)

        return [ch]

    gen_chunks = gen_declaration_chunks

    def gen_definition_chunks(self, generator):
        indent = ""
        ch = FunctionDefinition(self, indent)

        refs = gen_function_decl_ref_chunks(self, generator) + \
               gen_function_def_ref_chunks(self, generator)

        ch.add_references(refs)
        return [ch]

    def use_as_prototype(self, name,
        body = None,
        static = False,
        inline = False,
        used_types = []
    ):

        return Function(name,
            body = body,
            ret_type = self.ret_type,
            args = self.args,
            static = static,
            inline = inline,
            used_types = used_types
        )

    def gen_body(self, used_types = None, used_globals = None):
        new_used_types = [self]
        new_used_types.extend([] if used_types is None else used_types)
        new_used_globals = [] if used_globals is None else used_globals
        new_f = Function(
            self.name + ".body",
            self.body,
            self.ret_type,
            self.args,
            self.static,
            self.inline,
            new_used_types,
            new_used_globals
        )
        CopyFixerVisitor(new_f).visit()
        return new_f

    def gen_var(self, name, initializer = None, static = False):
        return Variable(name, self, initializer = initializer, static = static)

    __type_references__ = ["ret_type", "args", "body"]


class Pointer(Type):

    def __init__(self, _type, name = None, const = False):
        """
        const: pointer to constant (not a constant pointer).
        """
        self.is_named = name is not None
        if not self.is_named:
            name = _type.name + '*'
            if const:
                name = "const@b" + name

        # do not add nameless pointers to type registry
        if self.is_named:
            super(Pointer, self).__init__(name,
                incomplete = False,
                base = False
            )
        else:
            self.name = name
            self.incomplete = False
            self.base = False

        self.type = _type
        self.const = const

    def __eq__(self, other):
        if not isinstance(other, Pointer):
            return False

        if self.is_named:
            if other.is_named:
                return super(Pointer, self).__eq__(other)
            return False

        if other.is_named:
            return False

        return (self.type == other.type) and (self.const == other.const)

    def get_definers(self):
        if self.is_named:
            return super(Pointer, self).get_definers()
        else:
            return self.type.get_definers()

    def gen_chunks(self, generator):
        if not self.is_named:
            return []

        # strip function definition chunk, its references is only needed
        if isinstance(self.type, Function):
            ch = FunctionPointerTypeDeclaration(self.type, self.name)
            refs = gen_function_decl_ref_chunks(self.type, generator)
        else:
            ch = PointerTypeDeclaration(self.type, self.name)
            refs = generator.provide_chunks(self.type)

        """ 'typedef' does not require refererenced types to be visible.
Hence, it is not correct to add references to the PointerTypeDeclaration
chunk. The references is to be added to `users` of the 'typedef'.
    """
        ch.add_references(refs)

        return [ch]

    def __hash__(self):
        stars = "*"
        t = self.type
        while isinstance(t, Pointer) and not t.is_named:
            t = t.type
            stars += "*"
        return hash(hash(t) + hash(stars))

    __type_references__ = ["type"]


HDB_MACRO_NAME = "name"
HDB_MACRO_TEXT = "text"
HDB_MACRO_ARGS = "args"


class Macro(Type):

    # args is list of strings
    def __init__(self, name, args = None, text = None):
        super(Macro, self).__init__(name, incomplete = False)

        self.args = args
        self.text = text

    def gen_chunks(self, generator):
        return [ MacroDefinition(self) ]

    def gen_usage_string(self, init = None):
        if self.args is None:
            return self.name
        else:
            arg_val = "(@a" + ",@s".join(init[a] for a in self.args) + ")"

        return "%s%s" % (self.name, arg_val)

    def gen_var(self, name,
        pointer = False,
        initializer = None,
        static = False,
        array_size = None,
        used = False,
        macro_initializer = None
    ):
        mt = MacroType(self,  initializer = macro_initializer)
        return mt.gen_var(name,
            pointer = pointer,
            initializer = initializer,
            static = static,
            array_size = array_size,
            used = used
        )

    def gen_usage(self, initializer = None, name = None):
        return MacroType(self,
            initializer = initializer,
            name = name,
            is_usage = True
        )

    def gen_dict(self):
        res = {HDB_MACRO_NAME : self.name}
        if self.text is not None:
            res[HDB_MACRO_TEXT] = self.text
        if self.args is not None:
            res[HDB_MACRO_ARGS] = self.args

        return res

    @staticmethod
    def new_from_dict(_dict):
        return Macro(
            name = _dict[HDB_MACRO_NAME],
            args = _dict[HDB_MACRO_ARGS] if HDB_MACRO_ARGS in _dict else None,
            text = _dict[HDB_MACRO_TEXT] if HDB_MACRO_TEXT in _dict else None
        )


class MacroType(Type):
    def __init__(self, _macro,
        initializer = None,
        name = None,
        is_usage = False
    ):
        if not isinstance(_macro, Macro):
            raise ValueError("Attempt to create macrotype from "
                " %s which is not macro." % _macro.name
            )
        if is_usage or (name is not None):
            self.is_named = True
            if name is None:
                name = _macro.name + ".usage" + str(id(self))
            super(MacroType, self).__init__(name,
                incomplete = False,
                base = False
            )
        else:
            # do not add nameless macrotypes to type registry
            self.is_named = False
            self.name = _macro.gen_usage_string(initializer)
            self.incomplete = False
            self.base = False

        self.macro = _macro
        self.initializer = initializer

    def get_definers(self):
        if self.is_named:
            return super(MacroType, self).get_definers()
        else:
            return self.macro.get_definers()

    def gen_chunks(self, generator, indent = ""):
        if not self.is_named:
            return []

        macro = self.macro
        initializer = self.initializer

        ch = MacroTypeUsage(macro, initializer, indent)
        refs = list(generator.provide_chunks(macro))

        if initializer is not None:
            for v in initializer.used_variables:
                """ Note that 0-th chunk is variable and rest are its
                dependencies """
                refs.append(generator.provide_chunks(v)[0])

            for t in initializer.used_types:
                refs.extend(generator.provide_chunks(t))

        ch.add_references(refs)
        return [ch]

    __type_references__ = ["macro", "initializer"]


class CPPMacro(Macro):
    """ A kind of macro defined by the C preprocessor.
    For example __FILE__, __LINE__, __FUNCTION__ and etc.
    """

    def __init__(self, *args, **kw):
        super(CPPMacro, self).__init__(*args, **kw)

    def gen_chunks(self, _):
        # CPPMacro does't require referenced types
        # because it's defined by C preprocessor.
        return []


# Data models


class TypesCollector(ObjectVisitor):

    def __init__(self, code):
        super(TypesCollector, self).__init__(code,
            field_name = "__type_references__"
        )
        self.used_types = set()

    def on_visit(self):
        cur = self.cur
        if isinstance(cur, Type):
            self.used_types.add(cur)
            raise BreakVisiting()


class Initializer(object):

    # code is string for variables and dictionary for macros
    def __init__(self, code, used_types = [], used_variables = []):
        self.code = code
        self.used_types = set(used_types)
        self.used_variables = used_variables
        if isinstance(code, dict):
            self.__type_references__ = self.__type_references__ + ["code"]

            # automatically get types used in the code
            tc = TypesCollector(code)
            tc.visit()
            self.used_types.update(tc.used_types)

    def __getitem__(self, key):
        val = self.code[key]

        # adjust initializer value
        if isinstance(val, (string_types, text_type, binary_type)):
            val_str = val
        elif isinstance(val, Type):
            val_str = val.name
        else:
            raise TypeError("Unsupported initializer entry type '%s'"
                % type(val).__name__
            )

        return val_str

    __type_references__ = ["used_types", "used_variables"]


class Variable(object):

    def __init__(self, name, _type,
        initializer = None,
        static = False,
        const = False,
        array_size = None,
        used = False
    ):
        self.name = name
        self.type = _type
        self.initializer = initializer
        self.static = static
        self.const = const
        self.array_size = array_size
        self.used = used

    def gen_declaration_chunks(self, generator,
        indent = "",
        extern = False
    ):
        if isinstance(self.type, Pointer) and not self.type.is_named:
            if isinstance(self.type.type, Function):
                ch = FunctionPointerDeclaration(self, indent, extern)
                refs = gen_function_decl_ref_chunks(self.type.type, generator)
            else:
                ch = PointerVariableDeclaration(self, indent, extern)
                refs = generator.provide_chunks(self.type.type)
            ch.add_references(refs)
        else:
            ch = VariableDeclaration(self, indent, extern)
            refs = generator.provide_chunks(self.type)
            ch.add_references(refs)

        return [ch]

    def get_definition_chunks(self, generator, indent = "", enum = False):
        append_nl = True
        ch = VariableDefinition(self, indent, append_nl, enum)

        refs = list(generator.provide_chunks(self.type))

        if self.initializer is not None:
            for v in self.initializer.used_variables:
                """ Note that 0-th chunk is variable and rest are its
                dependencies """
                refs.append(generator.provide_chunks(v)[0])

            for t in self.initializer.used_types:
                refs.extend(generator.provide_chunks(t))

        ch.add_references(refs)
        return [ch]

    def get_definers(self):
        return self.type.get_definers()

    def __c__(self, writer):
        writer.write(self.name)
        if self.array_size is not None:
            writer.write("[%d]" % self.array_size)
        if not self.used:
            writer.write("@b__attribute__((unused))")

    __type_references__ = ["type", "initializer"]

# Type inspecting


class TypeFixerVisitor(ObjectVisitor):

    def __init__(self, source, type_object, *args, **kw):
        kw["field_name"] = "__type_references__"
        ObjectVisitor.__init__(self, type_object, *args, **kw)

        self.source = source
        self.replaced = False

    def replace(self, new_value):
        self.replaced = True
        ObjectVisitor.replace(self, new_value)

    def on_visit(self):
        if isinstance(self.cur, Type):
            t = self.cur
            if isinstance(t, TypeReference):
                try:
                    self_tr = self.source.types[t.name]
                except KeyError:
                    # The source does not have such type reference
                    self.source.add_inclusion(t.type.definer)
                    self_tr = self.source.types[t.name]

                if self_tr is not t:
                    self.replace(self_tr)

                raise BreakVisiting()

            if t.base:
                raise BreakVisiting()

            """ Skip pointer and macrotype types without name. Nameless pointer
            or macrotype does not have definer and should not be replaced with
            type reference """
            if isinstance(t, (Pointer, MacroType)) and not t.is_named:
                return

            # Do not add definerless types to the Source automatically
            if t.definer is None:
                return

            if t.definer is self.source:
                return

            try:
                tr = self.source.types[t.name]
            except KeyError:
                self.source.add_inclusion(t.definer)
                # Now a reference to type t must be in types of the source
                tr = self.source.types[t.name]

            self.replace(tr)

    """
    CopyVisitor is now used for true copying function body arguments
    in order to prevent wrong TypeReferences among them
    because before function prototype and body had the same args
    references (in terms of python references)
    """


class CopyFixerVisitor(ObjectVisitor):

    def __init__(self, type_object, *args, **kw):
        kw["field_name"] = "__type_references__"
        ObjectVisitor.__init__(self, type_object, *args, **kw)

    def on_visit(self):
        t = self.cur

        if (not isinstance(t, Type)
            or (isinstance(t, (Pointer, MacroType)) and not t.is_named)
        ):
            new_t = copy(t)

            self.replace(new_t, skip_trunk = False)
        else:
            raise BreakVisiting()

# Source code instances


class SourceChunk(object):
    """
`weight` is used during coarse chunk sorting. Chunks with less `weight` value
    are moved to the top of the file. Then all chunks are ordered topologically
    (with respect to inter-chunk references). But chunks which is not linked
    by references will preserve `weight`-based order.
    """
    weight = 5

    def __init__(self, origin, name, code, references = None):
        self.origin = origin
        self.name = name
        self.code = code
        # visited is used during depth-first sort
        self.visited = 0
        self.users = set()
        self.references = set()
        self.source = None
        if references is not None:
            for chunk in references:
                self.add_reference(chunk)

    def add_reference(self, chunk):
        self.references.add(chunk)
        chunk.users.add(self)

    def add_references(self, refs):
        for r in refs:
            self.add_reference(r)

    def del_reference(self, chunk):
        self.references.remove(chunk)
        chunk.users.remove(self)

    def clean_references(self):
        for r in list(self.references):
            self.del_reference(r)

    def check_cols_fix_up(self, max_cols = 80, indent = "    "):
        anc = "@a" # indent anchor
        can = "@c" # cancel indent anchor
        nbs = "@b" # non-breaking space
        nss = "@s" # non-slash space

        common_re = "(?<!@)((?:@@)*)({})"
        re_anc = compile(common_re.format(anc))
        re_can = compile(common_re.format(can))
        re_nbs = compile(common_re.format(nbs))
        re_nss = compile(common_re.format(nss))
        re_clr = compile("@(.|$)")

        lines = self.code.split('\n')
        code = ""
        last_line = len(lines) - 1

        for idx1, line in enumerate(lines):
            if idx1 == last_line and len(line) == 0:
                break;

            clear_line = re_clr.sub("\\1", re_anc.sub("\\1", re_can.sub("\\1",
                         re_nbs.sub("\\1 ", re_nss.sub("\\1 ", line)))))

            if len(clear_line) <= max_cols:
                code += clear_line + '\n'
                continue

            line_no_indent_len = len(line) - len(line.lstrip(' '))
            line_indent = line[:line_no_indent_len]
            indents = []
            indents.append(len(indent))
            tmp_indent = indent

            """
            1. cut off indent of the line
            2. surround non-slash spaces with ' ' moving them to separated words
            3. split the line onto words
            4. replace any non-breaking space with a regular space in each word
            """
            words = list(filter(None, map(
                lambda a: re_nbs.sub("\\1 ", a),
                re_nss.sub("\\1 " + nss + ' ', line.lstrip(' ')).split(' ')
            )))

            ll = 0 # line length
            last_word = len(words) - 1
            for idx2, word in enumerate(words):
                if word == nss:
                    slash = False
                    continue

                """ split the word onto anchor control sequences and n-grams
                around them """
                subwords = list(filter(None, chain(*map(
                    lambda a: re_can.split(a),
                    re_anc.split(word)
                ))))
                word = ""
                subword_indents = []
                for subword in subwords:
                    if subword == anc:
                        subword_indents.append(len(word))
                    elif subword == can:
                        if subword_indents:
                            subword_indents.pop()
                        else:
                            try:
                                indents.pop()
                            except IndexError:
                                raise RuntimeError("Trying to pop indent"
                                    " anchor from empty stack"
                                )
                    else:
                        word += re_clr.sub("\\1", subword)

                if ll > 0:
                    # The variable r reserves characters for " \\"
                    # that can be added after current word
                    if idx2 == last_word or words[idx2 + 1] == nss:
                        r = 0
                    else:
                        r = 2
                    """ If the line will be broken _after_ this word, its length
may be still longer than max_cols because of safe breaking (' \'). If so, brake
the line _before_ this word. Safe breaking is presented by 'r' variable in
the expression which is 0 if safe breaking is not required after this word.
                    """
                    if ll + 1 + len(word) + r > max_cols:
                        if slash:
                            code += " \\"
                        code += '\n' + line_indent + tmp_indent + word
                        ll = len(line_indent) + len(tmp_indent) + len(word)
                    else:
                        code += ' ' + word
                        ll += 1 + len(word)
                else:
                    code += line_indent + word
                    ll += len(line_indent) + len(word)

                word_indent = ll - len(line_indent) - len(word)
                for ind in subword_indents:
                    indents.append(word_indent + ind)
                tmp_indent = " " * indents[-1] if indents else ""
                slash = True

            code += '\n'

        self.code = '\n'.join(map(lambda a: a.rstrip(' '), code.split('\n')))

    def __lt__(self, other):
        sw = type(self).weight
        ow = type(other).weight
        if sw < ow:
            return True
        elif sw > ow:
            return False
        else:
            return self.name < other.name


class HeaderInclusion(SourceChunk):
    weight = 0

    def __init__(self, header):
        super(HeaderInclusion, self).__init__(header,
            "Header %s inclusion" % header.path,
            """\
#include {lq}{path}{rq}
""".format(
        lq = "<" if header.is_global else '"',
        # Always use UNIX path separator in `#include` directive.
        path = "/".join(path2tuple(header.path)),
        rq = ">" if header.is_global else '"'
            ),
            references = []
        )

    def __lt__(self, other):
        """ During coarse chunk sorting <global> header inclusions are moved to
        the top of "local". Same headers are ordered by path. """
        if isinstance(other, HeaderInclusion):
            shdr = self.origin
            ohdr = other.origin

            sg = shdr.is_global
            og = ohdr.is_global
            if sg == og:
                return shdr.path < ohdr.path
            else:
                # If self `is_global` flag is greater then order weight is less.
                return sg > og
        else:
            return super(HeaderInclusion, self).__lt__(other)


class MacroDefinition(SourceChunk):
    weight = 1

    def __init__(self, macro, indent = ""):
        if macro.args is None:
            args_txt = ""
        else:
            args_txt = '('
            for a in macro.args[:-1]:
                args_txt += a + ", "
            args_txt += macro.args[-1] + ')'

        super(MacroDefinition, self).__init__(macro,
            "Definition of macro %s" % macro.name,
            "%s#define %s%s%s" % (
                indent,
                macro.name,
                args_txt,
                "" if macro.text is None else (" %s" % macro.text)
            )
        )


class PointerTypeDeclaration(SourceChunk):

    def __init__(self, _type, def_name):
        self.def_name = def_name

        super(PointerTypeDeclaration, self).__init__(_type,
            "Definition of pointer to type " + self.type.name,
            "typedef@b" + self.type.name + "@b" + def_name
        )


class FunctionPointerTypeDeclaration(SourceChunk):

    def __init__(self, _type, def_name):
        self.def_name = def_name

        super(FunctionPointerTypeDeclaration, self).__init__(_type,
            "Definition of function pointer type " + self.type.name,
            ("typedef@b"
              + gen_function_declaration_string("", _type,
                    pointer_name = def_name
                )
              + ";\n"
            )
        )


class MacroTypeUsage(SourceChunk):

    def __init__(self, macro, initializer, indent):
        self.macro = macro
        self.initializer = initializer

        super(MacroTypeUsage, self).__init__(macro,
            "Usage of macro type " + macro.name,
            code = indent + macro.gen_usage_string(initializer)
        )


class PointerVariableDeclaration(SourceChunk):

    def __init__(self, var, indent = "", extern = False):
        t = var.type.type
        super(PointerVariableDeclaration, self).__init__(var,
            "Declaration of pointer %s to type %s" % (var.name, t.name),
            """\
{indent}{extern}{const}{type_name}@b*{var_name};
""".format(
                indent = indent,
                const = "const@b" if var.const else "",
                type_name = t.name,
                var_name = var.name,
                extern = "extern@b" if extern else ""
            )
        )


class FunctionPointerDeclaration(SourceChunk):

    def __init__(self, var, indent = "", extern = False):
        t = var.type.type
        super(FunctionPointerDeclaration, self).__init__(var,
            "Declaration of pointer %s to function %s" % (var.name, t.name),
            """\
{indent}{extern}{decl_str};
""".format(
        indent = indent,
        extern = "extern@b" if extern else "",
        decl_str = gen_function_declaration_string("", t,
            pointer_name = var.name,
            array_size = var.array_size
        )
            )
        )


class VariableDeclaration(SourceChunk):
    weight = 4

    def __init__(self, var, indent = "", extern = False):
        super(VariableDeclaration, self).__init__(var,
            "Variable %s of type %s declaration" % (
                var.name,
                var.type.name
            ),
            """\
{indent}{extern}{const}{type_name}@b{var_name}{array_decl};
""".format(
        indent = indent,
        const = "const@b" if var.const else "",
        type_name = var.type.name,
        var_name = var.name,
        array_decl = gen_array_declaration(var.array_size),
        extern = "extern@b" if extern else ""
    )
            )


class VariableDefinition(SourceChunk):
    weight = 5

    def __init__(self, var, indent = "", append_nl = True, enum = False):
        init_code = ""
        if var.initializer is not None:
            raw_code = var.type.gen_usage_string(var.initializer)
            # add indent to initializer code
            init_code_lines = raw_code.split('\n')
            init_code = "@b=@b" + init_code_lines[0]
            for line in init_code_lines[1:]:
                init_code += "\n" + indent + line

        super(VariableDefinition, self).__init__(var,
            "Variable %s of type %s definition" % (
                var.name, var.type.name
            ),
            """\
{indent}{static}{const}{type_name}@b{var_name}{array_decl}{init}{separ}{nl}
""".format(
        indent = indent,
        static = "static@b" if var.static else "",
        const = "const@b" if var.const else "",
        type_name = "" if enum else var.type.name,
        var_name = var.name,
        array_decl = gen_array_declaration(var.array_size),
        init = init_code,
        separ = "," if enum else ";",
        nl = "\n" if append_nl else ""
    )
        )


class StructureDeclarationBegin(SourceChunk):

    def __init__(self, struct, indent):
        super(StructureDeclarationBegin, self).__init__(struct,
            "Beginning of structure %s declaration" % struct.name,
            """\
{indent}typedef@bstruct@b{struct_name}@b{{
""".format(
    indent = indent,
    struct_name = struct.name
            )
        )


class StructureDeclaration(SourceChunk):
    weight = 2

    def __init__(self, struct, fields_indent = "    ", indent = "",
                 append_nl = True):
        super(StructureDeclaration, self).__init__(struct,
            "Ending of structure %s declaration" % struct.name,
            """\
{indent}}}@b{struct_name};{nl}
""".format(
    indent = indent,
    struct_name = struct.name,
    nl = "\n" if append_nl else ""
            ),
        )


class EnumerationDeclarationBegin(SourceChunk):

    def __init__(self, enum, indent = ""):
        super(EnumerationDeclarationBegin, self).__init__(enum,
            "Beginning of enumeration %s declaration" % enum.enum_name,
            """\
{indent}enum@b{enum_name}@b{{
""".format(
    indent = indent,
    enum_name = enum.enum_name
            )
        )


class EnumerationDeclaration(SourceChunk):
    weight = 3

    def __init__(self, enum, indent = ""):
        super(EnumerationDeclaration, self).__init__(enum,
            "Ending of enumeration %s declaration" % enum.enum_name,
            """\
{indent}}};\n
""".format(indent = indent, enum_name = enum.enum_name)
        )


class UsageTerminator(SourceChunk):

    def __init__(self, usage, **kw):
        super(UsageTerminator, self).__init__(usage,
            "Variable %s usage terminator" % usage.name, ";\n", **kw
        )


def gen_array_declaration(array_size):
    if array_size is not None:
        if array_size == 0:
            array_decl = "[]"
        elif array_size > 0:
            array_decl = '[' + str(array_size) + ']'
    else:
        array_decl = ""
    return array_decl


def gen_function_declaration_string(indent, function,
    pointer_name = None,
    array_size = None
):
    if function.args is None:
        args = "void"
    else:
        args = ""
        for a in function.args:
            args += a.type.name + "@b" + a.name
            if not a == function.args[-1]:
                args += ",@s"

    if function.name.find(".body") != -1:
        decl_name = function.name[:-5]
    else:
        decl_name = function.name

    return "{indent}{static}{inline}{ret_type}{name}(@a{args})".format(
        indent = indent,
        static = "static@b" if function.static else "",
        inline = "inline@b" if function.inline else "",
        ret_type = function.ret_type.name + "@b",
        name = decl_name if pointer_name is None else (
            "(*" + pointer_name + gen_array_declaration(array_size) + ')'
        ),
        args = args
    )


def gen_function_decl_ref_chunks(function, generator):
    references = list(generator.provide_chunks(function.ret_type))

    if function.args is not None:
        for a in function.args:
            references.extend(generator.provide_chunks(a.type))

    return references


def gen_function_def_ref_chunks(f, generator):
    references = []

    v = TypesCollector(f.body)
    v.visit()

    for t in v.used_types:
        references.extend(generator.provide_chunks(t))

    if isinstance(f.body, FunctionBodyString):
        for g in f.body.used_globals:
            # Note that 0-th chunk is the global and rest are its dependencies
            references.append(generator.provide_chunks(g)[0])

    return references


class FunctionDeclaration(SourceChunk):
    weight = 6

    def __init__(self, function, indent = ""):
        super(FunctionDeclaration, self).__init__(function,
            "Declaration of function %s" % function.name,
            "%s;" % gen_function_declaration_string(indent, function)
        )


class FunctionDefinition(SourceChunk):

    def __init__(self, function, indent = "", append_nl = True):
        body = " {}" if function.body is None else "\n{\n%s}" % function.body

        if append_nl:
            body += "\n"

        super(FunctionDefinition, self).__init__(function,
            "Definition of function %s" % function.name,
            "{dec}{body}\n".format(
                dec = gen_function_declaration_string(indent, function),
                body = body
            )
        )


def depth_first_sort(chunk, new_chunks):
    # visited:
    # 0 - not visited
    # 1 - visited
    # 2 - added to new_chunks
    chunk.visited = 1
    for ch in sorted(chunk.references):
        if ch.visited == 2:
            continue
        if ch.visited == 1:
            raise RuntimeError("A loop is found in source chunk references")
        depth_first_sort(ch, new_chunks)

    chunk.visited = 2
    new_chunks.add(chunk)


class SourceFile(object):

    def __init__(self, name, is_header = False, protection = True):
        self.name = name
        self.is_header = is_header
        # Note that, chunk order is significant while one reference per chunk
        # is enough.
        self.chunks = OrderedSet()
        self.sort_needed = False
        self.protection = protection

    def gen_chunks_graph(self, w):
        w.write("""\
digraph Chunks {
    rankdir=BT;
    node [shape=polygon fontname=Momospace]
    edge [style=filled]

"""
        )

        w.write("    /* Chunks */\n")

        def chunk_node_name(chunk, mapping = {}, counter = count(0)):
            try:
                name = mapping[chunk]
            except KeyError:
                name = "ch_%u" % next(counter)
                mapping[chunk] = name
            return name

        upper_cnn = None
        for ch in self.chunks:
            cnn = chunk_node_name(ch)
            w.write('\n    %s [label="%s"]\n' % (cnn, ch.name))

            # invisible edges provides vertical order like in the output file
            if upper_cnn is not None:
                w.write("\n    %s -> %s [style=invis]\n" % (cnn, upper_cnn))
            upper_cnn = cnn

            if ch.references:
                w.write("        /* References */\n")
                for ref in sorted(ch.references):
                    w.write("        %s -> %s\n" % (cnn, chunk_node_name(ref)))

        w.write("}\n")

    def gen_chunks_gv_file(self, file_name):
        f = open(file_name, "w")
        self.gen_chunks_graph(f)
        f.close()

    def remove_dup_chunk(self, ch, ch_remove):
        for user in list(ch_remove.users):
            user.del_reference(ch_remove)
            # prevent self references
            if user is not ch:
                user.add_reference(ch)

        self.chunks.remove(ch_remove)

    def remove_chunks_with_same_origin(self, types = []):
        for t in types:
            exists = {}

            for ch in list(self.chunks):
                if type(ch) is not t: # exact type match is required
                    continue

                origin = ch.origin

                try:
                    ech = exists[origin]
                except KeyError:
                    exists[origin] = ch
                    continue

                self.remove_dup_chunk(ech, ch)

                self.sort_needed = True

    def sort_chunks(self):
        if not self.sort_needed:
            return

        new_chunks = OrderedSet()
        # topology sorting
        for chunk in self.chunks:
            if not chunk.visited == 2:
                depth_first_sort(chunk, new_chunks)

        for chunk in new_chunks:
            chunk.visited = 0

        self.chunks = new_chunks

    def add_chunks(self, chunks):
        for ch in chunks:
            self.add_chunk(ch)

    def add_chunk(self, chunk):
        if chunk.source is None:
            self.sort_needed = True
            self.chunks.add(chunk)

            # Also add referenced chunks into the source
            for ref in chunk.references:
                self.add_chunk(ref)
        elif not chunk.source == self:
            raise RuntimeError("The chunk %s is already in %s"
                % (chunk.name, chunk.source.name)
            )

    def optimize_inclusions(self, log = lambda *args, **kw : None):
        log("-= inclusion optimization started for %s.%s =-" % (
            (self.name, "h" if self.is_header else "c")
        ))

        # use `visited` flag to prevent dead loop in case of inclusion cycle
        for h in Header.reg.values():
            h.visited = False
            h.root = None

        # Dictionary is used for fast lookup HeaderInclusion by Header.
        # Assuming only one inclusion per header.
        included_headers = {}

        for ch in self.chunks:
            if isinstance(ch, HeaderInclusion):
                h = ch.origin
                if h in included_headers:
                    raise RuntimeError("Duplicate inclusions must be removed "
                        " before inclusion optimization."
                    )
                included_headers[h] = ch
                # root is originally included header.
                h.root = h

        log("Originally included:\n"
            + "\n".join(h.path for h in included_headers)
        )

        stack = list(included_headers)

        while stack:
            h = stack.pop()

            for sp in h.inclusions:
                s = Header.lookup(sp)
                if s in included_headers:
                    """ If an originally included header (s) is transitively
included from another one (h.root) then inclusion of s is redundant and must
be deleted. All references to it must be redirected to inclusion of h (h.root).
                    """
                    redundant = included_headers[s]
                    substitution = included_headers[h.root]

                    """ Because the header inclusion graph is not acyclic,
a header can (transitively) include itself. Then nothing is to be substituted.
                    """
                    if redundant is substitution:
                        log("Cycle: " + s.path)
                        continue

                    if redundant.origin is not s:
                        # inclusion of s was already removed as redundant
                        log("%s includes %s which already substituted by "
                            "%s" % (h.root.path, s.path, redundant.origin.path)
                        )
                        continue

                    log("%s includes %s, substitute %s with %s" % (
                        h.root.path, s.path, redundant.origin.path,
                        substitution.origin.path
                    ))

                    self.remove_dup_chunk(substitution, redundant)

                    """ The inclusion of s was removed but s could transitively
include another header (s0) too. Then inclusion of any s0 must be removed and
all references to it must be redirected to inclusion of h. Hence, reference to
inclusion of h must be remembered. This algorithm keeps it in included_headers
replacing reference to removed inclusion of s. If s was processed before h then
there could be several references to inclusion of s in included_headers. All of
them must be replaced with reference to h. """
                    for hdr, chunk in included_headers.items():
                        if chunk is redundant:
                            included_headers[hdr] = substitution

                if s.root is None:
                    stack.append(s)
                    # Keep reference to originally included header.
                    s.root = h.root

        # Clear runtime variables
        for h in Header.reg.values():
            del h.visited
            del h.root

        log("-= inclusion optimization ended =-")

    def check_static_function_declarations(self):
        func_dec = {}
        for ch in list(self.chunks):
            if not isinstance(ch, (FunctionDeclaration, FunctionDefinition)):
                continue

            f = ch.origin

            if not f.static:
                continue

            try:
                prev_ch = func_dec[f]
            except KeyError:
                func_dec[f] = ch
                continue

            # TODO: check for loops in references, the cases in which forward
            # declarations is really needed.

            if isinstance(ch, FunctionDeclaration):
                self.remove_dup_chunk(prev_ch, ch)
            elif type(ch) is type(prev_ch):
                # prev_ch and ch are FunctionDefinition
                self.remove_dup_chunk(prev_ch, ch)
            else:
                # prev_ch is FunctionDeclaration but ch is FunctionDefinition
                self.remove_dup_chunk(ch, prev_ch)
                func_dec[f] = ch

    def generate(self, writer,
        gen_debug_comments = False,
        append_nl_after_headers = True
    ):
        self.remove_chunks_with_same_origin([
            HeaderInclusion
        ])

        self.check_static_function_declarations()

        self.sort_chunks()

        self.optimize_inclusions()

        # semantic sort
        self.chunks = OrderedSet(sorted(self.chunks))

        self.sort_chunks()

        writer.write(
            "/* %s.%s */\n" % (self.name, "h" if self.is_header else "c")
        )

        if self.is_header and self.protection:
            writer.write("""\
#ifndef INCLUDE_{name}_H
#define INCLUDE_{name}_H
""".format(name = self.name_for_macro())
            )

        prev_header = False

        for chunk in self.chunks:
            if isinstance(chunk, HeaderInclusion):
                prev_header = True
            else:
                if append_nl_after_headers and prev_header:
                    writer.write("\n")
                prev_header = False

            chunk.check_cols_fix_up()

            if gen_debug_comments:
                writer.write("/* source chunk %s */\n" % chunk.name)
            writer.write(chunk.code)

        if self.is_header and self.protection:
            writer.write(
                "#endif /* INCLUDE_%s_H */\n" % self.name_for_macro()
            )

    def name_for_macro(self):
        return macro_forbidden.sub('_', self.name.upper())

# Source tree container


HDB_HEADER_PATH = "path"
HDB_HEADER_IS_GLOBAL = "is_global"
HDB_HEADER_INCLUSIONS = "inclusions"
HDB_HEADER_MACROS = "macros"


class SourceTreeContainer(object):
    current = None

    def __init__(self):
        self.reg_header = {}
        self.reg_type = {}

        # add preprocessor macros those are always defined
        prev = self.set_cur_stc()

        CPPMacro("__FILE__")
        CPPMacro("__LINE__")
        CPPMacro("__FUNCTION__")

        if prev is not None:
            prev.set_cur_stc()

    def type_lookup(self, name):
        if name not in self.reg_type:
            raise TypeNotRegistered("Type with name %s is not registered"
                % name
            )
        return self.reg_type[name]

    def type_exists(self, name):
        try:
            self.type_lookup(name)
            return True
        except TypeNotRegistered:
            return False

    def header_lookup(self, path):
        tpath = path2tuple(path)

        if tpath not in self.reg_header:
            raise RuntimeError("Header with path %s is not registered" % path)
        return self.reg_header[tpath]

    def gen_header_inclusion_dot_file(self, dot_file_name):
        dot_writer = open(dot_file_name, "w")

        dot_writer.write("""\
digraph HeaderInclusion {
    node [shape=polygon fontname=Monospace]
    edge[style=filled]

"""
        )

        def _header_path_to_node_name(path):
            return path.replace(".", "_").replace("/", "__").replace("-", "_")

        dot_writer.write("    /* Header nodes: */\n")
        for h in  self.reg_header.values():
            node = _header_path_to_node_name(h.path)
            dot_writer.write('    %s [label="%s"]\n' % (node, h.path))

        dot_writer.write("\n    /* Header dependencies: */\n")

        for h in self.reg_header.values():
            h_node = _header_path_to_node_name(h.path)
            for i in h.inclusions.values():
                i_node = _header_path_to_node_name(i.path)

                dot_writer.write("    %s -> %s\n" % (i_node, h_node))

        dot_writer.write("}\n")

        dot_writer.close()

    def co_load_header_db(self, list_headers):
        # Create all headers
        for dict_h in list_headers:
            path = dict_h[HDB_HEADER_PATH]
            if path2tuple(path) not in self.reg_header:
                Header(
                    path = dict_h[HDB_HEADER_PATH],
                    is_global = dict_h[HDB_HEADER_IS_GLOBAL]
                )
            else:
                # Check if existing header equals the one from database?
                pass

        # Set up inclusions
        for dict_h in list_headers:
            yield

            path = dict_h[HDB_HEADER_PATH]
            h = self.header_lookup(path)

            for inc in dict_h[HDB_HEADER_INCLUSIONS]:
                i = self.header_lookup(inc)
                h.add_inclusion(i)

            for m in dict_h[HDB_HEADER_MACROS]:
                h.add_type(Macro.new_from_dict(m))

    def create_header_db(self):
        list_headers = []
        for h in self.reg_header.values():
            dict_h = {}
            dict_h[HDB_HEADER_PATH] = h.path
            dict_h[HDB_HEADER_IS_GLOBAL] = h.is_global

            inc_list = []
            for i in h.inclusions.values():
                inc_list.append(i.path)
            dict_h[HDB_HEADER_INCLUSIONS] = inc_list

            macro_list = []
            for t in h.types.values():
                if isinstance(t, Macro):
                    macro_list.append(t.gen_dict())
            dict_h[HDB_HEADER_MACROS] = macro_list

            list_headers.append(dict_h)

        return list_headers

    def set_cur_stc(self):
        Header.reg = self.reg_header
        Type.reg = self.reg_type

        previous = SourceTreeContainer.current
        SourceTreeContainer.current = self
        return previous


SourceTreeContainer().set_cur_stc()
