# This file is part of Indico.
# Copyright (C) 2002 - 2016 European Organization for Nuclear Research (CERN).
#
# Indico is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License as
# published by the Free Software Foundation; either version 3 of the
# License, or (at your option) any later version.
#
# Indico is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Indico; if not, see <http://www.gnu.org/licenses/>.

from __future__ import unicode_literals

from functools import update_wrapper, partial
from operator import attrgetter

from flask import has_request_context, g

from indico.util.string import return_ascii


class ACLProxyBase(object):
    """Base Proxy class for ACL settings"""

    def __init__(self, proxy):
        self.proxy = proxy
        self.module = self.proxy.module

    @property
    def _cache(self):
        return self.proxy._cache

    def _check_name(self, name):
        self.proxy._check_name(name, True)

    def _flush_cache(self):
        self.proxy._flush_cache()


class SettingsProxyBase(object):
    """Base proxy class to access settings for a certain module

    :param module: the module to use
    :param defaults: default values to use if there's nothing in the db
    :param strict: in strict mode any key that's not in `defaults` or
                   `acls` is illegal and triggers a `ValueError`
    :param acls: setting names which are referencing ACLs in a
                 separate table
    :param converters: a dict specifying how to convert the values of
                       certain settings from/to JSON-compatible types
    """

    acl_proxy_class = None
    default_sentinel = object()

    def __init__(self, module, defaults=None, strict=True, acls=None, converters=None):
        self.module = module
        self.defaults = defaults or {}
        self.strict = strict
        self.acl_names = set(acls or ())
        self.acls = self.acl_proxy_class(self) if self.acl_proxy_class else None
        self.converters = converters or {}
        self._bound_args = None
        if strict and not defaults and not acls:
            raise ValueError('cannot use strict mode with no defaults')
        if acls and not self.acl_proxy_class:
            raise ValueError('this proxy does not support acl settings')
        if acls and self.acl_names & self.defaults.viewkeys():
            raise ValueError('acl settings cannot have a default value')
        if acls and converters and acls & converters.viewkeys():
            raise ValueError('acl settings cannot have custom converters')

    @return_ascii
    def __repr__(self):
        if self._bound_args:
            return '<{}({}, {})>'.format(type(self).__name__, self.module, self._bound_args)
        else:
            return '<{}({})>'.format(type(self).__name__, self.module)

    def bind(self, *args):
        """Returns a version of this proxy that is bound to some arguments.

        This is useful for specialized versions of the proxy such as
        EventSettingsProxy where one might want to provide an easy-to-use
        version that does not require the event to be specified in each
        method.

        :param args: The positional argument that are prepended to each
                     function call.
        """

        self_type = type(self)
        bound = self_type(self.module, self.defaults, self.strict)
        bound._bound_args = args

        for name in dir(self_type):
            if name[0] == '_' or name == 'bind' or not callable(getattr(self_type, name)):
                continue
            func = getattr(bound, name)
            func = update_wrapper(partial(func, *args), func)
            setattr(bound, name, func)
        return bound

    def _check_name(self, name, acl=False):
        strict = self.strict or acl  # acl settings always use strict mode
        collection = self.acl_names if acl else self.defaults
        if strict and name not in collection:
            raise ValueError('invalid setting: {}.{}'.format(self.module, name))

    def _split_names(self, names):
        # Returns a ``(regular_names, acl_names)`` tuple
        return {x for x in names if x not in self.acl_names}, {x for x in names if x in self.acl_names}

    def _split_call(self, value, regular_func, acl_func):
        # Util to call different functions depending if the name is an acl or regular setting
        regular_names, acl_names = self._split_names(value)
        for name in regular_names:
            self._check_name(name)
        for name in acl_names:
            self._check_name(name, True)
        if isinstance(value, dict):
            regular_items = {name: value[name] for name in regular_names}
            acl_items = {name: value[name] for name in acl_names}
            if regular_items:
                regular_func(regular_items)
            if acl_items:
                acl_func(acl_items)
        else:
            if regular_names:
                regular_func(regular_names)
            if acl_names:
                acl_func(acl_names)

    def _flush_cache(self):
        if has_request_context():
            g.get('settings_cache', {}).clear()

    def _convert_from_python(self, name, value):
        if value is None:
            return None
        converter = self.converters.get(name)
        return converter.from_python(value) if converter else value

    def _convert_to_python(self, name, value):
        if value is None:
            return None
        converter = self.converters.get(name)
        return converter.to_python(value) if converter else value

    @property
    def _cache(self):
        if not has_request_context():
            return {}  # new dict everytime, this effectively disables the cache
        try:
            return g.settings_cache
        except AttributeError:
            g.settings_cache = rv = {}
            return rv


class SettingProperty(object):
    """Expose a SettingsProxy value as a property.

    Override `attr` in a subclass for settings proxies that are tied
    to specific objects (such as `EventSettingsProxy`)

    :param proxy: An instance of a settings proxy
    :param name: The name of the setting
    :param default: The default value in case the setting is not set
    :param attr: If `proxy` expects an object before the name, e.g.
                 for event settings, this is the name of the attribute
                 on the class containing the property from which the
                 object will be taken.
    """

    attr = None

    def __init__(self, proxy, name, default=SettingsProxyBase.default_sentinel, attr=None):
        self.proxy = proxy
        self.name = name
        self.default = default
        attr = attr or self.attr
        self.attrgetter = attrgetter(attr) if attr else None

    def _make_args(self, obj, *args):
        if self.attrgetter is not None:
            return (self.attrgetter(obj),) + args
        else:
            return args

    def __get__(self, obj, objtype=None):
        if obj is None:
            return self
        return self.proxy.get(*self._make_args(obj, self.name, self.default))

    def __set__(self, obj, value):
        self.proxy.set(*self._make_args(obj, self.name, value))

    def __delete__(self, obj):
        self.proxy.delete(*self._make_args(obj, self.name))
