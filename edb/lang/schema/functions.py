#
# This source file is part of the EdgeDB open source project.
#
# Copyright 2008-present MagicStack Inc. and the EdgeDB authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#


import typing

from edb.lang.common import typed

from edb.lang.edgeql import ast as qlast
from edb.lang.edgeql import errors as ql_errors
from edb.lang.edgeql import codegen

from . import delta as sd
from . import expr
from . import name as sn
from . import named
from . import objects as so
from . import types as s_types
from . import utils


class ParametersInfo(typing.NamedTuple):
    paramnames: typing.List[str]
    paramdefaults: typing.List[str]  # list of EdgeQL snippets
    paramtypes: typing.List[so.ObjectRef]
    paramtypemods: typing.List[qlast.TypeModifier]
    paramkinds: typing.List[qlast.ParameterKind]
    variadic: typing.Optional[int]


class FuncParamTypeModList(typed.TypedList, type=qlast.TypeModifier):
    pass


class FuncParamKindList(typed.TypedList, type=qlast.ParameterKind):
    pass


class Function(so.NamedObject):
    _type = 'function'

    paramnames = so.Field(so.StringList, default=None, coerce=True,
                          compcoef=0.4)

    paramtypes = so.Field(so.TypeList, default=None, coerce=True,
                          compcoef=0.4)

    paramtypemods = so.Field(FuncParamTypeModList,
                             default=None, coerce=True, compcoef=0.4)

    paramkinds = so.Field(FuncParamKindList,
                          default=None, coerce=True, compcoef=0.4)

    paramdefaults = so.Field(expr.ExpressionList, default=None, coerce=True)
    return_type = so.Field(so.Object, compcoef=0.2)
    aggregate = so.Field(bool, default=False, compcoef=0.4)

    code = so.Field(str, default=None, compcoef=0.4)
    language = so.Field(qlast.Language, default=None, compcoef=0.4,
                        coerce=True)
    from_function = so.Field(str, default=None, compcoef=0.4)

    initial_value = so.Field(expr.ExpressionText, default=None, compcoef=0.4,
                             coerce=True)

    return_typemod = so.Field(qlast.TypeModifier, compcoef=0.4, coerce=True)


class FunctionCommandContext(sd.ObjectCommandContext):
    pass


class FunctionCommandMixin:
    @classmethod
    def _get_function_fullname(cls, name, paramtypes):
        quals = []
        if paramtypes:
            for pt in paramtypes:
                if isinstance(pt, so.ObjectRef):
                    quals.append(pt.classname)
                elif isinstance(pt, s_types.Collection):
                    quals.append(pt.schema_name)
                    if isinstance(pt.element_type, so.ObjectRef):
                        quals.append(pt.element_type.classname)
                    else:
                        quals.append(pt.element_type.name)
                else:
                    quals.append(pt.name)

        return sn.Name(
            module=name.module,
            name=named.NamedObject.get_specialized_name(name, *quals))


class FunctionCommand(named.NamedObjectCommand, FunctionCommandMixin,
                      schema_metaclass=Function,
                      context_class=FunctionCommandContext):
    pass


