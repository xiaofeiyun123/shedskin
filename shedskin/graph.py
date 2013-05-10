'''
*** SHED SKIN Python-to-C++ Compiler ***
Copyright 2005-2011 Mark Dufour; License GNU GPL version 3 (See LICENSE)

graph.py: build constraint graph used in dataflow analysis

constraint graph: graph along which possible types 'flow' during an 'abstract execution' of a program (a dataflow analysis). consider the assignment statement 'a = b'. it follows that the set of possible types of b is smaller than or equal to that of a (a constraint). we can determine possible types of a, by 'flowing' the types from b to a, in other words, along the constraint.

constraint graph nodes are stored in getgx().cnode, and the set of types of for each node in getgx().types. nodes are identified by an AST Node, and two integers. the integers are used in infer.py to duplicate parts of the constraint graph along two dimensions. in the initial constraint graph, these integers are always 0.

class ModuleVisitor: inherits visitor pattern from compiler.visitor.ASTVisitor, to recursively generate constraints for each syntactical Python construct. for example, the visitFor method is called in case of a for-loop. temporary variables are introduced in many places, to enable translation to a lower-level language.
    import ipdb; ipdb.set_trace()  # BREAKPOINT

parse_module(): locate module by name (e.g. 'os.path'), and use ModuleVisitor if not cached

'''
import os
import imp
import sys
import copy
import re
from compiler import parse
from compiler.ast import Const, AssTuple, AssList, From, Add, ListCompFor, \
    UnaryAdd, Import, Bitand, Stmt, Assign, FloorDiv, Not, Mod, AssAttr, \
    Keyword, GenExpr, LeftShift, AssName, Div, Or, Lambda, And, CallFunc, \
    Global, Slice, RightShift, Sub, Getattr, Dict, Ellipsis, Mul, \
    Subscript, Function as FunctionNode, Return, Power, Bitxor, Class, Name, List, \
    Discard, Sliceobj, Tuple, Pass, UnarySub, Bitor, ListComp
from compiler.visitor import ASTVisitor

from shared import setmv, inode, is_zip2, FakeGetattr, in_out, Function, StaticClass, getmv, register_temp_var, lookup_class, aug_msg, lookup_func, default_var, FakeGetattr2, FakeGetattr3, Module, is_literal, is_enum, is_method, def_class, CNode, is_fastfor, assign_rec, class_, is_property_setter, lookup_var
from struct_ import struct_faketuple, struct_info, struct_unpack
from error import error
import config


# --- module visitor; analyze program, build constraint graph

