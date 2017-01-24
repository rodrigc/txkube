# Copyright Least Authority Enterprises.
# See LICENSE for details.

"""
Functional structures for representing different kinds of Kubernetes
state.
"""

from uuid import uuid4

from zope.interface import provider, implementer

from pyrsistent import CheckedPVector, PClass, field, pmap_field, pset, freeze, thaw

from . import IObject, IObjectLoader
from ._invariants import instance_of, provider_of


class ObjectMetadata(PClass):
    _required = pset({u"name", u"uid"})

    items = pmap_field(unicode, object)
    __invariant__ = lambda m: (
        len(m._required - pset(m.items)) == 0,
        u"Required metadata missing: {}".format(m._required - pset(m.items)),
    )

    @property
    def name(self):
        return self.items[u"name"]

    @property
    def uid(self):
        return self.items[u"uid"]


class NamespacedObjectMetadata(ObjectMetadata):
    _required = ObjectMetadata._required.add(u"namespace")

    @property
    def namespace(self):
        return self.items[u"namespace"]



class NamespaceStatus(PClass):
    """
    ``NamespaceStatus`` instances model `Kubernetes namespace status
    <https://kubernetes.io/docs/api-reference/v1/definitions/#_v1_namespacestatus>`_.
    """
    phase = field(mandatory=True)

    @classmethod
    def active(cls):
        return cls(phase=u"Active")


    @classmethod
    def terminating(cls):
        return cls(phase=u"Terminating")


    @classmethod
    def from_raw(cls, status):
        return cls(phase=status[u"phase"])


    def to_raw(self):
        return {u"phase": self.phase}



@provider(IObjectLoader)
@implementer(IObject)
class Namespace(PClass):
    """
    ``Namespace`` instances model `Kubernetes namespaces
    <https://kubernetes.io/docs/user-guide/namespaces/>`_.
    """
    kind = u"Namespace"

    metadata = field(
        mandatory=True,
        invariant=instance_of(ObjectMetadata),
    )

    status = field(mandatory=True, type=(NamespaceStatus, type(None)))

    @classmethod
    def default(cls):
        """
        Get the default namespace.
        """
        return cls(ObjectMetadata(items={u"name": u"default"}))


    @classmethod
    def from_raw(cls, raw):
        try:
            status_raw = raw[u"status"]
        except KeyError:
            status = None
        else:
            status = NamespaceStatus.from_raw(status_raw)
        return cls(
            metadata=ObjectMetadata(
                items=freeze(raw[u"metadata"]),
            ),
            status=status,
        )

    def fill_defaults(self):
        return self.transform(
            # TODO Also creationTimestamp, resourceVersion, maybe selfLink.
            [u"metadata", u"items", u"uid"], unicode(uuid4()),
            [u"status"], NamespaceStatus.active(),
        )

    def to_raw(self):
        result = {
            u"kind": self.kind,
            u"apiVersion": u"v1",
            u"metadata": thaw(self.metadata.items),
            u"spec": {},
        }
        if self.status is not None:
            result[u"status"] = self.status.to_raw()
        return result



@provider(IObjectLoader)
@implementer(IObject)
class ConfigMap(PClass):
    """
    ``ConfigMap`` instances model `ConfigMap objects
    <https://kubernetes.io/docs/api-reference/v1/definitions/#_v1_configmap>`_.
    """
    kind = u"ConfigMap"

    metadata = field(
        mandatory=True,
        invariant=instance_of(NamespacedObjectMetadata),
    )

    data = pmap_field(unicode, unicode, optional=True)

    @classmethod
    def from_raw(cls, raw):
        return cls(
            metadata=NamespacedObjectMetadata(
                items=freeze(raw[u"metadata"]),
            ),
            data=raw.get(u"data", None),
        )


    def fill_defaults(self):
        # TODO Surely some stuff to fill.
        return self


    def to_raw(self):
        result = {
            u"kind": self.kind,
            u"apiVersion": u"v1",
            u"metadata": thaw(self.metadata.items),
        }
        if self.data is not None:
            # Kubernetes only includes the item if there is some data.
            #
            # XXX I'm not sure if there's a difference between no data and
            # data with no items.
            result[u"data"] = thaw(self.data)
        return result



def object_sort_key(obj):
    """
    Define a predictable sort ordering for Kubernetes objects.

    This should be the same ordering that Kubernetes itself imposes.
    """
    return (
        # Not all objects have a namespace.
        getattr(obj.metadata, "namespace", None),
        obj.metadata.name,
    )



def _pvector_field(iface):
    class _CheckedIObjectPVector(CheckedPVector):
        __invariant__ = provider_of(iface)

    return field(
        mandatory=True,
        type=_CheckedIObjectPVector,
        factory=lambda v: _CheckedIObjectPVector.create(sorted(v, key=object_sort_key)),
        initial=_CheckedIObjectPVector(),
    )



@provider(IObjectLoader)
@implementer(IObject)
class ObjectCollection(PClass):
    """
    ``ObjectList`` is a collection of Kubernetes objects.

    This roughly corresponds to the `*List` Kubernetes types.  It's not clear
    this is actually more useful than a native Python collection such as a set
    but we'll try it out.

    :ivar pvector items: The objects belonging to this collection.
    """
    kind = u"List"
    items = _pvector_field(IObject)

    @classmethod
    def from_raw(cls, raw):
        items = (
            # Unfortunately `kind` is an optional field.  Fortunately, the
            # top-level `kind` is something like `ConfigMapList` if you
            # asked for `.../configmaps/`.  So pass that down as a hint.
            object_from_raw(obj, raw[u"kind"][:-len(u"List")])
            for obj
            in raw[u"items"]
        )
        return cls(items=items)


    def to_raw(self):
        return {
            u"kind": self.kind,
            u"apiVersion": u"v1",
            u"metadata": {},
            u"items": list(
                obj.to_raw()
                for obj
                in self.items
            ),
        }


    def item_by_name(self, name):
        """
        Find an item in this collection by its name metadata.

        :param unicode name: The name of the object for which to search.

        :raise KeyError: If no object matching the given name is found.
        :return IObject: The object with the matching name.
        """
        for obj in self.items:
            if obj.metadata.name == name:
                return obj
        raise KeyError(name)


    def add(self, obj):
        return self.transform([u"items"], add(obj))


    def replace(self, old, new):
        return self.transform(
            [u"items"], remove(old),
            [u"items"], add(new),
        )



def add(value):
    def evolver(pvector):
        return sorted(pvector.append(value), key=object_sort_key)
    return evolver


def remove(value):
    def evolver(pset):
        return pset.remove(value)
    return evolver


_loaders = {
    loader.kind: loader
    for loader
    in {Namespace, ConfigMap}
}



def object_from_raw(raw, kind_hint=None):
    """
    Load an object of unspecified type from the raw representation of it.

    :raise KeyError: If the kind of object is unsupported.

    :return IObject: The loaded object.
    """
    kind = raw.get(u"kind", kind_hint)
    if kind.endswith(u"List"):
        loader = ObjectCollection
    else:
        loader = _loaders[kind]
    return loader.from_raw(raw)