class CreateFunction(named.CreateNamedObject, FunctionCommand):
    astnode = qlast.CreateFunction

    def get_struct_properties(self, schema):
        props = super().get_struct_properties(schema)
        props['name'] = self._get_function_fullname(props['name'],
                                                    props.get('paramtypes'))
        return props

    def _add_to_schema(self, schema):
        props = super().get_struct_properties(schema)
        fullname = self._get_function_fullname(
            props['name'], props.get('paramtypes'))

        # check that params of type 'any' don't have defaults
        for pn, pt, pd in zip(props['paramnames'], props['paramtypes'],
                              props['paramdefaults']):
            if pt.name == 'std::any' and pd is not None:
                raise ql_errors.EdgeQLError(
                    f'Cannot create a function {self.classname}: '
                    f'polymorphic parameter of type {pt.name} cannot have '
                    f'a default value', context=self.source_context)

        func = schema.get(fullname, None)
        if func:
            raise ql_errors.EdgeQLError(
                f'Cannot create a function {self.classname}: '
                f'a function with the same signature '
                f'is already defined', context=self.source_context)

        super()._add_to_schema(schema)

    @classmethod
    def _cmd_tree_from_ast(cls, astnode, context, schema):
        cmd = super()._cmd_tree_from_ast(astnode, context, schema)

        modaliases = context.modaliases

        pi = parameters_from_ast(astnode, modaliases, schema)

        cmd.add(sd.AlterObjectProperty(
            property='paramnames',
            new_value=pi.paramnames
        ))

        cmd.add(sd.AlterObjectProperty(
            property='paramtypes',
            new_value=pi.paramtypes
        ))

        cmd.add(sd.AlterObjectProperty(
            property='paramtypemods',
            new_value=pi.paramtypemods
        ))

        cmd.add(sd.AlterObjectProperty(
            property='paramkinds',
            new_value=pi.paramkinds
        ))

        cmd.add(sd.AlterObjectProperty(
            property='paramdefaults',
            new_value=pi.paramdefaults
        ))

        cmd.add(sd.AlterObjectProperty(
            property='return_type',
            new_value=utils.ast_to_typeref(
                astnode.returning, modaliases=modaliases, schema=schema)
        ))

        cmd.add(sd.AlterObjectProperty(
            property='aggregate',
            new_value=astnode.aggregate
        ))

        cmd.add(sd.AlterObjectProperty(
            property='return_typemod',
            new_value=astnode.returning_typemod
        ))

        if astnode.initial_value is not None:
            iv = codegen.generate_source(astnode.initial_value)
            cmd.add(sd.AlterObjectProperty(
                property='initial_value',
                new_value=iv
            ))

        if astnode.code is not None:
            cmd.add(sd.AlterObjectProperty(
                property='language',
                new_value=astnode.code.language
            ))
            if astnode.code.from_name is not None:
                cmd.add(sd.AlterObjectProperty(
                    property='from_function',
                    new_value=astnode.code.from_name
                ))
            else:
                cmd.add(sd.AlterObjectProperty(
                    property='code',
                    new_value=astnode.code.code
                ))

        return cmd


class RenameFunction(named.RenameNamedObject, FunctionCommand):
    pass


class AlterFunction(named.AlterNamedObject, FunctionCommand):
    astnode = qlast.AlterFunction


class DeleteFunction(named.DeleteNamedObject, FunctionCommand):
    astnode = qlast.DropFunction

    @classmethod
    def _classname_from_ast(cls, astnode, context, schema):
        name = super()._classname_from_ast(astnode, context, schema)

        pi = parameters_from_ast(astnode, context.modaliases, schema)

        return cls._get_function_fullname(name, pi.paramtypes)


def parameters_from_ast(astnode, modaliases, schema, *, allow_named=True):
    paramdefaults = []
    paramnames = []
    paramtypes = []
    paramtypemods = []
    paramkinds = []
    variadic = None
    for argi, arg in enumerate(astnode.args):
        paramnames.append(arg.name)
        paramtypemods.append(arg.typemod)
        paramkinds.append(arg.kind)

        default = None
        if arg.default is not None:
            default = codegen.generate_source(arg.default)
        paramdefaults.append(default)

        paramtypes.append(utils.ast_to_typeref(
            arg.type, modaliases=modaliases, schema=schema))

        if arg.kind is qlast.ParameterKind.VARIADIC:
            variadic = argi

        if arg.kind is qlast.ParameterKind.NAMED_ONLY and not allow_named:
            raise ql_errors.EdgeQLError(
                'named only parameters are not allowed in this context',
                context=astnode.context)

    return ParametersInfo(
        paramnames=paramnames,
        paramdefaults=paramdefaults,
        paramtypes=paramtypes,
        paramtypemods=paramtypemods,
        paramkinds=paramkinds,
        variadic=variadic)