class ModuleVisitor(ASTVisitor):
    def __init__(self, module):
        ASTVisitor.__init__(self)
        self.module = module
        self.classes = {}
        self.funcs = {}
        self.globals = {}
        self.lambdas = {}
        self.imports = {}
        self.fake_imports = {}
        self.ext_classes = {}
        self.ext_funcs = {}
        self.lambdaname = {}
        self.lwrapper = {}
        self.tempcount = config.getgx().tempcount
        self.callfuncs = []
        self.for_in_iters = []
        self.listcomps = []
        self.defaults = {}
        self.importnodes = []

    def dispatch(self, node, *args):
        if (node, 0, 0) not in config.getgx().cnode:
            ASTVisitor.dispatch(self, node, *args)

    def fake_func(self, node, objexpr, attrname, args, func):
        if (node, 0, 0) in config.getgx().cnode:  # XXX
            newnode = config.getgx().cnode[node, 0, 0]
        else:
            newnode = CNode(node, parent=func)
            config.getgx().types[newnode] = set()

        fakefunc = CallFunc(Getattr(objexpr, attrname), args)
        fakefunc.lineno = objexpr.lineno
        self.visit(fakefunc, func)
        self.add_constraint((inode(fakefunc), newnode), func)

        inode(objexpr).fakefunc = fakefunc
        return fakefunc

    # simple heuristic for initial list split: count nesting depth, first constant child type
    def list_type(self, node):
        count = 0
        child = node
        while isinstance(child, (List, ListComp)):
            if not child.getChildNodes():
                return None
            child = child.getChildNodes()[0]
            count += 1

        if isinstance(child, (UnarySub, UnaryAdd)):
            child = child.expr

        if isinstance(child, CallFunc) and isinstance(child.node, Name):
            map = {'int': int, 'str': str, 'float': float}
            if child.node.name in ('range'):  # ,'xrange'):
                count, child = count + 1, int
            elif child.node.name in map:
                child = map[child.node.name]
            elif child.node.name in (cl.ident for cl in config.getgx().allclasses) or child.node.name in getmv().classes:  # XXX getmv().classes
                child = child.node.name
            else:
                if count == 1:
                    return None
                child = None
        elif isinstance(child, Const):
            child = type(child.value)
        elif isinstance(child, Name) and child.name in ('True', 'False'):
            child = bool
        elif isinstance(child, Tuple):
            child = tuple
        elif isinstance(child, Dict):
            child = dict
        else:
            if count == 1:
                return None
            child = None

        config.getgx().list_types.setdefault((count, child), len(config.getgx().list_types) + 2)
        # print 'listtype', node, getgx().list_types[count, child]
        return config.getgx().list_types[count, child]

    def instance(self, node, cl, func=None):
        if (node, 0, 0) in config.getgx().cnode:  # XXX to create_node() func
            newnode = config.getgx().cnode[node, 0, 0]
        else:
            newnode = CNode(node, parent=func)

        newnode.constructor = True

        if cl.ident in ['int_', 'float_', 'str_', 'none', 'class_', 'bool_']:
            config.getgx().types[newnode] = set([(cl, cl.dcpa - 1)])
        else:
            if cl.ident == 'list' and self.list_type(node):
                config.getgx().types[newnode] = set([(cl, self.list_type(node))])
            else:
                config.getgx().types[newnode] = set([(cl, cl.dcpa)])

    def constructor(self, node, classname, func):
        cl = def_class(classname)

        self.instance(node, cl, func)
        default_var('unit', cl)

        if classname in ['list', 'tuple'] and not node.nodes:
            config.getgx().empty_constructors.add(node)  # ifa disables those that flow to instance variable assignments

        # --- internally flow binary tuples
        if cl.ident == 'tuple2':
            default_var('first', cl)
            default_var('second', cl)
            elem0, elem1 = node.nodes

            self.visit(elem0, func)
            self.visit(elem1, func)

            self.add_dynamic_constraint(node, elem0, 'unit', func)
            self.add_dynamic_constraint(node, elem1, 'unit', func)

            self.add_dynamic_constraint(node, elem0, 'first', func)
            self.add_dynamic_constraint(node, elem1, 'second', func)

            return

        # --- add dynamic children constraints for other types
        if classname == 'dict':  # XXX filter children
            default_var('unit', cl)
            default_var('value', cl)

            for child in node.getChildNodes():
                self.visit(child, func)

            for (key, value) in node.items:  # XXX filter
                self.add_dynamic_constraint(node, key, 'unit', func)
                self.add_dynamic_constraint(node, value, 'value', func)
        else:
            for child in node.nodes:
                self.visit(child, func)

            for child in self.filter_redundant_children(node):
                self.add_dynamic_constraint(node, child, 'unit', func)

    # --- for compound list/tuple/dict constructors, we only consider a single child node for each subtype
    def filter_redundant_children(self, node):
        done = set()
        nonred = []
        for child in node.nodes:
            type = self.child_type_rec(child)
            if not type or not type in done:
                done.add(type)
                nonred.append(child)

        return nonred

    # --- determine single constructor child node type, used by the above
    def child_type_rec(self, node):
        if isinstance(node, (UnarySub, UnaryAdd)):
            node = node.expr

        if isinstance(node, (List, Tuple)):
            if isinstance(node, List):
                cl = def_class('list')
            elif len(node.nodes) == 2:
                cl = def_class('tuple2')
            else:
                cl = def_class('tuple')

            merged = set()
            for child in node.nodes:
                merged.add(self.child_type_rec(child))

            if len(merged) == 1:
                return (cl, merged.pop())

        elif isinstance(node, Const):
            return (list(inode(node).types())[0][0],)

    # --- add dynamic constraint for constructor argument, e.g. '[expr]' becomes [].__setattr__('unit', expr)
    def add_dynamic_constraint(self, parent, child, varname, func):
        # print 'dynamic constr', child, parent

        config.getgx().assign_target[child] = parent
        cu = Const(varname)
        self.visit(cu, func)
        fakefunc = CallFunc(FakeGetattr2(parent, '__setattr__'), [cu, child])
        self.visit(fakefunc, func)

        fakechildnode = CNode((child, varname), parent=func)  # create separate 'fake' CNode per child, so we can have multiple 'callfuncs'
        config.getgx().types[fakechildnode] = set()

        self.add_constraint((inode(parent), fakechildnode), func)  # add constraint from parent to fake child node. if parent changes, all fake child nodes change, and the callfunc for each child node is triggered
        fakechildnode.callfuncs.append(fakefunc)

    # --- add regular constraint to function
    def add_constraint(self, constraint, func):
        in_out(constraint[0], constraint[1])
        config.getgx().constraints.add(constraint)
        while isinstance(func, Function) and func.listcomp:
            func = func.parent  # XXX
        if isinstance(func, Function):
            func.constraints.add(constraint)

    def visitExec(self, node, func=None):
        error("'exec' is not supported", node, mv=self)

    def visitGenExpr(self, node, func=None):
        newnode = CNode(node, parent=func)
        config.getgx().types[newnode] = set()
        lc = ListComp(node.code.expr, [ListCompFor(qual.assign, qual.iter, qual.ifs, qual.lineno) for qual in node.code.quals], lineno=node.lineno)
        register_node(lc, func)
        config.getgx().genexp_to_lc[node] = lc
        self.visit(lc, func)
        self.add_constraint((inode(lc), newnode), func)

    def visitStmt(self, node, func=None):
        comments = []
        for b in node.nodes:
            if isinstance(b, Discard):
                self.bool_test_add(b.expr)
            if isinstance(b, Discard) and isinstance(b.expr, Const) and type(b.expr.value) == str:
                comments.append(b.expr.value)
            elif comments:
                config.getgx().comments[b] = comments
                comments = []
            self.visit(b, func)

    def visitModule(self, node):
        # --- bootstrap built-in classes
        if self.module.ident == 'builtin':
            for dummy in config.getgx().builtins:
                self.visit(Class(dummy, [], None, Pass()))

        if self.module.ident != 'builtin':
            n = From('builtin', [('*', None)], None)  # Python2.5+
            getmv().importnodes.append(n)
            self.visit(n)

        # --- __name__
        if self.module.ident != 'builtin':
            namevar = default_var('__name__', None)
            config.getgx().types[inode(namevar)] = set([(def_class('str_'), 0)])

        self.forward_references(node)

        # --- visit children
        for child in node.getChildNodes():
            if isinstance(child, Stmt):
                getmv().importnodes.extend(n for n in child.nodes if isinstance(n, (Import, From)))
            self.visit(child, None)

        # --- register classes
        for cl in getmv().classes.values():
            config.getgx().allclasses.add(cl)

        # --- inheritance expansion

        # determine base classes
        for cl in self.classes.values():
            for base in cl.node.bases:
                if not (isinstance(base, Name) and base.name == 'object'):
                    ancestor = lookup_class(base, getmv())
                    cl.bases.append(ancestor)
                    ancestor.children.append(cl)

        # for each base class, duplicate methods
        for cl in self.classes.values():
            for ancestor in cl.ancestors_upto(None)[1:]:

                cl.staticmethods.extend(ancestor.staticmethods)
                cl.properties.update(ancestor.properties)

                for func in ancestor.funcs.values():
                    if not func.node or func.inherited:
                        continue

                    ident = func.ident
                    if ident in cl.funcs:
                        ident += ancestor.ident + '__'

                    # deep-copy AST function nodes
                    func_copy = copy.deepcopy(func.node)
                    inherit_rec(func.node, func_copy, func.mv)
                    tempmv, mv = getmv(), func.mv
                    setmv(mv)
                    self.visitFunction(func_copy, cl, inherited_from=ancestor)
                    mv = tempmv
                    setmv(mv)

                    # maintain relation with original
                    config.getgx().inheritance_relations.setdefault(func, []).append(cl.funcs[ident])
                    cl.funcs[ident].inherited = func.node
                    cl.funcs[ident].inherited_from = func
                    func_copy.name = ident

                    if ident == func.ident:
                        cl.funcs[ident + ancestor.ident + '__'] = cl.funcs[ident]

    def stmt_nodes(self, node, cl):
        result = []
        for child in node.getChildNodes():
            if isinstance(child, Stmt):
                for n in child.nodes:
                    if isinstance(n, cl):
                        result.append(n)
        return result

    def forward_references(self, node):
        getmv().classnodes = []

        # classes
        for n in self.stmt_nodes(node, Class):
            check_redef(n)
            getmv().classnodes.append(n)
            newclass = class_(n)
            self.classes[n.name] = newclass
            getmv().classes[n.name] = newclass
            newclass.module = self.module
            newclass.parent = StaticClass(newclass)

            # methods
            for m in self.stmt_nodes(n, FunctionNode):
                if hasattr(m, 'decorators') and m.decorators and [dec for dec in m.decorators if is_property_setter(dec)]:
                    m.name = m.name + '__setter__'
                if m.name in newclass.funcs:  # and func.ident not in ['__getattr__', '__setattr__']: # XXX
                    error("function/class redefinition is not allowed", m, mv=self)
                func = Function(m, newclass)
                newclass.funcs[func.ident] = func
                self.set_default_vars(m, func)

        # functions
        getmv().funcnodes = []
        for n in self.stmt_nodes(node, FunctionNode):
            check_redef(n)
            getmv().funcnodes.append(n)
            func = getmv().funcs[n.name] = Function(n)
            self.set_default_vars(n, func)

        # global variables XXX visitGlobal
        for assname in self.local_assignments(node, global_=True):
            default_var(assname.name, None)

    def set_default_vars(self, node, func):
        globals = set(self.get_globals(node))
        for assname in self.local_assignments(node):
            if assname.name not in globals:
                default_var(assname.name, func)

    def get_globals(self, node):
        if isinstance(node, Global):
            result = node.names
        else:
            result = []
            for child in node.getChildNodes():
                result.extend(self.get_globals(child))
        return result

    def local_assignments(self, node, global_=False):
        if global_ and isinstance(node, (Class, FunctionNode)):
            return []
        elif isinstance(node, (ListComp, GenExpr)):
            return []
        elif isinstance(node, AssName):
            result = [node]
        else:
            result = []
            for child in node.getChildNodes():
                result.extend(self.local_assignments(child, global_))
        return result

    def visitImport(self, node, func=None):
        if not node in getmv().importnodes:
            error("please place all imports (no 'try:' etc) at the top of the file", node, mv=self)

        for (name, pseudonym) in node.names:
            if pseudonym:
                # --- import a.b as c: don't import a
                self.import_module(name, pseudonym, node, False)
            else:
                self.import_modules(name, node, False)

    def import_modules(self, name, node, fake):
        # --- import a.b.c: import a, then a.b, then a.b.c
        split = name.split('.')
        module = getmv().module
        for i in range(len(split)):
            subname = '.'.join(split[:i + 1])
            parent = module
            module = self.import_module(subname, subname, node, fake)
            if module.ident not in parent.mv.imports:  # XXX
                if not fake:
                    parent.mv.imports[module.ident] = module
        return module

    def import_module(self, name, pseudonym, node, fake):
        module = self.analyze_module(name, pseudonym, node, fake)
        if not fake:
            var = default_var(pseudonym or name, None)
            var.imported = True
            config.getgx().types[inode(var)] = set([(module, 0)])
        return module

    def visitFrom(self, node, parent=None):
        if not node in getmv().importnodes:  # XXX use (func, node) as parent..
            error("please place all imports (no 'try:' etc) at the top of the file", node, mv=self)
        if hasattr(node, 'level') and node.level:
            error("relative imports are not supported", node, mv=self)

        if node.modname == '__future__':
            for name, _ in node.names:
                if name not in ['with_statement', 'print_function']:
                    error("future '%s' is not yet supported" % name, node, mv=self)
            return

        module = self.import_modules(node.modname, node, True)
        config.getgx().from_module[node] = module

        for name, pseudonym in node.names:
            if name == '*':
                self.ext_funcs.update(module.mv.funcs)
                self.ext_classes.update(module.mv.classes)
                for import_name, import_module in module.mv.imports.items():
                    var = default_var(import_name, None)  # XXX merge
                    var.imported = True
                    config.getgx().types[inode(var)] = set([(import_module, 0)])
                    self.imports[import_name] = import_module
                for name, extvar in module.mv.globals.items():
                    if not extvar.imported and not name in ['__name__']:
                        var = default_var(name, None)  # XXX merge
                        var.imported = True
                        self.add_constraint((inode(extvar), inode(var)), None)
                continue

            path = module.path
            pseudonym = pseudonym or name
            if name in module.mv.funcs:
                self.ext_funcs[pseudonym] = module.mv.funcs[name]
            elif name in module.mv.classes:
                self.ext_classes[pseudonym] = module.mv.classes[name]
            elif name in module.mv.globals and not module.mv.globals[name].imported:  # XXX
                extvar = module.mv.globals[name]
                var = default_var(pseudonym, None)
                var.imported = True
                self.add_constraint((inode(extvar), inode(var)), None)
            elif os.path.isfile(os.path.join(path, name + '.py')) or \
                    os.path.isfile(os.path.join(path, name, '__init__.py')):
                modname = '.'.join(module.name_list + [name])
                self.import_module(modname, name, node, False)
            else:
                error("no identifier '%s' in module '%s'" % (name, node.modname), node, mv=self)

    def analyze_module(self, name, pseud, node, fake):
        module = parse_module(name, getmv().module, node)
        if not fake:
            self.imports[pseud] = module
        else:
            self.fake_imports[pseud] = module
        return module

    def visitFunction(self, node, parent=None, is_lambda=False, inherited_from=None):
        if not getmv().module.builtin and (node.varargs or node.kwargs):
            error('argument (un)packing is not supported', node, mv=self)

        if not parent and not is_lambda and node.name in getmv().funcs:
            func = getmv().funcs[node.name]
        elif isinstance(parent, class_) and not inherited_from and node.name in parent.funcs:
            func = parent.funcs[node.name]
        else:
            func = Function(node, parent, inherited_from)
            if inherited_from:
                self.set_default_vars(node, func)

        if not is_method(func):
            if not getmv().module.builtin and not node in getmv().funcnodes and not is_lambda:
                error("non-global function '%s'" % node.name, node, mv=self)

        if hasattr(node, 'decorators') and node.decorators:
            for dec in node.decorators.nodes:
                if isinstance(dec, Name) and dec.name == 'staticmethod':
                    parent.staticmethods.append(node.name)
                elif isinstance(dec, Name) and dec.name == 'property':
                    parent.properties[node.name] = [node.name, None]
                elif is_property_setter(dec):
                    parent.properties[dec.expr.name][1] = node.name
                else:
                    error("unsupported type of decorator", dec, mv=self)

        if parent:
            if not inherited_from and not func.ident in parent.staticmethods and (not func.formals or func.formals[0] != 'self'):
                error("formal arguments of method must start with 'self'", node, mv=self)
            if not func.mv.module.builtin and func.ident in ['__new__', '__getattr__', '__setattr__', '__radd__', '__rsub__', '__rmul__', '__rdiv__', '__rtruediv__', '__rfloordiv__', '__rmod__', '__rdivmod__', '__rpow__', '__rlshift__', '__rrshift__', '__rand__', '__rxor__', '__ror__', '__iter__', '__call__', '__enter__', '__exit__', '__del__', '__copy__', '__deepcopy__']:
                error("'%s' is not supported" % func.ident, node, warning=True, mv=self)

        if is_lambda:
            self.lambdas[node.name] = func

        # --- add unpacking statement for tuple formals
        func.expand_args = {}
        for i, formal in enumerate(func.formals):
            if isinstance(formal, tuple):
                tmp = self.temp_var((node, i), func)
                func.formals[i] = tmp.name
                fake_unpack = Assign([self.unpack_rec(formal)], Name(tmp.name))
                func.expand_args[tmp.name] = fake_unpack
                self.visit(fake_unpack, func)

        func.defaults = node.defaults

        for formal in func.formals:
            var = default_var(formal, func)
            var.formal_arg = True

        # --- flow return expressions together into single node
        func.retnode = retnode = CNode(node, parent=func)
        config.getgx().types[retnode] = set()
        func.yieldnode = yieldnode = CNode((node, 'yield'), parent=func)
        config.getgx().types[yieldnode] = set()

        self.visit(node.code, func)

        for i, default in enumerate(func.defaults):
            if not is_literal(default):
                self.defaults[default] = (len(self.defaults), func, i)
            self.visit(default, None)  # defaults are global

        # --- add implicit 'return None' if no return expressions
        if not func.returnexpr:
            func.fakeret = Return(Name('None'))
            self.visit(func.fakeret, func)

        # --- register function
        if isinstance(parent, class_):
            if func.ident not in parent.staticmethods:  # XXX use flag
                default_var('self', func)
                if func.ident == '__init__' and '__del__' in parent.funcs:  # XXX what if no __init__
                    self.visit(CallFunc(Getattr(Name('self'), '__del__'), []), func)
                    config.getgx().gc_cleanup = True
            parent.funcs[func.ident] = func

    def unpack_rec(self, formal):
        if isinstance(formal, str):
            return AssName(formal, 'OP_ASSIGN')
        else:
            return AssTuple([self.unpack_rec(elem) for elem in formal])

    def visitLambda(self, node, func=None):
        lambdanr = len(self.lambdas)
        name = '__lambda%d__' % lambdanr
        fakenode = FunctionNode(None, name, node.argnames, node.defaults, node.flags, None, Return(node.code))
        self.visit(fakenode, None, True)
        f = self.lambdas[name]
        f.lambdanr = lambdanr
        self.lambdaname[node] = name
        newnode = CNode(node, parent=func)
        config.getgx().types[newnode] = set([(f, 0)])
        newnode.copymetoo = True

    def visitAnd(self, node, func=None):
        self.visit_and_or(node, func)

    def visitOr(self, node, func=None):
        self.visit_and_or(node, func)

    def visit_and_or(self, node, func):
        newnode = CNode(node, parent=func)
        config.getgx().types[newnode] = set()
        for child in node.getChildNodes():
            if node in config.getgx().bool_test_only:
                self.bool_test_add(child)
            self.visit(child, func)
            self.add_constraint((inode(child), newnode), func)
            self.temp_var2(child, newnode, func)

    def visitIf(self, node, func=None):
        for test, code in node.tests:
            self.bool_test_add(test)
            faker = CallFunc(Name('bool'), [test])
            self.visit(faker, func)
            self.visit(code, func)
        if node.else_:
            self.visit(node.else_, func)

    def visitIfExp(self, node, func=None):
        newnode = CNode(node, parent=func)
        config.getgx().types[newnode] = set()

        for child in node.getChildNodes():
            self.visit(child, func)

        self.add_constraint((inode(node.then), newnode), func)
        self.add_constraint((inode(node.else_), newnode), func)

    def visitGlobal(self, node, func=None):
        func.globals += node.names

    def visitList(self, node, func=None):
        self.constructor(node, 'list', func)

    def visitDict(self, node, func=None):
        self.constructor(node, 'dict', func)
        if node.items:  # XXX library bug
            node.lineno = node.items[0][0].lineno

    def visitNot(self, node, func=None):
        self.bool_test_add(node.expr)
        newnode = CNode(node, parent=func)
        newnode.copymetoo = True
        config.getgx().types[newnode] = set([(def_class('bool_'), 0)])  # XXX new type?
        self.visit(node.expr, func)

    def visitBackquote(self, node, func=None):
        self.fake_func(node, node.expr, '__repr__', [], func)

    def visitTuple(self, node, func=None):
        if len(node.nodes) == 2:
            self.constructor(node, 'tuple2', func)
        else:
            self.constructor(node, 'tuple', func)

    def visitSubscript(self, node, func=None):  # XXX merge __setitem__, __getitem__
        if len(node.subs) > 1:
            subscript = Tuple(node.subs)
        else:
            subscript = node.subs[0]

        if isinstance(subscript, Ellipsis):  # XXX also check at setitem
            error('ellipsis is not supported', node, mv=self)

        if isinstance(subscript, Sliceobj):
            self.slice(node, node.expr, subscript.nodes, func)
        else:
            if node.flags == 'OP_DELETE':
                self.fake_func(node, node.expr, '__delitem__', [subscript], func)
            elif len(node.subs) > 1:
                self.fake_func(node, node.expr, '__getitem__', [subscript], func)
            else:
                ident = '__getitem__'
                self.fake_func(node, node.expr, ident, [subscript], func)

    def visitSlice(self, node, func=None):
        self.slice(node, node.expr, [node.lower, node.upper, None], func)

    def slice(self, node, expr, nodes, func, replace=None):
        nodes2 = slice_nums(nodes)
        if replace:
            self.fake_func(node, expr, '__setslice__', nodes2 + [replace], func)
        elif node.flags == 'OP_DELETE':
            self.fake_func(node, expr, '__delete__', nodes2, func)
        else:
            self.fake_func(node, expr, '__slice__', nodes2, func)

    def visitUnarySub(self, node, func=None):
        self.fake_func(node, node.expr, '__neg__', [], func)

    def visitUnaryAdd(self, node, func=None):
        self.fake_func(node, node.expr, '__pos__', [], func)

    def visitCompare(self, node, func=None):
        newnode = CNode(node, parent=func)
        newnode.copymetoo = True
        config.getgx().types[newnode] = set([(def_class('bool_'), 0)])  # XXX new type?
        self.visit(node.expr, func)
        msgs = {'<': 'lt', '>': 'gt', 'in': 'contains', 'not in': 'contains', '!=': 'ne', '==': 'eq', '<=': 'le', '>=': 'ge'}
        left = node.expr
        for op, right in node.ops:
            self.visit(right, func)
            msg = msgs.get(op)
            if msg == 'contains':
                self.fake_func(node, right, '__' + msg + '__', [left], func)
            elif msg in ('lt', 'gt', 'le', 'ge'):
                fakefunc = CallFunc(Name('__%s' % msg), [left, right])
                fakefunc.lineno = left.lineno
                self.visit(fakefunc, func)
            elif msg:
                self.fake_func(node, left, '__' + msg + '__', [right], func)
            left = right

        # tempvars, e.g. (t1=fun())
        for term in node.ops[:-1]:
            if not isinstance(term[1], (Name, Const)):
                self.temp_var2(term[1], inode(term[1]), func)

    def visitBitand(self, node, func=None):
        self.visitBitpair(node, aug_msg(node, 'and'), func)

    def visitBitor(self, node, func=None):
        self.visitBitpair(node, aug_msg(node, 'or'), func)

    def visitBitxor(self, node, func=None):
        self.visitBitpair(node, aug_msg(node, 'xor'), func)

    def visitBitpair(self, node, msg, func=None):
        CNode(node, parent=func)
        config.getgx().types[inode(node)] = set()
        left = node.nodes[0]
        for i, right in enumerate(node.nodes[1:]):
            faker = self.fake_func((left, i), left, msg, [right], func)
            left = faker
        self.add_constraint((inode(faker), inode(node)), func)

    def visitAdd(self, node, func=None):
        self.fake_func(node, node.left, aug_msg(node, 'add'), [node.right], func)

    def visitInvert(self, node, func=None):
        self.fake_func(node, node.expr, '__invert__', [], func)

    def visitRightShift(self, node, func=None):
        self.fake_func(node, node.left, aug_msg(node, 'rshift'), [node.right], func)

    def visitLeftShift(self, node, func=None):
        self.fake_func(node, node.left, aug_msg(node, 'lshift'), [node.right], func)

    def visitAugAssign(self, node, func=None):  # a[b] += c -> a[b] = a[b]+c, using tempvars to handle sidefx
        newnode = CNode(node, parent=func)
        config.getgx().types[newnode] = set()

        clone = copy.deepcopy(node)
        lnode = node.node

        if isinstance(node.node, Name):
            blah = AssName(clone.node.name, 'OP_ASSIGN')
        elif isinstance(node.node, Getattr):
            blah = AssAttr(clone.node.expr, clone.node.attrname, 'OP_ASSIGN')
        elif isinstance(node.node, Subscript):
            t1 = self.temp_var(node.node.expr, func)
            a1 = Assign([AssName(t1.name, 'OP_ASSIGN')], node.node.expr)
            self.visit(a1, func)
            self.add_constraint((inode(node.node.expr), inode(t1)), func)

            if len(node.node.subs) > 1:
                subs = Tuple(node.node.subs)
            else:
                subs = node.node.subs[0]
            t2 = self.temp_var(subs, func)
            a2 = Assign([AssName(t2.name, 'OP_ASSIGN')], subs)

            self.visit(a1, func)
            self.visit(a2, func)
            self.add_constraint((inode(subs), inode(t2)), func)

            inode(node).temp1 = t1.name
            inode(node).temp2 = t2.name
            inode(node).subs = subs

            blah = Subscript(Name(t1.name), 'OP_APPLY', [Name(t2.name)])
            lnode = Subscript(Name(t1.name), 'OP_APPLY', [Name(t2.name)])
        else:
            error('unsupported type of assignment', node, mv=self)

        if node.op == '-=':
            blah2 = Sub((lnode, node.expr))
        if node.op == '+=':
            blah2 = Add((lnode, node.expr))
        if node.op == '|=':
            blah2 = Bitor((lnode, node.expr))
        if node.op == '&=':
            blah2 = Bitand((lnode, node.expr))
        if node.op == '^=':
            blah2 = Bitxor((lnode, node.expr))
        if node.op == '**=':
            blah2 = Power((lnode, node.expr))
        if node.op == '<<=':
            blah2 = LeftShift((lnode, node.expr))
        if node.op == '>>=':
            blah2 = RightShift((lnode, node.expr))
        if node.op == '%=':
            blah2 = Mod((lnode, node.expr))
        if node.op == '*=':
            blah2 = Mul((lnode, node.expr))
        if node.op == '/=':
            blah2 = Div((lnode, node.expr))
        if node.op == '//=':
            blah2 = FloorDiv((lnode, node.expr))

        blah2.augment = True

        assign = Assign([blah], blah2)
        register_node(assign, func)
        inode(node).assignhop = assign
        self.visit(assign, func)

    def visitSub(self, node, func=None):
        self.fake_func(node, node.left, aug_msg(node, 'sub'), [node.right], func)

    def visitMul(self, node, func=None):
        self.fake_func(node, node.left, aug_msg(node, 'mul'), [node.right], func)

    def visitDiv(self, node, func=None):
        self.fake_func(node, node.left, aug_msg(node, 'div'), [node.right], func)

    def visitFloorDiv(self, node, func=None):
        self.fake_func(node, node.left, aug_msg(node, 'floordiv'), [node.right], func)

    def visitPower(self, node, func=None):
        self.fake_func(node, node.left, '__pow__', [node.right], func)

    def visitMod(self, node, func=None):
        if isinstance(node.right, (Tuple, Dict)):
            self.fake_func(node, node.left, '__mod__', [], func)
            for child in node.right.getChildNodes():
                self.visit(child, func)
                if isinstance(node.right, Tuple):
                    self.fake_func(inode(child), child, '__str__', [], func)
        else:
            self.fake_func(node, node.left, '__mod__', [node.right], func)

    def visitPrintnl(self, node, func=None):
        self.visitPrint(node, func)

    def visitPrint(self, node, func=None):
        pnode = CNode(node, parent=func)
        config.getgx().types[pnode] = set()

        for child in node.getChildNodes():
            self.visit(child, func)
            self.fake_func(inode(child), child, '__str__', [], func)

    def temp_var(self, node, func=None, looper=None, wopper=None):
        if node in config.getgx().parent_nodes:
            varname = self.tempcount[config.getgx().parent_nodes[node]]
        elif node in self.tempcount:  # XXX investigate why this happens
            varname = self.tempcount[node]
        else:
            varname = '__' + str(len(self.tempcount))

        var = default_var(varname, func)
        var.looper = looper
        var.wopper = wopper
        self.tempcount[node] = varname

        register_temp_var(var, func)
        return var

    def temp_var2(self, node, source, func):
        tvar = self.temp_var(node, func)
        self.add_constraint((source, inode(tvar)), func)
        return tvar

    def temp_var_int(self, node, func):
        var = self.temp_var(node, func)
        config.getgx().types[inode(var)] = set([(def_class('int_'), 0)])
        inode(var).copymetoo = True
        return var

    def visitRaise(self, node, func=None):
        if node.expr1 is None or node.expr2 is not None or node.expr3 is not None:
            error('unsupported raise syntax', node, mv=self)
        for child in node.getChildNodes():
            self.visit(child, func)

    def visitTryExcept(self, node, func=None):
        self.visit(node.body, func)

        for handler in node.handlers:
            self.visit(handler[2], func)
            if not handler[0]:
                continue

            if isinstance(handler[0], Tuple):
                pairs = [(n, handler[1]) for n in handler[0].nodes]
            else:
                pairs = [(handler[0], handler[1])]

            for (h0, h1) in pairs:
                if isinstance(h0, Name) and h0.name in ['int', 'float', 'str', 'class']:
                    continue  # handle in lookup_class
                cl = lookup_class(h0, getmv())
                if not cl:
                    error("unknown or unsupported exception type", h0, mv=self)

                if isinstance(h1, AssName):
                    var = default_var(h1.name, func)
                else:
                    var = self.temp_var(h0, func)

                var.invisible = True
                inode(var).copymetoo = True
                config.getgx().types[inode(var)] = set([(cl, 1)])

        # else
        if node.else_:
            self.visit(node.else_, func)
            self.temp_var_int(node.else_, func)

    def visitTryFinally(self, node, func=None):
        error("'try..finally' is not supported", node, mv=self)

    def visitYield(self, node, func):
        func.isGenerator = True
        func.yieldNodes.append(node)
        self.visit(Return(CallFunc(Name('__iter'), [node.value])), func)
        self.add_constraint((inode(node.value), func.yieldnode), func)

    def visitFor(self, node, func=None):
        # --- iterable contents -> assign node
        assnode = CNode(node.assign, parent=func)
        config.getgx().types[assnode] = set()

        get_iter = CallFunc(Getattr(node.list, '__iter__'), [])
        fakefunc = CallFunc(Getattr(get_iter, 'next'), [])

        self.visit(fakefunc, func)
        self.add_constraint((inode(fakefunc), assnode), func)

        # --- assign node -> variables  XXX merge into assign_pair
        if isinstance(node.assign, AssName):
            # for x in..
            lvar = default_var(node.assign.name, func)
            self.add_constraint((assnode, inode(lvar)), func)

        elif isinstance(node.assign, AssAttr):  # XXX experimental :)
            # for expr.x in..
            CNode(node.assign, parent=func)

            config.getgx().assign_target[node.assign.expr] = node.assign.expr  # XXX multiple targets possible please
            fakefunc2 = CallFunc(Getattr(node.assign.expr, '__setattr__'), [Const(node.assign.attrname), fakefunc])
            self.visit(fakefunc2, func)

        elif isinstance(node.assign, (AssTuple, AssList)):
            # for (a,b, ..) in..
            self.tuple_flow(node.assign, node.assign, func)
        else:
            error('unsupported type of assignment', node, mv=self)

        self.do_for(node, assnode, get_iter, func)

        # --- for-else
        if node.else_:
            self.temp_var_int(node.else_, func)
            self.visit(node.else_, func)

        # --- loop body
        config.getgx().loopstack.append(node)
        self.visit(node.body, func)
        config.getgx().loopstack.pop()
        self.for_in_iters.append(node.list)

    def do_for(self, node, assnode, get_iter, func):
        # --- for i in range(..) XXX i should not be modified.. use tempcounter; two bounds
        if is_fastfor(node):
            self.temp_var2(node.assign, assnode, func)
            self.temp_var2(node.list, inode(node.list.args[0]), func)

            if len(node.list.args) == 3 and not isinstance(node.list.args[2], Name) and not is_literal(node.list.args[2]):  # XXX merge with ListComp
                for arg in node.list.args:
                    if not isinstance(arg, Name) and not is_literal(arg):  # XXX create func for better check
                        self.temp_var2(arg, inode(arg), func)

        # --- temp vars for list, iter etc.
        else:
            self.temp_var2(node, inode(node.list), func)
            self.temp_var2((node, 1), inode(get_iter), func)
            self.temp_var_int(node.list, func)

            if is_enum(node) or is_zip2(node):
                self.temp_var2((node, 2), inode(node.list.args[0]), func)
                if is_zip2(node):
                    self.temp_var2((node, 3), inode(node.list.args[1]), func)
                    self.temp_var_int((node, 4), func)

            self.temp_var((node, 5), func, looper=node.list)
            if isinstance(node.list, CallFunc) and isinstance(node.list.node, Getattr):
                self.temp_var((node, 6), func, wopper=node.list.node.expr)
                self.temp_var2((node, 7), inode(node.list.node.expr), func)

    def bool_test_add(self, node):
        if isinstance(node, (And, Or, Not)):
            config.getgx().bool_test_only.add(node)

    def visitWhile(self, node, func=None):
        config.getgx().loopstack.append(node)
        self.bool_test_add(node.test)
        for child in node.getChildNodes():
            self.visit(child, func)
        config.getgx().loopstack.pop()

        if node.else_:
            self.temp_var_int(node.else_, func)
            self.visit(node.else_, func)

    def visitWith(self, node, func=None):
        if node.vars:
            varnode = CNode(node.vars, parent=func)
            config.getgx().types[varnode] = set()
            self.visit(node.expr, func)
            self.add_constraint((inode(node.expr), varnode), func)
            lvar = default_var(node.vars.name, func)
            self.add_constraint((varnode, inode(lvar)), func)
        else:
            self.visit(node.expr, func)
        for child in node.getChildNodes():
            self.visit(child, func)

    def visitListCompIf(self, node, func=None):
        self.bool_test_add(node.test)
        for child in node.getChildNodes():
            self.visit(child, func)

    def visitListComp(self, node, func=None):
        # --- [expr for iter in list for .. if cond ..]
        lcfunc = Function()
        lcfunc.listcomp = True
        lcfunc.ident = 'l.c.'  # XXX
        lcfunc.parent = func

        for qual in node.quals:
            # iter
            assnode = CNode(qual.assign, parent=func)
            config.getgx().types[assnode] = set()

            # list.unit->iter
            get_iter = CallFunc(Getattr(qual.list, '__iter__'), [])
            fakefunc = CallFunc(Getattr(get_iter, 'next'), [])
            self.visit(fakefunc, lcfunc)
            self.add_constraint((inode(fakefunc), inode(qual.assign)), lcfunc)

            if isinstance(qual.assign, AssName):  # XXX merge with visitFor
                lvar = default_var(qual.assign.name, lcfunc)  # XXX str or Name?
                self.add_constraint((inode(qual.assign), inode(lvar)), lcfunc)
            else:  # AssTuple, AssList
                self.tuple_flow(qual.assign, qual.assign, lcfunc)

            self.do_for(qual, assnode, get_iter, lcfunc)

            # cond
            for child in qual.ifs:
                self.visit(child, lcfunc)

            self.for_in_iters.append(qual.list)

        # node type
        if node in config.getgx().genexp_to_lc.values():  # converted generator expression
            self.instance(node, def_class('__iter'), func)
        else:
            self.instance(node, def_class('list'), func)

        # expr->instance.unit
        self.visit(node.expr, lcfunc)
        self.add_dynamic_constraint(node, node.expr, 'unit', lcfunc)

        lcfunc.ident = 'list_comp_' + str(len(self.listcomps))
        self.listcomps.append((node, lcfunc, func))

    def visitReturn(self, node, func):
        self.visit(node.value, func)
        func.returnexpr.append(node.value)
        if not (isinstance(node.value, Const) and node.value.value is None):
            newnode = CNode(node, parent=func)
            config.getgx().types[newnode] = set()
            if isinstance(node.value, Name):
                func.retvars.append(node.value.name)
        if func.retnode:
            self.add_constraint((inode(node.value), func.retnode), func)

    def visitAssign(self, node, func=None):
        # --- rewrite for struct.unpack XXX rewrite callfunc as tuple
        if len(node.nodes) == 1:
            lvalue, rvalue = node.nodes[0], node.expr
            if struct_unpack(rvalue, func) and isinstance(lvalue, (AssList, AssTuple)) and not [n for n in lvalue.nodes if isinstance(n, (AssList, AssTuple))]:
                if not isinstance(rvalue.args[0], (Const, Name)):
                    error('non-constant format string', node, mv=self)
                self.visit(node.expr, func)
                sinfo = struct_info(rvalue.args[0], func)
                faketuple = struct_faketuple(sinfo)
                self.visit(Assign(node.nodes, faketuple), func)
                tvar = self.temp_var2(rvalue.args[1], inode(rvalue.args[1]), func)
                tvar_pos = self.temp_var_int(rvalue.args[0], func)
                config.getgx().struct_unpack[node] = (sinfo, tvar.name, tvar_pos.name)
                return

        newnode = CNode(node, parent=func)
        config.getgx().types[newnode] = set()

        # --- a,b,.. = c,(d,e),.. = .. = expr
        for target_expr in node.nodes:
            pairs = assign_rec(target_expr, node.expr)
            for (lvalue, rvalue) in pairs:
                # expr[expr] = expr
                if isinstance(lvalue, Subscript) and not isinstance(lvalue.subs[0], Sliceobj):
                    self.assign_pair(lvalue, rvalue, func)  # XXX use here generally, and in tuple_flow

                # expr.attr = expr
                elif isinstance(lvalue, AssAttr):
                    self.assign_pair(lvalue, rvalue, func)

                # name = expr
                elif isinstance(lvalue, AssName):
                    if (rvalue, 0, 0) not in config.getgx().cnode:  # XXX generalize
                        self.visit(rvalue, func)
                    self.visit(lvalue, func)
                    if isinstance(func, Function) and lvalue.name in func.globals:
                        lvar = default_var(lvalue.name, None)
                    else:
                        lvar = default_var(lvalue.name, func)
                    if isinstance(rvalue, Const):
                        lvar.const_assign.append(rvalue)
                    self.add_constraint((inode(rvalue), inode(lvar)), func)

                # (a,(b,c), ..) = expr
                elif isinstance(lvalue, (AssTuple, AssList)):
                    self.visit(rvalue, func)
                    self.tuple_flow(lvalue, rvalue, func)

                # expr[a:b] = expr # XXX bla()[1:3] = [1]
                elif isinstance(lvalue, Slice):
                    self.slice(lvalue, lvalue.expr, [lvalue.lower, lvalue.upper, None], func, rvalue)

                # expr[a:b:c] = expr
                elif isinstance(lvalue, Subscript) and isinstance(lvalue.subs[0], Sliceobj):
                    self.slice(lvalue, lvalue.expr, lvalue.subs[0].nodes, func, rvalue)

        # temp vars
        if len(node.nodes) > 1 or isinstance(node.expr, Tuple):
            if isinstance(node.expr, Tuple):
                if [n for n in node.nodes if isinstance(n, AssTuple)]:
                    for child in node.expr.nodes:
                        if (child, 0, 0) not in config.getgx().cnode:  # (a,b) = (1,2): (1,2) never visited
                            continue
                        if not isinstance(child, Const) and not (isinstance(child, Name) and child.name == 'None'):
                            self.temp_var2(child, inode(child), func)
            elif not isinstance(node.expr, Const) and not (isinstance(node.expr, Name) and node.expr.name == 'None'):
                self.temp_var2(node.expr, inode(node.expr), func)

    def assign_pair(self, lvalue, rvalue, func):
        # expr[expr] = expr
        if isinstance(lvalue, Subscript) and not isinstance(lvalue.subs[0], Sliceobj):
            if len(lvalue.subs) > 1:
                subscript = Tuple(lvalue.subs)
            else:
                subscript = lvalue.subs[0]

            fakefunc = CallFunc(Getattr(lvalue.expr, '__setitem__'), [subscript, rvalue])
            self.visit(fakefunc, func)
            inode(lvalue.expr).fakefunc = fakefunc
            if len(lvalue.subs) > 1:
                inode(lvalue.expr).faketuple = subscript

            if not isinstance(lvalue.expr, Name):
                self.temp_var2(lvalue.expr, inode(lvalue.expr), func)

        # expr.attr = expr
        elif isinstance(lvalue, AssAttr):
            CNode(lvalue, parent=func)
            config.getgx().assign_target[rvalue] = lvalue.expr
            fakefunc = CallFunc(Getattr(lvalue.expr, '__setattr__'), [Const(lvalue.attrname), rvalue])
            self.visit(fakefunc, func)

    def tuple_flow(self, lvalue, rvalue, func=None):
        self.temp_var2(lvalue, inode(rvalue), func)

        if isinstance(lvalue, (AssTuple, AssList)):
            lvalue = lvalue.nodes
        for (i, item) in enumerate(lvalue):
            fakenode = CNode((item,), parent=func)  # fake node per item, for multiple callfunc triggers
            config.getgx().types[fakenode] = set()
            self.add_constraint((inode(rvalue), fakenode), func)

            fakefunc = CallFunc(FakeGetattr3(rvalue, '__getitem__'), [Const(i)])

            fakenode.callfuncs.append(fakefunc)
            self.visit(fakefunc, func)

            config.getgx().item_rvalue[item] = rvalue
            if isinstance(item, AssName):
                if isinstance(func, Function) and item.name in func.globals:  # XXX merge
                    lvar = default_var(item.name, None)
                else:
                    lvar = default_var(item.name, func)
                self.add_constraint((inode(fakefunc), inode(lvar)), func)
            elif isinstance(item, (Subscript, AssAttr)):
                self.assign_pair(item, fakefunc, func)
            elif isinstance(item, (AssTuple, AssList)):  # recursion
                self.tuple_flow(item, fakefunc, func)
            else:
                error('unsupported type of assignment', item, mv=self)

    def super_call(self, orig, parent):
        node = orig.node
        while isinstance(parent, Function):
            parent = parent.parent
        if (isinstance(node.expr, CallFunc) and
            node.attrname not in ('__getattr__', '__setattr__') and
            isinstance(node.expr.node, Name) and
                node.expr.node.name == 'super'):
            if (len(node.expr.args) >= 2 and
                    isinstance(node.expr.args[1], Name) and node.expr.args[1].name == 'self'):
                cl = lookup_class(node.expr.args[0], getmv())
                if cl.node.bases:
                    return cl.node.bases[0]
            error("unsupported usage of 'super'", orig, mv=self)

    def visitCallFunc(self, node, func=None):  # XXX clean up!!
        newnode = CNode(node, parent=func)

        if isinstance(node.node, Getattr):  # XXX import math; math.e
            # rewrite super(..) call
            base = self.super_call(node, func)
            if base:
                node.node = Getattr(copy.deepcopy(base), node.node.attrname)
                node.args = [Name('self')] + node.args

            # method call
            if isinstance(node.node, FakeGetattr):  # XXX butt ugly
                self.visit(node.node, func)
            elif isinstance(node.node, FakeGetattr2):
                config.getgx().types[newnode] = set()  # XXX move above

                self.callfuncs.append((node, func))

                for arg in node.args:
                    inode(arg).callfuncs.append(node)  # this one too

                return
            elif isinstance(node.node, FakeGetattr3):
                pass
            else:
                self.visitGetattr(node.node, func, callfunc=True)
                inode(node.node).callfuncs.append(node)  # XXX iterative dataflow analysis: move there?
                inode(node.node).fakert = True

            ident = node.node.attrname
            inode(node.node.expr).callfuncs.append(node)  # XXX iterative dataflow analysis: move there?

            if isinstance(node.node.expr, Name) and node.node.expr.name in getmv().imports and node.node.attrname == '__getattr__':  # XXX analyze_callfunc
                if node.args[0].value in getmv().imports[node.node.expr.name].mv.globals:  # XXX bleh
                    self.add_constraint((inode(getmv().imports[node.node.expr.name].mv.globals[node.args[0].value]), newnode), func)

        elif isinstance(node.node, Name):
            # direct call
            ident = node.node.name
            if ident == 'print':
                ident = node.node.name = '__print'  # XXX

            if ident in ['getattr', 'setattr', 'slice', 'type']:
                error("'%s' function is not supported" % ident, node.node, mv=self)
            if ident == 'dict' and [x for x in node.args if isinstance(x, Keyword)]:
                error('unsupported method of initializing dictionaries', node, mv=self)
            if ident == 'isinstance' and isinstance(node.args[1], Tuple):
                error("isinstance(.., (a, b, ..)) is not supported", node, mv=getmv())

            if lookup_var(ident, func):
                self.visit(node.node, func)
                inode(node.node).callfuncs.append(node)  # XXX iterative dataflow analysis: move there
        else:
            self.visit(node.node, func)
            inode(node.node).callfuncs.append(node)  # XXX iterative dataflow analysis: move there

        # --- arguments
        if not getmv().module.builtin and (node.star_args or node.dstar_args):
            error('argument (un)packing is not supported', node, mv=self)
        args = node.args[:]
        if node.star_args:
            args.append(node.star_args)  # partially allowed in builtins
        if node.dstar_args:
            args.append(node.dstar_args)
        for arg in args:
            if isinstance(arg, Keyword):
                arg = arg.expr
            self.visit(arg, func)
            inode(arg).callfuncs.append(node)  # this one too

        # --- handle instantiation or call
        constructor = lookup_class(node.node, getmv())
        if constructor and (not isinstance(node.node, Name) or not lookup_var(node.node.name, func)):
            self.instance(node, constructor, func)
            inode(node).callfuncs.append(node)  # XXX see above, investigate
        else:
            config.getgx().types[newnode] = set()

        self.callfuncs.append((node, func))

    def visitClass(self, node, parent=None):
        if not getmv().module.builtin and not node in getmv().classnodes:
            error("non-global class '%s'" % node.name, node, mv=self)
        if len(node.bases) > 1:
            error('multiple inheritance is not supported', node, mv=self)

        if not getmv().module.builtin:
            for base in node.bases:
                if not isinstance(base, (Name, Getattr)):
                    error("invalid expression for base class", node, mv=self)

                if isinstance(base, Name):
                    name = base.name
                else:
                    name = base.attrname

                cl = lookup_class(base, getmv())
                if not cl:
                    error("no such class: '%s'" % name, node, mv=self)

                elif cl.mv.module.builtin and name not in ['object', 'Exception', 'tzinfo']:
                    if def_class('Exception') not in cl.ancestors():
                        error("inheritance from builtin class '%s' is not supported" % name, node, mv=self)

        if node.name in getmv().classes:
            newclass = getmv().classes[node.name]  # set in visitModule, for forward references
        else:
            check_redef(node)  # XXX merge with visitModule
            newclass = class_(node)
            self.classes[node.name] = newclass
            getmv().classes[node.name] = newclass
            newclass.module = self.module
            newclass.parent = StaticClass(newclass)

        # --- built-in functions
        for cl in [newclass, newclass.parent]:
            for ident in ['__setattr__', '__getattr__']:
                func = Function()
                func.ident = ident
                func.parent = cl

                if ident == '__setattr__':
                    func.formals = ['name', 'whatsit']
                    retexpr = Return(Name('None'))
                    self.visit(retexpr, func)
                elif ident == '__getattr__':
                    func.formals = ['name']

                cl.funcs[ident] = func

        # --- built-in attributes
        if 'class_' in getmv().classes or 'class_' in getmv().ext_classes:
            var = default_var('__class__', newclass)
            var.invisible = True
            config.getgx().types[inode(var)] = set([(def_class('class_'), def_class('class_').dcpa)])
            def_class('class_').dcpa += 1

        # --- staticmethod, property
        skip = []
        for child in node.code.getChildNodes():
            if isinstance(child, Assign) and len(child.nodes) == 1:
                lvalue, rvalue = child.nodes[0], child.expr
                if isinstance(lvalue, AssName) and isinstance(rvalue, CallFunc) and isinstance(rvalue.node, Name) and rvalue.node.name in ['staticmethod', 'property']:
                    if rvalue.node.name == 'property':
                        if len(rvalue.args) == 1 and isinstance(rvalue.args[0], Name):
                            newclass.properties[lvalue.name] = rvalue.args[0].name, None
                        elif len(rvalue.args) == 2 and isinstance(rvalue.args[0], Name) and isinstance(rvalue.args[1], Name):
                            newclass.properties[lvalue.name] = rvalue.args[0].name, rvalue.args[1].name
                        else:
                            error("complex properties are not supported", rvalue, mv=self)
                    else:
                        newclass.staticmethods.append(lvalue.name)
                    skip.append(child)

        # --- children
        for child in node.code.getChildNodes():
            if child not in skip:
                cl = self.classes[node.name]
                if isinstance(child, FunctionNode):
                    self.visit(child, cl)
                else:
                    cl.parent.static_nodes.append(child)
                    self.visit(child, cl.parent)

        # --- __iadd__ etc.
        if not newclass.mv.module.builtin or newclass.ident in ['int_', 'float_', 'str_', 'tuple', 'complex']:
            msgs = ['add', 'mul']  # XXX mod, pow
            if newclass.ident in ['int_', 'float_']:
                msgs += ['sub', 'div', 'floordiv']
            if newclass.ident in ['int_']:
                msgs += ['lshift', 'rshift', 'and', 'xor', 'or']
            for msg in msgs:
                if not '__i' + msg + '__' in newclass.funcs:
                    self.visit(FunctionNode(None, '__i' + msg + '__', ['self', 'other'], [], 0, None, Stmt([Return(CallFunc(Getattr(Name('self'), '__' + msg + '__'), [Name('other')], None, None))])), newclass)

        # --- __str__, __hash__ # XXX model in lib/builtin.py, other defaults?
        if not newclass.mv.module.builtin and not '__str__' in newclass.funcs:
            self.visit(FunctionNode(None, '__str__', ['self'], [], 0, None, Return(CallFunc(Getattr(Name('self'), '__repr__'), []))), newclass)
            newclass.funcs['__str__'].invisible = True
        if not newclass.mv.module.builtin and not '__hash__' in newclass.funcs:
            self.visit(FunctionNode(None, '__hash__', ['self'], [], 0, None, Return(Const(0)), []), newclass)
            newclass.funcs['__hash__'].invisible = True

    def visitGetattr(self, node, func=None, callfunc=False):
        if node.attrname in ['__doc__']:
            error('%s attribute is not supported' % node.attrname, node, mv=self)

        newnode = CNode(node, parent=func)
        config.getgx().types[newnode] = set()

        fakefunc = CallFunc(FakeGetattr(node.expr, '__getattr__'), [Const(node.attrname)])
        self.visit(fakefunc, func)
        self.add_constraint((config.getgx().cnode[fakefunc, 0, 0], newnode), func)

        self.callfuncs.append((fakefunc, func))

        if not callfunc:
            self.fncl_passing(node, newnode, func)

    def visitConst(self, node, func=None):
        if type(node.value) == unicode:
            error('unicode is not supported', node, mv=self)
        map = {int: 'int_', str: 'str_', float: 'float_', type(None): 'none', long: 'int_', complex: 'complex'}  # XXX 'return' -> Return(Const(None))?
        self.instance(node, def_class(map[type(node.value)]), func)

    def fncl_passing(self, node, newnode, func):
        lfunc, lclass = lookup_func(node, getmv()), lookup_class(node, getmv())
        if lfunc:
            if lfunc.mv.module.builtin:
                lfunc = self.builtin_wrapper(node, func)
            elif lfunc.ident not in lfunc.mv.lambdas:
                lfunc.lambdanr = len(lfunc.mv.lambdas)
                lfunc.mv.lambdas[lfunc.ident] = lfunc
            config.getgx().types[newnode] = set([(lfunc, 0)])
        elif lclass:
            if lclass.mv.module.builtin:
                lclass = self.builtin_wrapper(node, func)
            else:
                lclass = lclass.parent
            config.getgx().types[newnode] = set([(lclass, 0)])
        else:
            return False
        newnode.copymetoo = True  # XXX merge into some kind of 'seeding' function
        return True

    def visitName(self, node, func=None):
        newnode = CNode(node, parent=func)
        config.getgx().types[newnode] = set()

        if node.name == '__doc__':
            error("'%s' attribute is not supported" % node.name, node, mv=self)

        if node.name in ['None', 'True', 'False']:
            if node.name == 'None':  # XXX also bools, remove def seed_nodes()
                self.instance(node, def_class('none'), func)
            else:
                self.instance(node, def_class('bool_'), func)
            return

        if isinstance(func, Function) and node.name in func.globals:
            var = default_var(node.name, None)
        else:
            var = lookup_var(node.name, func)
            if not var:
                if self.fncl_passing(node, newnode, func):
                    pass
                elif node.name in ['int', 'float', 'str']:  # XXX
                    cl = self.ext_classes[node.name + '_']
                    config.getgx().types[newnode] = set([(cl.parent, 0)])
                    newnode.copymetoo = True
                else:
                    var = default_var(node.name, None)
        if var:
            self.add_constraint((inode(var), newnode), func)

    def builtin_wrapper(self, node, func):
        node2 = CallFunc(copy.deepcopy(node), [Name(x) for x in 'abcde'])
        l = Lambda(list('abcde'), [], 0, node2)
        self.visit(l, func)
        self.lwrapper[node] = self.lambdaname[l]
        config.getgx().lambdawrapper[node2] = self.lambdaname[l]
        f = self.lambdas[self.lambdaname[l]]
        f.lambdawrapper = True
        inode(node2).lambdawrapper = f
        return f


def clear_block(m):
    return m.string.count('\n', m.start(), m.end()) * '\n'


def parse_file(name):
    # Convert block comments into strings which will be duely ignored.
    pat = re.compile(r"#{.*?#}[^\r\n]*$", re.MULTILINE | re.DOTALL)
    filebuf = re.sub(pat, clear_block, ''.join(open(name, 'U').readlines()))
    try:
        return parse(filebuf)
    except SyntaxError, s:
        print '*ERROR* %s:%d: %s' % (name, s.lineno, s.msg)
        sys.exit(1)


def find_module(name, paths):
    if '.' in name:
        name, module_name = name.rsplit('.', 1)
        name_as_path = name.replace('.', os.path.sep)
        import_paths = [os.path.join(path, name_as_path) for path in paths]
    else:
        module_name = name
        import_paths = paths
    _, filename, description = imp.find_module(module_name, import_paths)
    filename = os.path.splitext(filename)[0]

    absolute_import_paths = config.getgx().libdirs + [os.getcwd()]
    absolute_import_path = next(
        path for path in absolute_import_paths
        if filename.startswith(path)
    )
    relative_filename = os.path.relpath(filename, absolute_import_path)
    absolute_name = relative_filename.replace(os.path.sep, '.')
    builtin = absolute_import_path in config.getgx().libdirs

    is_a_package = description[2] == imp.PKG_DIRECTORY
    if is_a_package:
        filename = os.path.join(filename, '__init__.py')
        relative_filename = os.path.join(relative_filename, '__init__.py')
    else:
        filename = filename + '.py'
        relative_filename = relative_filename + '.py'

    return absolute_name, filename, relative_filename, builtin


def parse_module(name, parent=None, node=None):
    # --- valid name?
    if not re.match("^[a-zA-Z0-9_.]+$", name):
        print ("*ERROR*:%s.py: module names should consist of letters, digits and underscores" % name)
        sys.exit(1)

    # --- create module
    try:
        if parent and parent.path != os.getcwd():
            basepaths = [parent.path, os.getcwd()]
        else:
            basepaths = [os.getcwd()]
        module_paths = basepaths + config.getgx().libdirs
        absolute_name, filename, relative_filename, builtin = find_module(name, module_paths)
        module = Module(absolute_name, filename, relative_filename, builtin, node)
    except ImportError:
        error('cannot locate module: ' + name, node, mv=getmv())

    # --- check cache
    if module.name in config.getgx().modules:  # cached?
        return config.getgx().modules[module.name]
    config.getgx().modules[module.name] = module

    # --- not cached, so parse
    module.ast = parse_file(module.filename)

    old_mv = getmv()
    module.mv = mv = ModuleVisitor(module)
    setmv(mv)

    mv.visit = mv.dispatch
    mv.visitor = mv
    mv.dispatch(module.ast)
    module.import_order = config.getgx().import_order
    config.getgx().import_order += 1

    mv = old_mv
    setmv(mv)

    return module


def check_redef(node, s=None, onlybuiltins=False):  # XXX to modvisitor, rewrite
    if not getmv().module.builtin:
        existing = [getmv().ext_classes, getmv().ext_funcs]
        if not onlybuiltins:
            existing += [getmv().classes, getmv().funcs]
        for whatsit in existing:
            if s is not None:
                name = s
            else:
                name = node.name
            if name in whatsit:
                error("function/class redefinition is not supported", node, mv=getmv())

# --- maintain inheritance relations between copied AST nodes


def inherit_rec(original, copy, mv):
    config.getgx().inheritance_relations.setdefault(original, []).append(copy)
    config.getgx().inherited.add(copy)
    config.getgx().parent_nodes[copy] = original

    for (a, b) in zip(original.getChildNodes(), copy.getChildNodes()):
        inherit_rec(a, b, mv)


def register_node(node, func):
    if func:
        func.registered.append(node)


def slice_nums(nodes):
    nodes2 = []
    x = 0
    for i, n in enumerate(nodes):
        if not n or (isinstance(n, Const) and n.value is None):
            nodes2.append(Const(0))
        else:
            nodes2.append(n)
            x |= (1 << i)
    return [Const(x)] + nodes2
